"""Golden HAL payloads for the Phase 2 project writes, capabilities and versions.

Trimmed from OpenProject 16.x/17.x responses and kept as Python literals so the
suite stays offline and diffable. Only the fields the projections read are kept,
plus the ones that would break a naive parser: form envelopes with and without
``validationErrors``, a capability whose action name contains a slash, a version
shared from a parent project, and a sprint that is also a version.
"""

from __future__ import annotations

from typing import Any

# --- projects -------------------------------------------------------------

PROJECT_ID = 7
PROJECT_IDENTIFIER = "apollo-migration"

CREATED_PROJECT: dict[str, Any] = {
    "_type": "Project",
    "id": PROJECT_ID,
    "identifier": PROJECT_IDENTIFIER,
    "name": "Apollo migration",
    "active": True,
    "public": False,
    "description": {
        "format": "markdown",
        "raw": "Move Apollo off the legacy stack.",
        "html": "<p>Move Apollo off the legacy stack.</p>",
    },
    "statusExplanation": {"format": "markdown", "raw": "", "html": ""},
    "createdAt": "2026-07-26T09:00:00Z",
    "updatedAt": "2026-07-26T09:00:00Z",
    "_links": {
        "self": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "parent": {"href": "/api/v3/projects/3", "title": "Customer work"},
        "status": {"href": "/api/v3/project_statuses/on_track", "title": "On track"},
    },
}

UPDATED_PROJECT: dict[str, Any] = {
    **CREATED_PROJECT,
    "name": "Apollo migration (phase 2)",
    "updatedAt": "2026-07-27T10:30:00Z",
    "statusExplanation": {
        "format": "markdown",
        "raw": "Vendor slipped two weeks.",
        "html": "<p>Vendor slipped two weeks.</p>",
    },
    "_links": {
        "self": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration (phase 2)"},
        "parent": {"href": None},
        "status": {"href": "/api/v3/project_statuses/at_risk", "title": "At risk"},
    },
}

#: A form answer with no errors: OpenProject echoes the payload it would commit,
#: including the identifier it derived from the name.
PROJECT_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {
            "name": "Apollo migration",
            "identifier": "apollo-migration",
            "active": True,
            "public": False,
            "_links": {
                "parent": {"href": "/api/v3/projects/3"},
                "status": {"href": "/api/v3/project_statuses/on_track"},
            },
        },
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
    "_links": {"self": {"href": "/api/v3/projects/form"}},
}

PROJECT_UPDATE_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"name": "Apollo migration (phase 2)"},
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
    "_links": {"self": {"href": f"/api/v3/projects/{PROJECT_ID}/form"}},
}

PROJECT_FORM_IDENTIFIER_TAKEN: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"name": "Apollo migration", "identifier": "apollo-migration"},
        "schema": {"_type": "Schema"},
        "validationErrors": {
            "identifier": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Identifier has already been taken.",
                "_embedded": {"details": {"attribute": "identifier"}},
            }
        },
    },
}

PROJECT_CREATE_VALIDATION_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Name can't be blank.",
    "_embedded": {"details": {"attribute": "name"}},
}

PROJECT_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

PROJECT_CONFLICT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Your changes could not be saved, because the resource was updated.",
}

#: Some instances answer the async project deletion with a job status document.
DELETE_JOB_ACCEPTED: dict[str, Any] = {
    "_type": "JobStatus",
    "status": "in_queue",
    "message": "Deletion scheduled.",
    "_links": {
        "self": {"href": "/api/v3/job_statuses/9f4c1d5e-0e2a-4f2b-9a11-2f1b3c4d5e6f"},
    },
}

JOB_ID = "9f4c1d5e-0e2a-4f2b-9a11-2f1b3c4d5e6f"


# --- capabilities (SPEC §6.1) --------------------------------------------

CURRENT_USER: dict[str, Any] = {
    "_type": "User",
    "id": 1,
    "name": "Ada Lovelace",
    "login": "ada",
    "admin": False,
    "_links": {"self": {"href": "/api/v3/users/1", "title": "Ada Lovelace"}},
}

GLOBAL_CONTEXT_HREF = "/api/v3/capabilities/context/global"


def capability(
    action: str,
    *,
    context_href: str = GLOBAL_CONTEXT_HREF,
    context_title: str = "Global",
) -> dict[str, Any]:
    """One capability resource; ``action`` keeps its slash (``memberships/create``)."""
    slug = context_href.rsplit("/", 1)[-1]
    return {
        "_type": "Capability",
        "id": f"{action}/{slug}",
        "_links": {
            "self": {"href": f"/api/v3/capabilities/{action}/{slug}"},
            "action": {"href": f"/api/v3/actions/{action}", "title": action},
            "context": {"href": context_href, "title": context_title},
            "principal": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        },
    }


def capability_collection(
    elements: list[dict[str, Any]],
    *,
    total: int | None = None,
    page_size: int = 100,
    offset: int = 1,
) -> dict[str, Any]:
    return {
        "_type": "Collection",
        "total": len(elements) if total is None else total,
        "count": len(elements),
        "pageSize": page_size,
        "offset": offset,
        "_embedded": {"elements": elements},
    }


GLOBAL_CAPABILITIES: list[dict[str, Any]] = [
    capability("projects/create"),
    capability("users/read"),
]

PROJECT_CONTEXT_HREF = "/api/v3/projects/12"

PROJECT_CAPABILITIES: list[dict[str, Any]] = [
    capability(
        "work_packages/create", context_href=PROJECT_CONTEXT_HREF, context_title="Demo project"
    ),
    capability(
        "memberships/create", context_href=PROJECT_CONTEXT_HREF, context_title="Demo project"
    ),
    capability("versions/manage", context_href=PROJECT_CONTEXT_HREF, context_title="Demo project"),
]

CAPABILITIES_FILTER_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Filters Context filter has invalid values.",
    "_embedded": {"details": {"attribute": "filters"}},
}


# --- versions -------------------------------------------------------------

VERSION_ID = 41
SHARED_VERSION_ID = 42
SPRINT_ONLY_ID = 55

VERSION: dict[str, Any] = {
    "_type": "Version",
    "id": VERSION_ID,
    "name": "Sprint 12",
    "description": {
        "format": "markdown",
        "raw": "Hardening sprint.",
        "html": "<p>Hardening sprint.</p>",
    },
    "startDate": "2026-08-01",
    "endDate": "2026-08-14",
    "status": "open",
    "sharing": "none",
    "createdAt": "2026-07-01T08:00:00Z",
    "updatedAt": "2026-07-20T12:00:00Z",
    "_links": {
        "self": {"href": f"/api/v3/versions/{VERSION_ID}", "title": "Sprint 12"},
        "definingProject": {"href": "/api/v3/projects/7", "title": "Demo project"},
    },
}

SHARED_VERSION: dict[str, Any] = {
    "_type": "Version",
    "id": SHARED_VERSION_ID,
    "name": "Release 2.1",
    "description": {"format": "markdown", "raw": "", "html": ""},
    "startDate": None,
    "endDate": "2026-12-01",
    "status": "closed",
    "sharing": "descendants",
    "_links": {
        "self": {"href": f"/api/v3/versions/{SHARED_VERSION_ID}", "title": "Release 2.1"},
        "definingProject": {"href": "/api/v3/projects/3", "title": "Customer work"},
    },
}

SPRINT_ONLY: dict[str, Any] = {
    "_type": "Version",
    "id": SPRINT_ONLY_ID,
    "name": "Sprint 13",
    "startDate": "2026-08-15",
    "endDate": "2026-08-28",
    "status": "open",
    "sharing": "none",
    "_links": {
        "self": {"href": f"/api/v3/versions/{SPRINT_ONLY_ID}", "title": "Sprint 13"},
        "definingProject": {"href": "/api/v3/projects/7", "title": "Demo project"},
    },
}


def version_collection(
    elements: list[dict[str, Any]],
    *,
    total: int | None = None,
    page_size: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_type": "Collection",
        "total": len(elements) if total is None else total,
        "count": len(elements),
        "_embedded": {"elements": elements},
    }
    if page_size is not None:
        payload["pageSize"] = page_size
    if offset is not None:
        payload["offset"] = offset
    return payload


CREATED_VERSION: dict[str, Any] = {
    "_type": "Version",
    "id": 61,
    "name": "Sprint 14",
    "description": {"format": "markdown", "raw": "Payments hardening.", "html": ""},
    "startDate": "2026-09-01",
    "endDate": "2026-09-30",
    "status": "open",
    "sharing": "descendants",
    "createdAt": "2026-07-26T09:10:00Z",
    "updatedAt": "2026-07-26T09:10:00Z",
    "_links": {
        "self": {"href": "/api/v3/versions/61", "title": "Sprint 14"},
        "definingProject": {"href": "/api/v3/projects/7", "title": "Demo project"},
    },
}

UPDATED_VERSION: dict[str, Any] = {
    **VERSION,
    "status": "closed",
    "endDate": None,
    "updatedAt": "2026-07-26T09:20:00Z",
}

VERSION_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {
            "name": "Sprint 14",
            "status": "open",
            "sharing": "none",
            "_links": {"definingProject": {"href": "/api/v3/projects/7"}},
        },
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
}

VERSION_UPDATE_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"name": "Sprint 12", "status": "closed"},
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
}

VERSION_FORM_DUPLICATE_NAME: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"name": "Sprint 12"},
        "schema": {"_type": "Schema"},
        "validationErrors": {
            "name": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Name has already been taken.",
                "_embedded": {"details": {"attribute": "name"}},
            }
        },
    },
}

VERSION_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

VERSION_IN_USE_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Version cannot be deleted because it is in use by work packages.",
    "_embedded": {"details": {"attribute": "base"}},
}
