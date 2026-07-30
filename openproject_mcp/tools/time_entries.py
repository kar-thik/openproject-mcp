"""Time-tracking tools (SPEC §6.9 — Phase 2).

Lands here:

=============================  ======  ===============================================
Tool                           Phase   Endpoint(s)
=============================  ======  ===============================================
🔍 ``list_time_entries``       2       ``GET /time_entries``
✏️ ``log_time``                2       ``POST /time_entries/form`` → ``POST /time_entries``
✏️ ``update_time_entry``       2       ``PATCH /time_entries/{id}``
🗑 ``delete_time_entry``       2       ``DELETE /time_entries/{id}``
=============================  ======  ===============================================

Non-negotiables for this module:

* **The work-package filter name is probed, never assumed** (SPEC §4.7).
  Current OpenProject filters time entries with ``entityId`` + ``entityType``;
  the ``workPackage`` filter was removed in 2025-05 and older instances only
  understand that one. ``ctx.probe().time_entry_work_package_filter`` decides,
  and the legacy path leaves a note on the result (G5).
* Durations cross the wire as ISO 8601 (``PT1H30M``) and are float hours in
  every tool signature and projection (SPEC §5.8), both directions.
* Activities are instance data: ``activity`` accepts a name or an id and is
  resolved against the **form's** ``allowedValues``, so a miss fails locally
  with the valid activities listed instead of a bare 422 (G2/G3).
* ``sum_hours`` pages through the whole match set with an explicit cap and says
  so in ``notes`` when the cap bites (G1) — a total that silently covered one
  page would be worse than no total.
* ``update_time_entry`` decides between ``patch_with_lock`` and a plain PATCH
  from what the fetched resource actually reports: OpenProject exposes
  ``lockVersion`` on some resources and versions and not on others, and echoing
  a lock version that does not exist is as wrong as omitting one that does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    ValidationFailedError,
    violations_from_form,
)
from openproject_mcp.client.filters import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Filter,
    FilterType,
    Op,
    date_range_filter,
    make_filter,
    principal_filter,
    query_params,
    register_filter_type,
)
from openproject_mcp.client.locking import extract_lock_version, patch_with_lock
from openproject_mcp.client.payloads import link
from openproject_mcp.projections import Group, ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    DESTRUCTIVE,
    GROUP_TIME_ENTRIES,
    READ,
    WRITE,
    build_envelope,
    destructive_annotations,
    envelope_from_collection,
    get_tool_context,
    read_annotations,
    report_progress,
    require_confirmation,
    tool_errors,
    tool_tags,
    write_annotations,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["TimeEntry", "TimeEntryDeletion", "TimeEntryRow", "register"]

#: Filter validation scope for this endpoint (never the shared global table).
TIME_ENTRIES_RESOURCE = "time_entries"

#: Page size used internally while summing; the API's maximum useful chunk.
SUM_PAGE_SIZE = 100

#: Hard cap on entries pulled for a sum (SPEC §6.9); a cap hit is reported (G1).
SUM_ENTRY_CAP = 2000

LEGACY_FILTER_NOTE = (
    "this instance still uses the pre-15.x 'workPackage' time-entry filter, so work-package "
    "scoping was applied with that filter name instead of entityId/entityType"
)

SUM_SCOPE_NOTE = (
    "sums and groups were computed over every entry matching the filters, not just this page; "
    "'items' is still the requested page"
)


class TimeEntryRow(BaseModel):
    """One logged time entry, compact enough to list hundreds of."""

    id: int | str | None = Field(
        default=None,
        description="Time entry id. Feed it to update_time_entry or delete_time_entry.",
    )
    hours: float | None = Field(
        default=None,
        description="Logged duration in hours as a float (1.5 = one and a half hours), "
        "converted from OpenProject's ISO 8601 duration.",
    )
    spent_on: str | None = Field(
        default=None, description="The date the work was done, ISO YYYY-MM-DD."
    )
    comment: str | None = Field(
        default=None, description="Free-text comment as entered (raw); html is dropped."
    )
    user: Ref | None = Field(default=None, description="User the time is booked for.")
    activity: Ref | None = Field(
        default=None,
        description="Time-entry activity (Development, Management, …); instance-defined.",
    )
    work_package: Ref | None = Field(
        default=None,
        description="Work package the time is booked on; null for project-level entries.",
    )
    project: Ref | None = Field(default=None, description="Project the entry belongs to.")


class TimeEntry(TimeEntryRow):
    """A single time entry with the fields only a detail read carries."""

    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    lock_version: int | None = Field(
        default=None,
        description="Optimistic-locking version, when this instance reports one for time "
        "entries; null means the resource is updated without a lock version.",
    )


class TimeEntryDeletion(BaseModel):
    """Outcome of ``delete_time_entry``."""

    id: int = Field(description="Id of the time entry that was deleted.")
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    message: str = Field(description="Human-readable confirmation.")


# --- conversions ----------------------------------------------------------


def _iso_duration(hours: float) -> str:
    """``1.5`` → ``"PT1H30M"`` — the wire format for a time entry's ``hours``."""
    total_minutes = round(float(hours) * 60)
    whole_hours, minutes = divmod(total_minutes, 60)
    if whole_hours and minutes:
        return f"PT{whole_hours}H{minutes}M"
    if whole_hours:
        return f"PT{whole_hours}H"
    return f"PT{minutes}M"


def _require_hours(hours: float) -> str:
    """Validate a duration and return its wire form."""
    if hours != hours or hours in (float("inf"), float("-inf")):  # NaN / infinities
        raise InputValidationError(
            "hours must be a finite number of hours.",
            hint="Pass a positive float, e.g. 1.5 for one and a half hours.",
        )
    if hours <= 0:
        raise InputValidationError(
            f"hours={hours} is not a positive duration.",
            hint="Pass hours as a positive float, e.g. 0.5 for 30 minutes or 1.5 for 1h30.",
        )
    duration = _iso_duration(hours)
    if duration == "PT0M":
        raise InputValidationError(
            f"hours={hours} rounds to zero minutes.",
            hint="OpenProject stores time to the minute; the smallest entry is about 0.017 hours.",
        )
    return duration


def _require_iso_date(name: str, value: str) -> str:
    """Validate ``YYYY-MM-DD`` locally so a typo never costs a round trip."""
    text = str(value).strip()
    try:
        dt.date.fromisoformat(text)
    except ValueError as exc:
        raise InputValidationError(
            f"{name}={value!r} is not an ISO date.",
            hint=f"{name} must be YYYY-MM-DD, e.g. 2026-07-01.",
        ) from exc
    return text


def _work_package_ref(element: Mapping[str, Any]) -> Ref | None:
    """The work package a time entry belongs to, under either link spelling.

    Newer OpenProject models the target as a polymorphic ``entity``; older ones
    only expose ``workPackage``. Project-level entries have neither.
    """
    resolved = Ref.from_hal(element, "workPackage")
    if resolved is not None:
        return resolved
    entity_type = element.get("entityType")
    if isinstance(entity_type, str) and entity_type != "WorkPackage":
        return None
    return Ref.from_hal(element, "entity")


def _entry_row(element: Mapping[str, Any]) -> TimeEntryRow:
    return TimeEntryRow(
        id=hal.self_id(element),
        hours=hal.duration_hours(element.get("hours")),
        spent_on=element.get("spentOn"),
        comment=hal.formattable(element.get("comment")),
        user=Ref.from_hal(element, "user"),
        activity=Ref.from_hal(element, "activity"),
        work_package=_work_package_ref(element),
        project=Ref.from_hal(element, "project"),
    )


def _entry_detail(element: Mapping[str, Any]) -> TimeEntry:
    row = _entry_row(element)
    return TimeEntry(
        **row.model_dump(),
        created_at=element.get("createdAt"),
        updated_at=element.get("updatedAt"),
        lock_version=extract_lock_version(element),
    )


# --- activities and the form flow (SPEC §4.5) -----------------------------


def _activity_options(form: Mapping[str, Any]) -> list[tuple[int | str | None, str | None]]:
    """Allowed activities of a time-entry form as ``(id, name)`` pairs.

    Both spellings OpenProject uses are handled: ``_links.allowedValues`` (link
    objects with a title) and ``_embedded.allowedValues`` (whole resources).
    """
    schema = hal.embedded(form, "schema")
    entry = schema.get("activity") if isinstance(schema, Mapping) else None
    if not isinstance(entry, Mapping):
        return []

    links = entry.get("_links")
    if isinstance(links, Mapping):
        values = links.get("allowedValues")
        if isinstance(values, Sequence) and not isinstance(values, str | bytes):
            return [
                (
                    hal.id_from_href(
                        item.get("href") if isinstance(item.get("href"), str) else None
                    ),
                    item.get("title") if isinstance(item.get("title"), str) else None,
                )
                for item in values
                if isinstance(item, Mapping)
            ]

    inlined = hal.embedded(entry, "allowedValues")
    if isinstance(inlined, Sequence) and not isinstance(inlined, str | bytes):
        pairs: list[tuple[int | str | None, str | None]] = []
        for item in inlined:
            if not isinstance(item, Mapping):
                continue
            name = item.get("name") or item.get("title")
            pairs.append((hal.self_id(item), name if isinstance(name, str) else None))
        return pairs
    return []


def _list_activities(options: Sequence[tuple[int | str | None, str | None]]) -> str:
    listed = ", ".join(f"{name} ({option_id})" for option_id, name in options if name)
    return listed or "(the form did not list any; get_project_metadata shows them)"


def _numeric_activity(value: str | int) -> int | None:
    """The activity id when one was passed directly, else ``None`` (a name)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def _resolve_activity_name(name: str, form: Mapping[str, Any]) -> int | str:
    """Resolve an activity name against the form's allowed values (G2)."""
    options = _activity_options(form)
    lowered = name.strip().lower()
    matches = [
        option_id
        for option_id, option_name in options
        if option_name and option_name.strip().lower() == lowered and option_id is not None
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise InputValidationError(
            f"Activity name {name!r} is ambiguous on this instance.",
            hint=f"Pass the numeric id instead. Activities here: {_list_activities(options)}.",
        )
    raise InputValidationError(
        f"{name!r} is not a time-entry activity on this instance.",
        hint=(
            f"Valid activities here: {_list_activities(options)}. "
            "get_project_metadata(project_id=...) lists them with their ids."
        ),
    )


def _raise_form_errors(form: Mapping[str, Any]) -> None:
    """Turn a form's ``validationErrors`` into a typed 422 with allowed values.

    The form endpoint knows both what is wrong and what would be accepted; that
    second half is what makes an activity or date rejection actionable.
    """
    errors = hal.embedded(form, "validationErrors")
    if not isinstance(errors, Mapping) or not errors:
        return

    violations = violations_from_form(errors)
    hints: list[str] = []
    if "activity" in errors:
        hints.append(f"Allowed activities: {_list_activities(_activity_options(form))}.")
    if not hints:
        hints.append(
            "Fix the attributes listed in 'violations'. get_project_metadata(project_id=...) "
            "lists the activities this project accepts, and time can only be logged on "
            "projects where the time-tracking module is enabled and you hold the "
            "log-time permission."
        )

    identifier: str | None = None
    first = next((value for value in errors.values() if isinstance(value, Mapping)), None)
    if first is not None and isinstance(first.get("errorIdentifier"), str):
        identifier = first["errorIdentifier"]

    raise ValidationFailedError(
        violations[0]["message"] if violations else "OpenProject rejected the time entry.",
        http_status=422,
        error_identifier=identifier,
        hint=" ".join(hints),
        violations=violations,
    )


def _links_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    links = payload.get("_links")
    return dict(links) if isinstance(links, Mapping) else {}


def _merge_payload(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the form's defaulted payload with ours; ours wins, ``_links`` merges."""
    merged: dict[str, Any] = {
        key: value for key, value in base.items() if key not in ("_links", "_type")
    }
    merged.update({key: value for key, value in override.items() if key != "_links"})
    links = {**_links_of(base), **_links_of(override)}
    links.pop("self", None)
    if links:
        merged["_links"] = links
    return merged


def _entry_links(
    *,
    work_package_id: int | None = None,
    project_id: int | str | None = None,
    activity_id: int | str | None = None,
) -> dict[str, Any]:
    """``_links`` for a time-entry write, with the wire key spellings."""
    links: dict[str, Any] = {}
    if work_package_id is not None:
        links["workPackage"] = link("work_packages", work_package_id)
    if project_id is not None:
        links["project"] = link("projects", project_id)
    if activity_id is not None:
        links["activity"] = link("time_entries/activities", activity_id)
    return links


def _register_filters() -> None:
    """Teach the filter validator the time-entry filter names we send."""
    register_filter_type("spentOn", FilterType.DATE, TIME_ENTRIES_RESOURCE)
    register_filter_type("user", FilterType.LIST, TIME_ENTRIES_RESOURCE)
    register_filter_type("project", FilterType.LIST, TIME_ENTRIES_RESOURCE)
    register_filter_type("activity", FilterType.LIST, TIME_ENTRIES_RESOURCE)
    register_filter_type("workPackage", FilterType.LIST, TIME_ENTRIES_RESOURCE)
    register_filter_type("entityId", FilterType.LIST, TIME_ENTRIES_RESOURCE)
    register_filter_type("entityType", FilterType.LIST, TIME_ENTRIES_RESOURCE)


def register(mcp: FastMCP) -> None:
    """Register the time-tracking tools (SPEC §6.9)."""
    _register_filters()

    @mcp.tool(
        name="list_time_entries",
        tags=tool_tags(GROUP_TIME_ENTRIES, READ),
        annotations=read_annotations(title="List time entries"),
    )
    @tool_errors
    async def list_time_entries(
        work_package_id: Annotated[
            int | None,
            Field(
                description=(
                    "Only entries booked on this work package. Ids come from "
                    "search_work_packages / list_work_packages. The filter name differs between "
                    "OpenProject versions; this tool probes the instance and uses the right one."
                )
            ),
        ] = None,
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Only entries in this project (numeric id or identifier, from "
                    "list_projects). Includes project-level entries that have no work package."
                )
            ),
        ] = None,
        user: Annotated[
            int | str | None,
            Field(
                description=(
                    "Whose time to list: a numeric user id, or the literal 'me' for the token "
                    "owner. Omit for everyone you are allowed to see — on most instances that "
                    "is only your own entries unless you hold the view-all-time-entries "
                    "permission."
                )
            ),
        ] = None,
        from_date: Annotated[
            str | None,
            Field(
                description=(
                    "Earliest spent-on date, ISO YYYY-MM-DD, inclusive. Combine with to_date "
                    "for a range; either bound may be omitted for an open-ended one."
                )
            ),
        ] = None,
        to_date: Annotated[
            str | None,
            Field(description="Latest spent-on date, ISO YYYY-MM-DD, inclusive."),
        ] = None,
        activity_id: Annotated[
            int | None,
            Field(
                description=(
                    "Only entries booked on this activity. Activity ids are instance-specific "
                    "and come from get_project_metadata(project_id=...)."
                )
            ),
        ] = None,
        sum_hours: Annotated[
            bool,
            Field(
                description=(
                    "Compute an accurate total over EVERY matching entry (not just this page) "
                    "and break it down per activity. Costs one request per 100 matches and is "
                    f"capped at {SUM_ENTRY_CAP} entries — a cap hit is reported in 'notes'. "
                    "Leave false when you only need rows."
                )
            ),
        ] = False,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_PAGE_SIZE,
                description=(
                    f"Entries per page (max {MAX_PAGE_SIZE}); the instance may clamp it lower "
                    "and the returned pagination reports what actually came back."
                ),
            ),
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[TimeEntryRow]:
        """List logged time, filtered server-side, with an optional accurate total.

        Use it to answer "how much time went into this ticket?", "what did I
        book last week?" or "how much did the team spend on project X in June?".
        Filters combine with AND, so `project_id` + `user='me'` + a date range is
        one call.

        Returns the standard list envelope: ``items`` of ``{id, hours, spent_on,
        comment, user, activity, work_package, project}`` plus ``pagination``.
        ``hours`` is a float (1.5 = 1h30), never an ISO duration. With
        ``sum_hours=true`` the envelope also carries ``sums.total_hours`` over
        all matches and one ``groups`` bucket per activity with its own
        ``count`` and ``sums.total_hours`` — those cover the whole filtered set,
        so never add pages up yourself.

        Pitfalls. Visibility is permission-bound: without the
        view-all-time-entries permission you see only your own entries, and a
        small total may mean "not allowed to see" rather than "nobody booked
        time". ``work_package_id`` scopes to that one work package — child
        work packages are **not** included, so a parent's roll-up needs a query
        per child. The summing path stops at 2000 entries and says so in
        ``notes``; narrow the date range or the project when that happens
        rather than trusting the number.

        Cross-references: book time with ``log_time``; correct an entry with
        ``update_time_entry`` and remove one with ``delete_time_entry``; the
        activity ids and names valid in a project come from
        ``get_project_metadata``; the work package itself (including its
        aggregated ``spent_hours``) comes from ``get_work_package``.
        """
        ctx = get_tool_context()
        notes: list[str] = []
        filters: list[Filter] = []

        if work_package_id is not None:
            probe = await ctx.probe()
            filter_name = probe.time_entry_work_package_filter or "entityId"
            if filter_name == "workPackage":
                filters.append(
                    make_filter(
                        "workPackage", Op.EQ, [work_package_id], resource=TIME_ENTRIES_RESOURCE
                    )
                )
                notes.append(LEGACY_FILTER_NOTE)
            else:
                # Current OpenProject scopes by polymorphic entity: both halves
                # are required or the filter matches other entity types too.
                filters.append(
                    make_filter(
                        "entityId", Op.EQ, [work_package_id], resource=TIME_ENTRIES_RESOURCE
                    )
                )
                filters.append(
                    make_filter(
                        "entityType", Op.EQ, ["WorkPackage"], resource=TIME_ENTRIES_RESOURCE
                    )
                )

        if project_id is not None:
            filters.append(
                make_filter("project", Op.EQ, [project_id], resource=TIME_ENTRIES_RESOURCE)
            )
        if user is not None:
            filters.append(principal_filter("user", [user]))
        if from_date is not None or to_date is not None:
            filters.append(
                date_range_filter(
                    "spentOn",
                    after=_require_iso_date("from_date", from_date) if from_date else None,
                    before=_require_iso_date("to_date", to_date) if to_date else None,
                )
            )
        if activity_id is not None:
            filters.append(
                make_filter("activity", Op.EQ, [activity_id], resource=TIME_ENTRIES_RESOURCE)
            )

        if not sum_hours:
            payload = await ctx.client.get_json(
                "time_entries", params=query_params(filters=filters, page=page, page_size=page_size)
            )
            unwrapped = hal.collection(payload)
            rows = [_entry_row(element) for element in unwrapped]
            return envelope_from_collection(
                unwrapped, rows, page=page, page_size=page_size, notes=notes
            )

        elements: list[dict[str, Any]] = []
        total = 0
        current_page = 1
        while True:
            payload = await ctx.client.get_json(
                "time_entries",
                params=query_params(filters=filters, page=current_page, page_size=SUM_PAGE_SIZE),
            )
            chunk = hal.collection(payload)
            total = chunk.total
            elements.extend(chunk.elements)
            if not chunk.elements or len(elements) >= total or len(elements) >= SUM_ENTRY_CAP:
                break
            await report_progress(
                len(elements),
                float(min(total, SUM_ENTRY_CAP)),
                f"summing time entries ({len(elements)} of {total})",
            )
            # Yield to the event loop between pages so a cancelled tool call
            # stops here instead of paging on to the cap.
            await asyncio.sleep(0)
            current_page += 1

        capped = len(elements) >= SUM_ENTRY_CAP and total > SUM_ENTRY_CAP
        elements = elements[:SUM_ENTRY_CAP]
        rows = [_entry_row(element) for element in elements]

        total_hours = 0.0
        buckets: dict[tuple[int | str | None, str | None], list[float]] = {}
        for row in rows:
            hours = row.hours or 0.0
            total_hours += hours
            key = (row.activity.id, row.activity.name) if row.activity else (None, None)
            buckets.setdefault(key, []).append(hours)
        groups = [
            Group(
                value=name or (str(bucket_id) if bucket_id is not None else None),
                count=len(values),
                sums={"total_hours": round(sum(values), 2)},
            )
            for (bucket_id, name), values in buckets.items()
        ]

        notes.append(SUM_SCOPE_NOTE)
        if capped:
            notes.append(
                f"the sum covers the first {SUM_ENTRY_CAP} of {total} matching entries (cap); "
                "narrow the date range, project or work package for a complete total"
            )

        start = (page - 1) * page_size
        return build_envelope(
            rows[start : start + page_size],
            total=total,
            page=page,
            page_size=page_size,
            groups=groups,
            sums={"total_hours": round(total_hours, 2)},
            notes=notes,
        )

    @mcp.tool(
        name="log_time",
        tags=tool_tags(GROUP_TIME_ENTRIES, WRITE),
        annotations=write_annotations(title="Log time"),
    )
    @tool_errors
    async def log_time(
        hours: Annotated[
            float,
            Field(
                description=(
                    "Duration in hours as a float: 1.5 is one and a half hours, 0.25 is fifteen "
                    "minutes. Must be positive; OpenProject stores it to the minute."
                )
            ),
        ],
        spent_on: Annotated[
            str,
            Field(
                description=(
                    "The date the work was done, ISO YYYY-MM-DD. Required and never inferred — "
                    "'today' on the server may not be today for the user."
                )
            ),
        ],
        work_package_id: Annotated[
            int | None,
            Field(
                description=(
                    "Work package to book the time on. Ids come from search_work_packages or "
                    "list_work_packages. Either this or project_id is required; passing both "
                    "books on the work package inside that project."
                )
            ),
        ] = None,
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Project to book the time on when the work belongs to no single ticket "
                    "(numeric id or identifier). Required when work_package_id is omitted."
                )
            ),
        ] = None,
        activity: Annotated[
            str | int | None,
            Field(
                description=(
                    "Activity name ('Development') or numeric id. Omit to take the instance "
                    "default from the form. Names are resolved against this project's allowed "
                    "activities; an unknown name fails with the valid ones listed."
                )
            ),
        ] = None,
        comment: Annotated[
            str | None,
            Field(
                description=(
                    "What the time was spent on. Short and factual: it shows up in cost "
                    "reports next to the hours."
                )
            ),
        ] = None,
    ) -> TimeEntry:
        """Book time against a work package or a project.

        Use it when the user says "log 2 hours on #1234" or "book half a day to
        project X". The call is validated through OpenProject's own form
        endpoint first, so an activity this project does not allow, a missing
        permission or a closed cost-reporting period comes back as a typed error
        listing what would be accepted — nothing half-written is left behind.

        Returns the created entry: ``{id, hours, spent_on, comment, user,
        activity, work_package, project, created_at, updated_at,
        lock_version}``. ``hours`` comes back as a float.

        Pitfalls. This is **not** idempotent — calling it twice books the time
        twice, so never blind-retry after a timeout; list the day's entries
        first. The time is always booked for the token owner; you cannot log
        time on someone else's behalf through this tool. Logging time does not
        change the work package's status, estimate or progress — those are
        separate fields, and on instances that derive progress from work the
        percentage is read-only anyway. The work package's ``spent_hours``
        reflects the new entry on the next read.

        Cross-references: the activities and ids this project accepts come from
        ``get_project_metadata(project_id=...)``; review what is already booked
        with ``list_time_entries``; fix a mistake with ``update_time_entry`` or
        ``delete_time_entry``.
        """
        ctx = get_tool_context()
        if work_package_id is None and project_id is None:
            raise InputValidationError(
                "log_time needs work_package_id or project_id.",
                hint=(
                    "Pass work_package_id to book time on a ticket, or project_id to book it on "
                    "a project without a ticket. Ids come from list_work_packages / "
                    "list_projects."
                ),
            )

        duration = _require_hours(hours)
        date = _require_iso_date("spent_on", spent_on)

        attributes: dict[str, Any] = {"hours": duration, "spentOn": date}
        if comment is not None:
            # Only ``raw`` is sent: the time-entry comment is a plain formattable
            # upstream, so letting OpenProject keep its own format avoids a
            # pointless format mismatch.
            attributes["comment"] = {"raw": comment}

        activity_id = _numeric_activity(activity) if activity is not None else None
        links = _entry_links(
            work_package_id=work_package_id, project_id=project_id, activity_id=activity_id
        )
        payload: dict[str, Any] = {**attributes, "_links": links}

        form = await ctx.client.post_json("time_entries/form", json=payload)

        if activity is not None and activity_id is None:
            # The allowed activities live in the form's schema, so the name can
            # only be resolved after the first form round trip.
            resolved = _resolve_activity_name(str(activity), form)
            links = _entry_links(
                work_package_id=work_package_id, project_id=project_id, activity_id=resolved
            )
            payload = {**attributes, "_links": links}
            form = await ctx.client.post_json("time_entries/form", json=payload)

        _raise_form_errors(form)

        defaults = hal.embedded(form, "payload")
        body = _merge_payload(defaults if isinstance(defaults, Mapping) else {}, payload)
        created = await ctx.client.post_json("time_entries", json=body)
        return _entry_detail(created)

    @mcp.tool(
        name="update_time_entry",
        tags=tool_tags(GROUP_TIME_ENTRIES, WRITE),
        annotations=write_annotations(title="Update time entry"),
    )
    @tool_errors
    async def update_time_entry(
        time_entry_id: Annotated[
            int,
            Field(
                description=(
                    "Id of the entry to correct. It comes from list_time_entries or from the "
                    "log_time result — it is not a work package id."
                )
            ),
        ],
        hours: Annotated[
            float | None,
            Field(description="New duration in hours as a float (1.5 = 1h30). Omit to leave it."),
        ] = None,
        spent_on: Annotated[
            str | None,
            Field(description="New spent-on date, ISO YYYY-MM-DD. Omit to leave it."),
        ] = None,
        activity: Annotated[
            str | int | None,
            Field(
                description=(
                    "New activity as a name or numeric id. Names are resolved against the "
                    "entry's own form, so an unknown one fails with the valid ones listed."
                )
            ),
        ] = None,
        comment: Annotated[
            str | None,
            Field(
                description=(
                    "New comment text. Pass an empty string to clear it; omit to leave the "
                    "existing comment untouched."
                )
            ),
        ] = None,
    ) -> TimeEntry:
        """Correct an existing time entry.

        Use it for the everyday fixes: wrong duration, wrong day, wrong activity,
        a comment that needs to say what actually happened. Only the parameters
        you pass are written; everything else is left exactly as it is.

        Returns the updated entry in the same shape ``log_time`` returns.

        Pitfalls. What can be moved between entries is limited: the work package
        and the project a time entry belongs to are **not** editable here —
        delete the entry and log it again where it belongs. Some instances
        report a ``lock_version`` for time entries and some do not; this tool
        reads the entry first and only echoes a lock version when one exists, so
        a concurrent edit surfaces as a ``conflict`` error with the fresh state
        rather than silently overwriting a colleague's correction. Editing time
        inside a closed cost-reporting period is refused by OpenProject with a
        validation error.

        Cross-references: find the id with ``list_time_entries``; remove the
        entry entirely with ``delete_time_entry``; the valid activity names come
        from ``get_project_metadata``.
        """
        ctx = get_tool_context()
        if hours is None and spent_on is None and activity is None and comment is None:
            raise InputValidationError(
                "update_time_entry was called with nothing to change.",
                hint="Pass at least one of hours, spent_on, activity or comment.",
            )

        path = f"time_entries/{time_entry_id}"
        current = await ctx.client.get_json(path)

        attributes: dict[str, Any] = {}
        if hours is not None:
            attributes["hours"] = _require_hours(hours)
        if spent_on is not None:
            attributes["spentOn"] = _require_iso_date("spent_on", spent_on)
        if comment is not None:
            attributes["comment"] = {"raw": comment}

        links: dict[str, Any] = {}
        if activity is not None:
            resolved: int | str | None = _numeric_activity(activity)
            if resolved is None:
                # Names resolve against this entry's own form, which knows the
                # activities its project allows.
                form = await ctx.client.post_json(f"{path}/form", json=dict(attributes))
                resolved = _resolve_activity_name(str(activity), form)
            links = _entry_links(activity_id=resolved)

        payload: dict[str, Any] = dict(attributes)
        if links:
            payload["_links"] = links

        lock_version = extract_lock_version(current)
        if lock_version is None:
            # This instance does not version time entries: echoing a lockVersion
            # it never sent would be rejected, so PATCH plainly.
            updated = await ctx.client.patch_json(path, json=payload)
        else:
            updated = await patch_with_lock(ctx.client, path, payload, lock_version=lock_version)
        return _entry_detail(updated)

    @mcp.tool(
        name="delete_time_entry",
        tags=tool_tags(GROUP_TIME_ENTRIES, WRITE, DESTRUCTIVE),
        annotations=destructive_annotations(title="Delete time entry"),
    )
    @tool_errors
    async def delete_time_entry(
        time_entry_id: Annotated[
            int,
            Field(
                description=(
                    "Id of the time entry to delete permanently. It comes from "
                    "list_time_entries — confirm the id belongs to the entry you mean before "
                    "calling, since ids are not human-readable."
                )
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true. Ask the user to confirm first — the API offers no undo. "
                    "Calling with confirm=false returns a confirmation_required error rather "
                    "than deleting anything."
                )
            ),
        ] = False,
    ) -> TimeEntryDeletion:
        """Permanently delete a logged time entry.

        Use only on explicit user instruction, and only for genuinely wrong
        entries. Deletion removes the booked hours from every cost report and
        from the work package's aggregated spent time, with no API-side undo.

        Returns a small confirmation object once OpenProject accepts the
        deletion.

        Pitfalls. If the entry is merely on the wrong day, has the wrong
        duration or the wrong activity, ``update_time_entry`` is the better
        answer — it keeps the audit trail. Deleting someone else's entry needs
        an administrative permission and otherwise fails with
        ``permission_denied``. Entries inside a closed cost-reporting period
        cannot be deleted.

        Cross-references: find the id with ``list_time_entries``; correct
        instead of deleting with ``update_time_entry``.
        """
        require_confirmation(
            confirm,
            action="delete time entry",
            target=f"#{time_entry_id}",
            consequence=(
                "The logged hours disappear from cost reports and from the work package's "
                "spent time permanently."
            ),
        )
        ctx = get_tool_context()
        await ctx.client.delete(f"time_entries/{time_entry_id}")
        return TimeEntryDeletion(
            id=time_entry_id,
            deleted=True,
            message=f"Time entry #{time_entry_id} was deleted permanently.",
        )
