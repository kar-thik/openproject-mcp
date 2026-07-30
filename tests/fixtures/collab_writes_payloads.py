"""Golden payloads for the Phase 2 write tools (SPEC §6.3, §6.4).

Trimmed from real OpenProject responses: activity PATCH bodies, the user
document ``POST /work_packages/{id}/watchers`` answers with, relation resources
as ``RelationRepresenter`` renders them, and the error envelopes those endpoints
produce. The attachment section at the bottom belongs to ``delete_attachment``
(SPEC §6.4) — it lives here so the Phase 2 write tests share one fixture module.
"""

from __future__ import annotations

from typing import Any

WORK_PACKAGE_ID = 1234
OTHER_WORK_PACKAGE_ID = 4321
USER_ID = 12
ACTIVITY_ID = 620
FIELD_CHANGE_ACTIVITY_ID = 621
RELATION_ID = 650
ATTACHMENT_ID = 77

# --- activities: edit_work_package_comment --------------------------------

#: ``GET /activities/620`` — an editable comment entry (``notes`` present).
EDITABLE_COMMENT_ACTIVITY: dict[str, Any] = {
    "_type": "Activity::Comment",
    "id": ACTIVITY_ID,
    "version": 14,
    "internal": False,
    "createdAt": "2026-07-26T10:00:00Z",
    "updatedAt": "2026-07-26T10:00:00Z",
    "comment": {
        "format": "markdown",
        "raw": "Deployed to staging.",
        "html": "<p>Deployed to staging.</p>",
    },
    "details": [],
    "_links": {
        "self": {"href": f"/api/v3/activities/{ACTIVITY_ID}"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "user": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        "update": {"href": f"/api/v3/activities/{ACTIVITY_ID}", "method": "patch"},
    },
}

EDITED_COMMENT_TEXT = "Deployed to staging and smoke-tested."

#: ``PATCH /activities/620`` — the same entry after the edit.
EDITED_COMMENT_ACTIVITY: dict[str, Any] = {
    **EDITABLE_COMMENT_ACTIVITY,
    "version": 15,
    "updatedAt": "2026-07-26T11:30:00Z",
    "comment": {
        "format": "markdown",
        "raw": EDITED_COMMENT_TEXT,
        "html": f"<p>{EDITED_COMMENT_TEXT}</p>",
    },
}

#: A journal entry that only records field changes — not editable.
FIELD_CHANGE_ONLY_ACTIVITY: dict[str, Any] = {
    "_type": "Activity",
    "id": FIELD_CHANGE_ACTIVITY_ID,
    "version": 2,
    "createdAt": "2026-07-02T09:15:00Z",
    "updatedAt": "2026-07-02T09:15:00Z",
    "comment": {"format": "markdown", "raw": "", "html": ""},
    "details": [
        {
            "format": "custom",
            "raw": "Status changed from New to In progress",
            "html": "<strong>Status</strong> changed from <i>New</i> <strong>to</strong> "
            "<i>In progress</i>",
        }
    ],
    "_links": {
        "self": {"href": f"/api/v3/activities/{FIELD_CHANGE_ACTIVITY_ID}"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "user": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
    },
}

#: What an instance answers when it wants the other ``comment`` wire shape.
COMMENT_SHAPE_REJECTED: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidRequestBody",
    "message": "comment is invalid",
}

COMMENT_EDIT_VALIDATION_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Comment is too long (maximum is 65536 characters).",
    "_embedded": {"details": {"attribute": "comment"}},
}

ACTIVITY_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

# --- watchers -------------------------------------------------------------

#: ``POST /work_packages/{id}/watchers`` answers with the user resource.
WATCHER_USER: dict[str, Any] = {
    "_type": "User",
    "id": USER_ID,
    "name": "Grace Hopper",
    "firstName": "Grace",
    "lastName": "Hopper",
    "login": "ghopper",
    "email": "grace@example.test",
    "status": "active",
    "createdAt": "2024-01-04T08:00:00Z",
    "updatedAt": "2026-07-01T08:00:00Z",
    "_links": {
        "self": {"href": f"/api/v3/users/{USER_ID}", "title": "Grace Hopper"},
    },
}

WATCHER_NOT_ALLOWED: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "User is not allowed to view this work package.",
    "_embedded": {"details": {"attribute": "user"}},
}

WATCHER_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The specified user does not exist.",
}

# --- relations ------------------------------------------------------------

#: ``POST /work_packages/{id}/relations`` / ``GET /relations/650``.
FOLLOWS_RELATION: dict[str, Any] = {
    "_type": "Relation",
    "id": RELATION_ID,
    "name": "follows",
    "type": "follows",
    "reverseType": "precedes",
    "lag": 2,
    "description": "Start once the design is signed off.",
    "_links": {
        "self": {"href": f"/api/v3/relations/{RELATION_ID}"},
        "updateImmediately": {"href": f"/api/v3/relations/{RELATION_ID}", "method": "patch"},
        "delete": {
            "href": f"/api/v3/relations/{RELATION_ID}",
            "method": "delete",
            "title": "Remove relation",
        },
        "from": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "to": {
            "href": f"/api/v3/work_packages/{OTHER_WORK_PACKAGE_ID}",
            "title": "Design the client layer",
        },
    },
}

#: ``PATCH /relations/650`` — lag widened, description rewritten.
UPDATED_RELATION: dict[str, Any] = {
    **FOLLOWS_RELATION,
    "lag": 5,
    "description": "Start a week after sign-off.",
}

#: A relation type that carries no lag at all.
RELATES_RELATION: dict[str, Any] = {
    "_type": "Relation",
    "id": 651,
    "name": "relates to",
    "type": "relates",
    "reverseType": "relates",
    "description": None,
    "_links": {
        "self": {"href": "/api/v3/relations/651"},
        "from": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "to": {
            "href": f"/api/v3/work_packages/{OTHER_WORK_PACKAGE_ID}",
            "title": "Design the client layer",
        },
    },
}

RELATION_LAG_VIOLATION: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Lag must be a number greater than or equal to 0",
    "_embedded": {"details": {"attribute": "lag"}},
}

RELATION_CONFLICT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Couldn't update the resource because of conflicting modifications.",
}

RELATION_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

# --- attachments: delete_attachment (SPEC §6.4) ---------------------------

DELETABLE_ATTACHMENT: dict[str, Any] = {
    "_type": "Attachment",
    "id": ATTACHMENT_ID,
    "fileName": "obsolete-spec.pdf",
    "fileSize": 20480,
    "contentType": "application/pdf",
    "description": {
        "format": "plain",
        "raw": "Superseded draft",
        "html": "<p>Superseded draft</p>",
    },
    "createdAt": "2026-07-20T09:15:00Z",
    "status": "uploaded",
    "_links": {
        "self": {"href": f"/api/v3/attachments/{ATTACHMENT_ID}"},
        "author": {"href": f"/api/v3/users/{USER_ID}", "title": "Grace Hopper"},
        "container": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
    },
}

ATTACHMENT_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The specified attachment does not exist.",
}

ATTACHMENT_DELETE_FORBIDDEN: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission",
    "message": "You are not allowed to delete this attachment.",
}

ATTACHMENT_DELETE_VIOLATION: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Attachment cannot be removed while the antivirus scan is running.",
    "_embedded": {"details": {"attribute": "status"}},
}
