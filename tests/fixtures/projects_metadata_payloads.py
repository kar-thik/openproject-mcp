"""Golden HAL payloads for the project and metadata tools.

Trimmed from OpenProject 17.x responses and kept as Python literals so the suite
stays offline and diffable. Only the fields the Phase 1 projections read are
retained, plus the ones that would break a naive parser (``{"href": null}``
links, formattable objects, an unwritable custom field).
"""

from __future__ import annotations

from typing import Any

PROJECT: dict[str, Any] = {
    "_type": "Project",
    "id": 7,
    "identifier": "demo-project",
    "name": "Demo project",
    "active": True,
    "public": False,
    "description": {
        "format": "markdown",
        "raw": "The customer-facing demo.",
        "html": "<p>The customer-facing demo.</p>",
    },
    "statusExplanation": {
        "format": "markdown",
        "raw": "Sprint 4 slipped by two days.",
        "html": "<p>Sprint 4 slipped by two days.</p>",
    },
    "createdAt": "2026-01-05T08:30:00Z",
    "updatedAt": "2026-07-20T11:00:00Z",
    "_links": {
        "self": {"href": "/api/v3/projects/7", "title": "Demo project"},
        "parent": {"href": "/api/v3/projects/3", "title": "Customer work"},
        "status": {"href": "/api/v3/project_statuses/at_risk", "title": "At risk"},
    },
}

ARCHIVED_PROJECT: dict[str, Any] = {
    "_type": "Project",
    "id": 9,
    "identifier": "legacy",
    "name": "Legacy migration",
    "active": False,
    "public": True,
    "_links": {
        "self": {"href": "/api/v3/projects/9"},
        "parent": {"href": None},
        "status": {"href": None},
    },
}

PROJECT_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 42,
    "count": 2,
    "pageSize": 20,
    "offset": 2,
    "_embedded": {"elements": [PROJECT, ARCHIVED_PROJECT]},
}

CONFIGURATION: dict[str, Any] = {
    "_type": "Configuration",
    "maximumAttachmentFileSize": 5242880,
    "perPageOptions": [20, 100],
    "hostName": "openproject.test",
    "userDefaultTimezone": "Etc/UTC",
}

CURRENT_USER: dict[str, Any] = {
    "_type": "User",
    "id": 1,
    "name": "Ada Lovelace",
    "login": "ada",
    "email": "ada@openproject.test",
    "admin": True,
    "status": "active",
    "_links": {"self": {"href": "/api/v3/users/1", "title": "Ada Lovelace"}},
}

TYPE_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 3,
    "count": 3,
    "_embedded": {
        "elements": [
            {
                "_type": "Type",
                "id": 1,
                "name": "Task",
                "isDefault": True,
                "isMilestone": False,
                "_links": {"self": {"href": "/api/v3/types/1"}},
            },
            {
                "_type": "Type",
                "id": 2,
                "name": "Bug",
                "isDefault": False,
                "isMilestone": False,
                "_links": {"self": {"href": "/api/v3/types/2"}},
            },
            {
                "_type": "Type",
                "id": 4,
                "name": "Milestone",
                "isDefault": False,
                "isMilestone": True,
                "_links": {"self": {"href": "/api/v3/types/4"}},
            },
        ]
    },
}

PROJECT_TYPE_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 1,
    "count": 1,
    "_embedded": {
        "elements": [
            {
                "_type": "Type",
                "id": 1,
                "name": "Task",
                "isDefault": True,
                "isMilestone": False,
                "_links": {"self": {"href": "/api/v3/types/1"}},
            }
        ]
    },
}

STATUS_COLLECTION: dict[str, Any] = {
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
                "isDefault": True,
                "_links": {"self": {"href": "/api/v3/statuses/1"}},
            },
            {
                "_type": "Status",
                "id": 7,
                "name": "In progress",
                "isClosed": False,
                "isDefault": False,
                "_links": {"self": {"href": "/api/v3/statuses/7"}},
            },
            {
                "_type": "Status",
                "id": 12,
                "name": "Closed",
                "isClosed": True,
                "isDefault": False,
                "_links": {"self": {"href": "/api/v3/statuses/12"}},
            },
        ]
    },
}

PRIORITY_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 2,
    "count": 2,
    "_embedded": {
        "elements": [
            {
                "_type": "Priority",
                "id": 8,
                "name": "Normal",
                "isDefault": True,
                "isActive": True,
                "_links": {"self": {"href": "/api/v3/priorities/8"}},
            },
            {
                "_type": "Priority",
                "id": 9,
                "name": "High",
                "isDefault": False,
                "isActive": True,
                "_links": {"self": {"href": "/api/v3/priorities/9"}},
            },
        ]
    },
}

ROLE_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 2,
    "count": 2,
    "_embedded": {
        "elements": [
            {
                "_type": "Role",
                "id": 3,
                "name": "Member",
                "_links": {"self": {"href": "/api/v3/roles/3"}},
            },
            {
                "_type": "Role",
                "id": 4,
                "name": "Project admin",
                "_links": {"self": {"href": "/api/v3/roles/4"}},
            },
        ]
    },
}

VERSION_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 1,
    "count": 1,
    "_embedded": {
        "elements": [
            {
                "_type": "Version",
                "id": 3,
                "name": "Sprint 4",
                "status": "open",
                "sharing": "none",
                "startDate": "2026-07-06",
                "endDate": "2026-07-24",
                "_links": {"self": {"href": "/api/v3/versions/3"}},
            }
        ]
    },
}

CATEGORY_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 1,
    "count": 1,
    "_embedded": {
        "elements": [
            {
                "_type": "Category",
                "id": 5,
                "name": "Backend",
                "_links": {
                    "self": {"href": "/api/v3/categories/5"},
                    "defaultAssignee": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
                },
            }
        ]
    },
}

#: ``POST /time_entries/form`` — activities are discovered here, never hardcoded.
TIME_ENTRY_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"_type": "TimeEntry", "hours": None},
        "schema": {
            "_type": "Schema",
            "activity": {
                "type": "TimeEntriesActivity",
                "name": "Activity",
                "required": True,
                "hasDefault": True,
                "writable": True,
                "_links": {
                    "allowedValues": [
                        {"href": "/api/v3/time_entries/activities/3", "title": "Development"},
                        {"href": "/api/v3/time_entries/activities/4", "title": "Management"},
                    ]
                },
            },
        },
        "validationErrors": {},
    },
}

PROJECT_STATUS_FILTER_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Filters Favored is not a valid filter.",
}
