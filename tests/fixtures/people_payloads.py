"""Golden payloads for the people & access tools (SPEC §6.11).

Trimmed from real ``/principals``, ``/users``, ``/memberships`` and ``/roles``
responses so the protocol suite stays offline. The principal collection mixes a
user, a group and a placeholder user on purpose: the projection has to keep
their kinds apart and must not invent an email for the two that have none.
"""

from __future__ import annotations

from typing import Any

PROJECT_ID = 7
PROJECT_IDENTIFIER = "demo"
MEMBERSHIP_ID = 42

PROJECT_BY_IDENTIFIER: dict[str, Any] = {
    "_type": "Project",
    "id": PROJECT_ID,
    "identifier": PROJECT_IDENTIFIER,
    "name": "Demo project",
    "_links": {"self": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Demo project"}},
}

USER_PRINCIPAL: dict[str, Any] = {
    "_type": "User",
    "id": 12,
    "name": "Grace Hopper",
    "login": "ghopper",
    "email": "grace@example.test",
    "admin": False,
    "status": "active",
    "avatar": "https://openproject.test/assets/avatar.png",
    "language": "en",
    "createdAt": "2025-01-04T08:00:00Z",
    "updatedAt": "2026-07-19T11:30:00Z",
    "_links": {"self": {"href": "/api/v3/users/12", "title": "Grace Hopper"}},
}

GROUP_PRINCIPAL: dict[str, Any] = {
    "_type": "Group",
    "id": 5,
    "name": "Platform team",
    "createdAt": "2025-02-01T08:00:00Z",
    "_links": {"self": {"href": "/api/v3/groups/5", "title": "Platform team"}},
}

PLACEHOLDER_PRINCIPAL: dict[str, Any] = {
    "_type": "PlaceholderUser",
    "id": 31,
    "name": "Contractor A",
    "_links": {"self": {"href": "/api/v3/placeholder_users/31", "title": "Contractor A"}},
}

#: A user whose email/login the caller may not see — nulls must stay nulls.
SHIELDED_USER_PRINCIPAL: dict[str, Any] = {
    "_type": "User",
    "id": 88,
    "name": "Alan Turing",
    "_links": {"self": {"href": "/api/v3/users/88", "title": "Alan Turing"}},
}

PRINCIPAL_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 4,
    "count": 4,
    "pageSize": 20,
    "offset": 1,
    "_embedded": {
        "elements": [
            USER_PRINCIPAL,
            GROUP_PRINCIPAL,
            PLACEHOLDER_PRINCIPAL,
            SHIELDED_USER_PRINCIPAL,
        ]
    },
}

EMPTY_PRINCIPAL_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 0,
    "count": 0,
    "pageSize": 20,
    "offset": 1,
    "_embedded": {"elements": []},
}

CURRENT_USER: dict[str, Any] = {
    "_type": "User",
    "id": 1,
    "name": "Ada Lovelace",
    "login": "ada",
    "email": "ada@example.test",
    "admin": True,
    "status": "active",
    "avatar": "https://openproject.test/assets/ada.png",
    "language": "en",
    "createdAt": "2024-11-11T10:00:00Z",
    "updatedAt": "2026-07-01T09:00:00Z",
    "_links": {"self": {"href": "/api/v3/users/1", "title": "Ada Lovelace"}},
}


def membership(
    identifier: int = MEMBERSHIP_ID,
    *,
    principal: dict[str, Any] | None = None,
    roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One Membership resource, with the principal embedded as OpenProject does."""
    resolved_principal = principal or USER_PRINCIPAL
    resolved_roles = roles or [{"href": "/api/v3/roles/3", "title": "Member"}]
    principal_href = resolved_principal["_links"]["self"]["href"]
    return {
        "_type": "Membership",
        "id": identifier,
        "createdAt": "2026-03-02T12:00:00Z",
        "updatedAt": "2026-06-30T15:45:00Z",
        "_links": {
            "self": {"href": f"/api/v3/memberships/{identifier}"},
            "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Demo project"},
            "principal": {"href": principal_href, "title": resolved_principal["name"]},
            "roles": resolved_roles,
        },
        "_embedded": {"principal": resolved_principal},
    }


USER_MEMBERSHIP = membership()
GROUP_MEMBERSHIP = membership(
    43,
    principal=GROUP_PRINCIPAL,
    roles=[
        {"href": "/api/v3/roles/3", "title": "Member"},
        {"href": "/api/v3/roles/4", "title": "Reader"},
    ],
)

MEMBERSHIP_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 2,
    "count": 2,
    "pageSize": 20,
    "offset": 1,
    "_embedded": {"elements": [USER_MEMBERSHIP, GROUP_MEMBERSHIP]},
}

CREATED_MEMBERSHIP = membership(77)
UPDATED_MEMBERSHIP = membership(
    MEMBERSHIP_ID,
    roles=[
        {"href": "/api/v3/roles/4", "title": "Reader"},
        {"href": "/api/v3/roles/9", "title": "Project admin"},
    ],
)

#: A form that validated cleanly — no ``validationErrors`` key at all.
MEMBERSHIP_FORM_OK: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {
            "_links": {
                "project": {"href": f"/api/v3/projects/{PROJECT_ID}"},
                "principal": {"href": "/api/v3/principals/12"},
                "roles": [{"href": "/api/v3/roles/3"}],
            }
        },
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
    "_links": {"self": {"href": "/api/v3/memberships/form"}},
}

MEMBERSHIP_FORM_INVALID_ROLE: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"_links": {}},
        "schema": {
            "_type": "Schema",
            "roles": {
                "type": "[]Role",
                "name": "Roles",
                "required": True,
                "_embedded": {
                    "allowedValues": [
                        {
                            "_type": "Role",
                            "id": 3,
                            "name": "Member",
                            "_links": {"self": {"href": "/api/v3/roles/3"}},
                        },
                        {
                            "_type": "Role",
                            "id": 4,
                            "name": "Reader",
                            "_links": {"self": {"href": "/api/v3/roles/4"}},
                        },
                    ]
                },
            },
        },
        "validationErrors": {
            "roles": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Roles is not set to one of the allowed values.",
                "_embedded": {"details": {"attribute": "roles"}},
            }
        },
    },
}

MEMBERSHIP_FORM_DUPLICATE: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"_links": {}},
        "schema": {"_type": "Schema"},
        "validationErrors": {
            "principal": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "User has already been taken.",
                "_embedded": {"details": {"attribute": "principal"}},
            }
        },
    },
}

ROLE_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 3,
    "count": 3,
    "pageSize": 100,
    "offset": 1,
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
                "name": "Reader",
                "_links": {"self": {"href": "/api/v3/roles/4"}},
            },
            {
                "_type": "Role",
                "id": 9,
                "name": "Project admin",
                "_links": {"self": {"href": "/api/v3/roles/9"}},
            },
        ]
    },
}

ROLE_COLLECTION_WITH_PERMISSIONS: dict[str, Any] = {
    "_type": "Collection",
    "total": 2,
    "count": 2,
    "pageSize": 100,
    "offset": 1,
    "_embedded": {
        "elements": [
            {
                "_type": "Role",
                "id": 3,
                "name": "Member",
                "permissions": ["view_work_packages", "edit_work_packages", "add_work_packages"],
                "_links": {"self": {"href": "/api/v3/roles/3"}},
            },
            {
                "_type": "Role",
                "id": 4,
                "name": "Reader",
                "permissions": ["view_work_packages"],
                "_links": {"self": {"href": "/api/v3/roles/4"}},
            },
        ]
    },
}

NOT_FOUND_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

MEMBERSHIP_VALIDATION_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Roles is not set to one of the allowed values.",
    "_embedded": {"details": {"attribute": "roles"}},
}

MEMBERSHIP_CONFLICT_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "The membership was updated by somebody else while you were editing it.",
}
