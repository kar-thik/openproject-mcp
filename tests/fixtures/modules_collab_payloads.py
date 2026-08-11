"""Golden HAL payloads for the collaboration-module tools (SPEC §6.13).

Trimmed from OpenProject 16.x/17.x responses and kept as Python literals so the
suite stays offline and diffable. Only the fields the projections read are kept,
plus the ones that break a naive parser:

* a meeting whose participants are rendered both as ``_links.participants`` and
  as ``_embedded.participants``;
* an agenda item that links a work package the account may not see, which the
  API renders as the ``urn:…:undisclosed`` href rather than omitting the link;
* an agenda item carrying an embedded outcome;
* the 404 an uninstalled module answers, next to the 403 an installed one gives
  a user without permission.
"""

from __future__ import annotations

from typing import Any

MEETING_ID = 42
PAST_MEETING_ID = 41
PROJECT_ID = 7
PROJECT_IDENTIFIER = "apollo-migration"
WORK_PACKAGE_ID = 1234
WIKI_PAGE_ID = 501
DOCUMENT_ID = 8
CREATED_MEETING_ID = 61
CREATED_AGENDA_ITEM_ID = 94
OUTCOME_ID = 12
CREATED_OUTCOME_ID = 13


def hal_collection(elements: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Wrap elements in the HAL collection envelope OpenProject returns."""
    return {
        "_type": "Collection",
        "total": extra.pop("total", len(elements)),
        "count": len(elements),
        "_embedded": {"elements": elements},
        **extra,
    }


# --- meetings -------------------------------------------------------------

_GRACE = {"href": "/api/v3/users/3", "title": "Grace Hopper"}
_ALAN = {"href": "/api/v3/users/5", "title": "Alan Turing"}

#: An upcoming meeting: participants arrive as links *and* embedded resources.
MEETING: dict[str, Any] = {
    "_type": "Meeting",
    "id": MEETING_ID,
    "title": "Sprint 12 planning",
    "location": "Room 2.14",
    "lockVersion": 3,
    "startTime": "2026-08-03T14:00:00Z",
    "endTime": "2026-08-03T15:30:00Z",
    "duration": "PT1H30M",
    "state": "open",
    "template": False,
    "notify": False,
    "createdAt": "2026-07-20T09:00:00Z",
    "updatedAt": "2026-07-26T11:15:00Z",
    "_links": {
        "self": {"href": f"/api/v3/meetings/{MEETING_ID}", "title": "Sprint 12 planning"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": dict(_GRACE),
        "participants": [dict(_GRACE), dict(_ALAN)],
        "agendaItems": {"href": f"/api/v3/meetings/{MEETING_ID}/agenda_items"},
    },
    "_embedded": {
        "participants": [
            {"_type": "User", "id": 3, "name": "Grace Hopper"},
            {"_type": "User", "id": 5, "name": "Alan Turing"},
        ]
    },
}

#: A finished meeting, for the ``upcoming_only=False`` listing.
PAST_MEETING: dict[str, Any] = {
    "_type": "Meeting",
    "id": PAST_MEETING_ID,
    "title": "Retro 11",
    "location": "https://meet.example.test/retro-11",
    "startTime": "2026-07-13T10:00:00Z",
    "endTime": "2026-07-13T11:00:00Z",
    "duration": "PT1H",
    "state": "closed",
    "_links": {
        "self": {"href": f"/api/v3/meetings/{PAST_MEETING_ID}", "title": "Retro 11"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": dict(_ALAN),
        "participants": [],
    },
}

MEETING_COLLECTION: dict[str, Any] = hal_collection(
    [MEETING, PAST_MEETING], total=37, pageSize=20, offset=1
)

#: A meeting whose participants only arrive embedded (older renderings).
MEETING_EMBEDDED_PARTICIPANTS_ONLY: dict[str, Any] = {
    **MEETING,
    "_links": {key: value for key, value in MEETING["_links"].items() if key != "participants"},
}

#: What ``PATCH /meetings/{id}`` echoes back: the moved meeting, lockVersion bumped.
UPDATED_MEETING: dict[str, Any] = {
    **MEETING,
    "title": "Sprint 12 planning (moved)",
    "startTime": "2026-08-04T14:00:00Z",
    "endTime": "2026-08-04T15:00:00Z",
    "duration": "PT1H",
    "lockVersion": 4,
}

#: The fresh state a 409 re-read returns: someone else renamed the meeting first.
FRESH_MEETING_AFTER_CONFLICT: dict[str, Any] = {
    **MEETING,
    "title": "Sprint 12 planning (rescheduled)",
    "lockVersion": 7,
}

OUTCOME: dict[str, Any] = {
    "_type": "MeetingOutcome",
    "id": 12,
    "kind": "decision",
    "notes": {
        "format": "markdown",
        "raw": "Ship on Friday, feature flag stays off.",
        "html": "<p>Ship on Friday, feature flag stays off.</p>",
    },
    "_links": {
        "self": {"href": "/api/v3/meeting_outcomes/12", "title": "12"},
        "author": dict(_GRACE),
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
    },
}

AGENDA_ITEM_SIMPLE: dict[str, Any] = {
    "_type": "MeetingAgendaItem",
    "id": 91,
    "title": "Capacity check",
    "notes": {
        "format": "markdown",
        "raw": "Two people on vacation in week 33.",
        "html": "<p>Two people on vacation in week 33.</p>",
    },
    "position": 1,
    "durationInMinutes": 15,
    "itemType": "simple",
    "lockVersion": 0,
    "_links": {
        "self": {"href": "/api/v3/meeting_agenda_items/91", "title": "Capacity check"},
        "meeting": {"href": f"/api/v3/meetings/{MEETING_ID}", "title": "Sprint 12 planning"},
        "author": dict(_GRACE),
        "presenter": dict(_ALAN),
        "section": {"href": "/api/v3/meeting_sections/6", "title": "Agenda"},
    },
}

#: The work-package flavour: no title of its own, an embedded outcome.
AGENDA_ITEM_WORK_PACKAGE: dict[str, Any] = {
    "_type": "MeetingAgendaItem",
    "id": 92,
    "title": "",
    "notes": {"format": "markdown", "raw": "", "html": ""},
    "position": 2,
    "durationInMinutes": 30,
    "itemType": "work_package",
    "_links": {
        "self": {"href": "/api/v3/meeting_agenda_items/92"},
        "meeting": {"href": f"/api/v3/meetings/{MEETING_ID}", "title": "Sprint 12 planning"},
        "author": dict(_GRACE),
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "section": {"href": "/api/v3/meeting_sections/6", "title": "Agenda"},
        "outcomes": [{"href": "/api/v3/meeting_outcomes/12", "title": "12"}],
    },
    "_embedded": {"outcomes": [OUTCOME]},
}

#: The linked work package is invisible to this account: OpenProject renders the
#: undisclosed URN instead of an href, so no id may be surfaced.
AGENDA_ITEM_UNDISCLOSED: dict[str, Any] = {
    "_type": "MeetingAgendaItem",
    "id": 93,
    "title": "",
    "position": 3,
    "itemType": "work_package",
    "_links": {
        "self": {"href": "/api/v3/meeting_agenda_items/93"},
        "meeting": {"href": f"/api/v3/meetings/{MEETING_ID}", "title": "Sprint 12 planning"},
        "workPackage": {"href": "urn:openproject-org:api:v3:undisclosed"},
    },
}

AGENDA_ITEM_COLLECTION: dict[str, Any] = hal_collection(
    [AGENDA_ITEM_SIMPLE, AGENDA_ITEM_WORK_PACKAGE, AGENDA_ITEM_UNDISCLOSED]
)

#: ``POST /meetings/form`` with nothing to complain about: OpenProject echoes the
#: payload it would commit, with the instance defaults filled in.
MEETING_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {
            "title": "Design review",
            "startTime": "2026-09-01T09:00:00Z",
            "duration": "PT45M",
            "location": "",
            "state": "draft",
            "_links": {"project": {"href": f"/api/v3/projects/{PROJECT_ID}"}},
        },
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
    "_links": {"self": {"href": "/api/v3/meetings/form"}},
}

MEETING_FORM_BLANK_TITLE: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"title": "", "_links": {"project": {"href": f"/api/v3/projects/{PROJECT_ID}"}}},
        "schema": {"_type": "Schema"},
        "validationErrors": {
            "title": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Title can't be blank.",
                "_embedded": {"details": {"attribute": "title"}},
            }
        },
    },
    "_links": {"self": {"href": "/api/v3/meetings/form"}},
}

CREATED_MEETING: dict[str, Any] = {
    "_type": "Meeting",
    "id": CREATED_MEETING_ID,
    "title": "Design review",
    "location": "",
    "startTime": "2026-09-01T09:00:00Z",
    "endTime": "2026-09-01T09:45:00Z",
    "duration": "PT45M",
    "state": "draft",
    "createdAt": "2026-07-26T12:00:00Z",
    "updatedAt": "2026-07-26T12:00:00Z",
    "_links": {
        "self": {"href": f"/api/v3/meetings/{CREATED_MEETING_ID}", "title": "Design review"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": dict(_GRACE),
        "participants": [dict(_GRACE), dict(_ALAN)],
    },
}

#: What ``PATCH /meeting_agenda_items/{id}`` echoes back for the simple item.
UPDATED_AGENDA_ITEM: dict[str, Any] = {
    **AGENDA_ITEM_SIMPLE,
    "title": "Capacity check (final)",
    "durationInMinutes": 20,
    "position": 2,
    "lockVersion": 1,
}

CREATED_AGENDA_ITEM: dict[str, Any] = {
    "_type": "MeetingAgendaItem",
    "id": CREATED_AGENDA_ITEM_ID,
    "title": "Release readiness",
    "notes": {
        "format": "markdown",
        "raw": "Go / no-go for 2.1.",
        "html": "<p>Go / no-go for 2.1.</p>",
    },
    "position": 4,
    "durationInMinutes": 15,
    "itemType": "work_package",
    "createdAt": "2026-07-26T12:05:00Z",
    "_links": {
        "self": {
            "href": f"/api/v3/meeting_agenda_items/{CREATED_AGENDA_ITEM_ID}",
            "title": "Release readiness",
        },
        "meeting": {"href": f"/api/v3/meetings/{MEETING_ID}", "title": "Sprint 12 planning"},
        "author": dict(_GRACE),
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
    },
}

#: What ``POST /meeting_outcomes`` answers: a decision with its follow-up ticket.
CREATED_OUTCOME: dict[str, Any] = {
    "_type": "MeetingOutcome",
    "id": CREATED_OUTCOME_ID,
    "kind": "decision",
    "notes": {"format": "markdown", "raw": "Ship on Friday.", "html": "<p>Ship on Friday.</p>"},
    "_links": {
        "self": {"href": f"/api/v3/meeting_outcomes/{CREATED_OUTCOME_ID}"},
        "author": dict(_GRACE),
        "agendaItem": {"href": "/api/v3/meeting_agenda_items/92"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
    },
}

#: What ``PATCH /meeting_outcomes/{id}`` echoes back after a correction.
UPDATED_OUTCOME: dict[str, Any] = {
    **OUTCOME,
    "kind": "information",
    "notes": {
        "format": "markdown",
        "raw": "Deferred to next sprint.",
        "html": "<p>Deferred to next sprint.</p>",
    },
    "_links": {**OUTCOME["_links"], "agendaItem": {"href": "/api/v3/meeting_agenda_items/92"}},
}


# --- wiki, documents, budgets --------------------------------------------

WIKI_PAGE: dict[str, Any] = {
    "_type": "WikiPage",
    "id": WIKI_PAGE_ID,
    "title": "Deployment runbook",
    "_links": {
        "self": {"href": f"/api/v3/wiki_pages/{WIKI_PAGE_ID}"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "attachments": {"href": f"/api/v3/wiki_pages/{WIKI_PAGE_ID}/attachments"},
    },
}

DOCUMENT: dict[str, Any] = {
    "_type": "Document",
    "id": DOCUMENT_ID,
    "title": "Architecture decision record 4",
    "description": {
        "format": "markdown",
        "raw": "We keep HAL parsing in one module.",
        "html": "<p>We keep HAL parsing in one module.</p>",
    },
    "createdAt": "2026-05-04T08:00:00Z",
    "updatedAt": "2026-06-11T16:20:00Z",
    "_links": {
        "self": {
            "href": f"/api/v3/documents/{DOCUMENT_ID}",
            "title": "Architecture decision record 4",
        },
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "attachments": {"href": f"/api/v3/documents/{DOCUMENT_ID}/attachments"},
    },
}

OTHER_DOCUMENT: dict[str, Any] = {
    "_type": "Document",
    "id": 9,
    "title": "Vendor contract",
    "description": {"format": "markdown", "raw": "", "html": ""},
    "createdAt": "2026-06-30T13:00:00Z",
    "updatedAt": "2026-06-30T13:00:00Z",
    "_links": {
        "self": {"href": "/api/v3/documents/9", "title": "Vendor contract"},
        "project": {"href": "/api/v3/projects/3", "title": "Customer work"},
    },
}

DOCUMENT_COLLECTION: dict[str, Any] = hal_collection(
    [DOCUMENT, OTHER_DOCUMENT], total=45, pageSize=20, offset=1
)

BUDGET_COLLECTION: dict[str, Any] = hal_collection(
    [
        {
            "_type": "Budget",
            "id": 4,
            "subject": "2026 platform budget",
            "_links": {"self": {"href": "/api/v3/budgets/4", "title": "2026 platform budget"}},
        },
        {
            "_type": "Budget",
            "id": 5,
            "subject": "Hardware refresh",
            "_links": {"self": {"href": "/api/v3/budgets/5", "title": "Hardware refresh"}},
        },
    ]
)


# --- errors ---------------------------------------------------------------

#: What an uninstalled (or project-disabled) module answers.
MODULE_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

#: What an installed module answers a user without the read permission.
MODULE_FORBIDDEN: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission",
    "message": "You are not authorized to access this resource.",
}

#: The 400 an instance without the meetings sort order answers.
INVALID_SORT_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Sort criteria startTime is not supported.",
}

#: The 400 a pre-17.6 instance sends for the value-less ``upcoming`` operator
#: (captured live from OpenProject 16.6.10).
INVALID_TIME_FILTER_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Filters Start time is not set to one of the allowed values. and is invalid.",
}

#: The 422 a rejected agenda item comes back as.
AGENDA_ITEM_REJECTED: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Work package is invalid.",
    "_embedded": {"details": {"attribute": "workPackage"}},
}

#: The 409 a stale lockVersion answers on meetings and agenda items.
STALE_LOCK_CONFLICT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Your changes could not be saved, because the resource was changed since you"
    " opened it.",
}

#: The 422 every write against a CLOSED meeting (or its agenda) answers.
MEETING_NOT_EDITABLE: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "The meeting is not editable anymore.",
}

#: The 422 an outcome write answers whenever the meeting is not 'in_progress'.
OUTCOME_NOT_EDITABLE: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "The outcome is not editable anymore.",
}
