"""Golden HAL payloads for the work-package core tools, trimmed from 17.x.

Kept as Python literals so the suite stays offline and diffable. Anything the
whole suite shares lives in ``hal_payloads``; this module only holds what the
``work_packages`` tools exercise.
"""

from __future__ import annotations

from typing import Any

PROJECT: dict[str, Any] = {
    "_type": "Project",
    "id": 5,
    "identifier": "demo",
    "name": "Demo project",
    "_links": {"self": {"href": "/api/v3/projects/5", "title": "Demo project"}},
}

TYPES: dict[str, Any] = {
    "_type": "Collection",
    "total": 3,
    "count": 3,
    "_embedded": {
        "elements": [
            {
                "_type": "Type",
                "id": 1,
                "name": "Task",
                "isMilestone": False,
                "_links": {"self": {"href": "/api/v3/types/1", "title": "Task"}},
            },
            {
                "_type": "Type",
                "id": 2,
                "name": "Bug",
                "isMilestone": False,
                "_links": {"self": {"href": "/api/v3/types/2", "title": "Bug"}},
            },
            {
                "_type": "Type",
                "id": 4,
                "name": "Milestone",
                "isMilestone": True,
                "_links": {"self": {"href": "/api/v3/types/4", "title": "Milestone"}},
            },
        ]
    },
}

STATUSES: dict[str, Any] = {
    "_type": "Collection",
    "total": 3,
    "count": 3,
    "_embedded": {
        "elements": [
            {
                "_type": "Status",
                "id": 1,
                "name": "New",
                "isClosed": False,
                "_links": {"self": {"href": "/api/v3/statuses/1", "title": "New"}},
            },
            {
                "_type": "Status",
                "id": 7,
                "name": "In progress",
                "isClosed": False,
                "_links": {"self": {"href": "/api/v3/statuses/7", "title": "In progress"}},
            },
            {
                "_type": "Status",
                "id": 12,
                "name": "Closed",
                "isClosed": True,
                "_links": {"self": {"href": "/api/v3/statuses/12", "title": "Closed"}},
            },
        ]
    },
}

PRIORITIES: dict[str, Any] = {
    "_type": "Collection",
    "total": 2,
    "count": 2,
    "_embedded": {
        "elements": [
            {
                "_type": "Priority",
                "id": 8,
                "name": "Normal",
                "_links": {"self": {"href": "/api/v3/priorities/8", "title": "Normal"}},
            },
            {
                "_type": "Priority",
                "id": 9,
                "name": "High",
                "_links": {"self": {"href": "/api/v3/priorities/9", "title": "High"}},
            },
        ]
    },
}

#: A work package with custom fields set, a schema link and the optional
#: module links that drive the ``available`` map.
WORK_PACKAGE_DETAIL: dict[str, Any] = {
    "_type": "WorkPackage",
    "id": 1234,
    "lockVersion": 7,
    "subject": "Ship the client layer",
    "description": {
        "format": "markdown",
        "raw": "Pooled httpx client with retries.",
        "html": "<p>Pooled httpx client with retries.</p>",
    },
    "startDate": "2026-07-01",
    "dueDate": "2026-07-31",
    "percentageDone": 40,
    "estimatedTime": "PT7H30M",
    "spentTime": "PT2H15M",
    "createdAt": "2026-07-01T09:00:00Z",
    "updatedAt": "2026-07-20T14:12:00Z",
    "customField7": "https://tickets.example.com/OP-1",
    "_links": {
        "self": {"href": "/api/v3/work_packages/1234", "title": "Ship the client layer"},
        "schema": {"href": "/api/v3/work_packages/schemas/5-1"},
        "project": {"href": "/api/v3/projects/5", "title": "Demo project"},
        "type": {"href": "/api/v3/types/1", "title": "Task"},
        "status": {"href": "/api/v3/statuses/7", "title": "In progress"},
        "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
        "assignee": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "author": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        "responsible": {"href": None},
        "parent": {"href": "/api/v3/work_packages/1000", "title": "Epic"},
        "version": {"href": "/api/v3/versions/3", "title": "Sprint 4"},
        "revisions": {"href": "/api/v3/work_packages/1234/revisions"},
        "fileLinks": {"href": "/api/v3/work_packages/1234/file_links"},
        "customField12": {"href": "/api/v3/custom_options/4", "title": "High"},
        "customField9": [
            {"href": "/api/v3/users/12", "title": "Grace Hopper"},
            {"href": "/api/v3/users/13", "title": "Alan Turing"},
        ],
        "customActions": [
            {"href": "/api/v3/custom_actions/3", "title": "Send to QA"},
            {"href": "/api/v3/custom_actions/4", "title": "Escalate"},
        ],
    },
}

WORK_PACKAGE_SCHEMA_5_1: dict[str, Any] = {
    "_type": "Schema",
    "subject": {"type": "String", "name": "Subject", "required": True, "writable": True},
    "customField7": {
        "type": "String",
        "name": "Ticket URL",
        "required": False,
        "writable": True,
    },
    "customField9": {
        "type": "[]User",
        "name": "Reviewers",
        "required": False,
        "writable": True,
        "_links": {
            "allowedValues": [
                {"href": "/api/v3/users/12", "title": "Grace Hopper"},
                {"href": "/api/v3/users/13", "title": "Alan Turing"},
            ]
        },
    },
    "customField12": {
        "type": "CustomOption",
        "name": "Severity",
        "required": False,
        "writable": True,
        "_links": {
            "allowedValues": [
                {"href": "/api/v3/custom_options/4", "title": "High"},
                {"href": "/api/v3/custom_options/5", "title": "Low"},
            ]
        },
    },
}


def _row(work_package_id: int, subject: str, status_id: int, status_name: str) -> dict[str, Any]:
    return {
        "_type": "WorkPackage",
        "id": work_package_id,
        "subject": subject,
        "startDate": "2026-07-02",
        "dueDate": "2026-07-09",
        "percentageDone": 10,
        "updatedAt": "2026-07-18T08:00:00Z",
        "_links": {
            "self": {"href": f"/api/v3/work_packages/{work_package_id}"},
            "project": {"href": "/api/v3/projects/5", "title": "Demo project"},
            "type": {"href": "/api/v3/types/1", "title": "Task"},
            "status": {"href": f"/api/v3/statuses/{status_id}", "title": status_name},
            "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
            "assignee": {"href": None},
        },
    }


SEARCH_RESULT: dict[str, Any] = {
    "_type": "WorkPackageCollection",
    "total": 2,
    "count": 2,
    "pageSize": 20,
    "offset": 1,
    "_embedded": {
        "elements": [
            WORK_PACKAGE_DETAIL,
            _row(1240, "Client layer retries", 12, "Closed"),
        ]
    },
}

GROUPED_LIST: dict[str, Any] = {
    "_type": "WorkPackageCollection",
    "total": 34,
    "count": 2,
    "pageSize": 20,
    "offset": 1,
    "totalSums": {"estimatedTime": "PT120H", "storyPoints": 21},
    "groups": [
        {"value": "In progress", "count": 12, "sums": {"estimatedTime": "PT41H30M"}},
        {"value": "New", "count": 22, "sums": {"estimatedTime": "PT78H30M"}},
    ],
    "_embedded": {
        "elements": [
            _row(1301, "Wire the filter builder", 7, "In progress"),
            _row(1302, "Cache the schemas", 1, "New"),
        ]
    },
}

#: 20 elements out of 42 — the truncation path for the ``children`` include.
CHILDREN_COLLECTION: dict[str, Any] = {
    "_type": "WorkPackageCollection",
    "total": 42,
    "count": 20,
    "pageSize": 20,
    "offset": 1,
    "_embedded": {
        "elements": [
            _row(2000 + index, f"Subtask {index}", 7, "In progress") for index in range(20)
        ]
    },
}

RELATIONS_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 1,
    "count": 1,
    "_embedded": {
        "elements": [
            {
                "_type": "Relation",
                "id": 55,
                "type": "blocks",
                "reverseType": "blocked",
                "lag": None,
                "description": "waiting on the client layer",
                "_links": {
                    "self": {"href": "/api/v3/relations/55"},
                    "from": {"href": "/api/v3/work_packages/1234", "title": "Ship the client"},
                    "to": {"href": "/api/v3/work_packages/1300", "title": "Ship the tools"},
                },
            }
        ]
    },
}

WATCHERS_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 1,
    "count": 1,
    "_embedded": {
        "elements": [
            {
                "_type": "User",
                "id": 12,
                "name": "Grace Hopper",
                "_links": {"self": {"href": "/api/v3/users/12", "title": "Grace Hopper"}},
            }
        ]
    },
}

ATTACHMENTS_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 1,
    "count": 1,
    "_embedded": {
        "elements": [
            {
                "_type": "Attachment",
                "id": 91,
                "fileName": "spec.pdf",
                "fileSize": 20480,
                "contentType": "application/pdf",
                "description": {"format": "plain", "raw": "signed off"},
                "createdAt": "2026-07-10T11:00:00Z",
                "_links": {
                    "self": {"href": "/api/v3/attachments/91"},
                    "author": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
                },
            }
        ]
    },
}

#: A create form that validated cleanly; ``payload`` carries instance defaults.
CREATE_FORM_OK: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {
            "subject": "Write the tools layer",
            "scheduleManually": False,
            "_links": {
                "project": {"href": "/api/v3/projects/5"},
                "type": {"href": "/api/v3/types/1"},
                "status": {"href": "/api/v3/statuses/7"},
                "priority": {"href": "/api/v3/priorities/8"},
            },
        },
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
    "_links": {"commit": {"href": "/api/v3/work_packages", "method": "post"}},
}

#: A create form rejecting a status the workflow does not allow.
CREATE_FORM_INVALID_STATUS: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"subject": "Write the tools layer"},
        "schema": {
            "_type": "Schema",
            "status": {
                "type": "Status",
                "name": "Status",
                "writable": True,
                "_links": {
                    "allowedValues": [
                        {"href": "/api/v3/statuses/1", "title": "New"},
                        {"href": "/api/v3/statuses/7", "title": "In progress"},
                    ]
                },
            },
        },
        "validationErrors": {
            "status": {
                "_type": "Error",
                "errorIdentifier": (
                    "urn:openproject-org:api:v3:errors:PropertyConstraintViolation"
                ),
                "message": "Status is not set to one of the allowed values.",
                "_embedded": {"details": {"attribute": "status"}},
            }
        },
    },
}

UPDATE_FORM_OK: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"subject": "Ship the client layer", "lockVersion": 7},
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
}

CREATED_WORK_PACKAGE: dict[str, Any] = {
    "_type": "WorkPackage",
    "id": 1500,
    "lockVersion": 0,
    "subject": "Write the tools layer",
    "description": {"format": "markdown", "raw": "Six work-package tools.", "html": ""},
    "startDate": "2026-08-01",
    "dueDate": "2026-08-15",
    "createdAt": "2026-07-26T10:00:00Z",
    "updatedAt": "2026-07-26T10:00:00Z",
    "customField7": "https://tickets.example.com/OP-2",
    "_links": {
        "self": {"href": "/api/v3/work_packages/1500", "title": "Write the tools layer"},
        "schema": {"href": "/api/v3/work_packages/schemas/5-1"},
        "project": {"href": "/api/v3/projects/5", "title": "Demo project"},
        "type": {"href": "/api/v3/types/1", "title": "Task"},
        "status": {"href": "/api/v3/statuses/7", "title": "In progress"},
        "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
        "assignee": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "customField12": {"href": "/api/v3/custom_options/4", "title": "High"},
    },
}

UPDATED_WORK_PACKAGE: dict[str, Any] = {
    **WORK_PACKAGE_DETAIL,
    "lockVersion": 8,
    "subject": "Ship the client layer (v2)",
    "_links": {
        **WORK_PACKAGE_DETAIL["_links"],
        "assignee": {"href": None},
        "status": {"href": "/api/v3/statuses/12", "title": "Closed"},
    },
}

CONFLICT_BODY: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Your changes could not be saved, because the work package was changed.",
}

NOT_FOUND_BODY: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}
