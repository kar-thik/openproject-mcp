"""Golden HAL payloads for the recurring-meeting tools (SPEC §6.13).

Trimmed from OpenProject 17.x responses and kept as Python literals so the
suite stays offline and diffable. Only the fields the projections read are
kept, plus the ones that encode the upstream quirks these tools guard against:

* a freshly created series whose ``timeZone`` echoes the API account's zone
  instead of the requested one (the create-overwrite trap);
* an occurrence list mixing an instantiated slot (real ``meeting`` link, real
  state) with a planned one (no meeting link, synthetic ``"planned"`` state);
* the 409 the cancel route answers once an occurrence is instantiated.

The module-absence errors and the shared project/user links are imported from
the sibling meetings fixtures rather than re-invented.
"""

from __future__ import annotations

from typing import Any

from tests.fixtures.modules_collab_payloads import (
    MEETING,
    MODULE_FORBIDDEN,
    MODULE_NOT_FOUND,
    PROJECT_ID,
    hal_collection,
)

__all__ = [
    "CANCEL_CONFLICT",
    "CREATED_SERIES",
    "CREATED_SERIES_ID",
    "CREATED_SERIES_UTC",
    "CREATED_SERIES_ZONE_FIXED",
    "CREATED_TEMPLATE_MEETING_ID",
    "MODULE_FORBIDDEN",
    "MODULE_NOT_FOUND",
    "MONTHLY_RECURRING_MEETING",
    "OCCURRENCE_INSTANTIATED",
    "OCCURRENCE_MEETING",
    "OCCURRENCE_MEETING_ID",
    "OCCURRENCE_PLANNED",
    "PROJECT_ID",
    "RECURRING_COLLECTION",
    "RECURRING_MEETING",
    "RECURRING_MEETING_ID",
    "SERIES_START_REJECTED",
    "TEMPLATE_MEETING_ID",
    "UPCOMING_OCCURRENCES",
    "hal_collection",
]

RECURRING_MEETING_ID = 77
MONTHLY_RECURRING_ID = 78
TEMPLATE_MEETING_ID = 88
OCCURRENCE_MEETING_ID = 301
CREATED_SERIES_ID = 91
CREATED_TEMPLATE_MEETING_ID = 92

_GRACE = {"href": "/api/v3/users/3", "title": "Grace Hopper"}

#: A weekly series. ``duration`` is a plain number of hours on this resource —
#: 1.5, never "PT1H30M" — and ``timeZone`` echoes whatever valid string is
#: stored.
RECURRING_MEETING: dict[str, Any] = {
    "_type": "RecurringMeeting",
    "id": RECURRING_MEETING_ID,
    "title": "Weekly team sync",
    "startTime": "2026-08-17T07:00:00Z",
    "timeZone": "Europe/Berlin",
    "frequency": "weekly",
    "interval": 1,
    "endAfter": "never",
    "duration": 1.5,
    "location": "Room 2.14",
    "_links": {
        "self": {
            "href": f"/api/v3/recurring_meetings/{RECURRING_MEETING_ID}",
            "title": "Weekly team sync",
        },
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": dict(_GRACE),
        "template": {
            "href": f"/api/v3/meetings/{TEMPLATE_MEETING_ID}",
            "title": "Weekly team sync",
        },
    },
}

#: A bounded monthly series, exercising the nth-weekday and iterations fields.
MONTHLY_RECURRING_MEETING: dict[str, Any] = {
    "_type": "RecurringMeeting",
    "id": MONTHLY_RECURRING_ID,
    "title": "Steering board",
    "startTime": "2026-09-04T14:00:00Z",
    "timeZone": "Etc/UTC",
    "frequency": "monthly_nth_weekday",
    "interval": 1,
    "monthlyOrdinal": 1,
    "monthlyWeekday": "friday",
    "endAfter": "iterations",
    "iterations": 12,
    "duration": 1,
    "location": "",
    "_links": {
        "self": {
            "href": f"/api/v3/recurring_meetings/{MONTHLY_RECURRING_ID}",
            "title": "Steering board",
        },
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": dict(_GRACE),
        "template": {"href": "/api/v3/meetings/89", "title": "Steering board"},
    },
}

RECURRING_COLLECTION: dict[str, Any] = hal_collection(
    [RECURRING_MEETING, MONTHLY_RECURRING_MEETING], total=5, pageSize=20, offset=1
)

#: An instantiated occurrence: a real meeting backs it, so the state is the
#: meeting's own and the ``meeting`` link is present.
OCCURRENCE_INSTANTIATED: dict[str, Any] = {
    "_type": "MeetingOccurrence",
    "startTime": "2026-08-17T07:00:00Z",
    "state": "open",
    "_links": {
        "self": {
            "href": f"/api/v3/recurring_meetings/{RECURRING_MEETING_ID}"
            "/occurrences/2026-08-17T07:00:00Z"
        },
        "recurringMeeting": {"href": f"/api/v3/recurring_meetings/{RECURRING_MEETING_ID}"},
        "meeting": {
            "href": f"/api/v3/meetings/{OCCURRENCE_MEETING_ID}",
            "title": "Weekly team sync",
        },
    },
}

#: A slot that exists only in the schedule: synthetic ``planned`` state, and no
#: ``meeting`` link until somebody instantiates it.
OCCURRENCE_PLANNED: dict[str, Any] = {
    "_type": "MeetingOccurrence",
    "startTime": "2026-08-24T07:00:00Z",
    "state": "planned",
    "_links": {
        "self": {
            "href": f"/api/v3/recurring_meetings/{RECURRING_MEETING_ID}"
            "/occurrences/2026-08-24T07:00:00Z"
        },
        "recurringMeeting": {"href": f"/api/v3/recurring_meetings/{RECURRING_MEETING_ID}"},
    },
}

UPCOMING_OCCURRENCES: dict[str, Any] = hal_collection([OCCURRENCE_INSTANTIATED, OCCURRENCE_PLANNED])

#: What ``POST /recurring_meetings`` echoes: the requested 'Europe/Berlin' zone
#: has been silently overwritten with the API account's own ('Etc/UTC') — the
#: trap the create tool detects and corrects with a PATCH.
CREATED_SERIES: dict[str, Any] = {
    "_type": "RecurringMeeting",
    "id": CREATED_SERIES_ID,
    "title": "Design review",
    "startTime": "2026-09-01T07:00:00Z",
    "timeZone": "Etc/UTC",
    "frequency": "weekly",
    "interval": 2,
    "endAfter": "specific_date",
    "endDate": "2026-12-31",
    "duration": 0.75,
    "location": "Room 2.14",
    "_links": {
        "self": {
            "href": f"/api/v3/recurring_meetings/{CREATED_SERIES_ID}",
            "title": "Design review",
        },
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": dict(_GRACE),
        "template": {
            "href": f"/api/v3/meetings/{CREATED_TEMPLATE_MEETING_ID}",
            "title": "Design review",
        },
    },
}

#: The same series after the corrective ``PATCH {"timeZone": ...}``.
CREATED_SERIES_ZONE_FIXED: dict[str, Any] = {**CREATED_SERIES, "timeZone": "Europe/Berlin"}

#: A create whose requested zone happens to equal the account's — no fix-up.
CREATED_SERIES_UTC: dict[str, Any] = dict(CREATED_SERIES)

#: The full Meeting representer occurrence init answers (201): the template
#: copy, indistinguishable in shape from a ``GET /meetings/{id}``.
OCCURRENCE_MEETING: dict[str, Any] = {
    **MEETING,
    "id": OCCURRENCE_MEETING_ID,
    "title": "Weekly team sync",
    "startTime": "2026-08-17T07:00:00Z",
    "endTime": "2026-08-17T08:30:00Z",
    "state": "open",
    "_links": {
        **MEETING["_links"],
        "self": {
            "href": f"/api/v3/meetings/{OCCURRENCE_MEETING_ID}",
            "title": "Weekly team sync",
        },
    },
}

#: The 409 cancel answers once the occurrence is instantiated and not cancelled.
CANCEL_CONFLICT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Cannot cancel an already instantiated occurrence. Delete the meeting instead.",
}

#: The 422 a past ``startTime`` comes back as (CreateContract#start_time_constraints).
SERIES_START_REJECTED: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Start date must be after today.",
    "_embedded": {"details": {"attribute": "startDate"}},
}
