"""Reporting: structured project-report aggregation (SPEC §6.14 — Phase 3).

Lands here:

=====================================  ======  =====================================
Tool                                   Phase   Endpoint(s)
=====================================  ======  =====================================
🔍 ``get_project_report_data``         3       ``GET /projects/{id}``
                                               + ``…/work_packages`` (window x3,
                                               ``groupBy=status`` x1)
                                               + ``GET /statuses``
                                               + ``GET /time_entries``
                                               + ``GET /memberships``
=====================================  ======  =====================================

Non-negotiables for this module:

* **Closed is a flag, not a word.** Every open/closed decision reads the
  ``isClosed`` flag of the instance's own statuses (``GET /statuses``, cached) or
  the API's ``status`` filter operators ``o``/``c``. The old server classified by
  English status names, which mis-bucketed every localized instance and every
  workflow that spells "Done" differently.
* **Counts come from the server.** Bucket counts are the collection ``total``,
  and the open-by-status breakdown is a server-side ``groupBy=status`` computed
  over the whole filtered set — never a client-side sum of the pages we happened
  to fetch (SPEC §9.3).
* **Caps are reported, never silent** (G1). Each windowed work-package list stops
  at :data:`WORK_PACKAGE_CAP` rows and the time-entry scan at
  :data:`TIME_ENTRY_CAP`; a cap hit leaves an in-band note *and* the bucket still
  reports the true server ``total``, so a capped row list never becomes a wrong
  number. Progress notifications are emitted between pages (SPEC §5.9).
* **Rendering lives in prompts.** This module returns data only; the weekly
  report and standup templates are :mod:`openproject_mcp.prompts`, which call the
  collector coroutines below directly rather than going through the tool wrapper.

The collector coroutines (:func:`collect_report_data`, :func:`collect_impediments`,
:func:`collect_due_on`, :func:`collect_unread_notifications`,
:func:`collect_backlog_sweep`) are plain async functions taking an explicit
:class:`~openproject_mcp.tools._shared.ToolContext`. They are not MCP tools: the
prompts reuse them so a rendered report costs the same requests as the tool.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    PermissionDeniedError,
    UnexpectedResponseError,
    UpstreamServerError,
    ValidationFailedError,
)
from openproject_mcp.client.filters import (
    WORK_PACKAGE_SORT_KEYS,
    Filter,
    Op,
    date_range_filter,
    make_filter,
    query_params,
    status_filter,
)
from openproject_mcp.projections import Ref, RelationRow, TruncatedList
from openproject_mcp.tools import _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from openproject_mcp.tools._shared import ToolContext

__all__ = [
    "ActivityHours",
    "BacklogItem",
    "BacklogSweep",
    "Impediment",
    "InboxItem",
    "InboxReasonGroup",
    "InboxSummary",
    "ProjectReportData",
    "ReportWorkPackage",
    "RosterMember",
    "StatusCount",
    "TimeSummary",
    "UserHours",
    "collect_backlog_sweep",
    "collect_due_on",
    "collect_impediments",
    "collect_report_data",
    "collect_unread_notifications",
    "register",
]

#: Rows pulled per upstream request while paging an aggregation.
PAGE_SIZE = 100

#: Hard cap on rows fetched for one windowed work-package list (SPEC §6.14).
#: The bucket's ``total`` still comes from the server, so a cap costs rows, not
#: counts.
WORK_PACKAGE_CAP = 3000

#: Hard cap on time entries scanned for one window (SPEC §6.14).
TIME_ENTRY_CAP = 5000

#: Hard cap on membership rows pulled for the roster; a project with more members
#: than this is an instance-wide directory rather than a team.
ROSTER_CAP = 500

#: Open work packages scanned by :func:`collect_backlog_sweep`, oldest first.
BACKLOG_CAP = 500

#: Unread notifications scanned by :func:`collect_unread_notifications`.
INBOX_CAP = 200

#: Open work packages whose relations :func:`collect_impediments` probes. Each
#: one costs a request, so the sweep is deliberately shallow and says so.
IMPEDIMENT_PROBE_CAP = 25

#: ``anthropic/maxResultSizeChars`` (SPEC §5.4). A quarter's worth of a busy
#: project legitimately fills thousands of compact rows.
MAX_RESULT_CHARS = 300_000

#: Relation types that make a work package an impediment, in either direction.
BLOCK_RELATION_TYPES: frozenset[str] = frozenset({"blocks", "blocked"})

STATUS_FLAG_NOTE = (
    "open/closed bucketing uses each status's isClosed flag from GET /statuses, not status "
    "names; a status this instance renamed or translated is still bucketed correctly"
)


# --- projections ----------------------------------------------------------


class ReportWorkPackage(BaseModel):
    """One work package inside a report window, compact enough to list hundreds of."""

    id: int | str | None = Field(
        default=None, description="Work package id — the #1234 number get_work_package takes."
    )
    subject: str | None = Field(default=None, description="Subject line.")
    type: Ref | None = Field(default=None, description="Work package type (Task, Bug, …).")
    status: Ref | None = Field(default=None, description="Status as the instance names it.")
    assignee: Ref | None = Field(
        default=None, description="Assigned user or group; null when unassigned."
    )
    is_closed: bool | None = Field(
        default=None,
        description="True when this status carries the instance's isClosed flag. Null means the "
        "status was not in GET /statuses — treat it as unknown, never as open.",
    )
    due_date: str | None = Field(
        default=None, description="ISO date (YYYY-MM-DD); null when unset."
    )
    created_at: str | None = Field(
        default=None,
        description="ISO 8601 UTC timestamp the work package was raised. A created_at whose date "
        "equals updated_at's is a row nothing has happened to since it was raised.",
    )
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class StatusCount(BaseModel):
    """One bucket of the server-side ``groupBy=status`` breakdown."""

    status: Ref | None = Field(default=None, description="The status this bucket counts.")
    count: int = Field(default=0, description="Work packages in this status across the whole set.")
    is_closed: bool | None = Field(
        default=None, description="The status's isClosed flag; null when the status is unknown."
    )


class ActivityHours(BaseModel):
    """Logged hours for one time-entry activity."""

    activity: Ref | None = Field(
        default=None, description="Activity (Development, Management, …); null when unset."
    )
    hours: float = Field(default=0.0, description="Hours booked on this activity in the window.")
    entries: int = Field(default=0, description="Number of time entries behind the total.")


class UserHours(BaseModel):
    """Logged hours for one person."""

    user: Ref | None = Field(default=None, description="User the time is booked for.")
    hours: float = Field(default=0.0, description="Hours this person booked in the window.")
    entries: int = Field(default=0, description="Number of time entries behind the total.")


class TimeSummary(BaseModel):
    """Everything booked against the project inside the window."""

    total_hours: float = Field(default=0.0, description="Sum of every scanned entry, in hours.")
    entry_count: int = Field(
        default=0, description="Time entries the totals cover (see truncated)."
    )
    total_entries: int = Field(
        default=0, description="Time entries the server reported for the window."
    )
    truncated: bool = Field(
        default=False,
        description="True when the scan hit the internal cap, so the totals cover only part of "
        "the window. Narrow the range rather than quoting the number.",
    )
    by_activity: list[ActivityHours] = Field(
        default_factory=list[ActivityHours],
        description="Per-activity totals, highest first; always a list.",
    )
    by_user: list[UserHours] = Field(
        default_factory=list[UserHours],
        description="Per-user totals, highest first; always a list.",
    )


class RosterMember(BaseModel):
    """One member of the project, as the membership records it."""

    principal: Ref | None = Field(
        default=None, description="User, group or placeholder user holding the membership."
    )
    roles: list[str] = Field(
        default_factory=list[str], description="Role names held in this project; always a list."
    )


class ProjectReportData(BaseModel):
    """The whole report window as structured data (SPEC §6.14)."""

    project: Ref | None = Field(default=None, description="The project the report covers.")
    from_date: str = Field(description="Window start, ISO YYYY-MM-DD, inclusive.")
    to_date: str = Field(description="Window end, ISO YYYY-MM-DD, inclusive.")
    created: TruncatedList[ReportWorkPackage] = Field(
        description="Work packages created inside the window (createdAt range filter)."
    )
    updated: TruncatedList[ReportWorkPackage] = Field(
        description="Work packages changed inside the window (updatedAt range filter). Includes "
        "the ones that were closed."
    )
    closed: TruncatedList[ReportWorkPackage] = Field(
        description="Work packages in a closed status that changed inside the window — the "
        "'done this week' set."
    )
    open_total: int = Field(
        default=0, description="Open work packages in the project right now, server-reported."
    )
    open_by_status: list[StatusCount] = Field(
        default_factory=list[StatusCount],
        description="Server-side groupBy=status counts over the whole open set, independent of "
        "paging. Never re-add these from rows.",
    )
    time: TimeSummary = Field(description="Time logged against the project inside the window.")
    roster: list[RosterMember] = Field(
        default_factory=list[RosterMember],
        description="Project membership roster: who may act in the project and with which roles.",
    )
    notes: list[str] = Field(
        default_factory=list[str],
        description="In-band markers (G1/G5): which lists were capped, which sources degraded. "
        "Read them before quoting a number as complete.",
    )


class Impediment(BaseModel):
    """One blocking relation found on an open work package."""

    work_package: Ref | None = Field(default=None, description="The open work package.")
    direction: Literal["blocked_by", "blocks"] = Field(
        description="'blocked_by' when the work package cannot move until the related one does; "
        "'blocks' when it is holding the related one up."
    )
    related: Ref | None = Field(
        default=None, description="The work package on the other end of the relation."
    )
    relation_id: int | str | None = Field(
        default=None, description="Relation id — the handle delete_work_package_relation takes."
    )
    status: Ref | None = Field(default=None, description="Status of the open work package.")
    assignee: Ref | None = Field(
        default=None, description="Who owns the open work package; null when unassigned."
    )
    description: str | None = Field(
        default=None, description="Free-text note stored on the relation; null when unset."
    )


class InboxItem(BaseModel):
    """One unread notification."""

    id: int | str | None = Field(default=None, description="Notification id.")
    reason: str | None = Field(
        default=None, description="Why it was sent: 'mentioned', 'assigned', 'watched', …"
    )
    subject: str | None = Field(
        default=None, description="Title of the resource the notification is about."
    )
    resource_id: int | str | None = Field(
        default=None, description="Id of that resource (a work package id for reason-driven rows)."
    )
    resource_type: str | None = Field(
        default=None, description="'WorkPackage', 'WikiPage', 'Meeting', …"
    )
    project: Ref | None = Field(default=None, description="Project the resource belongs to.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class InboxReasonGroup(BaseModel):
    """Unread notifications sharing one reason."""

    reason: str = Field(description="The notification reason, or 'unknown' when unreported.")
    count: int = Field(default=0, description="Scanned notifications with this reason.")
    items: list[InboxItem] = Field(
        default_factory=list[InboxItem], description="The notifications themselves."
    )


class InboxSummary(BaseModel):
    """The unread inbox, grouped by reason."""

    total_unread: int = Field(
        default=0, description="Unread notifications the server reported, before any cap."
    )
    scanned: int = Field(default=0, description="Notifications actually pulled and grouped.")
    truncated: bool = Field(default=False, description="True when the scan hit the internal cap.")
    groups: list[InboxReasonGroup] = Field(
        default_factory=list[InboxReasonGroup], description="Largest reason group first."
    )


class BacklogItem(BaseModel):
    """One open work package in the backlog sweep."""

    id: int | str | None = Field(default=None, description="Work package id.")
    subject: str | None = Field(default=None, description="Subject line.")
    type: Ref | None = Field(default=None, description="Work package type.")
    status: Ref | None = Field(default=None, description="Status.")
    assignee: Ref | None = Field(default=None, description="Assignee; null when unassigned.")
    estimated_hours: float | None = Field(
        default=None, description="Estimate in hours; null when nobody estimated it."
    )
    due_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD).")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class BacklogSweep(BaseModel):
    """Open work packages that need grooming."""

    open_total: int = Field(default=0, description="Open work packages the server reported.")
    scanned: int = Field(
        default=0, description="Open work packages actually examined, oldest first."
    )
    truncated: bool = Field(default=False, description="True when the scan hit the internal cap.")
    stale_before: str = Field(description="Cut-off date used for 'stale', ISO YYYY-MM-DD.")
    unassigned: list[BacklogItem] = Field(
        default_factory=list[BacklogItem], description="Open items with no assignee."
    )
    unestimated: list[BacklogItem] = Field(
        default_factory=list[BacklogItem], description="Open items with no estimate."
    )
    stale: list[BacklogItem] = Field(
        default_factory=list[BacklogItem],
        description="Open items untouched since stale_before, oldest first.",
    )


# --- status flags (never keywords) ----------------------------------------


@dataclass(frozen=True, slots=True)
class _StatusIndex:
    """The instance's own open/closed verdict, by status id and by name."""

    by_id: dict[int | str, bool] = field(default_factory=dict[int | str, bool])
    by_name: dict[str, bool] = field(default_factory=dict[str, bool])

    def is_closed(self, status: Ref | None) -> bool | None:
        """The ``isClosed`` flag for a status ref, or ``None`` when unknown."""
        if status is None:
            return None
        if status.id is not None and status.id in self.by_id:
            return self.by_id[status.id]
        if status.name:
            return self.by_name.get(status.name.strip().casefold())
        return None


async def _status_index(ctx: ToolContext) -> _StatusIndex:
    """Fetch (and cache) ``GET /statuses`` as the open/closed lookup.

    The cache key matches the one the work-package tools use for the same
    document, so a report and a status resolution share one round trip.
    """

    async def fetch() -> dict[str, Any]:
        return await ctx.client.get_json("statuses")

    payload = await ctx.cache.get_or_set(("json", "statuses"), fetch, scope=ctx.scope)
    by_id: dict[int | str, bool] = {}
    by_name: dict[str, bool] = {}
    for element in hal.collection(payload):
        closed = element.get("isClosed") is True
        status_id = hal.self_id(element)
        if status_id is not None:
            by_id[status_id] = closed
        name = element.get("name")
        if isinstance(name, str):
            by_name[name.strip().casefold()] = closed
    return _StatusIndex(by_id=by_id, by_name=by_name)


# --- paging ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Paged:
    """The outcome of one capped paging run."""

    elements: list[dict[str, Any]]
    total: int
    capped: bool


async def _collect_pages(
    ctx: ToolContext,
    path: str,
    filters: Sequence[Filter],
    *,
    cap: int,
    label: str,
    sort_by: Sequence[Sequence[str]] | None = None,
) -> _Paged:
    """Page through a collection up to ``cap`` rows, reporting progress.

    ``total`` is always the server's own count, so a capped run still knows how
    many rows it did not fetch (G1). The loop yields to the event loop between
    pages so a cancelled call stops there instead of paging on to the cap.
    """
    elements: list[dict[str, Any]] = []
    total = 0
    page = 1
    while True:
        payload = await ctx.client.get_json(
            path,
            params=query_params(
                filters=filters or None,
                page=page,
                page_size=PAGE_SIZE,
                sort_by=sort_by,
                sort_keys=WORK_PACKAGE_SORT_KEYS if sort_by else None,
            ),
        )
        chunk = hal.collection(payload)
        total = chunk.total
        elements.extend(chunk.elements)
        if not chunk.elements or len(elements) >= total or len(elements) >= cap:
            break
        await _shared.report_progress(
            len(elements), float(min(total, cap)), f"{label}: {len(elements)} of {total}"
        )
        await asyncio.sleep(0)
        page += 1
    trimmed = elements[:cap]
    return _Paged(elements=trimmed, total=max(total, len(trimmed)), capped=total > len(trimmed))


def _cap_note(label: str, paged: _Paged, cap: int) -> str | None:
    """The in-band marker for a capped list (G1), or ``None`` when complete."""
    if not paged.capped:
        return None
    return (
        f"{label}: only the first {len(paged.elements)} of {paged.total} rows were read "
        f"(internal cap {cap}); the counts are still the server's own totals, but narrow the "
        "date range for a complete row list"
    )


# --- row projections ------------------------------------------------------


def _report_row(element: Mapping[str, Any], index: _StatusIndex) -> ReportWorkPackage:
    status = Ref.from_hal(element, "status")
    return ReportWorkPackage(
        id=hal.self_id(element),
        subject=element.get("subject"),
        type=Ref.from_hal(element, "type"),
        status=status,
        assignee=Ref.from_hal(element, "assignee"),
        is_closed=index.is_closed(status),
        due_date=element.get("dueDate"),
        created_at=element.get("createdAt"),
        updated_at=element.get("updatedAt"),
    )


def _bucket(
    paged: _Paged, index: _StatusIndex, *, more_via: str
) -> TruncatedList[ReportWorkPackage]:
    rows = [_report_row(element, index) for element in paged.elements]
    return TruncatedList[ReportWorkPackage](
        items=rows,
        truncated=paged.capped,
        total=paged.total,
        more_via=more_via if paged.capped else None,
    )


def _group_ref(raw: Any) -> Ref | None:
    """The status behind a ``groups`` entry, whether it is a link or a name."""
    entry = hal.as_object(raw)
    if entry is not None:
        href = entry.get("href")
        title = entry.get("title") or entry.get("name")
        return Ref(
            id=hal.id_from_href(href if isinstance(href, str) else None),
            name=title if isinstance(title, str) else None,
        )
    if isinstance(raw, str):
        return Ref(id=None, name=raw)
    return None


def _count(raw: Any) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _status_counts(groups: Sequence[Mapping[str, Any]], index: _StatusIndex) -> list[StatusCount]:
    counts: list[StatusCount] = []
    for entry in groups:
        status = _group_ref(entry.get("value"))
        counts.append(
            StatusCount(
                status=status, count=_count(entry.get("count")), is_closed=index.is_closed(status)
            )
        )
    return counts


def _backlog_item(element: Mapping[str, Any]) -> BacklogItem:
    return BacklogItem(
        id=hal.self_id(element),
        subject=element.get("subject"),
        type=Ref.from_hal(element, "type"),
        status=Ref.from_hal(element, "status"),
        assignee=Ref.from_hal(element, "assignee"),
        estimated_hours=hal.duration_hours(element.get("estimatedTime")),
        due_date=element.get("dueDate"),
        updated_at=element.get("updatedAt"),
    )


# --- input validation -----------------------------------------------------


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


def _require_window(from_date: str, to_date: str) -> tuple[str, str]:
    start = _require_iso_date("from_date", from_date)
    end = _require_iso_date("to_date", to_date)
    if start > end:
        raise InputValidationError(
            f"from_date {start} is after to_date {end}.",
            hint="Pass the earlier date as from_date; both bounds are inclusive.",
        )
    return start, end


# --- collectors (reused by the prompts) -----------------------------------


async def collect_report_data(
    ctx: ToolContext,
    *,
    project_id: int | str,
    from_date: str,
    to_date: str,
) -> ProjectReportData:
    """Aggregate one project's report window (SPEC §6.14).

    This is the plain coroutine behind ``get_project_report_data``; the report
    prompts call it directly so a rendered weekly report costs exactly the same
    upstream requests as the tool.

    Args:
        ctx: the caller's tool context (client, cache, settings).
        project_id: numeric project id or URL identifier.
        from_date: window start, ISO ``YYYY-MM-DD``, inclusive.
        to_date: window end, ISO ``YYYY-MM-DD``, inclusive.

    Returns:
        The populated :class:`ProjectReportData`, with every cap and degradation
        recorded in ``notes``.

    Raises:
        InputValidationError: the dates are not ISO or the window is inverted.
        OpenProjectError: the project could not be read; per-section failures
            degrade into notes instead (G5).
    """
    start, end = _require_window(from_date, to_date)

    project = await ctx.client.get_json(f"projects/{project_id}")
    numeric_id = hal.self_id(project)
    project_ref = Ref(id=numeric_id, name=project.get("name"))
    index = await _status_index(ctx)

    notes: list[str] = [STATUS_FLAG_NOTE]
    wp_path = f"projects/{project_id}/work_packages"

    created = await _collect_pages(
        ctx,
        wp_path,
        [status_filter("all"), date_range_filter("createdAt", after=start, before=end)],
        cap=WORK_PACKAGE_CAP,
        label="work packages created in the window",
        sort_by=[["created_at", "asc"]],
    )
    updated = await _collect_pages(
        ctx,
        wp_path,
        [status_filter("all"), date_range_filter("updatedAt", after=start, before=end)],
        cap=WORK_PACKAGE_CAP,
        label="work packages changed in the window",
        sort_by=[["updated_at", "desc"]],
    )
    closed = await _collect_pages(
        ctx,
        wp_path,
        [status_filter("closed"), date_range_filter("updatedAt", after=start, before=end)],
        cap=WORK_PACKAGE_CAP,
        label="work packages closed in the window",
        sort_by=[["updated_at", "desc"]],
    )

    # One row is enough: groupBy counts are computed server-side over the whole
    # filtered set, so pulling rows here would only cost tokens (SPEC §9.3).
    grouped = await ctx.client.get_json(
        wp_path,
        params=query_params(
            filters=[status_filter("open")], page=1, page_size=1, group_by="status"
        ),
    )
    open_collection = hal.collection(grouped)
    open_by_status = _status_counts(open_collection.groups, index)
    if not open_by_status and open_collection.total:
        notes.append(
            "this instance returned no groupBy=status buckets for the open set, so only the open "
            "total is available; list_work_packages(group_by='status') shows the same breakdown"
        )

    time_summary, time_note = await _collect_time(ctx, numeric_id, start, end)
    roster, roster_note = await _collect_roster(ctx, numeric_id)

    for note in (
        _cap_note("created-in-window work packages", created, WORK_PACKAGE_CAP),
        _cap_note("changed-in-window work packages", updated, WORK_PACKAGE_CAP),
        _cap_note("closed-in-window work packages", closed, WORK_PACKAGE_CAP),
        time_note,
        roster_note,
    ):
        if note:
            notes.append(note)

    window = f"created_since='{start}'"
    return ProjectReportData(
        project=project_ref,
        from_date=start,
        to_date=end,
        created=_bucket(
            created,
            index,
            more_via=f"list_work_packages(project={project_id!r}, status_scope='all', {window})",
        ),
        updated=_bucket(
            updated,
            index,
            more_via=(
                f"list_work_packages(project={project_id!r}, status_scope='all', "
                f"updated_since='{start}')"
            ),
        ),
        closed=_bucket(
            closed,
            index,
            more_via=(
                f"list_work_packages(project={project_id!r}, status_scope='closed', "
                f"updated_since='{start}')"
            ),
        ),
        open_total=open_collection.total,
        open_by_status=open_by_status,
        time=time_summary,
        roster=roster,
        notes=notes,
    )


async def _collect_time(
    ctx: ToolContext, project_id: int | str | None, start: str, end: str
) -> tuple[TimeSummary, str | None]:
    """Total, per-activity and per-user hours booked inside the window.

    Time and costs is a per-project module and ``view_time_entries`` is its own
    permission, so an unreadable ledger degrades to an empty summary plus a note
    (G5) instead of aborting a report whose other seven sections were readable.
    """
    filters: list[Filter] = [date_range_filter("spentOn", after=start, before=end)]
    if project_id is not None:
        filters.insert(0, make_filter("project", Op.EQ, [project_id]))

    try:
        paged = await _collect_pages(
            ctx, "time_entries", filters, cap=TIME_ENTRY_CAP, label="time entries in the window"
        )
    except PermissionDeniedError as exc:
        return TimeSummary(), f"time entries unavailable (no permission): {exc.message}"
    except NotFoundError as exc:
        return TimeSummary(), f"time entries unavailable (module absent): {exc.message}"

    total_hours = 0.0
    by_activity: dict[tuple[int | str | None, str | None], list[float]] = {}
    by_user: dict[tuple[int | str | None, str | None], list[float]] = {}
    for element in paged.elements:
        hours = hal.duration_hours(element.get("hours")) or 0.0
        total_hours += hours
        activity = Ref.from_hal(element, "activity")
        user = Ref.from_hal(element, "user")
        by_activity.setdefault(
            (activity.id, activity.name) if activity else (None, None), []
        ).append(hours)
        by_user.setdefault((user.id, user.name) if user else (None, None), []).append(hours)

    summary = TimeSummary(
        total_hours=round(total_hours, 2),
        entry_count=len(paged.elements),
        total_entries=paged.total,
        truncated=paged.capped,
        by_activity=sorted(
            (
                ActivityHours(
                    activity=Ref(id=key[0], name=key[1]) if key != (None, None) else None,
                    hours=round(sum(values), 2),
                    entries=len(values),
                )
                for key, values in by_activity.items()
            ),
            key=lambda item: item.hours,
            reverse=True,
        ),
        by_user=sorted(
            (
                UserHours(
                    user=Ref(id=key[0], name=key[1]) if key != (None, None) else None,
                    hours=round(sum(values), 2),
                    entries=len(values),
                )
                for key, values in by_user.items()
            ),
            key=lambda item: item.hours,
            reverse=True,
        ),
    )
    return summary, _cap_note("time entries", paged, TIME_ENTRY_CAP)


async def _collect_roster(
    ctx: ToolContext, project_id: int | str | None
) -> tuple[list[RosterMember], str | None]:
    """The project's membership roster; a permission failure degrades to a note (G5)."""
    if project_id is None:
        return [], "membership roster skipped: the project reported no numeric id"
    filters = [make_filter("project", Op.EQ, [project_id])]
    try:
        paged = await _collect_pages(
            ctx, "memberships", filters, cap=ROSTER_CAP, label="project memberships"
        )
    except (PermissionDeniedError, NotFoundError) as exc:
        return [], f"membership roster unavailable: {exc.message}"

    roster = [
        RosterMember(
            principal=Ref.from_hal(element, "principal"),
            roles=[item.name for item in hal.refs(element, "roles") if item.name],
        )
        for element in paged.elements
    ]
    return roster, _cap_note("membership roster", paged, ROSTER_CAP)


def _impediment_from(
    row: ReportWorkPackage, relation: RelationRow
) -> Impediment | None:
    """Read one relation from the perspective of ``row``, or ignore it.

    ``type`` is stored from the ``from`` end, so which end this work package sits
    on decides whether it is blocked or blocking.
    """
    relation_type = (relation.type or "").strip().casefold()
    if relation_type not in BLOCK_RELATION_TYPES:
        return None
    from_id = relation.from_work_package.id if relation.from_work_package else None
    to_id = relation.to_work_package.id if relation.to_work_package else None
    if from_id == row.id:
        direction: Literal["blocked_by", "blocks"] = (
            "blocked_by" if relation_type == "blocked" else "blocks"
        )
        related = relation.to_work_package
    elif to_id == row.id:
        direction = "blocks" if relation_type == "blocked" else "blocked_by"
        related = relation.from_work_package
    else:
        return None
    return Impediment(
        work_package=Ref(id=row.id, name=row.subject),
        direction=direction,
        related=related,
        relation_id=relation.id,
        status=row.status,
        assignee=row.assignee,
        description=relation.description,
    )


async def collect_impediments(
    ctx: ToolContext, rows: Sequence[ReportWorkPackage]
) -> tuple[list[Impediment], list[str]]:
    """Find blocking relations on the open work packages of a report window.

    OpenProject has no "everything blocked in this project" query, so this probes
    the relations of the first :data:`IMPEDIMENT_PROBE_CAP` open rows
    concurrently. A source that 403s or 404s becomes a note rather than an error
    (G5): "no impediments found" and "not allowed to look" are different answers.

    Args:
        ctx: the caller's tool context.
        rows: report rows; closed ones are skipped.

    Returns:
        ``(impediments, notes)`` — the notes say what was not looked at.
    """
    open_rows = [row for row in rows if row.is_closed is not True and row.id is not None]
    probed = open_rows[:IMPEDIMENT_PROBE_CAP]
    notes: list[str] = []
    if not probed:
        return [], notes
    if len(open_rows) > len(probed):
        notes.append(
            f"impediment scan covered {len(probed)} of {len(open_rows)} open work packages "
            f"(cap {IMPEDIMENT_PROBE_CAP}); there may be blockers on the rest"
        )

    async def fetch(row: ReportWorkPackage) -> tuple[ReportWorkPackage, list[RelationRow] | str]:
        # Only source-specific failures degrade (G5). Authentication, rate-limit
        # and network errors are instance-wide, and reporting "nothing is
        # blocked" over a broken connection would be a lie.
        try:
            payload = await ctx.client.get_json(f"work_packages/{row.id}/relations")
        except (
            PermissionDeniedError,
            NotFoundError,
            ValidationFailedError,
            UpstreamServerError,
            UnexpectedResponseError,
        ) as exc:
            return row, exc.message
        return row, [RelationRow.from_hal(element) for element in hal.collection(payload)]

    impediments: list[Impediment] = []
    failures = 0
    first_failure: str | None = None
    for row, outcome in await asyncio.gather(*(fetch(row) for row in probed)):
        if isinstance(outcome, str):
            failures += 1
            first_failure = first_failure or outcome
            continue
        for relation in outcome:
            found = _impediment_from(row, relation)
            if found is not None:
                impediments.append(found)
    if failures:
        notes.append(
            f"relations could not be read for {failures} of {len(probed)} work packages "
            f"({first_failure}); blockers on those are not listed"
        )
    return impediments, notes


async def collect_due_on(
    ctx: ToolContext, *, project_id: int | str, date: str
) -> TruncatedList[ReportWorkPackage]:
    """Open work packages due on one specific date (the standup's "due today")."""
    day = _require_iso_date("date", date)
    index = await _status_index(ctx)
    paged = await _collect_pages(
        ctx,
        f"projects/{project_id}/work_packages",
        [status_filter("open"), make_filter("dueDate", Op.ON_DATE, [day])],
        cap=PAGE_SIZE,
        label=f"work packages due on {day}",
        sort_by=[["id", "asc"]],
    )
    return _bucket(
        paged,
        index,
        more_via=(
            f"list_work_packages(project={project_id!r}, due_before='{day}', due_after='{day}')"
        ),
    )


async def collect_unread_notifications(ctx: ToolContext) -> InboxSummary:
    """The authenticated user's unread notifications, grouped by reason.

    The grouping is client-side over the scanned rows — the notifications
    endpoint exposes no ``groupBy`` — so ``total_unread`` (the server's count)
    and ``scanned`` are reported separately rather than conflated.
    """
    paged = await _collect_pages(
        ctx,
        "notifications",
        [make_filter("readIAN", Op.EQ, [False])],
        cap=INBOX_CAP,
        label="unread notifications",
    )

    buckets: dict[str, list[InboxItem]] = {}
    for element in paged.elements:
        resource = hal.ref(element, "resource")
        project = Ref.from_hal(element, "project")
        raw_reason = element.get("reason")
        reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "unknown"
        resource_type: str | None = None
        if resource is not None and resource.href:
            segments = [part for part in resource.href.split("?", 1)[0].split("/") if part]
            resource_type = segments[-2] if len(segments) >= 2 else None
        buckets.setdefault(reason, []).append(
            InboxItem(
                id=hal.self_id(element),
                reason=reason,
                subject=resource.name if resource is not None else None,
                resource_id=resource.id if resource is not None else None,
                resource_type=resource_type,
                project=project,
                updated_at=element.get("updatedAt"),
            )
        )

    groups = sorted(
        (
            InboxReasonGroup(reason=reason, count=len(items), items=items)
            for reason, items in buckets.items()
        ),
        key=lambda group: group.count,
        reverse=True,
    )
    return InboxSummary(
        total_unread=paged.total,
        scanned=len(paged.elements),
        truncated=paged.capped,
        groups=groups,
    )


async def collect_backlog_sweep(
    ctx: ToolContext, *, project_id: int | str, stale_days: int = 14
) -> BacklogSweep:
    """Open work packages that need grooming: unassigned, unestimated or stale.

    The open set is scanned oldest-changed first and classified locally, because
    "has no estimate" is not expressible as an OpenProject filter. ``open_total``
    stays the server's count so a capped scan never understates the backlog.
    """
    cutoff = (dt.date.today() - dt.timedelta(days=max(stale_days, 0))).isoformat()
    paged = await _collect_pages(
        ctx,
        f"projects/{project_id}/work_packages",
        [status_filter("open")],
        cap=BACKLOG_CAP,
        label="open work packages",
        sort_by=[["updated_at", "asc"]],
    )
    items = [_backlog_item(element) for element in paged.elements]
    return BacklogSweep(
        open_total=paged.total,
        scanned=len(items),
        truncated=paged.capped,
        stale_before=cutoff,
        unassigned=[item for item in items if item.assignee is None],
        unestimated=[item for item in items if item.estimated_hours is None],
        stale=[
            item for item in items if item.updated_at is not None and item.updated_at[:10] < cutoff
        ],
    )


# --- registration ---------------------------------------------------------


def register(mcp: FastMCP) -> None:
    """Register the reporting tool (SPEC §6.14)."""

    @mcp.tool(
        name="get_project_report_data",
        tags=_shared.tool_tags(_shared.GROUP_REPORTING, _shared.READ),
        annotations=_shared.read_annotations(
            title="Get project report data", max_result_chars=MAX_RESULT_CHARS
        ),
    )
    @_shared.tool_errors
    async def get_project_report_data(
        project_id: Annotated[
            int | str,
            Field(
                description="Numeric project id or project identifier (the URL slug). Both come "
                "from list_projects; the identifier is what appears in /projects/<identifier>."
            ),
        ],
        from_date: Annotated[
            str,
            Field(
                description="First day of the report window, ISO YYYY-MM-DD, inclusive. Required "
                "and never inferred — 'this week' means different days to different people."
            ),
        ],
        to_date: Annotated[
            str,
            Field(description="Last day of the report window, ISO YYYY-MM-DD, inclusive."),
        ],
    ) -> ProjectReportData:
        """Aggregate everything a status report needs about one project and one date window.

        Use it for weekly reports, sprint reviews, standups and "what happened in June" —
        one call replaces a dozen filtered listings. It returns, for the window: `created`,
        `updated` and `closed` work-package buckets (each `{items, total, truncated,
        more_via}` with compact rows), `open_total` plus `open_by_status` counts computed
        server-side over the whole open set, a `time` summary (total hours with per-activity
        and per-user breakdowns) and the project's membership `roster`.

        Done/in-progress classification is safe here: every row carries `is_closed`, read
        from the status's own `isClosed` flag on this instance, so it works on translated
        and renamed workflows where matching status names would not. `closed` is exactly
        "in a closed status and touched inside the window" — the done-this-week set.

        Pitfalls. Counts and row lists are different things: `total` is always the server's
        number, while `items` stops at an internal cap and then sets `truncated` and adds a
        `notes` entry — quote the count, not the row count. `open_by_status` covers the open
        set as it is *now*, not as it was during the window. `updated` includes the rows in
        `closed`. Time visibility is permission-bound, so a `total_hours` of 0 can mean "not
        allowed to see" rather than "nobody logged time" — an unreadable time ledger and an
        unreadable `roster` each degrade into a `notes` entry instead of failing the call.
        Read `notes` before calling any number complete.

        Cross-references: rendered reports are the `weekly_report` and `daily_standup`
        prompts, which run this same aggregation server-side; drill into a bucket with
        `list_work_packages`, into hours with `list_time_entries`, and into one row with
        `get_work_package`.
        """
        ctx = _shared.get_tool_context()
        return await collect_report_data(
            ctx, project_id=project_id, from_date=from_date, to_date=to_date
        )
