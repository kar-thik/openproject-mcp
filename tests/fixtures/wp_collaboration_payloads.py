"""Golden activity payloads for the collaboration tools (SPEC §6.3).

Trimmed from real ``GET /work_packages/{id}/activities`` responses. The journal
builder produces a collection longer than the default page size so the
client-side pagination math has something to be wrong about.
"""

from __future__ import annotations

from typing import Any

WORK_PACKAGE_ID = 1234

#: A field-change entry whose details cover every sentence shape OpenProject
#: renders, including a value that itself contains the word "to".
FIELD_CHANGE_ACTIVITY: dict[str, Any] = {
    "_type": "Activity",
    "id": 501,
    "version": 2,
    "createdAt": "2026-07-02T09:15:00Z",
    "updatedAt": "2026-07-02T09:15:00Z",
    "comment": {"format": "markdown", "raw": "", "html": ""},
    "details": [
        {
            "format": "custom",
            "raw": "Status changed from New to In progress",
            "html": (
                "<strong>Status</strong> changed from <i>New</i> "
                "<strong>to</strong> <i>In progress</i>"
            ),
        },
        {
            "format": "custom",
            "raw": "Start date set to 2026-07-01",
            "html": "<strong>Start date</strong> set to <i>2026-07-01</i>",
        },
        {
            "format": "custom",
            "raw": "Assignee deleted (Grace Hopper)",
            "html": "<strong>Assignee</strong> deleted (<i>Grace Hopper</i>)",
        },
        {
            "format": "custom",
            "raw": "Description updated",
            "html": "<strong>Description</strong> updated",
        },
        {
            "format": "custom",
            "raw": "Subject changed from Ship to prod to Ship to staging",
            "html": (
                "<strong>Subject</strong> changed from <i>Ship to prod</i> "
                "<strong>to</strong> <i>Ship to staging</i>"
            ),
        },
    ],
    "_links": {
        "self": {"href": "/api/v3/activities/501"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "user": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
    },
}

#: A public comment that also carries the field change saved with it.
COMMENT_ACTIVITY: dict[str, Any] = {
    "_type": "Activity::Comment",
    "id": 502,
    "version": 3,
    "internal": False,
    "createdAt": "2026-07-03T11:00:00Z",
    "updatedAt": "2026-07-03T11:05:00Z",
    "comment": {
        "format": "markdown",
        "raw": "Retries are in. Review when you get a chance.",
        "html": "<p>Retries are in. Review when you get a chance.</p>",
    },
    "details": [
        {
            "format": "custom",
            "raw": "Progress (%) changed from 20 to 40",
            "html": (
                "<strong>Progress (%)</strong> changed from <i>20</i> <strong>to</strong> <i>40</i>"
            ),
        }
    ],
    "_links": {
        "self": {"href": "/api/v3/activities/502"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "user": {"href": "/api/v3/users/13", "title": "Alan Turing"},
    },
}

INTERNAL_COMMENT_ACTIVITY: dict[str, Any] = {
    "_type": "Activity::Comment",
    "id": 503,
    "version": 4,
    "internal": True,
    "createdAt": "2026-07-04T08:30:00Z",
    "updatedAt": "2026-07-04T08:30:00Z",
    "comment": {
        "format": "markdown",
        "raw": "Customer has not paid the last invoice yet.",
        "html": "<p>Customer has not paid the last invoice yet.</p>",
    },
    "details": [],
    "_links": {
        "self": {"href": "/api/v3/activities/503"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "user": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
    },
}

#: 4,000 characters — longer than the 2,000-char default cap.
LONG_COMMENT_TEXT = "The retry policy needs a decision. " * 115
LONG_COMMENT_ACTIVITY: dict[str, Any] = {
    "_type": "Activity::Comment",
    "id": 599,
    "version": 12,
    "internal": False,
    "createdAt": "2026-07-05T16:45:00Z",
    "updatedAt": "2026-07-05T16:45:00Z",
    "comment": {
        "format": "markdown",
        "raw": LONG_COMMENT_TEXT,
        "html": f"<p>{LONG_COMMENT_TEXT}</p>",
    },
    "details": [],
    "_links": {
        "self": {"href": "/api/v3/activities/599"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "user": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
    },
}

#: An activity that belongs to a different work package — the mismatch guard.
FOREIGN_ACTIVITY: dict[str, Any] = {
    **LONG_COMMENT_ACTIVITY,
    "id": 777,
    "_links": {
        **LONG_COMMENT_ACTIVITY["_links"],
        "self": {"href": "/api/v3/activities/777"},
        "workPackage": {"href": "/api/v3/work_packages/4321", "title": "Another ticket"},
    },
}

CREATED_COMMENT_ACTIVITY: dict[str, Any] = {
    "_type": "Activity::Comment",
    "id": 610,
    "version": 13,
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
        "self": {"href": "/api/v3/activities/610"},
        "workPackage": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
        "user": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
    },
}

CREATED_INTERNAL_COMMENT_ACTIVITY: dict[str, Any] = {
    **CREATED_COMMENT_ACTIVITY,
    "id": 611,
    "internal": True,
    "comment": {
        "format": "markdown",
        "raw": "Internal: escalate to the account manager.",
        "html": "<p>Internal: escalate to the account manager.</p>",
    },
    "_links": {
        **CREATED_COMMENT_ACTIVITY["_links"],
        "self": {"href": "/api/v3/activities/611"},
    },
}

COMMENT_VALIDATION_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Comment is too long (maximum is 65536 characters).",
    "_embedded": {"details": {"attribute": "comment"}},
}

COMMENT_CONFLICT_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Your changes could not be saved, because the work package was updated.",
}

WORK_PACKAGE_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}


def journal_entry(index: int) -> dict[str, Any]:
    """A filler comment entry, numbered so pagination slices are identifiable."""
    return {
        "_type": "Activity::Comment",
        "id": 1000 + index,
        "version": index,
        "internal": False,
        "createdAt": "2026-07-06T09:00:00Z",
        "updatedAt": "2026-07-06T09:00:00Z",
        "comment": {
            "format": "markdown",
            "raw": f"Journal note {index}.",
            "html": f"<p>Journal note {index}.</p>",
        },
        "details": [],
        "_links": {
            "self": {"href": f"/api/v3/activities/{1000 + index}"},
            "workPackage": {
                "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
                "title": "Ship the client layer",
            },
            "user": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        },
    }


def activity_collection(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap activities the way the unpaginated endpoint does: no offset/pageSize."""
    return {
        "_type": "Collection",
        "total": len(elements),
        "count": len(elements),
        "_links": {
            "self": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/activities"},
        },
        "_embedded": {"elements": elements},
    }


#: 23 entries: the two hand-written ones plus filler, so page 3 of 10 is short.
JOURNAL_ELEMENTS: list[dict[str, Any]] = [
    FIELD_CHANGE_ACTIVITY,
    COMMENT_ACTIVITY,
    *[journal_entry(index) for index in range(1, 20)],
    INTERNAL_COMMENT_ACTIVITY,
    LONG_COMMENT_ACTIVITY,
]

WORK_PACKAGE_JOURNAL: dict[str, Any] = activity_collection(JOURNAL_ELEMENTS)
