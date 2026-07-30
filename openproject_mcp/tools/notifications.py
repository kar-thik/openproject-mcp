"""Notification tools (SPEC §6.8 — Phase 2).

Lands here:

=====================================  ======  =======================================
Tool                                   Phase   Endpoint(s)
=====================================  ======  =======================================
🔍 ``list_notifications``              2       ``GET /notifications``
✏️ ``mark_notifications``              2       ``POST /notifications/read_ian``
✏️ ``mark_all_notifications_read``     2       ``POST /notifications/read_ian``
=====================================  ======  =======================================

Non-negotiables for this module:

* **Two write tools, not one union-shaped footgun.** ``mark_notifications``
  requires explicit ids; the mass operation is a separately named tool that only
  marks **read** (the safe direction — an accidental "mark everything unread"
  cannot be expressed) and says in its description that it affects everything
  matching the filters. A single ``mark(ids?, all?)`` tool would let one missing
  argument clear an entire inbox.
* Both write tools issue **one** bulk request against the collection endpoint
  with an ``id`` / ``readIAN`` filter, so marking 40 notifications is one round
  trip rather than 40.
* ``reason='dateAlert'`` is Enterprise-gated (``date_alerts`` token). The API's
  own rejection is passed through with a hint that names the gate (SPEC §4.7),
  never swallowed and never silently dropped from the filter set.
* The notified-about resource is polymorphic (usually a work package, sometimes
  a wiki page or a meeting), so it is projected as ``{id, type, title}`` instead
  of being forced into a work-package shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import InputValidationError, ValidationFailedError
from openproject_mcp.client.filters import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Filter,
    FilterType,
    Op,
    make_filter,
    query_params,
    register_filter_type,
    serialize_filters,
)
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    GROUP_NOTIFICATIONS,
    READ,
    WRITE,
    envelope_from_collection,
    get_tool_context,
    read_annotations,
    tool_errors,
    tool_tags,
    write_annotations,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["MarkResult", "NotificationResource", "NotificationRow", "register"]

#: Filter validation scope for this endpoint (never the shared global table).
NOTIFICATIONS_RESOURCE = "notifications"

#: One request marks at most this many ids: the filter travels in the query
#: string, and a longer one risks a 414 from a proxy rather than an API error.
MAX_MARK_IDS = 200

#: Verified wire values of the ``reason`` filter (SPEC §6.8).
NotificationReason = Literal[
    "mentioned",
    "assigned",
    "responsible",
    "watched",
    "subscribed",
    "commented",
    "created",
    "processed",
    "prioritized",
    "scheduled",
    "shared",
    "reminder",
    "dateAlert",
]

DATE_ALERT_HINT = (
    "reason='dateAlert' filtering needs the OpenProject Enterprise 'date_alerts' feature, and "
    "this instance rejected the filter. Drop the reason filter — date-alert notifications still "
    "show up in an unfiltered list, where each row carries reason='dateAlert' — or ask an "
    "administrator whether the Enterprise token covers date alerts."
)

#: API collection segment → OpenProject resource type, for the polymorphic
#: notification subject. Unmapped kinds surface the collection name verbatim
#: rather than a guessed singular.
RESOURCE_TYPES: dict[str, str] = {
    "work_packages": "WorkPackage",
    "activities": "Activity",
    "wiki_pages": "WikiPage",
    "meetings": "Meeting",
    "projects": "Project",
    "news": "News",
    "documents": "Document",
}


class NotificationResource(BaseModel):
    """What a notification is about — polymorphic, usually a work package."""

    id: int | str | None = Field(
        default=None,
        description="Id of the notified-about resource. For type='WorkPackage' this is the "
        "work package id that get_work_package and list_work_package_comments take.",
    )
    type: str | None = Field(
        default=None,
        description="OpenProject resource type, e.g. 'WorkPackage', 'WikiPage', 'Meeting'. "
        "Kinds this server does not map surface their API collection name instead.",
    )
    title: str | None = Field(
        default=None, description="Subject/title of the resource as OpenProject renders it."
    )


class NotificationRow(BaseModel):
    """One notification in the authenticated user's inbox."""

    id: int | str | None = Field(
        default=None,
        description="Notification id. Pass it in mark_notifications(ids=[...]) to mark it read.",
    )
    reason: str | None = Field(
        default=None,
        description="Why it was sent: mentioned, assigned, responsible, watched, subscribed, "
        "commented, created, processed, prioritized, scheduled, shared, reminder, dateAlert.",
    )
    read: bool = Field(
        default=False,
        description="True once the notification was marked read in the in-app inbox (readIAN).",
    )
    created_at: str | None = Field(
        default=None, description="ISO 8601 UTC timestamp of when the notification was raised."
    )
    actor: Ref | None = Field(
        default=None,
        description="User whose action triggered it; null for system-generated notifications "
        "such as date alerts.",
    )
    project: Ref | None = Field(
        default=None, description="Project the notification belongs to; null for global ones."
    )
    resource: NotificationResource | None = Field(
        default=None,
        description="The notified-about resource: {id, type, title}. Usually a work package.",
    )


class MarkResult(BaseModel):
    """Outcome of a mark-read/unread call."""

    marked: int = Field(
        description="Number of notifications the call covered. For mark_notifications this is "
        "the number of ids sent; for mark_all_notifications_read it is how many unread "
        "notifications matched the filters when the call ran."
    )
    read: bool = Field(description="True when they were marked read, false when marked unread.")
    ids: list[int] | None = Field(
        default=None,
        description="The notification ids that were marked; null for the filter-based bulk tool.",
    )
    message: str = Field(description="Human-readable confirmation of what happened.")


def _resource_of(element: Mapping[str, Any]) -> NotificationResource | None:
    """Project ``_links.resource`` (plus any inlined copy) into {id, type, title}."""
    resolved = hal.ref(element, "resource")
    inlined = hal.as_object(hal.embedded(element, "resource"))
    if resolved is None and inlined is None:
        return None

    type_name: str | None = None
    if inlined is not None:
        raw_type = inlined.get("_type")
        type_name = raw_type if isinstance(raw_type, str) else None
    if type_name is None:
        segment = _collection_segment(resolved.href if resolved else None)
        type_name = RESOURCE_TYPES.get(segment, segment) if segment else None

    title = resolved.name if resolved else None
    if title is None and inlined is not None:
        candidate = inlined.get("subject") or inlined.get("name") or inlined.get("title")
        title = candidate if isinstance(candidate, str) else None

    resource_id = resolved.id if resolved else None
    if resource_id is None and inlined is not None:
        resource_id = hal.self_id(inlined)

    return NotificationResource(id=resource_id, type=type_name, title=title)


def _collection_segment(href: str | None) -> str | None:
    """``/api/v3/work_packages/12`` → ``work_packages``."""
    if not isinstance(href, str) or not href:
        return None
    parts = [part for part in href.split("?", 1)[0].split("/") if part]
    return parts[-2] if len(parts) >= 2 else None


def _notification_row(element: Mapping[str, Any]) -> NotificationRow:
    reason = element.get("reason")
    return NotificationRow(
        id=hal.self_id(element),
        reason=reason if isinstance(reason, str) else None,
        read=bool(element.get("readIAN")),
        created_at=element.get("createdAt"),
        actor=Ref.from_hal(element, "actor"),
        project=Ref.from_hal(element, "project"),
        resource=_resource_of(element),
    )


def _scope_filters(
    *,
    unread_only: bool,
    reason: str | None,
    project_id: int | str | None,
) -> list[Filter]:
    """The filter set shared by listing and the mass mark-read tool."""
    filters: list[Filter] = []
    if unread_only:
        filters.append(
            make_filter("readIAN", Op.EQ, [False], resource=NOTIFICATIONS_RESOURCE),
        )
    if reason:
        filters.append(make_filter("reason", Op.EQ, [reason], resource=NOTIFICATIONS_RESOURCE))
    if project_id is not None:
        filters.append(make_filter("project", Op.EQ, [project_id], resource=NOTIFICATIONS_RESOURCE))
    return filters


def _with_date_alert_hint(exc: ValidationFailedError, reason: str | None) -> ValidationFailedError:
    """Re-raise a filter rejection with the Enterprise gate named (SPEC §4.7)."""
    if reason != "dateAlert":
        return exc
    return ValidationFailedError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        violations=exc.violations,
        hint=DATE_ALERT_HINT,
    )


def _clean_ids(ids: list[int]) -> list[int]:
    """Validate and de-duplicate the id list a bulk mark call was given."""
    if not ids:
        raise InputValidationError(
            "ids must not be empty.",
            hint=(
                "Pass the notification ids to mark, e.g. ids=[12, 13]; they come from "
                "list_notifications. To clear a whole inbox use mark_all_notifications_read."
            ),
        )
    cleaned: list[int] = []
    for value in ids:
        if isinstance(value, bool) or value <= 0:
            raise InputValidationError(
                f"ids contains {value!r}, which is not a positive notification id.",
                hint="Every entry must be a numeric notification id from list_notifications.",
            )
        if value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > MAX_MARK_IDS:
        raise InputValidationError(
            f"{len(cleaned)} ids is more than the {MAX_MARK_IDS} this tool marks in one call.",
            hint=(
                f"Split the ids into batches of {MAX_MARK_IDS}, or use "
                "mark_all_notifications_read when the intent is 'clear everything matching "
                "these filters'."
            ),
        )
    return cleaned


def _register_filters() -> None:
    """Teach the filter validator the notification filter names we send."""
    register_filter_type("id", FilterType.LIST, NOTIFICATIONS_RESOURCE)
    register_filter_type("readIAN", FilterType.BOOLEAN, NOTIFICATIONS_RESOURCE)
    register_filter_type("reason", FilterType.LIST, NOTIFICATIONS_RESOURCE)
    register_filter_type("project", FilterType.LIST, NOTIFICATIONS_RESOURCE)


def register(mcp: FastMCP) -> None:
    """Register the notification tools (SPEC §6.8)."""
    _register_filters()

    @mcp.tool(
        name="list_notifications",
        tags=tool_tags(GROUP_NOTIFICATIONS, READ),
        annotations=read_annotations(title="List notifications"),
    )
    @tool_errors
    async def list_notifications(
        unread_only: Annotated[
            bool,
            Field(
                description=(
                    "true (default) returns only notifications still unread in the in-app "
                    "inbox; false returns read and unread together. There is no 'read only' "
                    "mode — filter the rows on read=false/true yourself if you need one."
                )
            ),
        ] = True,
        reason: Annotated[
            NotificationReason | None,
            Field(
                description=(
                    "Restrict to one trigger: mentioned, assigned, responsible, watched, "
                    "subscribed, commented, created, processed, prioritized, scheduled, shared, "
                    "reminder, dateAlert. 'mentioned' is what answers \"who needs me?\". "
                    "dateAlert filtering is an OpenProject Enterprise feature and is rejected "
                    "with an explanatory hint on Community instances."
                )
            ),
        ] = None,
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric project id or identifier to scope the inbox to one project. Ids "
                    "come from list_projects."
                )
            ),
        ] = None,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_PAGE_SIZE,
                description=(
                    f"Notifications per page (max {MAX_PAGE_SIZE}); the instance may clamp it "
                    "lower and the returned pagination reports what actually came back."
                ),
            ),
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[NotificationRow]:
        """Read the authenticated user's OpenProject inbox.

        Use this to answer "what needs my attention?", "was I mentioned
        anywhere?" or "what changed on the things I watch?" — it is the only
        tool that sees notifications, and it always reports the inbox of the
        **token owner**, never another user's.

        Returns the standard list envelope: ``items`` of ``{id, reason, read,
        created_at, actor, project, resource}`` plus ``pagination`` with
        ``total``/``page``/``page_size``/``has_more``. ``resource`` is the thing
        the notification is about — ``{id, type, title}``, usually
        ``type='WorkPackage'``, so ``resource.id`` feeds straight into
        ``get_work_package`` or ``list_work_package_comments``.

        Pitfalls. Several changes to the same work package are aggregated into
        one notification, so the count is not a count of events. Reading a
        notification here does **not** mark it read — that is
        ``mark_notifications``. ``unread_only=false`` can return a very long
        history; keep a page size that fits your reply. A notification whose
        project the token owner has lost access to disappears from the inbox
        entirely.

        Cross-references: mark specific rows read with ``mark_notifications``;
        clear the whole (optionally filtered) inbox with
        ``mark_all_notifications_read``; open the underlying ticket with
        ``get_work_package(resource.id)`` and its discussion with
        ``list_work_package_comments(resource.id)``.
        """
        ctx = get_tool_context()
        filters = _scope_filters(unread_only=unread_only, reason=reason, project_id=project_id)
        params = query_params(filters=filters, page=page, page_size=page_size)
        try:
            payload = await ctx.client.get_json("notifications", params=params)
        except ValidationFailedError as exc:
            raise _with_date_alert_hint(exc, reason) from exc

        unwrapped = hal.collection(payload)
        rows = [_notification_row(element) for element in unwrapped]
        return envelope_from_collection(unwrapped, rows, page=page, page_size=page_size)

    @mcp.tool(
        name="mark_notifications",
        tags=tool_tags(GROUP_NOTIFICATIONS, WRITE),
        annotations=write_annotations(title="Mark notifications read or unread", idempotent=True),
    )
    @tool_errors
    async def mark_notifications(
        ids: Annotated[
            list[int],
            Field(
                description=(
                    "Notification ids to mark, e.g. [4711, 4712]. Required and non-empty — this "
                    "tool never operates on 'everything'. Ids come from list_notifications "
                    f"(the row 'id', not resource.id). At most {MAX_MARK_IDS} per call."
                )
            ),
        ],
        read: Annotated[
            bool,
            Field(
                description=(
                    "true (default) marks them read; false puts them back in the unread inbox. "
                    "Both directions are safe here because the ids are explicit."
                )
            ),
        ] = True,
    ) -> MarkResult:
        """Mark specific notifications read (or unread) in one bulk request.

        Use it after you have actually handled what a notification was about, so
        the user's inbox reflects reality. It is the id-consuming counterpart of
        ``list_notifications``: pass the row ``id`` values from that tool.

        Returns ``{marked, read, ids, message}``. OpenProject answers the bulk
        endpoint with ``204 No Content``, so ``marked`` is the number of ids the
        call covered — ids that were already in the requested state, or that
        belong to someone else's inbox, are simply not changed.

        Pitfalls. Marking is idempotent, so a retry after a timeout is safe. Pass
        the **notification** id, not ``resource.id`` — the work package id
        underneath is a different number entirely. Unknown ids do not fail the
        call, so do not treat success as proof that every id existed.

        Cross-references: get the ids from ``list_notifications``; to clear an
        entire (optionally filtered) inbox in one go use
        ``mark_all_notifications_read``.
        """
        ctx = get_tool_context()
        cleaned = _clean_ids(ids)
        endpoint = "read_ian" if read else "unread_ian"
        serialized = serialize_filters(
            [make_filter("id", Op.EQ, cleaned, resource=NOTIFICATIONS_RESOURCE)]
        )
        await ctx.client.post_json(f"notifications/{endpoint}", params={"filters": serialized})

        state = "read" if read else "unread"
        return MarkResult(
            marked=len(cleaned),
            read=read,
            ids=cleaned,
            message=f"Marked {len(cleaned)} notification(s) {state}.",
        )

    @mcp.tool(
        name="mark_all_notifications_read",
        tags=tool_tags(GROUP_NOTIFICATIONS, WRITE),
        annotations=write_annotations(title="Mark all notifications read", idempotent=True),
    )
    @tool_errors
    async def mark_all_notifications_read(
        reason: Annotated[
            NotificationReason | None,
            Field(
                description=(
                    "Only clear notifications with this trigger (mentioned, assigned, watched, "
                    "…). Omit to clear every unread notification the filters allow. dateAlert "
                    "is Enterprise-gated and is rejected with an explanatory hint on Community "
                    "instances."
                )
            ),
        ] = None,
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Only clear notifications belonging to this project (numeric id or "
                    "identifier, from list_projects). Omit to clear across all projects."
                )
            ),
        ] = None,
    ) -> MarkResult:
        """Mark **everything** matching the filters as read — the whole inbox by default.

        Use it for "clear my notifications" or "I have dealt with everything in
        project X". Called with no arguments it marks every unread notification
        of the token owner as read, across all projects; ``reason`` and
        ``project_id`` narrow that blast radius, they do not create a preview.
        Check what is about to disappear with
        ``list_notifications(unread_only=true, ...)`` using the same filters
        first — there is no undo beyond re-marking individual ids unread.

        Returns ``{marked, read, message}``, where ``marked`` is how many unread
        notifications matched the filters at the moment of the call (counted
        immediately before the bulk update, so a notification arriving during
        the call may be counted differently than it was marked).

        This tool only ever marks **read**. There is deliberately no
        "mark everything unread" twin: that is a mistake with no upside, and the
        reverse direction stays available per-id through
        ``mark_notifications(ids=[...], read=false)``.

        Cross-references: preview or page the inbox with ``list_notifications``;
        mark a handful of rows with ``mark_notifications``.
        """
        ctx = get_tool_context()
        filters = _scope_filters(unread_only=True, reason=reason, project_id=project_id)
        serialized = serialize_filters(filters)

        try:
            preview = await ctx.client.get_json(
                "notifications", params={"filters": serialized, "pageSize": 1}
            )
        except ValidationFailedError as exc:
            raise _with_date_alert_hint(exc, reason) from exc
        matched = hal.collection(preview).total

        try:
            await ctx.client.post_json("notifications/read_ian", params={"filters": serialized})
        except ValidationFailedError as exc:
            raise _with_date_alert_hint(exc, reason) from exc

        scope: list[str] = []
        if reason:
            scope.append(f"reason={reason}")
        if project_id is not None:
            scope.append(f"project={project_id}")
        described = f" matching {', '.join(scope)}" if scope else ""
        return MarkResult(
            marked=matched,
            read=True,
            ids=None,
            message=f"Marked {matched} unread notification(s){described} as read.",
        )
