"""Golden HAL payloads, trimmed from OpenProject 17.x responses.

Kept as Python literals so the unit suite stays offline and diffable. A 14 LTS
set for version-sensitive surfaces (time-entry filters, comments) is added when
an instance is obtainable; until then the gap is documented rather than faked.
"""

from __future__ import annotations

from typing import Any

API_ROOT: dict[str, Any] = {
    "_type": "Root",
    "instanceName": "Test OpenProject",
    "coreVersion": "17.7.1",
    "_links": {
        "self": {"href": "/api/v3"},
        "user": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        "configuration": {"href": "/api/v3/configuration"},
    },
}

WORK_PACKAGE: dict[str, Any] = {
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
    "createdAt": "2026-07-01T09:00:00Z",
    "updatedAt": "2026-07-20T14:12:00Z",
    "_links": {
        "self": {"href": "/api/v3/work_packages/1234", "title": "Ship the client layer"},
        "project": {"href": "/api/v3/projects/demo", "title": "Demo project"},
        "type": {"href": "/api/v3/types/1", "title": "Task"},
        "status": {"href": "/api/v3/statuses/7", "title": "In progress"},
        "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
        "assignee": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "responsible": {"href": None},
        "parent": {"href": "/api/v3/work_packages/1000", "title": "Epic"},
        "version": {"href": "/api/v3/versions/3", "title": "Sprint 4"},
    },
}

WORK_PACKAGE_COLLECTION: dict[str, Any] = {
    "_type": "WorkPackageCollection",
    "total": 137,
    "count": 2,
    "pageSize": 20,
    "offset": 2,
    "totalSums": {"estimatedTime": "PT220H", "storyPoints": 41},
    "groups": [
        {"value": "In progress", "count": 12, "sums": {"estimatedTime": "PT41H30M"}},
        {"value": "New", "count": 3, "sums": {"estimatedTime": "PT8H"}},
    ],
    "_embedded": {
        "elements": [
            WORK_PACKAGE,
            {
                "_type": "WorkPackage",
                "id": 1235,
                "subject": "Write the filter builder",
                "_links": {
                    "self": {"href": "/api/v3/work_packages/1235"},
                    "status": {"href": "/api/v3/statuses/1", "title": "New"},
                    "assignee": {"href": None},
                },
            },
        ]
    },
}

VALIDATION_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Subject can't be blank.",
    "_embedded": {"details": {"attribute": "subject"}},
}

MULTIPLE_ERRORS: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MultipleErrors",
    "message": "Multiple field constraints have been violated.",
    "_embedded": {
        "errors": [
            {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Subject can't be blank.",
                "_embedded": {"details": {"attribute": "subject"}},
            },
            {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Type is not set to one of the allowed values.",
                "_embedded": {"details": {"attribute": "type"}},
            },
        ]
    },
}

WORK_PACKAGE_SCHEMA: dict[str, Any] = {
    "_type": "Schema",
    "subject": {"type": "String", "name": "Subject", "required": True, "writable": True},
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
    "customField20": {
        "type": "String",
        "name": "Computed",
        "required": False,
        "writable": False,
    },
}
