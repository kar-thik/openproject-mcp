"""Work-package core tools (SPEC §6.2).

Lands here:

===============================  ======  =============================================
Tool                             Phase   Endpoint(s)
===============================  ======  =============================================
🔍 ``search_work_packages``      1       ``GET /work_packages`` + ``typeahead``/``search``
🔍 ``list_work_packages``        1       ``GET /work_packages`` or ``/projects/{id}/work_packages``
🔍 ``get_work_package``          1       ``GET /work_packages/{id}`` (+ sub-resources)
✏️ ``create_work_package``       1       ``POST /work_packages/form`` → ``POST /work_packages``
✏️ ``update_work_package``       1       form → ``PATCH /work_packages/{id}``
🗑 ``delete_work_package``       1       ``DELETE /work_packages/{id}``
===============================  ======  =============================================

Non-negotiables for this module:

* Always send an explicit status filter derived from ``status_scope``
  (``search`` defaults to ``all``, ``list`` to ``open``); never rely on the
  server's implicit open-only default. ``status_ids`` overrides ``status_scope``.
* Writes go through the form endpoint first so validation errors carry allowed
  values, and ``lock_version`` handling goes through
  :func:`openproject_mcp.client.locking.patch_with_lock`.
* ``assignee=None`` clears via ``{"href": null}`` — use
  :func:`openproject_mcp.client.payloads.link`.
* Every include in ``get_work_package`` is capped at 20 items and reports
  ``{"truncated": true, "total": N}`` with a pointer to the full-listing tool.

Two conventions the tools share:

``KEEP``
    Update parameters that can be *cleared* default to the :data:`KEEP`
    sentinel. Omitting the parameter leaves the field untouched; passing JSON
    ``null`` (or the string ``"none"``) clears it via a null href. Without the
    sentinel, "leave alone" and "clear" would be the same wire value.

name-or-id
    ``type``/``status``/``priority`` accept a display name or a numeric id
    (SPEC §5.7); names resolve against the cached instance enumerations, and an
    unknown or ambiguous name fails locally with the valid values listed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import InputValidationError, NotFoundError, OpenProjectError
from openproject_mcp.client.filters import (
    WORK_PACKAGE_SORT_KEYS,
    Filter,
    Op,
    RawFilter,
    date_range_filter,
    filter_from_raw,
    make_filter,
    principal_filter,
    query_params,
    status_filter,
)
from openproject_mcp.client.locking import (
    extract_lock_version,
    patch_with_lock,
    resolve_lock_version,
)
from openproject_mcp.client.payloads import (
    build_write_payload,
    custom_field_payload,
    formattable_field,
    href_for,
    link,
)
from openproject_mcp.projections import (
    CustomFieldValue,
    ListEnvelope,
    Ref,
    RelationRow,
    TruncatedList,
    WorkPackageDetail,
    WorkPackageRow,
    custom_field_type_name,
)
from openproject_mcp.tools import _forms, _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from openproject_mcp.tools._shared import ToolContext

__all__ = ["KEEP", "register"]

#: Sentinel default for update parameters that can also be cleared.
KEEP = "__unchanged__"

#: Every ``get_work_package`` include is capped at this many items (G1).
INCLUDE_CAP = 20

SearchMode = Literal["quick", "fulltext"]
StatusScope = Literal["open", "closed", "all"]
IncludeName = Literal["relations", "watchers", "attachments", "children", "custom_actions"]

ATTACHMENT_SEARCH_NOTE = (
    "fulltext searches subject, description, comments and searchable custom fields; "
    "attachment content and filenames are included only when this instance's PostgreSQL "
    "full-text index is populated, which the API does not expose"
)

#: Instance enumerations that ``type``/``status``/``priority`` resolve against.
NAMED_COLLECTIONS: dict[str, str] = {
    "type": "types",
    "status": "statuses",
    "priority": "priorities",
}


# --- models ---------------------------------------------------------------


class WorkPackageFull(WorkPackageDetail):
    """Detail projection plus the milestone ``date`` field.

    Milestone types carry a single ``date`` instead of ``start_date``/
    ``due_date``; both shapes are surfaced so a caller never has to guess.
    """

    date: str | None = Field(
        default=None, description="Milestone date (ISO YYYY-MM-DD); null for non-milestones."
    )


class WatcherRow(BaseModel):
    """A user watching the work package."""

    id: int | str | None = Field(default=None, description="User id.")
    name: str | None = Field(default=None, description="Display name.")


class AttachmentRow(BaseModel):
    """An attachment on the work package."""

    id: int | str | None = Field(default=None, description="Attachment id.")
    file_name: str | None = Field(default=None, description="Stored file name.")
    file_size: int | None = Field(default=None, description="Size in bytes.")
    content_type: str | None = Field(default=None, description="MIME type reported by the server.")
    description: str | None = Field(default=None, description="Caption, when one was given.")
    author: Ref | None = Field(default=None, description="Uploading user.")
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class CustomActionRow(BaseModel):
    """An instance-defined one-click action available on this work package."""

    id: int | str | None = Field(default=None, description="Custom action id.")
    name: str | None = Field(default=None, description="Button label as configured by an admin.")


class WorkPackageWithIncludes(WorkPackageFull):
    """``get_work_package`` result: detail plus the requested capped includes."""

    relations: TruncatedList[RelationRow] | None = Field(
        default=None, description="Present only when 'relations' was requested."
    )
    watchers: TruncatedList[WatcherRow] | None = Field(
        default=None, description="Present only when 'watchers' was requested."
    )
    attachments: TruncatedList[AttachmentRow] | None = Field(
        default=None, description="Present only when 'attachments' was requested."
    )
    children: TruncatedList[WorkPackageRow] | None = Field(
        default=None, description="Present only when 'children' was requested."
    )
    custom_actions: TruncatedList[CustomActionRow] | None = Field(
        default=None, description="Present only when 'custom_actions' was requested."
    )


class DeletionResult(BaseModel):
    """Outcome of ``delete_work_package``."""

    id: int = Field(description="Id of the work package that was deleted.")
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    message: str = Field(description="Human-readable confirmation.")


# --- small conversions ----------------------------------------------------


def _is_keep(value: Any) -> bool:
    """True when an update parameter was omitted rather than set or cleared."""
    return isinstance(value, str) and value == KEEP


def _is_clear(value: Any) -> bool:
    """True when a value means "clear this field"."""
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in ("", "none", "null")


def _duration_from_hours(hours: float) -> str:
    """``7.5`` → ``"PT7H30M"`` — the wire format for ``estimatedTime``."""
    total_minutes = round(float(hours) * 60)
    whole_hours, minutes = divmod(total_minutes, 60)
    return f"PT{whole_hours}H{minutes}M" if minutes else f"PT{whole_hours}H"


def _numeric_id(value: Any, *, field: str, produced_by: str) -> int:
    """Coerce a caller-supplied reference to a numeric id, or fail with a pointer."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text.lower() == "me":
        raise InputValidationError(
            f"{field!r} needs a numeric id; 'me' works in filters but not in a write payload.",
            hint="get_instance_info returns the current user's numeric id.",
        )
    raise InputValidationError(
        f"{field!r} must be a numeric id (got {text!r}).",
        hint=f"Ids come from {produced_by}.",
    )


def _api_path(href: Any) -> str | None:
    """Turn an API href into a client path relative to ``/api/v3``."""
    if not isinstance(href, str) or not href:
        return None
    path = href.split("?", 1)[0]
    marker = "/api/v3/"
    index = path.find(marker)
    if index >= 0:
        return path[index + len(marker) :]
    return path.lstrip("/")


def _links_of(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    links = hal.as_object(payload.get("_links")) if payload is not None else None
    return links if links is not None else {}


# --- projections ----------------------------------------------------------


def _is_custom_field_key(key: str) -> bool:
    # Strictly customField<digits>: OpenProject also ships a plural
    # ``_links.customFields`` settings link on work packages, which is not a field.
    digits = key[len("customField") :]
    return key.startswith("customField") and digits.isdigit()


def _custom_field_order(key: str) -> tuple[int, str]:
    digits = key[len("customField") :]
    return (int(digits), key) if digits.isdigit() else (10**9, key)


def _custom_fields(
    payload: Mapping[str, Any], schema: Mapping[str, Any] | None
) -> list[CustomFieldValue]:
    """Canonical custom-field read shape (SPEC §6.2.1).

    Only fields that actually carry a value are returned — empty custom fields
    would otherwise cost tokens on every read; ``get_work_package_schema`` is
    the place to see the full field list.
    """
    links = _links_of(payload)
    link_keys = {key for key in links if _is_custom_field_key(key)}
    attribute_keys = {key for key in payload if _is_custom_field_key(key)}

    values: list[CustomFieldValue] = []
    for key in sorted(attribute_keys | link_keys, key=_custom_field_order):
        entry = hal.as_object(schema.get(key)) if schema is not None else None
        name = entry.get("name") if entry is not None else None

        value: Any = None
        value_ids: list[int | str] | None = None

        if key in link_keys:
            raw_link = links[key]
            if hal.as_object(raw_link) is None and hal.as_array(raw_link) is not None:
                resolved = hal.refs(payload, key)
                if resolved:
                    value = [item.name for item in resolved]
                    value_ids = [item.id for item in resolved if item.id is not None]
            else:
                single = hal.ref(payload, key)
                if single is not None:
                    value = single.name
                    value_ids = [single.id] if single.id is not None else None
        else:
            raw = payload.get(key)
            value = hal.formattable(raw) if hal.as_object(raw) is not None else raw

        if value is None and not value_ids:
            continue
        values.append(
            CustomFieldValue(
                key=key,
                name=name if isinstance(name, str) else None,
                type=custom_field_type_name(entry.get("type")) if entry is not None else None,
                value=value,
                value_ids=value_ids or None,
            )
        )
    return values


def _availability(payload: Mapping[str, Any]) -> dict[str, bool]:
    """Which optional surfaces this work package exposes (SPEC §6.2, G5)."""
    keys = set(_links_of(payload))
    return {
        "dev_links": bool(keys & {"revisions", "github", "gitlab"}),
        "meetings": bool(keys & {"meetings", "meetingAgendaItems"}),
        "files": "fileLinks" in keys,
    }


def _detail_fields(
    payload: Mapping[str, Any],
    schema: Mapping[str, Any] | None,
    notes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Every field of the full detail projection, as constructor kwargs."""
    return {
        **WorkPackageRow.from_hal(payload).model_dump(),
        "date": payload.get("date"),
        "description": hal.formattable(payload.get("description")),
        "author": Ref.from_hal(payload, "author"),
        "responsible": Ref.from_hal(payload, "responsible"),
        "version": Ref.from_hal(payload, "version"),
        "category": Ref.from_hal(payload, "category"),
        "parent": Ref.from_hal(payload, "parent"),
        "project_phase": Ref.from_hal(payload, "projectPhase"),
        "estimated_hours": hal.duration_hours(payload.get("estimatedTime")),
        "spent_hours": hal.duration_hours(payload.get("spentTime")),
        "created_at": payload.get("createdAt"),
        "lock_version": payload.get("lockVersion"),
        "custom_fields": _custom_fields(payload, schema),
        "available": _availability(payload),
        "notes": list(notes) if notes else None,
    }


def _truncated[ItemT](
    model: type[TruncatedList[ItemT]],
    items: Sequence[ItemT],
    total: int,
    *,
    more_via: str | None,
) -> TruncatedList[ItemT]:
    """Cap an include at :data:`INCLUDE_CAP` and say so honestly (G1)."""
    capped = list(items[:INCLUDE_CAP])
    resolved_total = max(total, len(items))
    truncated = resolved_total > len(capped)
    return model(
        items=capped,
        truncated=truncated,
        total=resolved_total,
        more_via=more_via if truncated else None,
    )


# --- cached metadata ------------------------------------------------------


async def _cached_json(ctx: ToolContext, path: str) -> dict[str, Any]:
    async def fetch() -> dict[str, Any]:
        return await ctx.client.get_json(path)

    return await ctx.cache.get_or_set(("json", path), fetch, scope=ctx.scope)


async def _cached_elements(ctx: ToolContext, path: str) -> list[dict[str, Any]]:
    return hal.collection(await _cached_json(ctx, path)).elements


async def _resolve_named(ctx: ToolContext, kind: str, value: int | str) -> int:
    """Resolve a ``type``/``status``/``priority`` name **or** id to an id (SPEC §5.7)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if not text:
        raise InputValidationError(
            f"{kind} must not be empty.",
            hint=f"Pass a {kind} name or numeric id; get_project_metadata lists both.",
        )

    elements = await _cached_elements(ctx, NAMED_COLLECTIONS[kind])
    lowered = text.lower()
    matches = [
        element
        for element in elements
        if isinstance(element.get("name"), str) and element["name"].strip().lower() == lowered
    ]
    catalog = ", ".join(
        sorted(str(element["name"]) for element in elements if isinstance(element.get("name"), str))
    )
    if len(matches) > 1:
        raise InputValidationError(
            f"The {kind} name {text!r} is ambiguous on this instance.",
            hint=f"Pass the numeric id instead. Names in use: {catalog}.",
        )
    if not matches:
        raise InputValidationError(
            f"Unknown {kind} {text!r} on this instance.",
            hint=(
                f"Valid {kind} values: {catalog or '(none reported)'}. "
                "get_project_metadata returns them with their ids."
            ),
        )
    resolved = hal.self_id(matches[0])
    if isinstance(resolved, int):
        return resolved
    raise InputValidationError(
        f"The {kind} {text!r} has no usable id on this instance.",
        hint="Pass the numeric id directly; get_project_metadata lists them.",
    )


async def _resolve_project_id(ctx: ToolContext, project: int | str) -> int:
    """Resolve a project id **or** identifier to the numeric id a write payload needs."""
    if isinstance(project, int) and not isinstance(project, bool):
        return project
    text = str(project).strip()
    if text.isdigit():
        return int(text)
    payload = await _cached_json(ctx, f"projects/{text}")
    resolved = hal.self_id(payload)
    if isinstance(resolved, int):
        return resolved
    raise InputValidationError(
        f"Project {text!r} did not report a numeric id.",
        hint="Pass the numeric project id; list_projects returns it alongside the identifier.",
    )


async def _milestone_type_ids(ctx: ToolContext) -> list[int]:
    elements = await _cached_elements(ctx, "types")
    ids = [hal.self_id(element) for element in elements if element.get("isMilestone") is True]
    return [value for value in ids if isinstance(value, int)]


async def _schema_for(
    ctx: ToolContext, payload: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch the work package's own schema; failure degrades to a note, never raises (G5)."""
    entry = hal.as_object(_links_of(payload).get("schema"))
    path = _api_path(entry.get("href") if entry is not None else None)
    if not path:
        return None, "custom-field names and types unavailable: no schema link on this work package"
    try:
        return await _cached_json(ctx, path), None
    except OpenProjectError as exc:
        return None, f"custom-field names and types unavailable: {exc.message}"


async def _schema_for_project_type(
    ctx: ToolContext, project_id: int, type_id: int
) -> dict[str, Any]:
    return await _cached_json(ctx, f"work_packages/schemas/{project_id}-{type_id}")


# --- form flow (SPEC §4.5) ------------------------------------------------


def _raise_form_validation_errors(form: Mapping[str, Any]) -> None:
    """Turn a form's ``validationErrors`` into a typed error with allowed values.

    The generic half lives in :mod:`openproject_mcp.tools._forms`; what is
    domain knowledge is the fallback hint naming the tool that shows this
    project and type's required fields.
    """
    _forms.raise_validation_errors(
        form,
        subject="work package",
        hints=_forms.allowed_value_hints,
        fallback_hint=(
            "Fix the attributes listed in 'violations'; get_work_package_schema shows required "
            "fields and allowed values for this project and type."
        ),
    )


# --- includes (SPEC §6.2) -------------------------------------------------


async def _fetch_relations(ctx: ToolContext, work_package_id: int) -> TruncatedList[RelationRow]:
    payload = await ctx.client.get_json(
        f"work_packages/{work_package_id}/relations", params={"pageSize": INCLUDE_CAP}
    )
    unwrapped = hal.collection(payload)
    rows = [RelationRow.from_hal(element) for element in unwrapped]
    return _truncated(
        TruncatedList[RelationRow],
        rows,
        unwrapped.total,
        more_via=f"GET /api/v3/work_packages/{work_package_id}/relations (no Phase 1 tool)",
    )


async def _fetch_watchers(ctx: ToolContext, work_package_id: int) -> TruncatedList[WatcherRow]:
    payload = await ctx.client.get_json(f"work_packages/{work_package_id}/watchers")
    unwrapped = hal.collection(payload)
    rows = [WatcherRow(id=hal.self_id(element), name=element.get("name")) for element in unwrapped]
    return _truncated(
        TruncatedList[WatcherRow],
        rows,
        unwrapped.total,
        more_via=f"GET /api/v3/work_packages/{work_package_id}/watchers (no Phase 1 tool)",
    )


async def _fetch_attachments(
    ctx: ToolContext, work_package_id: int
) -> TruncatedList[AttachmentRow]:
    payload = await ctx.client.get_json(f"work_packages/{work_package_id}/attachments")
    unwrapped = hal.collection(payload)
    rows = [
        AttachmentRow(
            id=hal.self_id(element),
            file_name=element.get("fileName"),
            file_size=element.get("fileSize"),
            content_type=element.get("contentType"),
            description=hal.formattable(element.get("description")),
            author=Ref.from_hal(element, "author"),
            created_at=element.get("createdAt"),
        )
        for element in unwrapped
    ]
    return _truncated(
        TruncatedList[AttachmentRow],
        rows,
        unwrapped.total,
        more_via=(
            f"list_attachments(container_type='work_package', container_id={work_package_id})"
        ),
    )


async def _fetch_children(ctx: ToolContext, work_package_id: int) -> TruncatedList[WorkPackageRow]:
    payload = await ctx.client.get_json(
        "work_packages",
        params=query_params(
            filters=[status_filter("all"), make_filter("parent", Op.EQ, [work_package_id])],
            page=1,
            page_size=INCLUDE_CAP,
        ),
    )
    unwrapped = hal.collection(payload)
    rows = [WorkPackageRow.from_hal(element) for element in unwrapped]
    return _truncated(
        TruncatedList[WorkPackageRow],
        rows,
        unwrapped.total,
        more_via=f"list_work_packages(parent_id={work_package_id}, status_scope='all')",
    )


def _custom_actions(payload: Mapping[str, Any]) -> TruncatedList[CustomActionRow]:
    rows = [
        CustomActionRow(id=item.id, name=item.name) for item in hal.refs(payload, "customActions")
    ]
    return _truncated(TruncatedList[CustomActionRow], rows, len(rows), more_via=None)


# --- registration ---------------------------------------------------------


def register(mcp: FastMCP) -> None:
    """Register the six Phase 1 work-package core tools."""

    @mcp.tool(
        name="search_work_packages",
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.READ),
        annotations=_shared.read_annotations(title="Search work packages"),
    )
    @_shared.tool_errors
    async def search_work_packages(
        query: Annotated[
            str,
            Field(
                description=(
                    "Free text to look for. In 'quick' mode a bare number also matches a work "
                    "package id, so '1234' finds #1234."
                )
            ),
        ],
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Restrict the search to one project: numeric id or the project identifier "
                    "(the slug in the OpenProject URL). Both come from list_projects. Omit to "
                    "search every project the user can see."
                )
            ),
        ] = None,
        mode: Annotated[
            SearchMode,
            Field(
                description=(
                    "'quick' (default) matches subject, id, project name and type/status names — "
                    "this is what the OpenProject header search runs and the right choice for "
                    "'find the ticket called X'. 'fulltext' additionally matches description "
                    "text, comments and searchable custom fields; use it for 'which ticket "
                    "mentions Y'."
                )
            ),
        ] = "quick",
        status_scope: Annotated[
            StatusScope,
            Field(
                description=(
                    "Which statuses to search. Defaults to 'all' because finding closed items is "
                    "usually the point of a search; pass 'open' to hide finished work. An "
                    "explicit status filter is always sent, so the server's implicit open-only "
                    "default never applies."
                )
            ),
        ] = "all",
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="Results per page (max 100).")
        ] = 20,
    ) -> ListEnvelope[WorkPackageRow]:
        """Find work packages by text when you do not know their ids.

        Use this first whenever a user names a ticket instead of numbering it, then feed the
        returned `id` into `get_work_package`, `update_work_package` or `list_work_packages`.

        Returns the standard list envelope: compact rows (id, subject, type, status, priority,
        assignee, project, dates, progress) plus `pagination` with total/page/page_size/has_more.

        Pitfalls: search filters, it does not rank, so a broad query returns a lot — narrow it
        with `project_id`, or switch to `list_work_packages` when you want structured filters
        (assignee, due date, version) rather than text. Attachment-content matching in 'fulltext'
        mode depends on instance database configuration and is reported honestly in `notes`.

        For structured filtering use `list_work_packages`; for one work package's full detail
        (description, custom fields, children) use `get_work_package`.
        """
        ctx = _shared.get_tool_context()
        text = query.strip()
        if not text:
            raise InputValidationError(
                "search_work_packages needs a non-empty query.",
                hint="Pass the text to look for, or use list_work_packages for structured filters.",
            )

        name = "typeahead" if mode == "quick" else "search"
        filters = [status_filter(status_scope), make_filter(name, Op.SEARCH, [text])]
        path = f"projects/{project_id}/work_packages" if project_id else "work_packages"
        payload = await ctx.client.get_json(
            path, params=query_params(filters=filters, page=page, page_size=page_size)
        )
        unwrapped = hal.collection(payload)
        rows = [WorkPackageRow.from_hal(element) for element in unwrapped]
        notes = [ATTACHMENT_SEARCH_NOTE] if mode == "fulltext" else None
        return _shared.envelope_from_collection(
            unwrapped, rows, page=page, page_size=page_size, notes=notes
        )

    @mcp.tool(
        name="list_work_packages",
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.READ),
        annotations=_shared.read_annotations(title="List work packages"),
    )
    @_shared.tool_errors
    async def list_work_packages(
        project: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric project id or project identifier (URL slug) to scope the query. "
                    "Both come from list_projects. Omit for a cross-project view."
                )
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Optional free text, AND-combined with every other filter. Matches subject, "
                    "description and comments. For text-only lookups prefer search_work_packages."
                )
            ),
        ] = None,
        status_scope: Annotated[
            StatusScope,
            Field(
                description=(
                    "Status bucket: 'open' (default), 'closed' or 'all'. An explicit status "
                    "filter is always sent, so the server's implicit open-only default never "
                    "silently applies. Ignored when status_ids is given."
                )
            ),
        ] = "open",
        status_ids: Annotated[
            list[int] | None,
            Field(
                description=(
                    "Exact status ids. **Overrides status_scope** — the two never fight. Ids come "
                    "from get_project_metadata."
                )
            ),
        ] = None,
        type_ids: Annotated[
            list[int] | None,
            Field(description="Work package type ids (Task, Bug…); from get_project_metadata."),
        ] = None,
        priority_ids: Annotated[
            list[int] | None,
            Field(
                description=(
                    "Priority ids; from get_project_metadata. Never guess these — priority ids "
                    "differ per instance."
                )
            ),
        ] = None,
        assignee: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Assignee filter: numeric user ids, the single value 'me', or the single "
                    "value 'none' for unassigned work. Ids come from search_principals; "
                    "get_instance_info gives the current user."
                )
            ),
        ] = None,
        author: Annotated[
            list[str] | None,
            Field(description="Author (creator) user ids, or 'me'. 'none' is not valid here."),
        ] = None,
        responsible: Annotated[
            list[str] | None,
            Field(description="Accountable user ids, 'me', or 'none' for no accountable user."),
        ] = None,
        version_ids: Annotated[
            list[int] | None,
            Field(description="Version / sprint ids; from get_project_metadata."),
        ] = None,
        parent_id: Annotated[
            int | None,
            Field(
                description=(
                    "Direct children of this work package only. Mutually exclusive with "
                    "top_level_only; use ancestor_id for the whole subtree."
                )
            ),
        ] = None,
        top_level_only: Annotated[
            bool,
            Field(description="Only work packages that have no parent. Excludes every subtask."),
        ] = False,
        ancestor_id: Annotated[
            int | None,
            Field(
                description=(
                    "Everything in this work package's subtree at any depth, unlike parent_id "
                    "which is one level only."
                )
            ),
        ] = None,
        milestones_only: Annotated[
            bool,
            Field(
                description=(
                    "Only milestone-type work packages. Resolved against this instance's own "
                    "types (no hardcoded ids) and intersected with type_ids when both are given."
                )
            ),
        ] = False,
        due_before: Annotated[
            str | None, Field(description="Due on or before this ISO date (YYYY-MM-DD).")
        ] = None,
        due_after: Annotated[
            str | None, Field(description="Due on or after this ISO date (YYYY-MM-DD).")
        ] = None,
        start_before: Annotated[
            str | None, Field(description="Starts on or before this ISO date (YYYY-MM-DD).")
        ] = None,
        start_after: Annotated[
            str | None, Field(description="Starts on or after this ISO date (YYYY-MM-DD).")
        ] = None,
        created_since: Annotated[
            str | None, Field(description="Created on or after this ISO date (YYYY-MM-DD).")
        ] = None,
        updated_since: Annotated[
            str | None, Field(description="Last changed on or after this ISO date (YYYY-MM-DD).")
        ] = None,
        percentage_done_min: Annotated[
            int | None, Field(ge=0, le=100, description="Minimum progress percentage, 0-100.")
        ] = None,
        percentage_done_max: Annotated[
            int | None, Field(ge=0, le=100, description="Maximum progress percentage, 0-100.")
        ] = None,
        watcher: Annotated[
            list[str] | None,
            Field(description="Work packages watched by these user ids, or 'me'."),
        ] = None,
        raw_filters: Annotated[
            list[RawFilter] | None,
            Field(
                description=(
                    "Escape hatch for filters this tool does not type, most importantly custom "
                    "fields: [{'name': 'customField12', 'operator': '=', 'values': ['4']}]. "
                    "Custom field names and option ids come from get_work_package_schema."
                )
            ),
        ] = None,
        sort_by: Annotated[
            list[list[str]] | None,
            Field(
                description=(
                    "Server-side sort as snake_case pairs, e.g. [['due_date','asc'],"
                    "['priority','desc']]. An unknown key fails with the allowed set listed."
                )
            ),
        ] = None,
        group_by: Annotated[
            str | None,
            Field(
                description=(
                    "Group the full filtered set by one snake_case column (e.g. 'status', "
                    "'assignee'). Counts in `groups` cover every page, not just this one."
                )
            ),
        ] = None,
        show_sums: Annotated[
            bool,
            Field(
                description=(
                    "Ask the server for totals (estimated/remaining/spent hours, story points) "
                    "over the full filtered set. Never add up pages yourself."
                )
            ),
        ] = False,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="Results per page (max 100).")
        ] = 20,
        fetch_all: Annotated[
            bool,
            Field(
                description=(
                    "Aggregate every page into one result instead of returning page 1. "
                    f"Capped at {_shared.FETCH_ALL_CAP} items with a note when the cap "
                    "bites; mutually exclusive with page."
                )
            ),
        ] = False,
    ) -> ListEnvelope[WorkPackageRow]:
        """List work packages with structured filters — the workhorse read tool.

        Use it for every "what is assigned to me", "what is overdue", "what is in this sprint"
        question. Convenience queries are parameters here, not separate tools: overdue →
        `due_before=<today>`; unassigned → `assignee=['none']`; nearly done →
        `percentage_done_min=80`; subtasks of a ticket → `parent_id=<id>`.

        Returns the standard list envelope: compact rows plus `pagination`, plus `groups` when
        `group_by` was requested and `sums` when `show_sums` was requested. Groups and sums are
        computed server-side over the whole filtered set, independent of paging — never re-add
        them from the rows on one page.

        Pitfalls: this returns **open work packages only** unless you pass `status_scope` or
        `status_ids`, so say so when you report counts. `status_ids` overrides `status_scope`.
        Status, type, priority and version ids differ per instance and must come from
        `get_project_metadata`, never from memory.

        For text lookups use `search_work_packages`; for one work package's description, custom
        fields and children use `get_work_package`.
        """
        ctx = _shared.get_tool_context()

        if parent_id is not None and top_level_only:
            raise InputValidationError(
                "parent_id and top_level_only contradict each other.",
                hint="top_level_only means 'no parent at all'; drop one of the two.",
            )

        filters: list[Filter] = [status_filter(status_scope, status_ids)]
        if query and query.strip():
            filters.append(make_filter("search", Op.SEARCH, [query.strip()]))

        resolved_types = [int(value) for value in type_ids or []]
        if milestones_only:
            milestone_ids = await _milestone_type_ids(ctx)
            if not milestone_ids:
                raise InputValidationError(
                    "This instance defines no milestone work package types.",
                    hint="Drop milestones_only, or check get_project_metadata for the type list.",
                )
            resolved_types = (
                [value for value in resolved_types if value in milestone_ids]
                if resolved_types
                else milestone_ids
            )
            if not resolved_types:
                raise InputValidationError(
                    "None of the given type_ids are milestone types.",
                    hint=f"Milestone type ids on this instance: {milestone_ids}.",
                )
        if resolved_types:
            filters.append(make_filter("type", Op.EQ, resolved_types))
        if priority_ids:
            filters.append(make_filter("priority", Op.EQ, list(priority_ids)))
        if version_ids:
            filters.append(make_filter("version", Op.EQ, list(version_ids)))
        if assignee:
            filters.append(principal_filter("assignee", list(assignee)))
        if author:
            filters.append(principal_filter("author", list(author)))
        if responsible:
            filters.append(principal_filter("responsible", list(responsible)))
        if watcher:
            filters.append(principal_filter("watcher", list(watcher)))
        if parent_id is not None:
            filters.append(make_filter("parent", Op.EQ, [parent_id]))
        if top_level_only:
            filters.append(make_filter("parent", Op.NONE))
        if ancestor_id is not None:
            filters.append(make_filter("ancestor", Op.EQ, [ancestor_id]))
        if due_before or due_after:
            filters.append(date_range_filter("dueDate", after=due_after, before=due_before))
        if start_before or start_after:
            filters.append(date_range_filter("startDate", after=start_after, before=start_before))
        if created_since:
            filters.append(date_range_filter("createdAt", after=created_since))
        if updated_since:
            filters.append(date_range_filter("updatedAt", after=updated_since))
        if percentage_done_min is not None:
            filters.append(make_filter("percentageDone", Op.GTE, [percentage_done_min]))
        if percentage_done_max is not None:
            filters.append(make_filter("percentageDone", Op.LTE, [percentage_done_max]))
        filters.extend(filter_from_raw(raw) for raw in raw_filters or [])

        if group_by and group_by not in WORK_PACKAGE_SORT_KEYS:
            raise InputValidationError(
                f"Unknown group_by column {group_by!r}.",
                hint=f"Allowed columns: {', '.join(sorted(WORK_PACKAGE_SORT_KEYS))}.",
            )

        _shared.require_first_page_for_fetch_all(fetch_all, page)
        path = f"projects/{project}/work_packages" if project else "work_packages"

        async def get_page(fetch_page: int, fetch_size: int) -> Mapping[str, Any]:
            return await ctx.client.get_json(
                path,
                params=query_params(
                    filters=filters,
                    page=fetch_page,
                    page_size=fetch_size,
                    sort_by=sort_by,
                    sort_keys=WORK_PACKAGE_SORT_KEYS,
                    group_by=group_by,
                    show_sums=show_sums or None,
                ),
            )

        if fetch_all:
            elements, last_chunk, cap_notes = await _shared.collect_all(
                get_page, label="fetching all work packages"
            )
            return _shared.fetch_all_envelope(
                [WorkPackageRow.from_hal(element) for element in elements],
                last_chunk,
                notes=cap_notes,
            )

        payload = await get_page(page, page_size)
        unwrapped = hal.collection(payload)
        rows = [WorkPackageRow.from_hal(element) for element in unwrapped]
        return _shared.envelope_from_collection(unwrapped, rows, page=page, page_size=page_size)

    @mcp.tool(
        name="get_work_package",
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.READ),
        annotations=_shared.read_annotations(title="Get work package detail"),
    )
    @_shared.tool_errors
    async def get_work_package(
        id: Annotated[
            int | str,
            Field(
                description=(
                    "Work package id — the number shown as #1234 in OpenProject. Comes from "
                    "search_work_packages or list_work_packages. Instances on 17.x with "
                    "semantic identifiers enabled also accept the semantic form ('PROJ-42', "
                    "the row's display_id)."
                )
            ),
        ],
        include: Annotated[
            list[IncludeName] | None,
            Field(
                description=(
                    "Extra sub-resources, fetched concurrently: 'relations', 'watchers', "
                    "'attachments', 'children', 'custom_actions'. Each is capped at 20 items and "
                    "reports {truncated, total, more_via} when there are more. Ask only for what "
                    "you need — every include is one more upstream request."
                )
            ),
        ] = None,
    ) -> WorkPackageWithIncludes:
        """Read one work package in full: description, dates, custom fields, parent and progress.

        This is the tool to call once a search or list has given you an id, and the only way to
        read a work package's description text. The `lock_version` in the result is what
        `update_work_package` needs for a safe concurrent edit.

        Returns every core field, `custom_fields` in the canonical
        `[{key, name, type, value, value_ids}]` shape (only fields that have a value), an
        `available` map saying whether this work package exposes dev links, meetings or file
        links, and any requested includes.

        Pitfalls: includes are capped at 20 — a truncated `children` list means you should call
        `list_work_packages(parent_id=…)` for the rest, which `more_via` spells out verbatim. A
        sub-resource that 403s or 404s (module off, no permission) degrades into a `notes` entry
        instead of failing the whole read.

        For the comment thread use `list_work_package_comments`; for attachment bytes use
        `download_attachment`; for linked PRs and commits use `get_work_package_git_activity`.
        """
        ctx = _shared.get_tool_context()
        requested = list(dict.fromkeys(include or []))

        key = str(id).strip()
        if not key:
            raise InputValidationError(
                "id must not be empty.",
                hint=(
                    "Pass the numeric work package id, or its display_id on instances with "
                    "semantic identifiers."
                ),
            )
        try:
            payload = await ctx.client.get_json(f"work_packages/{quote(key, safe='')}")
        except NotFoundError as exc:
            if key.isdigit():
                raise
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    f"{key!r} did not resolve. Semantic identifiers ('PROJ-42') only work on "
                    "OpenProject 17+ with semantic work package identifiers enabled — pass "
                    "the numeric id from search_work_packages or list_work_packages instead."
                ),
            ) from exc
        # Includes always address the numeric id from the payload's self link, so a
        # semantic lookup never leaks the semantic form onto nested routes.
        self_id = hal.self_id(payload)
        wp_id = self_id if isinstance(self_id, int) else (id if isinstance(id, int) else None)
        schema, schema_note = await _schema_for(ctx, payload)
        notes: list[str] = [schema_note] if schema_note else []

        fetchers = {
            "relations": _fetch_relations,
            "watchers": _fetch_watchers,
            "attachments": _fetch_attachments,
            "children": _fetch_children,
        }
        remote: list[str] = []
        gathered: list[Any] = []
        if wp_id is not None:
            remote = [name for name in requested if name in fetchers]
            gathered = list(
                await asyncio.gather(
                    *(fetchers[name](ctx, wp_id) for name in remote), return_exceptions=True
                )
            )
        elif any(name in fetchers for name in requested):
            notes.append("includes unavailable: the work package exposed no numeric id")

        detail = WorkPackageWithIncludes(**_detail_fields(payload, schema))
        for name, outcome in zip(remote, gathered, strict=True):
            if isinstance(outcome, OpenProjectError):
                notes.append(f"{name} unavailable: {outcome.message}")
                continue
            if isinstance(outcome, BaseException):
                raise outcome
            setattr(detail, name, outcome)
        if "custom_actions" in requested:
            detail.custom_actions = _custom_actions(payload)
        detail.notes = notes or None
        return detail

    @mcp.tool(
        name="create_work_package",
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.WRITE),
        annotations=_shared.write_annotations(title="Create work package"),
    )
    @_shared.tool_errors
    async def create_work_package(
        project: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric project id or project identifier (URL slug). Both come from "
                    "list_projects."
                )
            ),
        ],
        type: Annotated[
            str,
            Field(
                description=(
                    "Work package type as a **name or numeric id** ('Task', 'Bug', 'Milestone', "
                    "or 7). Names resolve against this instance's types; an unknown or ambiguous "
                    "name fails with the valid values listed."
                )
            ),
        ],
        subject: Annotated[str, Field(description="The title. Required and must not be blank.")],
        description: Annotated[
            str | None, Field(description="Body text in markdown. Omit for an empty description.")
        ] = None,
        start_date: Annotated[
            str | None, Field(description="ISO date (YYYY-MM-DD). Not valid on milestone types.")
        ] = None,
        due_date: Annotated[
            str | None, Field(description="ISO date (YYYY-MM-DD). Not valid on milestone types.")
        ] = None,
        date: Annotated[
            str | None,
            Field(
                description=(
                    "The single ISO date of a **milestone**. Milestones carry `date` instead of "
                    "start_date/due_date; passing both shapes is rejected locally."
                )
            ),
        ] = None,
        status: Annotated[
            str | None,
            Field(
                description=(
                    "Status name or numeric id. Omit to take the type's default status — do not "
                    "guess an id."
                )
            ),
        ] = None,
        priority: Annotated[
            str | None,
            Field(
                description=(
                    "Priority name or numeric id ('High', 'Normal', or 8). Omit for the instance "
                    "default; priority ids differ per instance."
                )
            ),
        ] = None,
        assignee: Annotated[
            str | None,
            Field(
                description=(
                    "Numeric user id to assign. 'me' is not accepted in writes — call "
                    "get_instance_info for the current user's id."
                )
            ),
        ] = None,
        responsible: Annotated[
            str | None, Field(description="Numeric user id of the accountable person.")
        ] = None,
        version: Annotated[
            str | None,
            Field(description="Numeric version / sprint id; from get_project_metadata."),
        ] = None,
        parent_id: Annotated[
            int | None,
            Field(description="Create this as a child of an existing work package id."),
        ] = None,
        estimated_hours: Annotated[
            float | None,
            Field(ge=0, description="Estimate in hours as a decimal (7.5 = seven and a half)."),
        ] = None,
        custom_fields: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Custom field writes keyed by wire key or display name: "
                    "{'customField12': 'High'} or {'Severity': 'High'}. List/user/version fields "
                    "accept option ids or option names. Unknown or ambiguous keys fail with the "
                    "valid keys listed — nothing is ever silently dropped. "
                    "get_work_package_schema shows what this project and type accept."
                )
            ),
        ] = None,
        attachment_paths: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Local file paths to attach. Files upload uncontainered first and are claimed "
                    "by the new work package, which is the flow that works even when the author "
                    "lacks edit permission. Only usable when the server shares a filesystem with "
                    "you (stdio transport)."
                )
            ),
        ] = None,
        notify: Annotated[
            bool, Field(description="Send OpenProject notification emails for this creation.")
        ] = True,
    ) -> WorkPackageFull:
        """Create a work package, validated through OpenProject's own form endpoint first.

        Use it for new tasks, bugs, subtasks (`parent_id`) and milestones (`date`). The form
        pre-flight is what makes failures useful: an invalid status, a missing required custom
        field or a type the project does not enable comes back as structured violations *with the
        allowed values*, before anything is written.

        Returns the created work package in full detail, including its new `id`, `lock_version`
        and resolved custom fields.

        Pitfalls: `type`, `status` and `priority` take names or ids, but versions, assignees and
        parents need numeric ids. Milestone types reject `start_date`/`due_date` — use `date`.
        Custom fields must exist on the project/type schema; check `get_work_package_schema` when
        unsure.

        To change it afterwards use `update_work_package`; to attach a file to an existing work
        package use `upload_attachment`.
        """
        ctx = _shared.get_tool_context()

        if date and (start_date or due_date):
            raise InputValidationError(
                "A milestone takes 'date'; other types take 'start_date'/'due_date'.",
                hint="Pass either 'date' alone or start_date/due_date, never both shapes.",
            )
        if not subject.strip():
            raise InputValidationError(
                "subject must not be blank.", hint="Give the work package a title."
            )

        project_id = await _resolve_project_id(ctx, project)
        type_id = await _resolve_named(ctx, "type", type)

        attributes: dict[str, Any] = {"subject": subject}
        if description is not None:
            attributes["description"] = formattable_field(description)
        if start_date:
            attributes["startDate"] = start_date
        if due_date:
            attributes["dueDate"] = due_date
        if date:
            attributes["date"] = date
        if estimated_hours is not None:
            attributes["estimatedTime"] = _duration_from_hours(estimated_hours)

        links: dict[str, Any] = {
            "project": link("projects", project_id),
            "type": link("types", type_id),
        }
        if status is not None:
            links["status"] = link("statuses", await _resolve_named(ctx, "status", status))
        if priority is not None:
            links["priority"] = link("priorities", await _resolve_named(ctx, "priority", priority))
        if assignee is not None:
            links["assignee"] = link(
                "users", _numeric_id(assignee, field="assignee", produced_by="search_principals")
            )
        if responsible is not None:
            links["responsible"] = link(
                "users",
                _numeric_id(responsible, field="responsible", produced_by="search_principals"),
            )
        if version is not None:
            links["version"] = link(
                "versions",
                _numeric_id(version, field="version", produced_by="get_project_metadata"),
            )
        if parent_id is not None:
            links["parent"] = link("work_packages", parent_id)

        if custom_fields:
            schema = await _schema_for_project_type(ctx, project_id, type_id)
            cf_attributes, cf_links = custom_field_payload(custom_fields, schema)
            attributes.update(cf_attributes)
            links.update(cf_links)

        if attachment_paths:
            # Imported at call time: attachments.py is a sibling Phase 1 tool module and a
            # module-level import would couple registration order between the two.
            from openproject_mcp.tools.attachments import upload_uncontainered_attachment

            total = len(attachment_paths)
            attachment_ids: list[int] = []
            for index, file_path in enumerate(attachment_paths):
                await _shared.report_progress(index, total, f"uploading {file_path}")
                attachment_ids.append(await upload_uncontainered_attachment(ctx, file_path))
            await _shared.report_progress(total, total, "uploads complete")
            links["attachments"] = [
                {"href": href_for("attachments", attachment_id)} for attachment_id in attachment_ids
            ]

        payload = build_write_payload(attributes, links)

        form = await ctx.client.post_json("work_packages/form", json=payload)
        _raise_form_validation_errors(form)
        body = _forms.merge_form_payload(_forms.form_payload(form) or {}, payload)

        created = await ctx.client.post_json(
            "work_packages", json=body, params={"notify": "true" if notify else "false"}
        )
        schema, schema_note = await _schema_for(ctx, created)
        return WorkPackageFull(
            **_detail_fields(created, schema, [schema_note] if schema_note else None)
        )

    @mcp.tool(
        name="update_work_package",
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.WRITE),
        annotations=_shared.write_annotations(title="Update work package"),
    )
    @_shared.tool_errors
    async def update_work_package(
        id: Annotated[int, Field(description="Work package id to change (the #1234 number).")],
        lock_version: Annotated[
            int | None,
            Field(
                description=(
                    "The `lock_version` you read from get_work_package. Pass it and the write "
                    "fails loudly (409) if somebody else edited the work package in the meantime. "
                    "Omit it and the current version is fetched and echoed — still safe, just one "
                    "more round trip and a slightly wider conflict window."
                )
            ),
        ] = None,
        subject: Annotated[
            str | None, Field(description="New title. Omit to leave unchanged; cannot be cleared.")
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description=(
                    "New markdown body. Omit to leave unchanged; pass null to empty it. Replaces "
                    "the whole description — read it with get_work_package first if you mean to "
                    "append."
                )
            ),
        ] = KEEP,
        type: Annotated[
            str | None,
            Field(description="New type as a name or numeric id. Cannot be cleared."),
        ] = None,
        status: Annotated[
            str | None,
            Field(
                description=(
                    "New status as a name or numeric id. Validated through the form endpoint, so "
                    "an invalid workflow transition comes back listing the statuses that *are* "
                    "reachable from the current one."
                )
            ),
        ] = None,
        priority: Annotated[
            str | None, Field(description="New priority as a name or numeric id.")
        ] = None,
        assignee: Annotated[
            str | None,
            Field(
                description=(
                    "Numeric user id to assign. Omit to leave unchanged; pass null (or 'none') to "
                    "unassign — that sends a null href rather than a bogus user id."
                )
            ),
        ] = KEEP,
        responsible: Annotated[
            str | None,
            Field(description="Numeric user id of the accountable person; null clears it."),
        ] = KEEP,
        version: Annotated[
            str | None,
            Field(description="Numeric version / sprint id; null removes it from the version."),
        ] = KEEP,
        parent_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Re-parent this work package under another id; null detaches it and makes it "
                    "top level. This is the only hierarchy tool — there is no separate "
                    "set/remove-parent tool."
                )
            ),
        ] = KEEP,
        start_date: Annotated[
            str | None, Field(description="ISO date (YYYY-MM-DD); null clears it.")
        ] = KEEP,
        due_date: Annotated[
            str | None, Field(description="ISO date (YYYY-MM-DD); null clears it.")
        ] = KEEP,
        date: Annotated[
            str | None,
            Field(description="Milestone date (YYYY-MM-DD); only valid on milestone types."),
        ] = KEEP,
        percentage_done: Annotated[
            int | None, Field(ge=0, le=100, description="Progress 0-100.")
        ] = None,
        estimated_hours: Annotated[
            float | None, Field(ge=0, description="Estimate in hours as a decimal.")
        ] = None,
        custom_fields: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Custom field writes keyed by wire key or display name, e.g. "
                    "{'Severity': 'High'}. Unknown or non-writable keys fail with the valid keys "
                    "listed. Only the keys you pass are touched."
                )
            ),
        ] = None,
        notify: Annotated[
            bool, Field(description="Send OpenProject notification emails for this change.")
        ] = True,
    ) -> WorkPackageFull:
        """Change any writable field of a work package, with optimistic locking done properly.

        Use it to assign or unassign, move a status forward, re-schedule, re-parent, set progress
        or write custom fields. Every convenience the old tooling spread across a dozen tools is a
        parameter here.

        Returns the updated work package in full detail, including the new `lock_version` to use
        for a follow-up edit.

        Pitfalls: omitted parameters are left alone, while passing null **clears** a field
        (assignee, responsible, version, parent, dates, description). A 409 error means somebody
        else changed the work package first — the error carries the fresh `lock_version` and the
        conflicting fields, so re-read, decide, and retry deliberately rather than blindly.
        Status changes are validated against the workflow, so an invalid transition lists the
        allowed targets.

        Ids come from `get_work_package` / `list_work_packages`; status, priority, type and
        version values come from `get_project_metadata`.
        """
        ctx = _shared.get_tool_context()
        path = f"work_packages/{id}"

        attributes: dict[str, Any] = {}
        links: dict[str, Any] = {}

        if subject is not None:
            if not subject.strip():
                raise InputValidationError(
                    "subject must not be blank.",
                    hint="Omit 'subject' to leave the title unchanged.",
                )
            attributes["subject"] = subject
        if not _is_keep(description):
            attributes["description"] = formattable_field(description or "")
        for wire_name, value in (
            ("startDate", start_date),
            ("dueDate", due_date),
            ("date", date),
        ):
            if not _is_keep(value):
                attributes[wire_name] = None if _is_clear(value) else value
        if percentage_done is not None:
            attributes["percentageDone"] = percentage_done
        if estimated_hours is not None:
            attributes["estimatedTime"] = _duration_from_hours(estimated_hours)

        if type is not None:
            links["type"] = link("types", await _resolve_named(ctx, "type", type))
        if status is not None:
            links["status"] = link("statuses", await _resolve_named(ctx, "status", status))
        if priority is not None:
            links["priority"] = link("priorities", await _resolve_named(ctx, "priority", priority))
        if not _is_keep(assignee):
            links["assignee"] = link(
                "users",
                None
                if _is_clear(assignee)
                else _numeric_id(assignee, field="assignee", produced_by="search_principals"),
            )
        if not _is_keep(responsible):
            links["responsible"] = link(
                "users",
                None
                if _is_clear(responsible)
                else _numeric_id(responsible, field="responsible", produced_by="search_principals"),
            )
        if not _is_keep(version):
            links["version"] = link(
                "versions",
                None
                if _is_clear(version)
                else _numeric_id(version, field="version", produced_by="get_project_metadata"),
            )
        if not _is_keep(parent_id):
            links["parent"] = link(
                "work_packages",
                None
                if _is_clear(parent_id)
                else _numeric_id(parent_id, field="parent_id", produced_by="list_work_packages"),
            )

        if not attributes and not links and not custom_fields:
            raise InputValidationError(
                "update_work_package was called with nothing to change.",
                hint="Pass at least one writable field, e.g. subject, status or assignee.",
            )

        # One read serves both the custom-field schema link and the lock version, so the two can
        # never come from different snapshots of the work package.
        current = await ctx.client.get_json(path) if custom_fields or lock_version is None else None

        if custom_fields:
            cf_schema, cf_note = await _schema_for(ctx, current or {})
            if cf_schema is None:
                raise InputValidationError(
                    f"Cannot write custom fields: {cf_note}.",
                    hint=(
                        "Read the schema with get_work_package_schema and pass wire keys, or drop "
                        "custom_fields from the call."
                    ),
                )
            cf_attributes, cf_links = custom_field_payload(custom_fields, cf_schema)
            attributes.update(cf_attributes)
            links.update(cf_links)

        supplied_version = lock_version
        if supplied_version is None and current is not None:
            supplied_version = extract_lock_version(current)
        resolved_version, _ = await resolve_lock_version(
            ctx.client, path, supplied=supplied_version
        )
        payload = build_write_payload(attributes, links)

        # Form first (SPEC §4.5): the form knows the workflow, so an invalid status transition
        # comes back with the reachable statuses instead of an opaque 422.
        form = await ctx.client.post_json(
            f"{path}/form",
            json=build_write_payload(attributes, links, lock_version=resolved_version),
        )
        _raise_form_validation_errors(form)

        # The PATCH deliberately sends only what the caller asked for, not the form's echoed
        # payload — echoing that back would rewrite fields somebody else just changed.
        updated = await patch_with_lock(
            ctx.client,
            path,
            payload,
            lock_version=resolved_version,
            params={"notify": "true" if notify else "false"},
        )
        schema, schema_note = await _schema_for(ctx, updated)
        return WorkPackageFull(
            **_detail_fields(updated, schema, [schema_note] if schema_note else None)
        )

    @mcp.tool(
        name="delete_work_package",
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.WRITE, _shared.DESTRUCTIVE),
        annotations=_shared.destructive_annotations(title="Delete work package"),
    )
    @_shared.tool_errors
    async def delete_work_package(
        id: Annotated[int, Field(description="Work package id to delete permanently.")],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true. Ask the user to confirm first — the API offers no undo. "
                    "Calling with confirm=false returns a confirmation_required error rather than "
                    "deleting anything."
                )
            ),
        ] = False,
    ) -> DeletionResult:
        """Permanently delete a work package and everything attached to it.

        Use only on explicit user instruction. Deletion removes the work package with its
        comments, attachments, time entries and relations, and OpenProject offers no API-side
        undo.

        Returns a small confirmation object once OpenProject accepts the deletion.

        Pitfalls: children are **not** deleted with the parent, so check
        `get_work_package(id, include=['children'])` first and decide what happens to them. If you
        only want the work package out of the way, `update_work_package(id, status=<a closed
        status>)` is almost always the better answer — `get_project_metadata` lists which statuses
        this instance treats as closed.
        """
        _shared.require_confirmation(
            confirm,
            action="delete work package",
            target=f"#{id}",
            consequence=(
                "The work package and its comments, attachments, time entries and relations are "
                "removed permanently."
            ),
        )
        ctx = _shared.get_tool_context()
        await ctx.client.delete(f"work_packages/{id}")
        return DeletionResult(
            id=id, deleted=True, message=f"Work package #{id} was deleted permanently."
        )
