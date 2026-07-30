"""Golden payloads for the Phase 3 collaboration extras (SPEC §6.3, §4.7).

Trimmed from real OpenProject responses: the grouped ``EmojiReaction``
collection ``PATCH /activities/{id}/emoji_reactions`` answers with, the
``Reminder`` resource and its collections, the work package
``POST /custom_actions/{id}/execute`` returns, and the error envelopes those
endpoints produce (the 409 that turns a reminder create into an update, the
400 an activity without comment text gets, the 409 a stale lockVersion gets).
"""

from __future__ import annotations

from typing import Any

WORK_PACKAGE_ID = 1234
ACTIVITY_ID = 620
FIELD_CHANGE_ACTIVITY_ID = 621
REMINDER_ID = 42
OTHER_REMINDER_ID = 43
CUSTOM_ACTION_ID = 9
CURRENT_USER_ID = 1
OTHER_USER_ID = 12
LOCK_VERSION = 7

# --- emoji reactions (SPEC §6.3, gated on OpenProject >= 16) --------------

HEART = "❤️"
ROCKET = "\U0001f680"

#: One grouped reaction: the current user plus a colleague reacted with a heart.
HEART_REACTION_MINE: dict[str, Any] = {
    "_type": "EmojiReaction",
    "id": f"{ACTIVITY_ID}-heart",
    "reaction": "heart",
    "emoji": HEART,
    "reactionsCount": 2,
    "firstReactionAt": "2026-07-27T09:00:00Z",
    "_links": {
        "reactable": {"href": f"/api/v3/activities/{ACTIVITY_ID}"},
        "reactingUsers": [
            {"href": f"/api/v3/users/{CURRENT_USER_ID}", "title": "Ada Lovelace"},
            {"href": f"/api/v3/users/{OTHER_USER_ID}", "title": "Grace Hopper"},
        ],
    },
}

#: The same group after the current user took their reaction back.
HEART_REACTION_THEIRS: dict[str, Any] = {
    **HEART_REACTION_MINE,
    "reactionsCount": 1,
    "_links": {
        "reactable": {"href": f"/api/v3/activities/{ACTIVITY_ID}"},
        "reactingUsers": [
            {"href": f"/api/v3/users/{OTHER_USER_ID}", "title": "Grace Hopper"},
        ],
    },
}

ROCKET_REACTION: dict[str, Any] = {
    "_type": "EmojiReaction",
    "id": f"{ACTIVITY_ID}-rocket",
    "reaction": "rocket",
    "emoji": ROCKET,
    "reactionsCount": 1,
    "firstReactionAt": "2026-07-27T09:05:00Z",
    "_links": {
        "reactable": {"href": f"/api/v3/activities/{ACTIVITY_ID}"},
        "reactingUsers": [
            {"href": f"/api/v3/users/{OTHER_USER_ID}", "title": "Grace Hopper"},
        ],
    },
}


def reaction_collection(*elements: dict[str, Any]) -> dict[str, Any]:
    """Wrap reaction groups in the unpaginated collection the endpoint returns."""
    return {
        "_type": "Collection",
        "total": len(elements),
        "count": len(elements),
        "_embedded": {"elements": list(elements)},
        "_links": {"self": {"href": f"/api/v3/activities/{ACTIVITY_ID}/emoji_reactions"}},
    }


#: After adding a heart: the caller's own reaction is in the group.
REACTIONS_AFTER_ADD: dict[str, Any] = reaction_collection(HEART_REACTION_MINE, ROCKET_REACTION)

#: After toggling the same heart off again: the caller is gone from the group.
REACTIONS_AFTER_REMOVE: dict[str, Any] = reaction_collection(
    HEART_REACTION_THEIRS, ROCKET_REACTION
)

#: The last reaction on a comment removed — an honestly empty collection.
REACTIONS_EMPTY: dict[str, Any] = reaction_collection()

#: What a field-change journal entry answers: it carries no comment to react to.
REACTION_ON_NON_COMMENT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:BadRequest",
    "message": "Emoji reactions are only supported on comments.",
}

ACTIVITY_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

# --- reminders ------------------------------------------------------------

REMIND_AT = "2026-08-03T09:00:00Z"
LATER_REMIND_AT = "2026-08-10T09:00:00Z"
REMINDER_NOTE = "Check the staging deploy"

EXISTING_REMINDER: dict[str, Any] = {
    "_type": "Reminder",
    "id": REMINDER_ID,
    "remindAt": REMIND_AT,
    "note": REMINDER_NOTE,
    "_links": {
        "self": {"href": f"/api/v3/reminders/{REMINDER_ID}"},
        "creator": {"href": f"/api/v3/users/{CURRENT_USER_ID}", "title": "Ada Lovelace"},
        "remindable": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
    },
}

#: ``PATCH /reminders/42`` — moved a week out, note rewritten.
UPDATED_REMINDER: dict[str, Any] = {
    **EXISTING_REMINDER,
    "remindAt": LATER_REMIND_AT,
    "note": "Check the staging deploy after the freeze",
}

#: ``POST /work_packages/1234/reminders`` — the created resource.
CREATED_REMINDER: dict[str, Any] = {
    "_type": "Reminder",
    "id": OTHER_REMINDER_ID,
    "remindAt": REMIND_AT,
    "note": REMINDER_NOTE,
    "_links": {
        "self": {"href": f"/api/v3/reminders/{OTHER_REMINDER_ID}"},
        "creator": {"href": f"/api/v3/users/{CURRENT_USER_ID}", "title": "Ada Lovelace"},
        "remindable": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}",
            "title": "Ship the client layer",
        },
    },
}

#: A second reminder of the caller's, on another work package.
OTHER_WORK_PACKAGE_REMINDER: dict[str, Any] = {
    "_type": "Reminder",
    "id": 44,
    "remindAt": "2026-08-05T14:30:00Z",
    "note": "Follow up with the vendor",
    "_links": {
        "self": {"href": "/api/v3/reminders/44"},
        "creator": {"href": f"/api/v3/users/{CURRENT_USER_ID}", "title": "Ada Lovelace"},
        "remindable": {
            "href": "/api/v3/work_packages/4321",
            "title": "Design the client layer",
        },
    },
}


def reminder_collection(*elements: dict[str, Any], total: int | None = None) -> dict[str, Any]:
    """Wrap reminders in the offset-paginated collection both endpoints return."""
    return {
        "_type": "Collection",
        "total": len(elements) if total is None else total,
        "count": len(elements),
        "pageSize": 100,
        "offset": 1,
        "_embedded": {"elements": list(elements)},
        "_links": {"self": {"href": "/api/v3/reminders"}},
    }


NO_REMINDERS: dict[str, Any] = reminder_collection()
ONE_REMINDER: dict[str, Any] = reminder_collection(EXISTING_REMINDER)
MY_REMINDERS: dict[str, Any] = reminder_collection(EXISTING_REMINDER, OTHER_WORK_PACKAGE_REMINDER)

#: More upcoming reminders than one page holds — the G1 cap-hit note.
CAPPED_REMINDERS: dict[str, Any] = reminder_collection(EXISTING_REMINDER, total=137)

#: ``POST /work_packages/{id}/reminders`` when one already exists.
REMINDER_CONFLICT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": (
        "You can only set one reminder at a time for a work package. "
        "Please delete or update the existing reminder."
    ),
}

REMINDER_IN_THE_PAST: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Remind at must be in the future.",
    "_embedded": {"details": {"attribute": "remindAt"}},
}

WORK_PACKAGE_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The work package you are looking for cannot be found or has been deleted.",
}

# --- custom actions -------------------------------------------------------

#: ``GET /work_packages/1234`` — read for its lockVersion before the action runs.
WORK_PACKAGE_BEFORE_ACTION: dict[str, Any] = {
    "_type": "WorkPackage",
    "id": WORK_PACKAGE_ID,
    "lockVersion": LOCK_VERSION,
    "subject": "Ship the client layer",
    "startDate": "2026-07-01",
    "dueDate": "2026-07-31",
    "percentageDone": 40,
    "updatedAt": "2026-07-20T14:12:00Z",
    "_links": {
        "self": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"},
        "project": {"href": "/api/v3/projects/demo", "title": "Demo project"},
        "type": {"href": "/api/v3/types/1", "title": "Task"},
        "status": {"href": "/api/v3/statuses/7", "title": "In progress"},
        "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
        "assignee": {"href": None},
    },
}

#: ``POST /custom_actions/9/execute`` — the action assigned it and closed it.
WORK_PACKAGE_AFTER_ACTION: dict[str, Any] = {
    **WORK_PACKAGE_BEFORE_ACTION,
    "lockVersion": LOCK_VERSION + 1,
    "percentageDone": 100,
    "updatedAt": "2026-07-30T08:45:00Z",
    "_links": {
        **WORK_PACKAGE_BEFORE_ACTION["_links"],
        "status": {"href": "/api/v3/statuses/12", "title": "Closed"},
        "assignee": {"href": f"/api/v3/users/{OTHER_USER_ID}", "title": "Grace Hopper"},
    },
}

#: The work package as it really is once someone else has edited it.
WORK_PACKAGE_MOVED_ON: dict[str, Any] = {
    **WORK_PACKAGE_BEFORE_ACTION,
    "lockVersion": LOCK_VERSION + 3,
    "subject": "Ship the client layer (renamed)",
    "updatedAt": "2026-07-30T08:00:00Z",
}

CUSTOM_ACTION_CONFLICT: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
    "message": "Couldn't update the resource because of conflicting modifications.",
}

CUSTOM_ACTION_NOT_FOUND: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

CUSTOM_ACTION_FORBIDDEN: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission",
    "message": "You are not authorized to access this resource.",
}
