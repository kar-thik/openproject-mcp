"""Golden payloads for the notification and time-tracking tools (SPEC §6.8, §6.9).

Trimmed from real ``GET /notifications``, ``GET /time_entries`` and
``POST /time_entries/form`` responses of an OpenProject 16.6 instance. Nothing
here is invented: the link spellings (``readIAN``, ``entityType``,
``time_entries/activities``) and the duration format are what the wire carries.
"""

from __future__ import annotations

from typing import Any

WORK_PACKAGE_ID = 1234
PROJECT_ID = 7
TIME_ENTRY_ID = 88

# --- notifications ---------------------------------------------------------

NOTIFICATION_MENTIONED: dict[str, Any] = {
    "_type": "Notification",
    "id": 4711,
    "reason": "mentioned",
    "readIAN": False,
    "createdAt": "2026-07-20T08:30:00Z",
    "updatedAt": "2026-07-20T08:30:00Z",
    "_links": {
        "self": {"href": "/api/v3/notifications/4711"},
        "actor": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Client layer"},
        "activity": {"href": "/api/v3/activities/502"},
        "resource": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "readIAN": {"href": "/api/v3/notifications/4711/read_ian", "method": "post"},
    },
}

NOTIFICATION_DATE_ALERT: dict[str, Any] = {
    "_type": "Notification",
    "id": 4712,
    "reason": "dateAlert",
    "readIAN": False,
    "createdAt": "2026-07-21T06:00:00Z",
    "updatedAt": "2026-07-21T06:00:00Z",
    "_links": {
        "self": {"href": "/api/v3/notifications/4712"},
        # System-generated: no actor at all.
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Client layer"},
        "resource": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
    },
}

#: A notification about something that is not a work package, with the resource
#: inlined the way OpenProject does when it can.
NOTIFICATION_WIKI: dict[str, Any] = {
    "_type": "Notification",
    "id": 4713,
    "reason": "watched",
    "readIAN": True,
    "createdAt": "2026-07-22T09:45:00Z",
    "updatedAt": "2026-07-22T10:00:00Z",
    "_links": {
        "self": {"href": "/api/v3/notifications/4713"},
        "actor": {"href": "/api/v3/users/13", "title": "Alan Turing"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Client layer"},
        "resource": {"href": "/api/v3/wiki_pages/44", "title": "Release checklist"},
    },
    "_embedded": {
        "resource": {
            "_type": "WikiPage",
            "id": 44,
            "title": "Release checklist",
            "_links": {"self": {"href": "/api/v3/wiki_pages/44"}},
        }
    },
}

DATE_ALERT_FILTER_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Filters Reason filter has invalid values.",
}

NOTIFICATION_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

MARK_VALIDATION_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Ids is not a valid notification filter value.",
    "_embedded": {"details": {"attribute": "ids"}},
}


def notification_collection(
    elements: list[dict[str, Any]],
    *,
    total: int | None = None,
    offset: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    return {
        "_type": "Collection",
        "total": len(elements) if total is None else total,
        "count": len(elements),
        "pageSize": page_size,
        "offset": offset,
        "_embedded": {"elements": elements},
        "_links": {"self": {"href": "/api/v3/notifications"}},
    }


NOTIFICATION_PAGE: dict[str, Any] = notification_collection(
    [NOTIFICATION_MENTIONED, NOTIFICATION_DATE_ALERT, NOTIFICATION_WIKI], total=37
)

# --- time entries ----------------------------------------------------------


def time_entry(
    entry_id: int,
    *,
    hours: str = "PT1H",
    spent_on: str = "2026-07-20",
    comment: str = "Client layer work.",
    activity_id: int = 3,
    activity_name: str = "Development",
    work_package_id: int | None = WORK_PACKAGE_ID,
    lock_version: int | None = None,
) -> dict[str, Any]:
    """One TimeEntry resource, shaped exactly as the API returns it."""
    links: dict[str, Any] = {
        "self": {"href": f"/api/v3/time_entries/{entry_id}"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Client layer"},
        "user": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        "activity": {
            "href": f"/api/v3/time_entries/activities/{activity_id}",
            "title": activity_name,
        },
    }
    if work_package_id is not None:
        links["workPackage"] = {
            "href": f"/api/v3/work_packages/{work_package_id}",
            "title": "Ship the client layer",
        }
    payload: dict[str, Any] = {
        "_type": "TimeEntry",
        "id": entry_id,
        "hours": hours,
        "spentOn": spent_on,
        "comment": {"format": "plain", "raw": comment, "html": f"<p>{comment}</p>"},
        "createdAt": "2026-07-20T17:00:00Z",
        "updatedAt": "2026-07-20T17:00:00Z",
        "_links": links,
    }
    if lock_version is not None:
        payload["lockVersion"] = lock_version
    return payload


TIME_ENTRY_LONG: dict[str, Any] = time_entry(TIME_ENTRY_ID, hours="PT7H30M")
TIME_ENTRY_SHORT: dict[str, Any] = time_entry(
    89, hours="PT1H15M", activity_id=1, activity_name="Management", comment="Standup."
)
TIME_ENTRY_PROJECT_LEVEL: dict[str, Any] = time_entry(
    90, hours="PT2H", work_package_id=None, comment="Sprint planning."
)

TIME_ENTRY_WITH_LOCK: dict[str, Any] = time_entry(TIME_ENTRY_ID, hours="PT7H30M", lock_version=3)
TIME_ENTRY_UPDATED: dict[str, Any] = time_entry(
    TIME_ENTRY_ID, hours="PT2H", comment="Corrected.", lock_version=4
)


def time_entry_collection(
    elements: list[dict[str, Any]],
    *,
    total: int | None = None,
    offset: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    return {
        "_type": "Collection",
        "total": len(elements) if total is None else total,
        "count": len(elements),
        "pageSize": page_size,
        "offset": offset,
        "_embedded": {"elements": elements},
        "_links": {"self": {"href": "/api/v3/time_entries"}},
    }


TIME_ENTRY_PAGE: dict[str, Any] = time_entry_collection(
    [TIME_ENTRY_LONG, TIME_ENTRY_SHORT, TIME_ENTRY_PROJECT_LEVEL], total=3
)

#: The activities this instance offers, as the form's schema lists them.
ACTIVITY_ALLOWED_VALUES: list[dict[str, Any]] = [
    {"href": "/api/v3/time_entries/activities/1", "title": "Management"},
    {"href": "/api/v3/time_entries/activities/3", "title": "Development"},
    {"href": "/api/v3/time_entries/activities/4", "title": "Specification"},
]


def time_entry_form(
    *,
    activity_id: int | None = 1,
    validation_errors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A ``POST /time_entries/form`` response with defaults and allowed values."""
    links: dict[str, Any] = {
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}"},
        "workPackage": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"},
    }
    if activity_id is not None:
        links["activity"] = {"href": f"/api/v3/time_entries/activities/{activity_id}"}
    return {
        "_type": "Form",
        "_embedded": {
            "payload": {
                "_type": "TimeEntry",
                # The instance default the form fills in when we send none.
                "ongoing": False,
                "_links": links,
            },
            "schema": {
                "_type": "Schema",
                "activity": {
                    "type": "TimeEntriesActivity",
                    "name": "Activity",
                    "required": True,
                    "writable": True,
                    "_links": {"allowedValues": ACTIVITY_ALLOWED_VALUES},
                },
            },
            "validationErrors": validation_errors or {},
        },
        "_links": {
            "self": {"href": "/api/v3/time_entries/form"},
            "validate": {"href": "/api/v3/time_entries/form"},
            "commit": {"href": "/api/v3/time_entries"},
        },
    }


TIME_ENTRY_FORM: dict[str, Any] = time_entry_form()
TIME_ENTRY_FORM_DEVELOPMENT: dict[str, Any] = time_entry_form(activity_id=3)

#: The form's own rejection: the date sits in a closed cost-reporting period.
TIME_ENTRY_FORM_INVALID: dict[str, Any] = time_entry_form(
    validation_errors={
        "spentOn": {
            "_type": "Error",
            "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
            "message": "Date is locked and cannot be edited.",
            "_embedded": {"details": {"attribute": "spentOn"}},
        }
    }
)

CREATED_TIME_ENTRY: dict[str, Any] = time_entry(
    91, hours="PT1H30M", spent_on="2026-07-21", comment="Wrote the retry policy."
)

TIME_ENTRY_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

TIME_ENTRY_VALIDATION_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Hours must be greater than 0.",
    "_embedded": {"details": {"attribute": "hours"}},
}

TIME_ENTRY_CONFLICT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Your changes could not be saved, because the entry was updated.",
}

#: What an instance too old for ``entityId`` answers the probe with (SPEC §4.7).
INVALID_FILTER_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Filters Entity id filter does not exist.",
}
