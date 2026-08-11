"""Recurring-meeting tools: series CRUD and occurrence control (SPEC §6.13).

Lands here:

==========================================  ======  ==========================================
Tool                                        Phase   Endpoint(s)
==========================================  ======  ==========================================
🔍Ⓜ ``list_recurring_meetings``             3       ``GET /recurring_meetings``
🔍Ⓜ ``get_recurring_meeting``               3       ``GET /recurring_meetings/{id}``
                                                    + ``…/occurrences/upcoming``
✏️Ⓜ ``create_recurring_meeting``            3       ``POST /recurring_meetings``
🗑Ⓜ ``delete_recurring_meeting``            3       ``DELETE /recurring_meetings/{id}``
✏️Ⓜ ``init_recurring_meeting_occurrence``   3       ``POST …/occurrences/{start_time}/init``
🗑Ⓜ ``cancel_recurring_meeting_occurrence``  3       ``DELETE …/occurrences/{start_time}``
==========================================  ======  ==========================================

Non-negotiables for this module:

* **Everything here is 17.4+.** The entire ``/recurring_meetings`` subtree only
  ships with OpenProject 17.4 — before that every route in this family answers
  a plain 404 regardless of the id, even when the Meetings module is enabled
  and ``list_meetings`` works. Every tool's 404 hint names that reading next to
  the usual two (bad id, module off), mirroring the write fencing in
  ``modules_collab``.
* **No ``lockVersion``, no form.** The recurring-meeting resource has no
  optimistic locking and no form/schema endpoints, so validation errors come
  straight back as 422 and there is nothing to echo on writes.
* **The schedule conditionals are validated locally.** Upstream's "infer the
  monthly fields from startTime" defaults run in ``after_initialize``, *before*
  API params are assigned, so they never apply to API creates — a create that
  omits ``monthly_day`` for a monthly series is broken on the wire. The
  frequency/end_after combination matrix is therefore enforced here, before
  anything is sent (G2), with the allowed combinations spelled out.
* **``time_zone`` is required, IANA-validated, and enforced by a follow-up
  PATCH.** The model requires a zone but checks presence only: an invalid
  string is *stored* without error and silently read back as the account's
  zone, and on CREATE the payload zone is unconditionally overwritten with the
  API account's own. So the value is validated locally against the IANA
  database, and after the POST the stored zone is compared and corrected with
  a PATCH — a refused correction degrades to a note, never to silence (G5).
* **``duration`` is a plain number of hours on this endpoint.** The opposite of
  ``/meetings``, where it is an ISO 8601 duration; an ISO string sent here
  parses to nil upstream and silently becomes the 1-hour default. The tools
  take ``duration_minutes`` like their siblings and convert.
* **Occurrence instants are matched exactly, and never validated upstream.**
  Init and cancel do a timestamp-equality lookup: a wrong instant silently
  creates an off-schedule meeting (init) or cancels a phantom stub (cancel).
  Callers must echo a ``start_time`` read from the occurrence rows; the tools
  normalize whatever offset arrives into the canonical UTC ``Z`` form, which
  also keeps ``+`` (decodable as a space) out of the URL path.
* **A fresh series cannot instantiate occurrences.** ``POST
  /recurring_meetings`` creates the template meeting as a *draft*, and init
  against a draft template fails upstream with an unclean HTTP 500, not a 422.
  ``create_recurring_meeting`` says so in ``notes`` (naming the template
  meeting id), and the init tool's 500 hint explains the fix:
  ``update_meeting(meeting_id=<template>, state='open')``.
"""

# The sibling ``modules_collab`` module owns the meetings-family machinery —
# module gating, the version-aware hints, the meeting projection. Reusing those
# helpers beats re-implementing them (they must stay word-for-word identical),
# so the private-usage rule is relaxed for this file's imports of them.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime as dt
import zoneinfo
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    ConflictError,
    InputValidationError,
    NotFoundError,
    OpenProjectError,
    PermissionDeniedError,
    UpstreamServerError,
    ValidationFailedError,
)
from openproject_mcp.client.filters import DEFAULT_PAGE_SIZE, pagination_params
from openproject_mcp.client.payloads import build_write_payload, link
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools import _shared
from openproject_mcp.tools.modules_collab import (
    MEETINGS,
    MEETINGS_DELETE,
    MEETINGS_EDIT,
    MeetingDeletionResult,
    MeetingDetail,
    _agenda_item,
    _agenda_items,
    _int_or_none,
    _iso_datetime,
    _meeting_detail,
    _Module,
    _module_delete,
    _module_get,
    _module_list_get,
    _module_post,
    _unavailable_envelope,
    _undisclosed_note,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "OccurrenceCancellationResult",
    "RecurringMeetingDetail",
    "RecurringMeetingRow",
    "RecurringOccurrence",
    "register",
]

#: The Meetings module seen through the occurrence-init permission: since 17.5
#: init requires ``create_meetings`` (17.4 briefly wanted ``edit_meetings``),
#: and a 403 hint must name the permission the write actually needs.
MEETINGS_CREATE = _Module("Meetings", "create meetings")

#: Repetition rules the model accepts. ``working_days`` runs every working day
#: and force-sets interval=1 upstream; the two monthly flavours carry their own
#: required fields (see :data:`SCHEDULE_RULES`).
Frequency = Literal[
    "daily", "working_days", "weekly", "monthly_day_of_month", "monthly_nth_weekday"
]

#: How a series ends. ``never`` is the upstream default; the other two require
#: their companion field, enforced locally.
EndAfter = Literal["never", "specific_date", "iterations"]

#: Weekday spelling the model accepts for ``monthly_nth_weekday`` — lowercase
#: English names, exactly as upstream validates them.
MonthlyWeekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]

#: Upcoming occurrences fetched per series. The upstream ``limit`` defaults to
#: 20 and the sub-collection does not paginate, so the cap is honest: hitting
#: it is reported in ``notes`` rather than read as the end of the series.
OCCURRENCES_CAP = 10

#: The one version reading every tool in this family appends to its 404 hint:
#: the whole subtree is 17.4+, so a 404 here can also mean "too old".
RECURRING_DISCOVERY = (
    "Recurring meeting ids come from list_recurring_meetings (they are not meeting ids). A "
    "third reading applies here: OpenProject before 17.4 does not route /recurring_meetings "
    "at all, so every recurring-meeting tool fails there regardless of the id even when the "
    "Meetings module is enabled and list_meetings works — recurring meetings require "
    "OpenProject 17.4 or later."
)

#: The allowed frequency/end_after combinations, spelled out once and attached
#: to every local rejection so a bad combination teaches the whole matrix.
SCHEDULE_RULES = (
    "Allowed combinations — frequency: 'daily' and 'weekly' repeat every `interval` "
    "days/weeks (1-100); 'working_days' runs every working day (interval is fixed at 1, do "
    "not pass another value); 'monthly_day_of_month' needs monthly_day (1-31); "
    "'monthly_nth_weekday' needs monthly_ordinal (1|2|3|4, or -1 for 'last') AND "
    "monthly_weekday ('monday'…'sunday'). end_after: 'never' (default) takes neither "
    "end_date nor iterations; 'specific_date' needs end_date (YYYY-MM-DD, on or after the "
    "first occurrence); 'iterations' needs iterations (1-1000)."
)

OCCURRENCES_FORBIDDEN_NOTE = (
    "occurrences: no permission (403) — the series itself is readable, but this account may "
    "not read its occurrence schedule"
)
OCCURRENCES_MISSING_NOTE = (
    "occurrences: not available (404) — this instance does not expose "
    "/recurring_meetings/{id}/occurrences/upcoming, so the schedule could not be read"
)
OCCURRENCES_CAP_NOTE = (
    f"occurrences shows the next {OCCURRENCES_CAP} slots only — the schedule computes further "
    "ones, so the last row is a cap, not the end of the series"
)


# --- projections ----------------------------------------------------------


class RecurringOccurrence(BaseModel):
    """One occurrence of a recurring series: a scheduled slot, instantiated or not."""

    start_time: str | None = Field(
        default=None,
        description="Scheduled instant as ISO 8601 UTC ('2026-08-12T10:00:00Z'). This exact "
        "string is what init_recurring_meeting_occurrence and "
        "cancel_recurring_meeting_occurrence take — matching is by timestamp equality, so "
        "never retype or round it.",
    )
    state: str | None = Field(
        default=None,
        description="'planned' for a slot that exists only in the schedule; once instantiated, "
        "the backing meeting's own state ('open', 'draft', 'in_progress', 'cancelled', "
        "'closed').",
    )
    meeting_id: int | str | None = Field(
        default=None,
        description="Id of the backing meeting for get_meeting / delete_meeting. Null until "
        "the occurrence is instantiated — a 'planned' slot has no meeting yet.",
    )


class RecurringMeetingRow(BaseModel):
    """One recurring meeting series as list results return it."""

    id: int | str | None = Field(
        default=None,
        description="Series id — what get_recurring_meeting and the occurrence tools take. "
        "Not a meeting id.",
    )
    title: str | None = Field(default=None, description="Series title.")
    project: Ref | None = Field(default=None, description="Project the series belongs to.")
    start_time: str | None = Field(
        default=None, description="First-occurrence start as ISO 8601 UTC."
    )
    time_zone: str | None = Field(
        default=None,
        description="Zone the schedule computes in, as OpenProject stores it (an IANA "
        "identifier or a Rails zone name).",
    )
    frequency: str | None = Field(
        default=None,
        description="Repetition rule: 'daily', 'working_days', 'weekly', "
        "'monthly_day_of_month' or 'monthly_nth_weekday'.",
    )
    interval: int | None = Field(
        default=None, description="Every N days/weeks/months; always 1 for 'working_days'."
    )
    monthly_day: int | None = Field(
        default=None, description="Day of month (1-31); only for 'monthly_day_of_month'."
    )
    monthly_ordinal: int | None = Field(
        default=None,
        description="Which weekday occurrence (1-4, -1 = last); only for 'monthly_nth_weekday'.",
    )
    monthly_weekday: str | None = Field(
        default=None, description="Weekday name; only for 'monthly_nth_weekday'."
    )
    end_after: str | None = Field(
        default=None, description="'never', 'specific_date' or 'iterations'."
    )
    end_date: str | None = Field(
        default=None, description="Last possible date (ISO); only when end_after='specific_date'."
    )
    iterations: int | None = Field(
        default=None, description="Total occurrences; only when end_after='iterations'."
    )
    duration_hours: float | None = Field(
        default=None, description="Length of each occurrence in hours (1.5 = 90 minutes)."
    )
    location: str | None = Field(
        default=None, description="Room name or meeting URL each occurrence inherits."
    )


class RecurringMeetingDetail(RecurringMeetingRow):
    """One series in full: the schedule plus its next occurrences."""

    author: Ref | None = Field(default=None, description="User who created the series.")
    template_meeting_id: int | str | None = Field(
        default=None,
        description="Id of the template meeting the occurrences are copied from. Its agenda is "
        "edited with the regular meeting tools, and a freshly created template is a DRAFT — "
        "publish it with update_meeting(meeting_id=<this>, state='open') before initialising "
        "occurrences.",
    )
    occurrences: list[RecurringOccurrence] = Field(
        default_factory=list[RecurringOccurrence],
        description="The next upcoming slots in order (capped; see 'notes'). meeting_id is "
        "null until a slot is instantiated, and state 'planned' marks exactly those.",
    )
    notes: list[str] = Field(
        default_factory=list[str],
        description="Degradation markers: an unreadable schedule, the occurrence cap, a "
        "time zone that could not be applied, a draft template.",
    )


class OccurrenceCancellationResult(BaseModel):
    """Outcome of ``cancel_recurring_meeting_occurrence``."""

    recurring_meeting_id: int = Field(description="Series the occurrence belongs to.")
    start_time: str = Field(
        description="The cancelled instant, normalized to the canonical UTC 'Z' form."
    )
    cancelled: bool = Field(description="True once OpenProject accepted the cancellation.")
    message: str = Field(description="Human-readable confirmation naming what was cancelled.")


# --- payload helpers ------------------------------------------------------


def _recurring_row(payload: Mapping[str, Any]) -> RecurringMeetingRow:
    time_zone = payload.get("timeZone")
    frequency = payload.get("frequency")
    monthly_weekday = payload.get("monthlyWeekday")
    end_after = payload.get("endAfter")
    return RecurringMeetingRow(
        id=hal.self_id(payload),
        title=payload.get("title"),
        project=Ref.from_hal(payload, "project"),
        start_time=payload.get("startTime"),
        time_zone=time_zone if isinstance(time_zone, str) else None,
        frequency=frequency if isinstance(frequency, str) else None,
        interval=_int_or_none(payload.get("interval")),
        monthly_day=_int_or_none(payload.get("monthlyDay")),
        monthly_ordinal=_int_or_none(payload.get("monthlyOrdinal")),
        monthly_weekday=monthly_weekday if isinstance(monthly_weekday, str) else None,
        end_after=end_after if isinstance(end_after, str) else None,
        end_date=payload.get("endDate"),
        iterations=_int_or_none(payload.get("iterations")),
        duration_hours=hal.duration_hours(payload.get("duration")),
        location=payload.get("location"),
    )


def _recurring_detail(
    payload: Mapping[str, Any],
    *,
    occurrences: list[RecurringOccurrence],
    notes: list[str],
) -> RecurringMeetingDetail:
    template = hal.ref(payload, "template")
    return RecurringMeetingDetail(
        **_recurring_row(payload).model_dump(),
        author=Ref.from_hal(payload, "author"),
        template_meeting_id=template.id if template is not None else None,
        occurrences=occurrences,
        notes=notes,
    )


def _occurrence(payload: Mapping[str, Any]) -> RecurringOccurrence:
    state = payload.get("state")
    meeting = hal.ref(payload, "meeting")
    return RecurringOccurrence(
        start_time=payload.get("startTime"),
        state=state if isinstance(state, str) else None,
        meeting_id=meeting.id if meeting is not None else None,
    )


async def _occurrence_rows(
    recurring_meeting_id: int | str,
) -> tuple[list[RecurringOccurrence], list[str]]:
    """A series' next occurrences; an unreadable schedule degrades to a note (G5)."""
    ctx = _shared.get_tool_context()
    try:
        payload = await ctx.client.get_json(
            f"recurring_meetings/{recurring_meeting_id}/occurrences/upcoming",
            params={"limit": OCCURRENCES_CAP},
        )
    except NotFoundError:
        return [], [OCCURRENCES_MISSING_NOTE]
    except PermissionDeniedError:
        return [], [OCCURRENCES_FORBIDDEN_NOTE]
    elements = hal.collection(payload).elements
    notes = [OCCURRENCES_CAP_NOTE] if len(elements) >= OCCURRENCES_CAP else []
    return [_occurrence(element) for element in elements], notes


# --- input helpers --------------------------------------------------------


def _validated_time_zone(value: str) -> str:
    """Require an IANA zone identifier, checked locally (G2).

    OpenProject stores any non-empty string as the zone and silently falls back
    to the account's zone when reading an unknown one — a typo would misplace
    every occurrence without a single error. Rails city names ('Berlin') are
    also accepted upstream but cannot be validated here, so the tool takes the
    IANA form only; every IANA identifier is valid upstream.
    """
    candidate = value.strip()
    problem = "is not a known IANA time zone identifier" if candidate else "is empty"
    if candidate:
        try:
            zoneinfo.ZoneInfo(candidate)
            return candidate
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            try:
                zoneinfo.ZoneInfo("UTC")
            except zoneinfo.ZoneInfoNotFoundError:
                # No local tz database at all: nothing can be validated, so an
                # unverifiable value beats rejecting every valid one.
                return candidate
    raise InputValidationError(
        f"time_zone={value!r} {problem}.",
        hint=(
            "Pass an IANA identifier such as 'Europe/Berlin', 'America/New_York' or "
            "'Etc/UTC'. OpenProject stores an unknown zone string without complaint and then "
            "silently computes the schedule in the account's own zone, so the value is "
            "checked locally instead."
        ),
    )


def _iso_date(value: str, field_name: str) -> str:
    """Validate a plain ISO date locally (G2)."""
    candidate = value.strip()
    try:
        dt.date.fromisoformat(candidate)
    except ValueError as exc:
        raise InputValidationError(
            f"{field_name}={value!r} is not an ISO date.",
            hint=f"{field_name} must be a calendar date like '2026-12-31' (no time part).",
        ) from exc
    return candidate


def _schedule_attributes(
    *,
    frequency: Frequency,
    interval: int,
    monthly_day: int | None,
    monthly_ordinal: int | None,
    monthly_weekday: str | None,
    end_after: EndAfter,
    end_date: str | None,
    iterations: int | None,
) -> dict[str, Any]:
    """Enforce the frequency/end_after matrix locally and build the wire fields.

    Upstream would 422 a *missing* companion field, but its "infer from
    startTime" defaults never apply to API creates, and extra fields for the
    wrong frequency would be stored misleadingly — so both directions are
    rejected here with the whole matrix spelled out.
    """

    def reject(problem: str) -> InputValidationError:
        return InputValidationError(problem, hint=SCHEDULE_RULES)

    attributes: dict[str, Any] = {"frequency": frequency, "endAfter": end_after}

    if frequency == "monthly_day_of_month":
        if monthly_day is None:
            raise reject("monthly_day is required for frequency='monthly_day_of_month'.")
        if monthly_ordinal is not None or monthly_weekday is not None:
            raise reject(
                "monthly_ordinal/monthly_weekday do not apply to "
                "frequency='monthly_day_of_month' — use frequency='monthly_nth_weekday' for "
                "'the Nth weekday of the month'."
            )
        attributes["monthlyDay"] = monthly_day
    elif frequency == "monthly_nth_weekday":
        if monthly_ordinal is None or monthly_weekday is None:
            raise reject(
                "monthly_ordinal AND monthly_weekday are both required for "
                "frequency='monthly_nth_weekday'."
            )
        if monthly_day is not None:
            raise reject(
                "monthly_day does not apply to frequency='monthly_nth_weekday' — use "
                "frequency='monthly_day_of_month' for 'the Nth day of the month'."
            )
        attributes["monthlyOrdinal"] = monthly_ordinal
        attributes["monthlyWeekday"] = monthly_weekday
    elif monthly_day is not None or monthly_ordinal is not None or monthly_weekday is not None:
        raise reject(
            f"monthly_day/monthly_ordinal/monthly_weekday do not apply to frequency='{frequency}'."
        )

    if frequency == "working_days":
        # Upstream force-sets interval=1 here; silently ignoring another value
        # would misreport the schedule, so it is refused instead (G2/G3).
        if interval != 1:
            raise reject(
                "interval does not apply to frequency='working_days' — the series runs every "
                "working day. Omit interval, or use frequency='daily' for 'every N days'."
            )
    else:
        attributes["interval"] = interval

    if end_after == "specific_date":
        if end_date is None:
            raise reject("end_date is required for end_after='specific_date'.")
        if iterations is not None:
            raise reject("iterations does not apply to end_after='specific_date'.")
        attributes["endDate"] = _iso_date(end_date, "end_date")
    elif end_after == "iterations":
        if iterations is None:
            raise reject("iterations is required for end_after='iterations'.")
        if end_date is not None:
            raise reject("end_date does not apply to end_after='iterations'.")
        attributes["iterations"] = iterations
    elif end_date is not None or iterations is not None:
        raise reject(
            "end_date/iterations do not apply to end_after='never' — pass "
            "end_after='specific_date' or end_after='iterations' to bound the series."
        )

    return attributes


def _occurrence_token(start_time: str) -> str:
    """Normalize an ISO instant to the canonical UTC ``Z`` path token.

    The occurrence routes key on timestamp equality against exactly the form
    the occurrence rows render (``start_time.utc.iso8601``). Converting to UTC
    here lets a caller echo a ``+02:00`` offset safely — and keeps a literal
    ``+`` (decodable as a space) out of the URL path. The resulting token
    contains only digits, ``-``, ``:``, ``T`` and ``Z``, all legal unencoded
    in a path segment.
    """
    validated = _iso_datetime(start_time, "start_time")
    parsed = dt.datetime.fromisoformat(validated)
    return parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(mcp: FastMCP) -> None:
    """Register the recurring-meeting tools (SPEC §6.13)."""

    @mcp.tool(
        name="list_recurring_meetings",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.READ),
        annotations=_shared.read_annotations(title="List recurring meetings"),
    )
    @_shared.tool_errors
    async def list_recurring_meetings(
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="Series per page (max 100).")
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[RecurringMeetingRow]:
        """List recurring meeting series — the repetition rules, not the individual meetings.

        Use it to answer "what regular meetings do we have" and to find the series id that
        `get_recurring_meeting`, the occurrence tools and `delete_recurring_meeting` need.
        Series ids are their own id space: a series id is never a meeting id, and the weekly
        occurrences themselves show up in `list_meetings`, not here.

        Returns the standard list envelope: rows of `{id, title, project, start_time,
        time_zone, frequency, interval, monthly_day, monthly_ordinal, monthly_weekday,
        end_after, end_date, iterations, duration_hours, location}` plus `pagination` and
        `notes`.

        Pitfalls. The listing is instance-wide — the endpoint takes no project filter, so
        scope by reading `project` on the rows. `duration_hours` is per occurrence. Recurring
        meetings are 17.4+ AND a module: where the API predates them, the module is off, or
        this account may not read meetings, the call still SUCCEEDS with an empty `items` and
        the reason in `notes` — read `notes` before saying there are no recurring meetings.

        Cross-references: `get_recurring_meeting(recurring_meeting_id=...)` for the schedule
        with its next occurrences; `create_recurring_meeting` to start a series;
        `list_meetings` for the instantiated occurrences.
        """
        payload, degraded = await _module_list_get(
            "recurring_meetings",
            module=MEETINGS,
            subject="the recurring meetings collection",
            discovery=RECURRING_DISCOVERY,
            params=pagination_params(page, page_size),
        )
        if payload is None:
            # Ⓜ module absent, account blocked, or pre-17.4 — a note, never an
            # empty schedule (G5).
            return _unavailable_envelope(degraded)

        collection = hal.collection(payload)
        rows = [_recurring_row(element) for element in collection]
        return _shared.envelope_from_collection(collection, rows, page=page, page_size=page_size)

    @mcp.tool(
        name="get_recurring_meeting",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.READ),
        annotations=_shared.read_annotations(title="Get recurring meeting"),
    )
    @_shared.tool_errors
    async def get_recurring_meeting(
        recurring_meeting_id: Annotated[
            int,
            Field(
                description="Numeric series id from list_recurring_meetings. Never a meeting "
                "id — a series and its occurrences are different resources."
            ),
        ],
    ) -> RecurringMeetingDetail:
        """Read one recurring series in full: the schedule plus its next occurrences.

        This is the step before touching any occurrence: the `occurrences` rows carry the
        exact `start_time` strings that `init_recurring_meeting_occurrence` and
        `cancel_recurring_meeting_occurrence` key on, and the `meeting_id` that `get_meeting`
        / `delete_meeting` take once a slot is instantiated.

        Returns the series fields (schedule, `duration_hours`, `location`, `author`,
        `template_meeting_id`) plus `occurrences` in date order: `{start_time, state,
        meeting_id}`. `state` is 'planned' for a slot that exists only in the schedule;
        once instantiated it is the backing meeting's own state, and only then is
        `meeting_id` non-null.

        Pitfalls. `occurrences` is capped at the next few slots — `notes` says when the cap
        was hit, and an unbounded series always computes more. The template meeting (its
        agenda seeds every occurrence) is edited through the regular meeting tools via
        `template_meeting_id`; while it is still a draft, occurrences cannot be initialized.
        A 404 means a wrong id, no 'view meetings' permission, the module is off — or
        OpenProject before 17.4, which has no recurring-meetings API; the hint names all
        readings.

        Cross-references: `list_recurring_meetings` for the series id;
        `init_recurring_meeting_occurrence` / `cancel_recurring_meeting_occurrence` for one
        slot; `update_meeting` on the template to build the shared agenda or publish a draft
        template; `delete_recurring_meeting` to remove the whole series.
        """
        payload = await _module_get(
            f"recurring_meetings/{recurring_meeting_id}",
            module=MEETINGS,
            subject=f"recurring meeting {recurring_meeting_id}",
            discovery=RECURRING_DISCOVERY,
        )
        occurrences, notes = await _occurrence_rows(recurring_meeting_id)
        return _recurring_detail(payload, occurrences=occurrences, notes=notes)

    @mcp.tool(
        name="create_recurring_meeting",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(title="Create recurring meeting"),
    )
    @_shared.tool_errors
    async def create_recurring_meeting(
        project_id: Annotated[
            int | str,
            Field(
                description="Numeric id or identifier of the project the series belongs to. It "
                "must have the Meetings module enabled and this account needs the 'create "
                "meetings' permission in it."
            ),
        ],
        title: Annotated[
            str,
            Field(description="Series title, e.g. 'Weekly team sync'."),
        ],
        start_time: Annotated[
            str,
            Field(
                description="First occurrence as ISO 8601 WITH a timezone: "
                "'2026-09-01T09:00:00Z' or '2026-09-01T11:00:00+02:00'. Must be now or in the "
                "future; a time without an offset is rejected locally."
            ),
        ],
        duration_minutes: Annotated[
            int,
            Field(
                ge=1,
                description="Length of each occurrence in minutes (90 = one and a half "
                "hours). The result reports it back as duration_hours.",
            ),
        ],
        time_zone: Annotated[
            str,
            Field(
                description="IANA time zone the schedule computes in, e.g. 'Europe/Berlin' or "
                "'Etc/UTC' — required, because it decides what 'every Monday 09:00' means "
                "across DST changes. Validated locally: OpenProject would store a typo "
                "silently and fall back to the account's zone."
            ),
        ],
        frequency: Annotated[
            Frequency,
            Field(
                description="Repetition rule: 'daily', 'working_days' (every working day), "
                "'weekly' (the default), 'monthly_day_of_month' (needs monthly_day) or "
                "'monthly_nth_weekday' (needs monthly_ordinal + monthly_weekday)."
            ),
        ] = "weekly",
        interval: Annotated[
            int,
            Field(
                ge=1,
                le=100,
                description="Every N days/weeks/months (default 1 = every occurrence of the "
                "rule). Not applicable to 'working_days'.",
            ),
        ] = 1,
        monthly_day: Annotated[
            int | None,
            Field(
                ge=1,
                le=31,
                description="Day of the month (1-31); required for, and only valid with, "
                "frequency='monthly_day_of_month'.",
            ),
        ] = None,
        monthly_ordinal: Annotated[
            Literal[1, 2, 3, 4, -1] | None,
            Field(
                description="Which weekday of the month: 1-4, or -1 for the last one; "
                "required for, and only valid with, frequency='monthly_nth_weekday'."
            ),
        ] = None,
        monthly_weekday: Annotated[
            MonthlyWeekday | None,
            Field(
                description="Weekday name ('monday'…'sunday'); required for, and only valid "
                "with, frequency='monthly_nth_weekday'."
            ),
        ] = None,
        end_after: Annotated[
            EndAfter,
            Field(
                description="How the series ends: 'never' (the default), 'specific_date' "
                "(needs end_date) or 'iterations' (needs iterations)."
            ),
        ] = "never",
        end_date: Annotated[
            str | None,
            Field(
                description="Last possible date as 'YYYY-MM-DD'; required for, and only valid "
                "with, end_after='specific_date'."
            ),
        ] = None,
        iterations: Annotated[
            int | None,
            Field(
                ge=1,
                le=1000,
                description="Total number of occurrences (1-1000); required for, and only "
                "valid with, end_after='iterations'.",
            ),
        ] = None,
        location: Annotated[
            str | None,
            Field(description="Room name or meeting URL every occurrence inherits. Omit for none."),
        ] = None,
        notify: Annotated[
            bool,
            Field(
                description="True emails participants about schedule changes and "
                "cancellations. Defaults to false — an API-created series stays quiet."
            ),
        ] = False,
    ) -> RecurringMeetingDetail:
        """Create a recurring meeting series: a schedule plus a template the occurrences copy.

        Use it for "set up a weekly sync Mondays at 9" style requests. The frequency and
        end_after combinations are validated locally BEFORE anything is sent — OpenProject's
        own "infer the monthly fields" defaults never apply to API creates, so a bad
        combination is rejected here with the allowed matrix spelled out.

        Returns the created series in the same shape as `get_recurring_meeting`, including
        the computed next `occurrences` (their `start_time` strings are what the occurrence
        tools take) and `template_meeting_id`.

        Pitfalls — two upstream quirks are handled but must be understood. First, the
        template meeting is created as a DRAFT: `notes` says so, and occurrences cannot be
        initialized until `update_meeting(meeting_id=<template_meeting_id>, state='open')`
        publishes it. Second, OpenProject overwrites `time_zone` on create with the API
        account's own zone; this tool detects that and corrects it with a follow-up PATCH —
        if that correction is refused (it needs 'edit meetings'), the series is still created
        and `notes` names the zone it actually runs in. `start_time` must be now or in the
        future, or the create is rejected with a validation error.

        Cross-references: `get_recurring_meeting` to read it back;
        `update_meeting(meeting_id=<template_meeting_id>, ...)` to build the shared agenda
        and publish the template; `init_recurring_meeting_occurrence` to materialize a slot;
        `list_projects` for the project id.
        """
        if not title.strip():
            raise InputValidationError(
                "title is empty.",
                hint="Pass the series title, e.g. 'Weekly team sync'.",
            )
        requested_zone = _validated_time_zone(time_zone)

        attributes: dict[str, Any] = {
            "title": title.strip(),
            "startTime": _iso_datetime(start_time, "start_time"),
            # A plain number of hours — this endpoint does NOT speak ISO
            # durations; "PT1H30M" would silently become the 1-hour default.
            "duration": duration_minutes / 60,
            "timeZone": requested_zone,
            "notify": notify,
        }
        attributes.update(
            _schedule_attributes(
                frequency=frequency,
                interval=interval,
                monthly_day=monthly_day,
                monthly_ordinal=monthly_ordinal,
                monthly_weekday=monthly_weekday,
                end_after=end_after,
                end_date=end_date,
                iterations=iterations,
            )
        )
        if location is not None:
            attributes["location"] = location

        subject = f"project {project_id}"
        try:
            created = await _module_post(
                "recurring_meetings",
                json=build_write_payload(attributes, {"project": link("projects", project_id)}),
                module=MEETINGS_CREATE,
                subject=subject,
                discovery=RECURRING_DISCOVERY,
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject rejected the series. The usual causes: start_time is in the "
                    "past (it must be now or later), end_date lies before the first computed "
                    "occurrence, or the project does not exist / has the Meetings module "
                    "disabled. 'violations' names the attribute. " + SCHEDULE_RULES
                ),
            ) from exc

        notes: list[str] = []
        # OpenProject overwrites timeZone on create with the account's own zone;
        # only a PATCH makes the requested one stick (SPEC §6.13).
        stored_zone = created.get("timeZone")
        if isinstance(stored_zone, str) and stored_zone != requested_zone:
            ctx = _shared.get_tool_context()
            series_id = hal.self_id(created) or ""
            try:
                created = await ctx.client.patch_json(
                    f"recurring_meetings/{series_id}", json={"timeZone": requested_zone}
                )
            except OpenProjectError:
                notes.append(
                    f"time_zone '{requested_zone}' could not be applied: OpenProject "
                    f"overwrites the zone on create with the API account's own "
                    f"('{stored_zone}'), and the follow-up PATCH that corrects it was "
                    "refused — it needs the 'edit meetings' permission. The schedule "
                    f"computes in '{stored_zone}' until an account holding it re-sets the "
                    "zone."
                )

        template = hal.ref(created, "template")
        template_target = (
            f"meeting_id={template.id}"
            if template is not None and template.id is not None
            else "meeting_id=<template_meeting_id>"
        )
        notes.append(
            "the new series' template meeting is a DRAFT: occurrences cannot be "
            "initialized (init fails with HTTP 500) until it is published with "
            f"update_meeting({template_target}, state='open')"
        )

        occurrences, occurrence_notes = await _occurrence_rows(hal.self_id(created) or "")
        return _recurring_detail(created, occurrences=occurrences, notes=notes + occurrence_notes)

    @mcp.tool(
        name="delete_recurring_meeting",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE, _shared.DESTRUCTIVE),
        annotations=_shared.destructive_annotations(title="Delete recurring meeting"),
    )
    @_shared.tool_errors
    async def delete_recurring_meeting(
        recurring_meeting_id: Annotated[
            int,
            Field(
                description="Numeric series id to delete permanently, from "
                "list_recurring_meetings. Read it back with get_recurring_meeting first and "
                "show the user the title — this removes every occurrence, not one meeting."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Must be true. Ask the user to confirm first — the API offers no "
                "undo. Calling with confirm=false returns a confirmation_required error rather "
                "than deleting anything."
            ),
        ] = False,
    ) -> MeetingDeletionResult:
        """Permanently delete a recurring series: template, schedule, and EVERY occurrence.

        Use only on explicit user instruction, and make sure the user means the whole series:
        every instantiated meeting of the series — past minutes included — is destroyed with
        the template, and when the series has `notify` set, participants are emailed
        cancellations. To drop a single slot instead, `cancel_recurring_meeting_occurrence`
        is the right tool, and `delete_meeting` removes one instantiated meeting.

        Returns a small confirmation object once OpenProject accepts the deletion.

        Pitfalls. This needs the 'delete meetings' permission, so a 403 is about the account,
        not the id. A 404 means the id is wrong, the series was already deleted — or
        OpenProject before 17.4, which has no recurring-meetings API at all; the hint names
        all readings.

        Cross-references: `get_recurring_meeting` to check what you are about to destroy;
        `cancel_recurring_meeting_occurrence` for one slot; `delete_meeting` for one
        instantiated meeting.
        """
        _shared.require_confirmation(
            confirm,
            action="delete recurring meeting series",
            target=f"#{recurring_meeting_id}",
            consequence=(
                "The series, its template meeting and EVERY instantiated occurrence — past "
                "ones and their minutes included — are removed permanently, participants may "
                "be emailed cancellations, and the deletion cannot be undone through the API."
            ),
        )
        await _module_delete(
            f"recurring_meetings/{recurring_meeting_id}",
            module=MEETINGS_DELETE,
            subject=f"recurring meeting {recurring_meeting_id}",
            discovery=RECURRING_DISCOVERY,
        )
        return MeetingDeletionResult(
            id=recurring_meeting_id,
            deleted=True,
            message=(
                f"Recurring meeting series #{recurring_meeting_id} was deleted permanently, "
                "together with its template and every instantiated occurrence."
            ),
        )

    @mcp.tool(
        name="init_recurring_meeting_occurrence",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(
            title="Init recurring meeting occurrence", idempotent=True
        ),
    )
    @_shared.tool_errors
    async def init_recurring_meeting_occurrence(
        recurring_meeting_id: Annotated[
            int,
            Field(
                description="Numeric series id from list_recurring_meetings. Never a meeting id."
            ),
        ],
        start_time: Annotated[
            str,
            Field(
                description="The occurrence's scheduled instant, copied VERBATIM from a "
                "get_recurring_meeting occurrences row ('2026-08-12T10:00:00Z'). Matching is "
                "exact-instant and OpenProject does not check the value against the schedule "
                "— a retyped or rounded time silently creates an off-schedule meeting."
            ),
        ],
    ) -> MeetingDetail:
        """Materialize one occurrence of a series as a real meeting, copied from the template.

        Use it when a specific slot needs its own agenda, minutes or attachments before the
        day: the occurrence becomes a normal meeting (template agenda and attachments
        copied) that every meeting tool can work on. Called on a cancelled occurrence it
        RESTORES it to 'open'; called where an open meeting already exists it idempotently
        returns that meeting.

        Returns the instantiated meeting in the same shape as `get_meeting` — its `id` is
        the meeting id for follow-up calls, distinct from the series id.

        Pitfalls — the instant is trusted, not validated. OpenProject matches `start_time`
        by timestamp equality and does NOT check it against the schedule, so a wrong instant
        creates a real off-schedule meeting: always copy the string from
        `get_recurring_meeting`'s occurrences (offsets are normalized to UTC 'Z' form on the
        wire). An HTTP 500 here almost always means the series' template is still a DRAFT —
        OpenProject fails uncleanly on that instead of answering 422; publish the template
        with `update_meeting(meeting_id=<template_meeting_id>, state='open')` and retry.
        This needs the 'create meetings' permission (403 otherwise; OpenProject 17.4 itself
        briefly wanted 'edit meetings').

        Cross-references: `get_recurring_meeting` for the exact start_time strings and the
        template id; `update_meeting` / `add_meeting_agenda_item` on the result;
        `cancel_recurring_meeting_occurrence` for the opposite move.
        """
        token = _occurrence_token(start_time)
        try:
            created = await _module_post(
                f"recurring_meetings/{recurring_meeting_id}/occurrences/{token}/init",
                # No payload is defined for this route, but a bodyless POST is
                # answered 406 (missing content-type) — send an empty object.
                json={},
                module=MEETINGS_CREATE,
                subject=f"recurring meeting {recurring_meeting_id}",
                discovery=RECURRING_DISCOVERY,
            )
        except UpstreamServerError as exc:
            raise UpstreamServerError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    "A 500 on occurrence init almost always means the series' template "
                    "meeting is still a DRAFT — OpenProject cannot instantiate from an "
                    "unpublished template and fails uncleanly instead of answering 422. "
                    "get_recurring_meeting reports the template_meeting_id; publish it with "
                    "update_meeting(meeting_id=<that id>, state='open'), then retry. If the "
                    "template is already open, the instance hit a real server error."
                ),
            ) from exc

        meeting_id = hal.self_id(created)
        elements: list[dict[str, Any]] = []
        agenda_note: str | None = None
        if isinstance(meeting_id, int):
            elements, agenda_note = await _agenda_items(meeting_id)
        notes = [note for note in (agenda_note, _undisclosed_note(elements)) if note]
        return _meeting_detail(
            created, agenda_items=[_agenda_item(element) for element in elements], notes=notes
        )

    @mcp.tool(
        name="cancel_recurring_meeting_occurrence",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE, _shared.DESTRUCTIVE),
        annotations=_shared.destructive_annotations(
            title="Cancel recurring meeting occurrence", idempotent=True
        ),
    )
    @_shared.tool_errors
    async def cancel_recurring_meeting_occurrence(
        recurring_meeting_id: Annotated[
            int,
            Field(
                description="Numeric series id from list_recurring_meetings. Never a meeting id."
            ),
        ],
        start_time: Annotated[
            str,
            Field(
                description="The occurrence's scheduled instant, copied VERBATIM from a "
                "get_recurring_meeting occurrences row ('2026-08-12T10:00:00Z'). Matching is "
                "exact-instant — a wrong time cancels a phantom stub while the real "
                "occurrence lives on."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Must be true. Ask the user to confirm first. Calling with "
                "confirm=false returns a confirmation_required error rather than cancelling "
                "anything."
            ),
        ] = False,
    ) -> OccurrenceCancellationResult:
        """Cancel one occurrence of a series — skip a slot without touching the schedule.

        Use it for "no sync next Monday": the slot stays in the occurrences list as
        'cancelled' (backed by a cancelled stub meeting) while the series keeps running. A cancelled occurrence is recoverable —
        `init_recurring_meeting_occurrence` at the same instant restores it to 'open' — but
        the cancellation itself may email participants when the series has `notify` set,
        which is why it is confirm-gated.

        Returns a small confirmation object carrying the normalized instant.

        Pitfalls. The instant is matched exactly and never validated against the schedule:
        cancelling at a wrong time succeeds (204) by creating a cancelled phantom stub while
        the real occurrence lives on — always copy `start_time` from
        `get_recurring_meeting`'s occurrences. A `conflict` error (409) means the occurrence
        is already instantiated as a live meeting, which OpenProject refuses to cancel in
        place: `delete_meeting(meeting_id=...)` (the id is in the occurrences row) is the
        move then. Cancelling an already-cancelled slot is an idempotent success. This needs
        the 'edit meetings' permission.

        Cross-references: `get_recurring_meeting` for the exact start_time and the
        occurrence's meeting_id; `delete_meeting` for an instantiated occurrence;
        `delete_recurring_meeting` to end the whole series.
        """
        token = _occurrence_token(start_time)
        _shared.require_confirmation(
            confirm,
            action="cancel occurrence",
            target=f"of series #{recurring_meeting_id} at {token}",
            consequence=(
                "The occurrence is marked cancelled (it stays listed with state 'cancelled'), and "
                "participants may be emailed when the series notifies. "
                "init_recurring_meeting_occurrence at the same instant can restore it."
            ),
        )
        try:
            await _module_delete(
                f"recurring_meetings/{recurring_meeting_id}/occurrences/{token}",
                module=MEETINGS_EDIT,
                subject=f"recurring meeting {recurring_meeting_id}",
                discovery=RECURRING_DISCOVERY,
            )
        except ConflictError as exc:
            raise ConflictError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    "This occurrence is already instantiated as a live meeting, and "
                    "OpenProject refuses to cancel it in place. Delete the meeting instead: "
                    "get_recurring_meeting's occurrences carry its meeting_id, and "
                    "delete_meeting(meeting_id=..., confirm=true) removes it."
                ),
            ) from exc
        return OccurrenceCancellationResult(
            recurring_meeting_id=recurring_meeting_id,
            start_time=token,
            cancelled=True,
            message=(
                f"Occurrence {token} of recurring meeting series #{recurring_meeting_id} "
                "was cancelled. It can be restored with init_recurring_meeting_occurrence."
            ),
        )
