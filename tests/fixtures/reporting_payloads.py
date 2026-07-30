"""Golden payloads for the reporting tool, the prompts and the resources.

Trimmed from real OpenProject 16.x responses:

* ``GET /projects/{id}`` and ``GET /statuses`` (the ``isClosed`` flags every
  bucket decision reads — deliberately including a *renamed* closed status so a
  keyword classifier would get it wrong).
* ``GET /projects/{id}/work_packages`` for the three report windows, for the
  ``groupBy=status`` open breakdown, for "due today" and for the backlog sweep.
* ``GET /time_entries``, ``GET /memberships``, ``GET /work_packages/{id}/relations``
  and ``GET /notifications``.
* ``GET /work_packages/{id}`` and ``GET /attachments/{id}`` for the resources.

:func:`work_package_response` is the respx side-effect that routes one mocked
work-package endpoint to the right window, because the three window reads differ
only by their filter payload.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

PROJECT_ID = 5
PROJECT_IDENTIFIER = "platform"
WORK_PACKAGE_ID = 1234
ATTACHMENT_ID = 77

FROM_DATE = "2026-07-01"
TO_DATE = "2026-07-07"


def hal_collection(elements: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Wrap elements in the HAL collection envelope OpenProject returns."""
    return {
        "_type": "Collection",
        "total": extra.pop("total", len(elements)),
        "count": len(elements),
        "_embedded": {"elements": elements},
        **extra,
    }


# --- project and statuses -------------------------------------------------

PROJECT: dict[str, Any] = {
    "_type": "Project",
    "id": PROJECT_ID,
    "identifier": PROJECT_IDENTIFIER,
    "name": "Platform",
    "active": True,
    "public": False,
    "description": {"format": "markdown", "raw": "The platform team's board.", "html": "<p>x</p>"},
    "statusExplanation": {"format": "markdown", "raw": "Shipping on time.", "html": ""},
    "createdAt": "2026-01-05T08:00:00Z",
    "updatedAt": "2026-07-06T09:00:00Z",
    "_links": {
        "self": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Platform"},
        "status": {"href": "/api/v3/project_statuses/on_track", "title": "On track"},
        "parent": {"href": None},
    },
}

#: 'Shipped' and 'Rejected' are closed, and neither name contains "closed" or
#: "done" — the old keyword classifier put both in the wrong bucket.
STATUS_COLLECTION: dict[str, Any] = hal_collection(
    [
        {
            "_type": "Status",
            "id": 1,
            "name": "New",
            "isClosed": False,
            "_links": {"self": {"href": "/api/v3/statuses/1"}},
        },
        {
            "_type": "Status",
            "id": 7,
            "name": "In progress",
            "isClosed": False,
            "_links": {"self": {"href": "/api/v3/statuses/7"}},
        },
        {
            "_type": "Status",
            "id": 12,
            "name": "Shipped",
            "isClosed": True,
            "_links": {"self": {"href": "/api/v3/statuses/12"}},
        },
        {
            "_type": "Status",
            "id": 14,
            "name": "Rejected",
            "isClosed": True,
            "_links": {"self": {"href": "/api/v3/statuses/14"}},
        },
    ]
)


# --- work packages --------------------------------------------------------


def work_package_element(
    work_package_id: int,
    subject: str,
    *,
    status: tuple[int, str],
    type_ref: tuple[int, str] = (1, "Task"),
    assignee: tuple[int, str] | None = None,
    due_date: str | None = None,
    updated_at: str = "2026-07-06T09:00:00Z",
    created_at: str = "2026-07-01T08:00:00Z",
    estimated_time: str | None = None,
) -> dict[str, Any]:
    """One work-package element as the project collection carries it."""
    links: dict[str, Any] = {
        "self": {"href": f"/api/v3/work_packages/{work_package_id}", "title": subject},
        "type": {"href": f"/api/v3/types/{type_ref[0]}", "title": type_ref[1]},
        "status": {"href": f"/api/v3/statuses/{status[0]}", "title": status[1]},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Platform"},
    }
    if assignee is not None:
        links["assignee"] = {"href": f"/api/v3/users/{assignee[0]}", "title": assignee[1]}
    element: dict[str, Any] = {
        "_type": "WorkPackage",
        "id": work_package_id,
        "subject": subject,
        "dueDate": due_date,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "_links": links,
    }
    if estimated_time is not None:
        element["estimatedTime"] = estimated_time
    return element


IN_PROGRESS = work_package_element(
    1234,
    "Ship the client layer",
    status=(7, "In progress"),
    assignee=(12, "Grace Hopper"),
    due_date="2026-07-31",
    updated_at="2026-07-06T09:00:00Z",
    estimated_time="PT8H",
)
#: Raised inside the window and untouched since — its ``updatedAt`` is still its
#: ``createdAt``, which is what makes it *Planned* rather than *In progress*.
NEW_BUG = work_package_element(
    1235,
    "Drop sentinel dates",
    status=(1, "New"),
    type_ref=(2, "Bug"),
    created_at="2026-07-02T07:30:00Z",
    updated_at="2026-07-02T07:30:00Z",
)
SHIPPED = work_package_element(
    1236,
    "Pool the httpx client",
    status=(12, "Shipped"),
    assignee=(1, "Ada Lovelace"),
    due_date="2026-07-03",
    created_at="2026-07-01T09:15:00Z",
    updated_at="2026-07-04T16:00:00Z",
)
REJECTED = work_package_element(
    1237,
    "Retire the legacy filter",
    status=(14, "Rejected"),
    assignee=(12, "Grace Hopper"),
    updated_at="2026-07-02T11:00:00Z",
)
DUE_TODAY = work_package_element(
    1238,
    "Publish the release notes",
    status=(7, "In progress"),
    assignee=(1, "Ada Lovelace"),
    due_date=TO_DATE,
)

CREATED_COLLECTION = hal_collection([NEW_BUG, SHIPPED], total=2, pageSize=100, offset=1)
#: Everything whose ``updatedAt`` falls inside the window, newest change first —
#: which necessarily includes every row of ``CREATED_COLLECTION``, because a row
#: created inside the window was also changed inside it. A fixture that omitted
#: those would contradict the ``updatedAt <>d`` filter the tool actually sends.
UPDATED_COLLECTION = hal_collection(
    [IN_PROGRESS, SHIPPED, REJECTED, NEW_BUG], total=4, pageSize=100, offset=1
)
CLOSED_COLLECTION = hal_collection([SHIPPED, REJECTED], total=2, pageSize=100, offset=1)
DUE_TODAY_COLLECTION = hal_collection([DUE_TODAY], total=1, pageSize=100, offset=1)

#: The server-side ``groupBy=status`` answer: counts over the whole open set,
#: with one bucket sent as a link object and one as a bare string (both occur).
OPEN_GROUPED: dict[str, Any] = {
    "_type": "WorkPackageCollection",
    "total": 13,
    "count": 1,
    "pageSize": 1,
    "offset": 1,
    "groups": [
        {"value": {"href": "/api/v3/statuses/7", "title": "In progress"}, "count": 4},
        {"value": "New", "count": 9},
    ],
    "_embedded": {"elements": [IN_PROGRESS]},
}


def backlog_collection(recent_updated_at: str) -> dict[str, Any]:
    """The open set, oldest-changed first, for the backlog sweep.

    ``recent_updated_at`` is supplied by the test so "stale" stays deterministic
    however far the calendar has moved.
    """
    stale = work_package_element(
        1235,
        "Drop sentinel dates",
        status=(1, "New"),
        type_ref=(2, "Bug"),
        updated_at="2025-01-04T08:00:00Z",
    )
    fresh = work_package_element(
        1234,
        "Ship the client layer",
        status=(7, "In progress"),
        assignee=(12, "Grace Hopper"),
        due_date="2026-07-31",
        updated_at=recent_updated_at,
        estimated_time="PT8H",
    )
    return hal_collection([stale, fresh], total=2, pageSize=100, offset=1)


def work_package_response(request: httpx.Request) -> httpx.Response:
    """Route one mocked ``…/work_packages`` endpoint to the right window.

    The three window reads differ only by their filter payload, so the router
    keys off the filter names rather than off separate respx routes.
    """
    params = request.url.params
    if "groupBy" in params:
        return httpx.Response(200, json=OPEN_GROUPED)
    entries = json.loads(params.get("filters", "[]"))
    names = {name for entry in entries for name in entry}
    operators = {
        entry[name]["operator"] for entry in entries for name in entry if name == "status"
    }
    if "dueDate" in names:
        return httpx.Response(200, json=DUE_TODAY_COLLECTION)
    if "createdAt" in names:
        return httpx.Response(200, json=CREATED_COLLECTION)
    if "updatedAt" in names:
        if "c" in operators:
            return httpx.Response(200, json=CLOSED_COLLECTION)
        return httpx.Response(200, json=UPDATED_COLLECTION)
    return httpx.Response(200, json=hal_collection([], total=0))


# --- time entries ---------------------------------------------------------


def time_entry_element(
    entry_id: int,
    hours: str,
    *,
    spent_on: str,
    activity: tuple[int, str],
    user: tuple[int, str],
) -> dict[str, Any]:
    return {
        "_type": "TimeEntry",
        "id": entry_id,
        "hours": hours,
        "spentOn": spent_on,
        "comment": {"format": "plain", "raw": "", "html": ""},
        "_links": {
            "self": {"href": f"/api/v3/time_entries/{entry_id}"},
            "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Platform"},
            "activity": {
                "href": f"/api/v3/time_entries/activities/{activity[0]}",
                "title": activity[1],
            },
            "user": {"href": f"/api/v3/users/{user[0]}", "title": user[1]},
        },
    }


TIME_ENTRY_COLLECTION = hal_collection(
    [
        time_entry_element(
            901,
            "PT4H",
            spent_on="2026-07-02",
            activity=(3, "Development"),
            user=(12, "Grace Hopper"),
        ),
        time_entry_element(
            902,
            "PT2H",
            spent_on="2026-07-03",
            activity=(3, "Development"),
            user=(1, "Ada Lovelace"),
        ),
        time_entry_element(
            903,
            "PT1H30M",
            spent_on="2026-07-06",
            activity=(4, "Management"),
            user=(1, "Ada Lovelace"),
        ),
    ],
    total=3,
    pageSize=100,
    offset=1,
)


# --- memberships ----------------------------------------------------------

MEMBERSHIP_COLLECTION = hal_collection(
    [
        {
            "_type": "Membership",
            "id": 61,
            "createdAt": "2026-01-06T08:00:00Z",
            "_links": {
                "self": {"href": "/api/v3/memberships/61"},
                "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Platform"},
                "principal": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
                "roles": [{"href": "/api/v3/roles/3", "title": "Member"}],
            },
        },
        {
            "_type": "Membership",
            "id": 62,
            "createdAt": "2026-01-06T08:05:00Z",
            "_links": {
                "self": {"href": "/api/v3/memberships/62"},
                "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Platform"},
                "principal": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
                "roles": [
                    {"href": "/api/v3/roles/4", "title": "Project admin"},
                    {"href": "/api/v3/roles/3", "title": "Member"},
                ],
            },
        },
    ],
    total=2,
    pageSize=100,
    offset=1,
)


# --- relations (impediments) ----------------------------------------------

BLOCKED_RELATION_COLLECTION = hal_collection(
    [
        {
            "_type": "Relation",
            "id": 501,
            "type": "blocked",
            "reverseType": "blocks",
            "lag": None,
            "description": "Waiting on the infrastructure ticket.",
            "_links": {
                "self": {"href": "/api/v3/relations/501"},
                "from": {"href": "/api/v3/work_packages/1234", "title": "Ship the client layer"},
                "to": {"href": "/api/v3/work_packages/1240", "title": "Provision the CI runners"},
            },
        }
    ],
    total=1,
)

EMPTY_RELATION_COLLECTION = hal_collection([], total=0)


# --- notifications --------------------------------------------------------


def notification_element(
    notification_id: int, reason: str, *, resource_id: int, subject: str
) -> dict[str, Any]:
    return {
        "_type": "Notification",
        "id": notification_id,
        "reason": reason,
        "readIAN": False,
        "updatedAt": "2026-07-06T09:30:00Z",
        "_links": {
            "self": {"href": f"/api/v3/notifications/{notification_id}"},
            "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Platform"},
            "resource": {"href": f"/api/v3/work_packages/{resource_id}", "title": subject},
        },
    }


NOTIFICATION_COLLECTION = hal_collection(
    [
        notification_element(21, "mentioned", resource_id=1234, subject="Ship the client layer"),
        notification_element(22, "mentioned", resource_id=1235, subject="Drop sentinel dates"),
        notification_element(23, "assigned", resource_id=1238, subject="Publish the release notes"),
    ],
    total=3,
    pageSize=100,
    offset=1,
)


# --- resources ------------------------------------------------------------

WORK_PACKAGE_DETAIL: dict[str, Any] = {
    "_type": "WorkPackage",
    "id": WORK_PACKAGE_ID,
    "subject": "Ship the client layer",
    "description": {
        "format": "markdown",
        "raw": "Pool the httpx client and add retries.",
        "html": "<p>Pool the httpx client and add retries.</p>",
    },
    "startDate": "2026-07-01",
    "dueDate": "2026-07-31",
    "percentageDone": 40,
    "estimatedTime": "PT8H",
    "spentTime": "PT6H30M",
    "createdAt": "2026-07-01T08:00:00Z",
    "updatedAt": "2026-07-06T09:00:00Z",
    "lockVersion": 9,
    "_links": {
        "self": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"},
        "type": {"href": "/api/v3/types/1", "title": "Task"},
        "status": {"href": "/api/v3/statuses/7", "title": "In progress"},
        "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
        "assignee": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "author": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Platform"},
        "version": {"href": "/api/v3/versions/3", "title": "Sprint 12"},
        "revisions": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/revisions"},
        "fileLinks": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/file_links"},
    },
}

ATTACHMENT_BYTES = b"\x89PNG\r\n\x1a\n burndown"

#: ``fileSize`` is the real length of :data:`ATTACHMENT_BYTES`: the declared size
#: is what the resource's pre-flight cap reads, so a fixture that disagreed with
#: its own body could pass a size guard the live instance would fail.
ATTACHMENT_METADATA: dict[str, Any] = {
    "_type": "Attachment",
    "id": ATTACHMENT_ID,
    "fileName": "burndown.png",
    "fileSize": len(ATTACHMENT_BYTES),
    "contentType": "image/png",
    "status": "scanned",
    "description": {"format": "plain", "raw": "Sprint burndown", "html": ""},
    "createdAt": "2026-07-06T10:00:00Z",
    "_links": {
        "self": {"href": f"/api/v3/attachments/{ATTACHMENT_ID}"},
        "author": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "container": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "downloadLocation": {"href": f"/api/v3/attachments/{ATTACHMENT_ID}/content"},
    },
}

QUARANTINED_ATTACHMENT: dict[str, Any] = {
    **ATTACHMENT_METADATA,
    "id": 78,
    "fileName": "suspicious.zip",
    "contentType": "application/zip",
    "status": "quarantined",
    "_links": {
        **ATTACHMENT_METADATA["_links"],
        "self": {"href": "/api/v3/attachments/78"},
    },
}


# --- errors ---------------------------------------------------------------

NOT_FOUND_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

FORBIDDEN_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission",
    "message": "You are not authorized to access this resource.",
}
