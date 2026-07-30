"""Golden HAL payloads for the news tools (SPEC §6.13).

Trimmed from OpenProject 16.x responses and kept as Python literals so the suite
stays offline and diffable. Only the fields the projections read are kept, plus
the ones a naive parser gets wrong: the permission-gated ``updateImmediately`` /
``delete`` links (present only with 'manage news'), a formattable description
with both ``raw`` and ``html``, an entry with no summary at all, and the error
bodies the write paths have to translate.
"""

from __future__ import annotations

from typing import Any

NEWS_ID = 42
OTHER_NEWS_ID = 43
PROJECT_ID = 7
PROJECT_IDENTIFIER = "apollo-migration"


def hal_collection(elements: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Wrap elements in the HAL collection envelope OpenProject returns."""
    return {
        "_type": "Collection",
        "total": extra.pop("total", len(elements)),
        "count": len(elements),
        "_embedded": {"elements": elements},
        **extra,
    }


def _manage_links(news_id: int) -> dict[str, Any]:
    """The two links OpenProject renders only for 'manage news' holders."""
    return {
        "updateImmediately": {"href": f"/api/v3/news/{news_id}", "method": "patch"},
        "delete": {"href": f"/api/v3/news/{news_id}", "method": "delete"},
    }


#: A news entry the current user may edit: the manage links are rendered.
NEWS: dict[str, Any] = {
    "_type": "News",
    "id": NEWS_ID,
    "title": "Release 2.1 is live",
    "summary": "Payments hardening shipped to production.",
    "description": {
        "format": "markdown",
        "raw": "# Release 2.1\n\n- Payments hardening\n- Faster search",
        "html": "<h1>Release 2.1</h1>",
    },
    "createdAt": "2026-07-20T08:00:00Z",
    "updatedAt": "2026-07-20T08:00:00Z",
    "_links": {
        "self": {"href": f"/api/v3/news/{NEWS_ID}", "title": "Release 2.1 is live"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        **_manage_links(NEWS_ID),
    },
}

#: Read-only for this account: no manage links, and no summary either.
NEWS_READ_ONLY: dict[str, Any] = {
    "_type": "News",
    "id": OTHER_NEWS_ID,
    "title": "Maintenance window on Saturday",
    "summary": "",
    "description": {"format": "markdown", "raw": "", "html": ""},
    "createdAt": "2026-07-18T16:30:00Z",
    "updatedAt": "2026-07-18T16:30:00Z",
    "_links": {
        "self": {"href": f"/api/v3/news/{OTHER_NEWS_ID}", "title": "Maintenance window"},
        "project": {"href": "/api/v3/projects/3", "title": "Customer work"},
        "author": {"href": "/api/v3/users/5", "title": "Ada Lovelace"},
    },
}

NEWS_COLLECTION: dict[str, Any] = hal_collection(
    [NEWS, NEWS_READ_ONLY], total=37, offset=1, pageSize=20
)

EMPTY_COLLECTION: dict[str, Any] = hal_collection([], total=0, offset=1, pageSize=20)

CREATED_NEWS: dict[str, Any] = {
    **NEWS,
    "id": 61,
    "title": "Weekly report — week 30",
    "summary": "Two features closed, one blocked.",
    "description": {
        "format": "markdown",
        "raw": "## Highlights\n\n- Feature X closed",
        "html": "<h2>Highlights</h2>",
    },
    "createdAt": "2026-07-26T09:10:00Z",
    "updatedAt": "2026-07-26T09:10:00Z",
    "_links": {
        "self": {"href": "/api/v3/news/61", "title": "Weekly report — week 30"},
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
        "author": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        **_manage_links(61),
    },
}

UPDATED_NEWS: dict[str, Any] = {
    **NEWS,
    "title": "Release 2.1 is live (hotfixed)",
    "summary": "",
    "description": {
        "format": "markdown",
        "raw": "# Release 2.1\n\nHotfix 2.1.1 followed.",
        "html": "<h1>Release 2.1</h1>",
    },
    "updatedAt": "2026-07-27T11:45:00Z",
}

#: ``GET /projects/{identifier}`` — what the identifier→id resolution reads.
PROJECT: dict[str, Any] = {
    "_type": "Project",
    "id": PROJECT_ID,
    "identifier": PROJECT_IDENTIFIER,
    "name": "Apollo migration",
    "_links": {
        "self": {"href": f"/api/v3/projects/{PROJECT_ID}", "title": "Apollo migration"},
    },
}

NEWS_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

PROJECT_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

NEWS_FORBIDDEN: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission",
    "message": "You are not allowed to create new news.",
}

NEWS_TITLE_BLANK: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Title is too long (maximum is 256 characters).",
    "_embedded": {"details": {"attribute": "title"}},
}
