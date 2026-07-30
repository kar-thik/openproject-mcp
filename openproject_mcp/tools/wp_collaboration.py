"""Work-package collaboration tools (SPEC §6.3).

Lands here:

=====================================  ======  =======================================
Tool                                   Phase   Endpoint(s)
=====================================  ======  =======================================
🔍 ``list_work_package_comments``      1       ``GET /work_packages/{id}/activities``
✏️ ``add_work_package_comment``        1       ``POST /work_packages/{id}/activities``
✏️ ``edit_work_package_comment``       2       ``PATCH /activities/{id}``
✏️ ``add_work_package_watcher``        2       ``POST /work_packages/{id}/watchers``
✏️ ``remove_work_package_watcher``     2       ``DELETE /work_packages/{id}/watchers/{uid}``
✏️ ``create_work_package_relation``    2       ``POST /work_packages/{id}/relations``
✏️ ``update_work_package_relation``    2       ``PATCH /relations/{id}``
🗑 ``delete_work_package_relation``    2       ``DELETE /relations/{id}``
✏️Ⓜ ``toggle_comment_reaction``        3       ``PATCH /activities/{id}/emoji_reactions``
✏️ ``set_work_package_reminder``       3       ``/work_packages/{id}/reminders``
🔍 ``list_reminders``                  3       ``GET /reminders``
✏️ ``execute_custom_action``           3       ``POST /custom_actions/{id}/execute``
=====================================  ======  =======================================

Non-negotiables for this module:

* The activities endpoint is **unpaginated upstream** — fetch the full journal
  and page client-side, and say so in the description rather than pretending.
* Comment text is capped by ``max_comment_chars`` with a per-item ``truncated``
  marker (G1); ``activity_id`` fetches one entry uncapped.
* ``internal=true`` requires OpenProject ≥ 16: check
  ``(await ctx.probe()).supports_internal_comments`` and hard-error otherwise —
  older servers silently ignore the flag (G2).
* Relation ``type`` is a closed enum and ``lag`` is only valid on
  ``follows``/``precedes``; validate locally.
* Emoji reactions require OpenProject ≥ 16 too, but the gate is 404-tolerant
  rather than hard (SPEC §4.7): refuse only when the probe *positively* reports
  a pre-16 version. ``coreVersion`` is rendered on the API root for admins
  only, so a non-admin token leaves it null on a perfectly capable instance —
  in that case the call goes out and a 404 carries the version explanation.
* ``set_work_package_reminder`` is an upsert over an endpoint pair that has no
  upsert: read the work package's one active reminder, then create or patch. A
  409 on create means one appeared in between, so it becomes a patch — that is
  a different request, not a retried write (G6).

Journal details are rendered sentences upstream ("Status changed from New to In
progress"). :func:`_parse_detail` turns them back into ``{field, from, to}``,
preferring the markup OpenProject emits alongside the plain text because a value
may itself contain the separator words.
"""

from __future__ import annotations

import datetime as dt
import html as html_text
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    ConflictError,
    InputValidationError,
    NotFoundError,
    OpenProjectError,
    ValidationFailedError,
)
from openproject_mcp.client.locking import conflict_from_snapshot, resolve_lock_version
from openproject_mcp.client.payloads import (
    build_write_payload,
    formattable_field,
    link,
    links_payload,
)
from openproject_mcp.projections import ListEnvelope, Ref, RelationRow, WorkPackageRow
from openproject_mcp.tools import _shared
from openproject_mcp.version_probe import probe_root

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "ActivityDetail",
    "ActivityEntry",
    "CommentReaction",
    "CommentReactionState",
    "RelationDeletionResult",
    "ReminderResult",
    "ReminderRow",
    "WatcherResult",
    "register",
]

#: ``anthropic/maxResultSizeChars`` for the comment thread (SPEC §5.4).
MAX_RESULT_CHARS = 100_000

DEFAULT_COMMENT_PAGE_SIZE = 10
MAX_COMMENT_PAGE_SIZE = 100
MIN_COMMENT_CHARS = 50
MAX_COMMENT_CHARS = 50_000

UNPAGINATED_NOTE = (
    "OpenProject's work-package activities endpoint is unpaginated: the whole journal was "
    "fetched and this page was computed here. 'total' is the real number of journal entries."
)
SINGLE_ACTIVITY_NOTE = (
    "Single activity fetched by activity_id; its comment text is returned in full, "
    "unaffected by max_comment_chars."
)
JOURNAL_ORDER_NOTE = "Entries are ordered oldest first, as OpenProject stores the journal."

# --- journal detail parsing -----------------------------------------------

_LABEL_RE = re.compile(r"<(strong|b)>(.*?)</\1>", re.DOTALL)
_VALUE_RE = re.compile(r"<(i|em)>(.*?)</\1>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# OpenProject's journal sentences (config/locales/en.yml: text_journal_*).
_CHANGED_RE = re.compile(r"^(?P<field>.+?) changed from (?P<from>.*) to (?P<to>.*)$", re.DOTALL)
_SET_RE = re.compile(r"^(?P<field>.+?) set to (?P<to>.*)$", re.DOTALL)
_DELETED_RE = re.compile(r"^(?P<field>.+?) deleted \((?P<from>.*)\)$", re.DOTALL)
_ADDED_RE = re.compile(r"^(?P<field>.+?) (?P<to>.+) added$", re.DOTALL)
_UPDATED_RE = re.compile(r"^(?P<field>.+?) (?:updated|changed)$", re.DOTALL)


class ActivityDetail(BaseModel):
    """One field change inside a journal entry, parsed into its parts."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    field: str | None = Field(
        default=None,
        description="Changed field as OpenProject labels it, e.g. 'Status', 'Start date'.",
    )
    from_: str | None = Field(
        default=None,
        alias="from",
        description="Previous value; null when the field was set for the first time.",
    )
    to: str | None = Field(
        default=None,
        description="New value; null when the field was cleared.",
    )
    text: str | None = Field(
        default=None,
        description="The change as OpenProject phrases it, kept verbatim for anything "
        "the parser could not split (attachment adds, diff-only notes).",
    )


class ActivityEntry(BaseModel):
    """One journal entry: a comment, a set of field changes, or both.

    OpenProject aggregates a comment and the edits saved with it into a single
    journal entry, so ``kind='comment'`` entries can still carry ``details``.
    """

    id: int | str | None = Field(
        default=None,
        description="Activity id. Pass it back as list_work_package_comments(activity_id=...) "
        "to read a truncated comment in full.",
    )
    kind: Literal["comment", "field_change"] = Field(
        description="'comment' when the entry carries comment text, 'field_change' otherwise."
    )
    author: Ref | None = Field(default=None, description="User who wrote the entry.")
    work_package: Ref | None = Field(default=None, description="Work package the entry belongs to.")
    comment: str | None = Field(
        default=None,
        description="Comment body as markdown (raw); html is dropped. Cut to max_comment_chars "
        "when 'truncated' is true.",
    )
    truncated: bool = Field(
        default=False,
        description="True when the comment was cut to max_comment_chars.",
    )
    comment_length: int | None = Field(
        default=None,
        description="Character length of the untruncated comment; set only when truncated.",
    )
    internal: bool = Field(
        default=False,
        description="True for internal (project-member-only) comments; OpenProject >= 16.",
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(
        default=None, description="ISO 8601 UTC timestamp of the last edit."
    )
    version: int | None = Field(
        default=None, description="Journal version number of this entry within the work package."
    )
    details: list[ActivityDetail] = Field(
        default_factory=list[ActivityDetail],
        description="Field changes recorded with this entry; always a list, empty for a "
        "comment-only entry.",
    )


def _clean(markup: str | None) -> str | None:
    """Strip tags and unescape entities from a rendered journal fragment."""
    if markup is None:
        return None
    stripped = html_text.unescape(_TAG_RE.sub("", markup)).strip()
    return stripped or None


def _structure(text: str) -> tuple[str | None, str | None, str | None]:
    """Split a journal sentence into ``(field, from, to)``."""
    for pattern in (_CHANGED_RE, _SET_RE, _DELETED_RE, _ADDED_RE, _UPDATED_RE):
        match = pattern.match(text)
        if match:
            groups = match.groupdict()
            return (
                groups.get("field"),
                groups.get("from"),
                groups.get("to"),
            )
    return None, None, None


def _parse_detail(entry: Any) -> ActivityDetail:
    """Parse one ``details`` element into ``{field, from, to}`` plus its text.

    The plain sentence alone is ambiguous — a value containing " to " would be
    split in the wrong place — so the field name and the values are taken from
    the markup OpenProject renders next to it whenever the shapes agree.
    """
    text = hal.formattable(entry)
    element = hal.as_object(entry)
    markup = element.get("html") if element is not None else None
    field, from_value, to_value = _structure(text or "")

    if isinstance(markup, str):
        label = _LABEL_RE.search(markup)
        if label:
            field = _clean(label.group(2)) or field
        values = [_clean(match.group(2)) for match in _VALUE_RE.finditer(markup)]
        if len(values) == 2 and from_value is not None and to_value is not None:
            from_value, to_value = values
        elif len(values) == 1:
            if from_value is None and to_value is not None:
                to_value = values[0]
            elif to_value is None and from_value is not None:
                from_value = values[0]

    # model_validate rather than the constructor: the field is named ``from_``
    # but carries the ``from`` alias, and ``from`` cannot be a keyword argument.
    return ActivityDetail.model_validate(
        {
            "field": _clean(field),
            "from_": _clean(from_value),
            "to": _clean(to_value),
            "text": text,
        }
    )


def _details_of(element: Mapping[str, Any]) -> list[ActivityDetail]:
    return [_parse_detail(item) for item in hal.as_array(element.get("details")) or ()]


def _activity_entry(element: Mapping[str, Any], *, max_chars: int | None) -> ActivityEntry:
    """Project one activity resource, truncating the comment when asked."""
    raw_comment = hal.formattable(element.get("comment"))
    has_comment = bool(raw_comment and raw_comment.strip())
    # Field-change entries carry an empty formattable rather than no comment.
    comment = raw_comment if has_comment else None
    truncated = False
    comment_length: int | None = None
    if comment is not None and max_chars is not None and len(comment) > max_chars:
        comment_length = len(comment)
        comment = comment[:max_chars]
        truncated = True
    version = element.get("version")
    return ActivityEntry(
        id=hal.self_id(element),
        kind="comment" if has_comment else "field_change",
        author=Ref.from_hal(element, "user"),
        work_package=Ref.from_hal(element, "workPackage"),
        comment=comment,
        truncated=truncated,
        comment_length=comment_length,
        internal=bool(element.get("internal")),
        created_at=element.get("createdAt"),
        updated_at=element.get("updatedAt"),
        version=version if isinstance(version, int) and not isinstance(version, bool) else None,
        details=_details_of(element),
    )


def _require_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise InputValidationError(
            f"{name}={value} is out of range.",
            hint=f"{name} must be between {low} and {high}.",
        )


# --- Phase 2: watchers and relations --------------------------------------

#: The relation vocabulary OpenProject accepts (``Relation::TYPES``).
RelationType = Literal[
    "relates",
    "precedes",
    "follows",
    "blocks",
    "blocked",
    "duplicates",
    "duplicated",
    "includes",
    "partof",
    "requires",
    "required",
]

#: Only scheduling relations carry a lag; every other type ignores it.
LAG_RELATION_TYPES: frozenset[str] = frozenset({"follows", "precedes"})


class WatcherResult(BaseModel):
    """Outcome of adding or removing one watcher."""

    work_package_id: int = Field(description="Work package whose watcher list was changed.")
    user: Ref | None = Field(
        default=None,
        description="The watcher. 'id' is the user id; 'name' is filled in only when "
        "OpenProject returned the user resource (it does on add, not on remove).",
    )
    watching: bool = Field(
        description="Watch state after the call: true after adding, false after removing."
    )
    changed: bool | None = Field(
        default=None,
        description="True when this call actually changed the watcher list, false when the user "
        "was already watching. Null when OpenProject does not report it — removals answer "
        "204 whether or not the user was watching.",
    )
    message: str = Field(description="Human-readable confirmation.")


class RelationDeletionResult(BaseModel):
    """Outcome of ``delete_work_package_relation``."""

    id: int = Field(description="Id of the relation that was deleted.")
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    message: str = Field(description="Human-readable confirmation.")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Best-effort JSON body of a write response; ``{}`` when there is none.

    Used where the *status code* carries meaning the body does not, so the
    response object has to be handled directly instead of via ``post_json``.
    """
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    body = hal.as_object(payload)
    return dict(body) if body is not None else {}


def _is_comment_activity(payload: Mapping[str, Any]) -> bool:
    """True when a journal entry carries comment text — only those are editable."""
    if hal.formattable(payload.get("comment")):
        return True
    return payload.get("_type") == "Activity::Comment"


def _user_ref(payload: Mapping[str, Any], fallback_id: int) -> Ref:
    resolved = hal.self_id(payload)
    return Ref(
        id=resolved if resolved is not None else fallback_id,
        name=_text(payload.get("name")),
    )


def _validate_lag(relation_type: str | None, lag: int | None) -> None:
    """Refuse a lag on a relation type that cannot schedule (SPEC §6.3)."""
    if lag is None or relation_type in LAG_RELATION_TYPES:
        return
    raise InputValidationError(
        f"lag is only valid on 'follows' and 'precedes' relations (this one is "
        f"{relation_type or 'of unknown type'}).",
        hint=(
            "Lag is the scheduling gap between a predecessor and its successor, so OpenProject "
            "stores it for follows/precedes only. Drop lag, or set type='follows' (the 'from' "
            "work package runs after the 'to' one) or type='precedes' (it runs before) in the "
            "same call."
        ),
    )


async def _patch_comment(
    context: _shared.ToolContext, activity_id: int, comment: str
) -> dict[str, Any]:
    """PATCH an activity's comment, tolerating both wire shapes it has shipped with.

    ``PATCH /activities/{id}`` declares ``comment`` as a plain string — that is
    what OpenProject's own request specs send and it has not changed since the
    endpoint was written — while the published OpenAPI schema documents the
    formattable ``{"raw": ...}`` object. A rejected parameter is refused before
    the journal is touched, so nothing has been written when the second shape is
    tried; the first rejection is what gets reported if that fails too.
    """
    path = f"activities/{activity_id}"
    try:
        return await context.client.patch_json(path, json={"comment": comment})
    except ValidationFailedError as rejected:
        first = rejected
    try:
        return await context.client.patch_json(path, json={"comment": formattable_field(comment)})
    except ValidationFailedError:
        raise first from None


# --- Phase 3: reactions, reminders and custom actions ---------------------

#: The reaction vocabulary OpenProject accepts (``EmojiReaction::EMOJI_MAP``).
ReactionName = Literal[
    "thumbs_up",
    "thumbs_down",
    "grinning_face_with_smiling_eyes",
    "confused_face",
    "heart",
    "party_popper",
    "rocket",
    "eyes",
]

#: Sentinel that keeps "leave the reminder time alone" apart from an explicit
#: null, which is the documented way to clear the reminder.
KEEP = "__unchanged__"

#: Reminders are a small fetched-in-full collection (SPEC §9.3); this is the
#: ceiling one page is allowed to hold before the envelope says so (G1).
REMINDERS_PAGE_SIZE = 100

REMINDER_SCOPE_NOTE = (
    "Reminders are personal: this lists only the reminders YOU created that are still "
    "upcoming. Ones already delivered or completed are gone from the API, and other "
    "people's reminders are never visible."
)
REACTION_OWNER_UNKNOWN_NOTE = (
    "Could not tell whether the reaction is yours: this instance did not report the "
    "authenticated user on its API root, so 'reacted' is null. 'reactions' is the full "
    "current state either way."
)


class CommentReaction(BaseModel):
    """One reaction group on a comment: an emoji and everyone who picked it."""

    reaction: str | None = Field(
        default=None, description="Reaction name, e.g. 'thumbs_up' or 'rocket'."
    )
    emoji: str | None = Field(default=None, description="The emoji character itself, e.g. '🚀'.")
    count: int = Field(default=0, description="How many people reacted with this emoji.")
    users: list[Ref] = Field(
        default_factory=list[Ref],
        description="Users who reacted with it; always a list, never empty for a group "
        "OpenProject still reports.",
    )
    first_reaction_at: str | None = Field(
        default=None, description="ISO 8601 UTC timestamp of the first reaction of this kind."
    )


class CommentReactionState(BaseModel):
    """The reactions on a comment after one of them was toggled."""

    activity_id: int = Field(description="Activity (journal entry) whose reactions these are.")
    reaction: ReactionName = Field(description="The reaction this call toggled.")
    reacted: bool | None = Field(
        default=None,
        description="True when YOU now react with it, false when this call removed your "
        "reaction. Null when the authenticated user could not be determined.",
    )
    reactions: list[CommentReaction] = Field(
        default_factory=list[CommentReaction],
        description="Every reaction on the comment after the toggle; always a list, empty "
        "once the last one is removed.",
    )
    notes: list[str] = Field(
        default_factory=list[str],
        description="Degradation notes; always a list, usually empty.",
    )
    message: str = Field(description="Human-readable confirmation.")


class ReminderRow(BaseModel):
    """One personal reminder on a work package."""

    id: int | str | None = Field(
        default=None,
        description="Reminder id. Personal and short-lived — set_work_package_reminder "
        "finds it by work package, so this is for reporting, not for addressing.",
    )
    remind_at: str | None = Field(
        default=None, description="ISO 8601 UTC timestamp the reminder fires at."
    )
    note: str | None = Field(
        default=None, description="Free-text note shown with the reminder; null when unset."
    )
    work_package: Ref | None = Field(
        default=None, description="Work package the reminder is attached to."
    )
    creator: Ref | None = Field(
        default=None, description="Who created it — always the authenticated user."
    )


class ReminderResult(BaseModel):
    """Outcome of ``set_work_package_reminder``."""

    work_package_id: int = Field(description="Work package whose reminder was set or cleared.")
    action: Literal["created", "updated", "deleted", "unchanged"] = Field(
        description="What actually happened: 'created' a new reminder, 'updated' the existing "
        "one, 'deleted' it, or 'unchanged' when there was nothing to clear."
    )
    reminder: ReminderRow | None = Field(
        default=None,
        description="The reminder after the call; the deleted one when action='deleted', "
        "null when there was none.",
    )
    message: str = Field(description="Human-readable confirmation.")


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _comment_reaction(element: Mapping[str, Any]) -> CommentReaction:
    """Project one ``EmojiReaction`` group; the count falls back to the user list."""
    users = Ref.list_from_hal(element, "reactingUsers")
    count = _optional_int(element.get("reactionsCount"))
    return CommentReaction(
        reaction=_text(element.get("reaction")),
        emoji=_text(element.get("emoji")),
        count=count if count is not None else len(users),
        users=users,
        first_reaction_at=_text(element.get("firstReactionAt")),
    )


def _reminder_row(element: Mapping[str, Any]) -> ReminderRow:
    return ReminderRow(
        id=hal.self_id(element),
        remind_at=_text(element.get("remindAt")),
        note=_text(element.get("note")),
        work_package=Ref.from_hal(element, "remindable"),
        creator=Ref.from_hal(element, "creator"),
    )


async def _authenticated_user_id(context: _shared.ToolContext) -> int | str | None:
    """The current user's id, from the API root the feature probe already cached.

    The probe fetched ``GET /api/v3`` a moment ago and the root names the
    authenticated user, so this costs no extra round trip. A failure is not fatal:
    the caller degrades into a note rather than losing the reaction state (G5).
    """
    try:
        root = await probe_root(context.client, context.cache)
    except OpenProjectError:
        return None
    me = hal.ref(root, "user")
    return me.id if me is not None else None


def _iso_datetime(value: str, field_name: str) -> str:
    """Validate an ISO 8601 datetime that carries a timezone (SPEC §5.8).

    A reminder fires at an instant, so a naive local time is refused instead of
    being read as UTC — guessing the zone is precisely the fabricated default G3
    rules out, and the mistake only surfaces hours later.
    """
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise InputValidationError(
            f"{field_name}={value!r} is not an ISO 8601 datetime.",
            hint=(
                f"{field_name} must be an ISO 8601 datetime with a timezone, "
                "e.g. '2026-08-03T09:00:00Z'."
            ),
        ) from exc
    if parsed.tzinfo is None:
        raise InputValidationError(
            f"{field_name}={value!r} carries no timezone.",
            hint=(
                f"{field_name} must state its offset so the reminder fires at the instant you "
                "mean, e.g. '2026-08-03T09:00:00Z' (UTC) or '2026-08-03T09:00:00+02:00'. "
                "Neither the server's nor the user's timezone is assumed."
            ),
        )
    return candidate


async def _active_reminder(
    context: _shared.ToolContext, work_package_id: int
) -> tuple[dict[str, Any] | None, int | str | None]:
    """The work package's one active reminder, if it has one, plus its id.

    ``GET /work_packages/{id}/reminders`` only ever returns upcoming, undelivered
    reminders created by the authenticated user, which is exactly the set the
    upsert may touch.
    """
    payload = await context.client.get_json(f"work_packages/{work_package_id}/reminders")
    elements = hal.collection(payload).elements
    if not elements:
        return None, None
    existing = elements[0]
    return existing, hal.self_id(existing)


def _reaction_not_found(exc: NotFoundError, activity_id: int) -> NotFoundError:
    return NotFoundError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=(
            f"No comment with activity id {activity_id} is readable here. Activity ids come "
            "from list_work_package_comments (an entry with kind='comment') or from "
            "add_work_package_comment — a work package id is not one. A 404 can also mean the "
            "instance does not expose emoji reactions at all; get_instance_info reports the "
            "detected version."
        ),
    )


def _custom_action_not_found(exc: NotFoundError, custom_action_id: int) -> NotFoundError:
    return NotFoundError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=(
            f"No custom action with id {custom_action_id}. Custom actions are defined per "
            "instance by an administrator and are only offered on work packages whose state "
            "matches their conditions — list the ones available on a given work package with "
            "get_work_package(id=..., include=['custom_actions'])."
        ),
    )


def register(mcp: FastMCP) -> None:
    """Register the collaboration tools. Phase 1 fills in the two comment tools."""

    @mcp.tool(
        name="list_work_package_comments",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.READ),
        annotations=_shared.read_annotations(
            title="List work package comments",
            max_result_chars=MAX_RESULT_CHARS,
        ),
    )
    @_shared.tool_errors
    async def list_work_package_comments(
        id: Annotated[
            int,
            Field(
                description="Work package id. Get it from search_work_packages, "
                "list_work_packages or get_work_package — never guess it."
            ),
        ],
        page: Annotated[
            int,
            Field(
                description="1-based page number over the journal. Entries are oldest first, "
                "so the newest comments are on the LAST page."
            ),
        ] = 1,
        page_size: Annotated[
            int,
            Field(description="Journal entries per page, 1-100. Keep it small: comments are long."),
        ] = DEFAULT_COMMENT_PAGE_SIZE,
        max_comment_chars: Annotated[
            int,
            Field(
                description="Per-comment character cap, 50-50000. A cut comment comes back with "
                "truncated=true and comment_length; re-read it in full via activity_id."
            ),
        ] = 2000,
        activity_id: Annotated[
            int | None,
            Field(
                description="Read exactly one journal entry, uncapped, instead of a page. The id "
                "comes from a previous call to this tool. Must belong to work package `id`."
            ),
        ] = None,
    ) -> ListEnvelope[ActivityEntry]:
        """Read the comment thread and change history of a work package.

        Use this whenever the question is "what did people say about this
        ticket" or "what changed on it": it returns the full activity journal —
        comment entries (author, markdown text, internal flag, timestamps) and
        field-change entries whose details are parsed into
        ``{field, from, to}`` (for example
        ``{"field": "Status", "from": "New", "to": "In progress"}``).

        Returns the standard list envelope: ``items`` plus
        ``pagination{total,page,page_size,has_more}`` and ``notes``.

        Pitfalls. OpenProject's activities endpoint is **unpaginated** — this
        tool fetches the entire journal on every call and pages it here, so
        ``page``/``page_size`` cost the same upstream but keep the reply small.
        Entries are ordered oldest first, so ask for the last page to see the
        latest discussion. Comment text is cut at ``max_comment_chars`` and
        marked ``truncated: true``; pass that entry's ``id`` back as
        ``activity_id`` to read it in full.

        Cross-references: post a comment with ``add_work_package_comment``; the
        work package itself (description, custom fields, watchers) comes from
        ``get_work_package``; files referenced in a comment are listed by
        ``list_attachments``.
        """
        context = _shared.get_tool_context()
        _require_range("page", page, 1, 1_000_000)
        _require_range("page_size", page_size, 1, MAX_COMMENT_PAGE_SIZE)
        _require_range("max_comment_chars", max_comment_chars, MIN_COMMENT_CHARS, MAX_COMMENT_CHARS)

        if activity_id is not None:
            payload = await context.client.get_json(f"activities/{activity_id}")
            owner = hal.ref(payload, "workPackage")
            if owner is not None and owner.id is not None and str(owner.id) != str(id):
                raise InputValidationError(
                    f"Activity {activity_id} belongs to work package {owner.id}, not {id}.",
                    hint=(
                        f"Call list_work_package_comments(id={owner.id}, "
                        f"activity_id={activity_id}) instead, or drop activity_id to page "
                        f"through the journal of work package {id}."
                    ),
                )
            entry = _activity_entry(payload, max_chars=None)
            return _shared.build_envelope(
                [entry],
                total=1,
                page=1,
                page_size=1,
                notes=[SINGLE_ACTIVITY_NOTE],
            )

        payload = await context.client.get_json(f"work_packages/{id}/activities")
        journal = hal.collection(payload)
        total = len(journal.elements)
        start = (page - 1) * page_size
        window = journal.elements[start : start + page_size]
        entries = [_activity_entry(element, max_chars=max_comment_chars) for element in window]

        notes = [UNPAGINATED_NOTE, JOURNAL_ORDER_NOTE]
        cut = [str(entry.id) for entry in entries if entry.truncated]
        if cut:
            notes.append(
                f"{len(cut)} comment(s) cut at {max_comment_chars} characters "
                f"(activity ids {', '.join(cut)}); re-call with activity_id=<id> for the "
                "full text."
            )
        return _shared.build_envelope(
            entries, total=total, page=page, page_size=page_size, notes=notes
        )

    @mcp.tool(
        name="add_work_package_comment",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Add work package comment"),
    )
    @_shared.tool_errors
    async def add_work_package_comment(
        id: Annotated[
            int,
            Field(
                description="Work package id to comment on. Get it from search_work_packages "
                "or list_work_packages."
            ),
        ],
        comment: Annotated[
            str,
            Field(
                description="Comment body in markdown. OpenProject renders it; @-mentions need "
                "the user's mention syntax, plain names do not notify anyone."
            ),
        ],
        notify: Annotated[
            bool,
            Field(
                description="Send the usual watcher/assignee notifications. Set false for bulk "
                "or bookkeeping comments so inboxes stay quiet."
            ),
        ] = True,
        internal: Annotated[
            bool,
            Field(
                description="Post as an internal comment, visible only to project members with "
                "the internal-comments permission. Requires OpenProject >= 16; on older "
                "instances the call fails instead of posting publicly."
            ),
        ] = False,
    ) -> ActivityEntry:
        """Post a comment on a work package.

        Use this to reply in a ticket's thread, record a decision, or leave a
        handover note. Returns the created journal entry (activity id, author,
        markdown text, internal flag, timestamps) — the same shape
        ``list_work_package_comments`` returns, so the id can be reused.

        Pitfalls. Every call creates a new comment; it is not idempotent, so do
        not retry blindly after a timeout — read the thread first.
        ``internal=true`` is refused on OpenProject below 16.0 because those
        versions accept the flag and publish the comment anyway; upgrade or post
        publicly, deliberately. ``notify=false`` suppresses notifications only,
        the comment is still visible to everyone who can see the work package.

        Cross-references: read the thread with ``list_work_package_comments``;
        change fields (status, assignee, dates) with ``update_work_package``
        rather than describing the change in prose; attach a file with
        ``upload_attachment``.
        """
        context = _shared.get_tool_context()
        if not comment or not comment.strip():
            raise InputValidationError(
                "comment is empty.",
                hint="Pass the markdown text to post. OpenProject rejects blank comments.",
            )

        if internal:
            probe = await context.probe()
            if not probe.supports_internal_comments:
                reported = probe.core_version or "no version"
                raise InputValidationError(
                    f"internal=true needs OpenProject 16.0 or newer; this instance reports "
                    f"{reported}.",
                    hint=(
                        "Older versions accept the flag and publish the comment anyway, so this "
                        "call was refused rather than posting privately-intended text in public. "
                        "Re-call with internal=false to comment publicly, or upgrade the "
                        "instance. get_instance_info reports the detected version."
                    ),
                )

        body: dict[str, Any] = {"comment": formattable_field(comment)}
        if internal:
            body["internal"] = True

        payload = await context.client.post_json(
            f"work_packages/{id}/activities",
            json=body,
            params={"notify": "true" if notify else "false"},
        )
        return _activity_entry(payload, max_chars=None)

    @mcp.tool(
        name="edit_work_package_comment",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Edit work package comment", idempotent=True),
    )
    @_shared.tool_errors
    async def edit_work_package_comment(
        activity_id: Annotated[
            int,
            Field(
                description="Id of the journal entry to rewrite. It comes from "
                "list_work_package_comments (the 'id' of an entry with kind='comment') or from "
                "add_work_package_comment's result. It is an activity id, not a work package id."
            ),
        ],
        comment: Annotated[
            str,
            Field(
                description="The replacement body in markdown. It replaces the whole comment — "
                "there is no append mode, so read the current text first if you mean to add to "
                "it. @-mentions need OpenProject's mention syntax; plain names notify nobody."
            ),
        ],
    ) -> ActivityEntry:
        """Rewrite the text of an existing work-package comment.

        Use this to fix a typo, correct a wrong statement, or extend a note you
        just posted. Returns the updated journal entry (activity id, author,
        markdown text, internal flag, timestamps) in the same shape
        ``list_work_package_comments`` returns.

        Pitfalls. Only **comment** entries are editable: the journal also holds
        field-change entries ("Status changed from New to In progress"), which
        OpenProject records automatically and refuses to alter — this tool
        rejects those locally, before any write. Editing needs the
        edit-work-package-comments permission (or edit-own for your own
        comments); a 403 means the account may read the thread but not rewrite
        it. The edit replaces the text entirely and OpenProject keeps no
        API-visible history of the previous version, so do not use it to
        "undo" — post a correcting comment when the record matters. Editing does
        not notify anyone.

        Cross-references: read the thread and get activity ids with
        ``list_work_package_comments``; post a new comment with
        ``add_work_package_comment``; change fields (status, assignee, dates)
        with ``update_work_package`` instead of describing them in prose.
        """
        context = _shared.get_tool_context()
        if not comment or not comment.strip():
            raise InputValidationError(
                "comment is empty.",
                hint=(
                    "Pass the replacement markdown text. OpenProject rejects a blank comment, and "
                    "clearing a comment is not what editing is for — delete nothing, post a "
                    "correction instead."
                ),
            )

        current = await context.client.get_json(f"activities/{activity_id}")
        if not _is_comment_activity(current):
            raise InputValidationError(
                f"Activity {activity_id} is a field-change journal entry, not a comment.",
                hint=(
                    "Only comment entries can be edited; OpenProject writes field-change entries "
                    "itself and keeps them immutable. list_work_package_comments marks the "
                    "editable ones with kind='comment'. To change a field, call "
                    "update_work_package."
                ),
            )

        payload = await _patch_comment(context, activity_id, comment)
        return _activity_entry(payload, max_chars=None)

    @mcp.tool(
        name="add_work_package_watcher",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Add work package watcher", idempotent=True),
    )
    @_shared.tool_errors
    async def add_work_package_watcher(
        work_package_id: Annotated[
            int,
            Field(
                description="Work package to watch. Ids come from search_work_packages, "
                "list_work_packages or get_work_package."
            ),
        ],
        user_id: Annotated[
            int,
            Field(
                description="Numeric id of the user to add. Get it from list_users, from "
                "get_instance_info for the current user, or from any Ref in a work-package "
                "result. The string 'me' is not accepted here — the API needs a real id."
            ),
        ],
    ) -> WatcherResult:
        """Subscribe a user to a work package's notifications.

        Use this when someone should be kept in the loop on a ticket without
        being assigned to it. Watchers receive OpenProject's notifications for
        comments and changes. Returns the watcher (user id and name), the watch
        state after the call, and whether this call actually changed anything.

        Pitfalls. The user must already be able to see the work package;
        OpenProject answers 422 with a violation on ``user`` otherwise, and
        adding someone other than yourself needs the add-work-package-watchers
        permission (adding yourself only needs view access). Calling twice is
        harmless: the second call reports ``changed: false``. Watching is not
        assignment — use ``update_work_package(assignee=...)`` for that.

        Cross-references: ``get_work_package(include=['watchers'])`` lists who
        already watches; ``remove_work_package_watcher`` is the reverse;
        ``list_work_package_comments`` shows what watchers are being notified
        about.
        """
        context = _shared.get_tool_context()
        response = await context.client.request(
            "POST",
            f"work_packages/{work_package_id}/watchers",
            json=build_write_payload(links=links_payload(user=user_id)),
        )
        payload = _json_object(response)
        # OpenProject answers 201 for a new watcher and 200 when the user was
        # already watching — the only signal that distinguishes the two.
        created = response.status_code == 201
        user = _user_ref(payload, user_id)
        return WatcherResult(
            work_package_id=work_package_id,
            user=user,
            watching=True,
            changed=created,
            message=(
                f"{user.name or f'User {user_id}'} now watches work package #{work_package_id}."
                if created
                else (
                    f"{user.name or f'User {user_id}'} already watched work package "
                    f"#{work_package_id}; nothing changed."
                )
            ),
        )

    @mcp.tool(
        name="remove_work_package_watcher",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Remove work package watcher", idempotent=True),
    )
    @_shared.tool_errors
    async def remove_work_package_watcher(
        work_package_id: Annotated[
            int,
            Field(
                description="Work package to unsubscribe the user from. Ids come from "
                "search_work_packages, list_work_packages or get_work_package."
            ),
        ],
        user_id: Annotated[
            int,
            Field(
                description="Numeric id of the watcher to remove. "
                "get_work_package(include=['watchers']) lists the current watchers with their "
                "ids. The string 'me' is not accepted here."
            ),
        ],
    ) -> WatcherResult:
        """Unsubscribe a user from a work package's notifications.

        Use this to stop notifying someone who no longer needs the updates.
        Returns the user, the watch state after the call (always not watching)
        and a confirmation message.

        Pitfalls. OpenProject answers the same 204 whether or not the user was
        watching, so ``changed`` comes back null — do not report "removed" as
        proof that they were subscribed. Removing another user needs the
        delete-work-package-watchers permission; removing yourself only needs
        view access. A 404 means the *user* id is unknown (or the work package
        is), not that they were not watching. This does not unassign anyone and
        does not remove them from the project.

        Cross-references: ``get_work_package(include=['watchers'])`` shows who
        watches today; ``add_work_package_watcher`` is the reverse.
        """
        context = _shared.get_tool_context()
        await context.client.delete(f"work_packages/{work_package_id}/watchers/{user_id}")
        return WatcherResult(
            work_package_id=work_package_id,
            user=Ref(id=user_id),
            watching=False,
            changed=None,
            message=(
                f"User {user_id} no longer watches work package #{work_package_id}. "
                "OpenProject reports the same result whether or not they were watching before."
            ),
        )

    @mcp.tool(
        name="create_work_package_relation",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Create work package relation"),
    )
    @_shared.tool_errors
    async def create_work_package_relation(
        from_id: Annotated[
            int,
            Field(
                description="Work package the relation is read from — 'type' describes what this "
                "one does to the other. Ids come from search_work_packages or list_work_packages."
            ),
        ],
        to_id: Annotated[
            int,
            Field(
                description="The other work package. It must be visible to the account and "
                "different from from_id."
            ),
        ],
        type: Annotated[
            RelationType,
            Field(
                description="How from_id relates to to_id. 'follows' schedules from_id after "
                "to_id, 'precedes' before it; 'blocks'/'blocked' express dependency without "
                "scheduling; 'duplicates'/'duplicated', 'includes'/'partof', "
                "'requires'/'required' come in mirrored pairs; 'relates' is the neutral link. "
                "Parent/child hierarchy is NOT a relation — set it with "
                "update_work_package(parent_id=...)."
            ),
        ],
        lag: Annotated[
            int | None,
            Field(
                description="Working days to keep between the two work packages. Valid only on "
                "'follows' and 'precedes'; passing it with any other type is refused here before "
                "the request is sent."
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Optional note explaining why the two work packages are linked."),
        ] = None,
    ) -> RelationRow:
        """Link two work packages (blocks, follows, duplicates, relates, ...).

        Use this to record a dependency the schedule or the reader needs to
        know about: "ship the client layer follows design sign-off", "this
        duplicates #4321". Returns the created relation with its id, type,
        reverse_type, both work packages, lag and description.

        Pitfalls. OpenProject stores one canonical direction per pair, so the
        passive spellings are rewritten on save: creating ``precedes`` from A to
        B comes back as B ``follows`` A, with ``from_work_package`` and
        ``to_work_package`` swapped. That is the same fact, not an error — read
        ``type`` and ``reverse_type`` from the result rather than assuming what
        you sent. Only one relation may
        exist between two work packages: a second one answers 409 conflict, and
        changing it means ``update_work_package_relation`` on the existing id. A
        relation that would close a scheduling cycle is rejected with a
        validation error. Creating a ``follows`` relation can move dates, since
        OpenProject reschedules the successor.

        Cross-references: ``get_work_package(include=['relations'])`` lists what
        a work package is already linked to and produces relation ids;
        ``update_work_package_relation`` edits one;
        ``delete_work_package_relation`` removes it; parent/child hierarchy goes
        through ``update_work_package(parent_id=...)``.
        """
        context = _shared.get_tool_context()
        if from_id == to_id:
            raise InputValidationError(
                f"A work package cannot be related to itself (from_id and to_id are both "
                f"{from_id}).",
                hint="Pass the two different work packages that should be linked.",
            )
        _validate_lag(type, lag)

        attributes: dict[str, Any] = {"type": type}
        if lag is not None:
            attributes["lag"] = lag
        if description is not None:
            attributes["description"] = description

        payload = await context.client.post_json(
            f"work_packages/{from_id}/relations",
            json=build_write_payload(attributes, {"to": link("work_packages", to_id)}),
        )
        return RelationRow.from_hal(payload)

    @mcp.tool(
        name="update_work_package_relation",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(
            title="Update work package relation", idempotent=True
        ),
    )
    @_shared.tool_errors
    async def update_work_package_relation(
        relation_id: Annotated[
            int,
            Field(
                description="Id of the relation to change. It comes from "
                "create_work_package_relation or get_work_package(include=['relations']) — it is "
                "not a work package id."
            ),
        ],
        type: Annotated[
            RelationType | None,
            Field(
                description="New relation type. Omit to keep the current one. Switching to or "
                "from 'follows'/'precedes' changes whether the relation schedules dates."
            ),
        ] = None,
        lag: Annotated[
            int | None,
            Field(
                description="New lag in working days; valid only while the relation is "
                "'follows' or 'precedes'. Omit to leave it untouched, pass 0 to remove an "
                "existing lag. When lag is given without type, the current type is read first "
                "and a lag on a non-scheduling relation is refused before any write."
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="New note for the relation. Omit to leave it untouched; pass an "
                "empty string to clear it."
            ),
        ] = None,
    ) -> RelationRow:
        """Change an existing relation's type, lag or description.

        Use it to widen the gap between a predecessor and its successor, to
        correct a link that was created with the wrong type, or to explain why
        two work packages are connected. Returns the updated relation.

        Pitfalls. At least one of type, lag or description must be given.
        Relations carry no lock version, so this is a plain overwrite with no
        conflict detection — a concurrent edit is silently replaced; re-read the
        relation if that matters. The two work packages cannot be changed here:
        delete the relation and create a new one instead. Raising the lag on a
        ``follows`` relation reschedules the successor, so dates can move.

        Cross-references: ``create_work_package_relation`` makes one;
        ``delete_work_package_relation`` removes it;
        ``get_work_package(include=['relations'])`` lists the ids.
        """
        context = _shared.get_tool_context()
        if type is None and lag is None and description is None:
            raise InputValidationError(
                "Nothing to update: type, lag and description were all omitted.",
                hint=(
                    "Pass at least one of type, lag or description. To remove the relation "
                    "entirely use delete_work_package_relation."
                ),
            )

        effective_type = type
        if lag is not None and type is None:
            current = await context.client.get_json(f"relations/{relation_id}")
            effective_type = _text(current.get("type"))
        _validate_lag(effective_type, lag)

        attributes: dict[str, Any] = {}
        if type is not None:
            attributes["type"] = type
        if lag is not None:
            attributes["lag"] = lag
        if description is not None:
            attributes["description"] = description

        payload = await context.client.patch_json(
            f"relations/{relation_id}", json=build_write_payload(attributes)
        )
        return RelationRow.from_hal(payload)

    @mcp.tool(
        name="delete_work_package_relation",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE, _shared.DESTRUCTIVE),
        annotations=_shared.destructive_annotations(title="Delete work package relation"),
    )
    @_shared.tool_errors
    async def delete_work_package_relation(
        relation_id: Annotated[
            int,
            Field(
                description="Id of the relation to remove permanently. It comes from "
                "create_work_package_relation or get_work_package(include=['relations'])."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Must be true. Ask the user first — the API offers no undo. Calling "
                "with confirm=false returns a confirmation_required error and deletes nothing."
            ),
        ] = False,
    ) -> RelationDeletionResult:
        """Remove the link between two work packages.

        Use it when a dependency no longer holds. Neither work package is
        touched, only the relation between them. Returns a small confirmation
        object.

        Pitfalls. Deleting a ``follows`` relation drops the scheduling
        constraint, so OpenProject may reschedule the work package that was
        waiting. There is no undo; recreating the relation with
        ``create_work_package_relation`` is the only way back. A second delete
        of the same id answers 404. Parent/child hierarchy is not a relation —
        clear it with ``update_work_package(parent_id=null)``.

        Cross-references: ``get_work_package(include=['relations'])`` shows what
        would be removed; ``update_work_package_relation`` changes a relation
        instead of removing it.
        """
        _shared.require_confirmation(
            confirm,
            action="delete relation",
            target=f"#{relation_id}",
            consequence=(
                "The link between the two work packages is removed permanently and any "
                "scheduling it enforced is dropped."
            ),
        )
        context = _shared.get_tool_context()
        await context.client.delete(f"relations/{relation_id}")
        return RelationDeletionResult(
            id=relation_id,
            deleted=True,
            message=f"Relation #{relation_id} was deleted; both work packages are unchanged.",
        )

    @mcp.tool(
        name="toggle_comment_reaction",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Toggle comment reaction"),
    )
    @_shared.tool_errors
    async def toggle_comment_reaction(
        activity_id: Annotated[
            int,
            Field(
                description="Id of the comment to react to. It comes from "
                "list_work_package_comments (the 'id' of an entry with kind='comment') or from "
                "add_work_package_comment's result — it is an activity id, not a work package id."
            ),
        ],
        reaction: Annotated[
            ReactionName,
            Field(
                description="Which emoji to toggle. OpenProject accepts exactly eight: "
                "thumbs_up (👍), thumbs_down (👎), grinning_face_with_smiling_eyes (😄), "
                "confused_face (😕), heart (❤️), party_popper (🎉), rocket (🚀), eyes (👀). "
                "Passing the emoji character itself is rejected — use these names."
            ),
        ],
    ) -> CommentReactionState:
        """React to a work-package comment with an emoji, or take your reaction back.

        Use this for the lightweight acknowledgement a comment does not deserve:
        👍 on a decision, 👀 to say you are looking at it, 🎉 when something
        shipped. Returns the comment's full reaction state afterwards — every
        emoji on it with the people who picked it — plus ``reacted``, which says
        whether you are now among them.

        Pitfalls. This is a **toggle**, not an add: calling it twice with the
        same reaction leaves the comment exactly as it started, so it is not safe
        to retry blindly after a timeout — read ``reacted`` from the result
        instead. Reactions belong to the authenticated account; you cannot react
        on someone else's behalf and cannot remove their reaction. Only comment
        entries can be reacted to — a field-change journal entry ("Status changed
        from New to In progress") is refused by OpenProject with a 400. The
        feature needs OpenProject 16.0 or newer; an instance known to be older
        is refused up front with a version hint rather than pretending to have
        reacted, and one whose version is not readable answers 404 instead.

        Cross-references: ``list_work_package_comments`` reads the thread and
        produces activity ids; ``add_work_package_comment`` says something in
        words when an emoji is not enough; ``get_instance_info`` reports the
        detected OpenProject version.
        """
        context = _shared.get_tool_context()
        probe = await context.probe()
        # Only a *known* pre-16 version blocks the call. OpenProject renders
        # coreVersion on the API root for admins alone, so an unknown version is
        # far more often a non-admin token than an old server — attempt the
        # PATCH and let a 404 explain itself (SPEC §4.7, 404-tolerant).
        if probe.core_version is not None and not probe.supports_emoji_reactions:
            raise InputValidationError(
                f"Emoji reactions need OpenProject 16.0 or newer; this instance reports "
                f"{probe.core_version}.",
                hint=(
                    "The /activities/{id}/emoji_reactions endpoint does not exist before 16.0, "
                    "so this call was refused instead of reporting a reaction that was never "
                    "recorded. Post an ordinary comment with add_work_package_comment, or "
                    "upgrade the instance. get_instance_info reports the detected version."
                ),
            )

        try:
            payload = await context.client.patch_json(
                f"activities/{activity_id}/emoji_reactions", json={"reaction": reaction}
            )
        except NotFoundError as exc:
            raise _reaction_not_found(exc, activity_id) from exc

        reactions = [_comment_reaction(element) for element in hal.collection(payload)]
        group = next((item for item in reactions if item.reaction == reaction), None)

        notes: list[str] = []
        me = await _authenticated_user_id(context)
        reacted: bool | None
        if me is None:
            reacted = None
            notes.append(REACTION_OWNER_UNKNOWN_NOTE)
        else:
            reacted = group is not None and any(str(user.id) == str(me) for user in group.users)

        emoji = group.emoji if group is not None else None
        label = f"{reaction} {emoji}" if emoji else reaction
        if reacted is None:
            message = (
                f"Toggled {label} on comment #{activity_id}; 'reactions' is the resulting state."
            )
        elif reacted:
            others = (group.count - 1) if group is not None else 0
            message = f"You reacted {label} to comment #{activity_id}" + (
                f", along with {others} other(s)." if others > 0 else "."
            )
        else:
            message = f"Your {label} reaction was removed from comment #{activity_id}."

        return CommentReactionState(
            activity_id=activity_id,
            reaction=reaction,
            reacted=reacted,
            reactions=reactions,
            notes=notes,
            message=message,
        )

    @mcp.tool(
        name="set_work_package_reminder",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Set work package reminder", idempotent=True),
    )
    @_shared.tool_errors
    async def set_work_package_reminder(
        work_package_id: Annotated[
            int,
            Field(
                description="Work package to be reminded about. Ids come from "
                "search_work_packages, list_work_packages or get_work_package."
            ),
        ],
        remind_at: Annotated[
            str | None,
            Field(
                description="When to be reminded, as an ISO 8601 datetime WITH a timezone, "
                "e.g. '2026-08-03T09:00:00Z'. Pass null to delete the existing reminder. Omit "
                "the parameter entirely to keep the current time and only change the note."
            ),
        ] = KEEP,
        note: Annotated[
            str | None,
            Field(
                description="Short text shown with the reminder ('check the staging deploy'). "
                "Omit to leave an existing note untouched; pass an empty string to clear it."
            ),
        ] = None,
    ) -> ReminderResult:
        """Set, change or clear your personal reminder on a work package.

        Use this when something should resurface later: "remind me about this on
        Monday", "ping me an hour before the release". Reminders are private —
        only you see yours, and only you are notified. The tool upserts: it looks
        for your active reminder on the work package and creates one if there is
        none, updates it if there is. Returns ``action``
        (created/updated/deleted/unchanged) and the resulting reminder.

        Passing ``remind_at=null`` **deletes** the reminder. That is the
        documented way to clear it rather than a destructive operation — nothing
        but your own pending notification is removed, the work package and its
        history are untouched — so this tool does not ask for ``confirm``. Set a
        new time to get it back.

        Pitfalls. OpenProject allows exactly one active reminder per work package
        per person, so a second "create" becomes an update of the first — there
        is no way to stack two. ``remind_at`` must carry a timezone; a bare
        '2026-08-03T09:00' is refused rather than guessed at. A reminder in the
        past is rejected by the instance. Once a reminder has fired it disappears
        from the API, so a later call creates a fresh one rather than reviving
        it. Reminders are personal: you cannot set one for a colleague — add them
        as a watcher or mention them in a comment instead.

        Cross-references: ``list_reminders`` shows everything you have pending;
        ``add_work_package_watcher`` notifies someone about *changes* rather than
        at a chosen time; ``add_work_package_comment`` with an @-mention is how
        you get another person's attention.
        """
        context = _shared.get_tool_context()

        when: str | None = None
        if remind_at is not None and remind_at != KEEP:
            when = _iso_datetime(remind_at, "remind_at")
        if remind_at is None and note is not None:
            raise InputValidationError(
                "remind_at=null deletes the reminder, so note has nothing to attach to.",
                hint=(
                    "Drop note to delete the reminder, or pass a remind_at datetime to keep the "
                    "reminder and rewrite its note."
                ),
            )

        # Assembled before the read so a call with nothing in it costs no round trip.
        attributes: dict[str, Any] = {}
        if when is not None:
            attributes["remindAt"] = when
        if note is not None:
            attributes["note"] = note
        if remind_at is not None and not attributes:
            raise InputValidationError(
                "set_work_package_reminder was called with nothing to set.",
                hint=(
                    "Pass remind_at to schedule or move the reminder, note to rewrite its text, "
                    "or remind_at=null to delete it."
                ),
            )

        existing, existing_id = await _active_reminder(context, work_package_id)

        if remind_at is None:
            if existing is None or existing_id is None:
                return ReminderResult(
                    work_package_id=work_package_id,
                    action="unchanged",
                    reminder=None,
                    message=(
                        f"Work package #{work_package_id} has no active reminder of yours; "
                        "nothing was deleted."
                    ),
                )
            await context.client.delete(f"reminders/{existing_id}")
            return ReminderResult(
                work_package_id=work_package_id,
                action="deleted",
                reminder=_reminder_row(existing),
                message=(
                    f"Your reminder on work package #{work_package_id} was deleted; the work "
                    "package itself is unchanged."
                ),
            )

        if existing is not None and existing_id is not None:
            updated = await context.client.patch_json(f"reminders/{existing_id}", json=attributes)
            row = _reminder_row(updated)
            return ReminderResult(
                work_package_id=work_package_id,
                action="updated",
                reminder=row,
                message=(
                    f"Your reminder on work package #{work_package_id} now fires at "
                    f"{row.remind_at or 'the stored time'}."
                ),
            )

        if when is None:
            raise InputValidationError(
                f"Work package #{work_package_id} has no reminder of yours to update.",
                hint=(
                    "note alone can only rewrite an existing reminder. Pass remind_at as an ISO "
                    "8601 datetime with a timezone (e.g. '2026-08-03T09:00:00Z') to create one."
                ),
            )

        try:
            created = await context.client.post_json(
                f"work_packages/{work_package_id}/reminders", json=attributes
            )
        except ConflictError:
            # OpenProject allows one active reminder per work package and answers
            # 409 for a second: another client created one between the read above
            # and this write, so the upsert switches branch (SPEC §6.3). This is
            # not a retry of the failed write (G6) — it is a different request
            # against the reminder that now exists.
            racing, racing_id = await _active_reminder(context, work_package_id)
            if racing is None or racing_id is None:
                raise
            updated = await context.client.patch_json(f"reminders/{racing_id}", json=attributes)
            row = _reminder_row(updated)
            return ReminderResult(
                work_package_id=work_package_id,
                action="updated",
                reminder=row,
                message=(
                    f"A reminder already existed on work package #{work_package_id}, so it was "
                    f"updated instead; it now fires at {row.remind_at or 'the stored time'}."
                ),
            )

        row = _reminder_row(created)
        return ReminderResult(
            work_package_id=work_package_id,
            action="created",
            reminder=row,
            message=(
                f"You will be reminded about work package #{work_package_id} at "
                f"{row.remind_at or 'the requested time'}."
            ),
        )

    @mcp.tool(
        name="list_reminders",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.READ),
        annotations=_shared.read_annotations(title="List reminders"),
    )
    @_shared.tool_errors
    async def list_reminders() -> ListEnvelope[ReminderRow]:
        """List your own upcoming work-package reminders.

        Use this to answer "what have I asked to be reminded about", to check
        whether a reminder is already set before creating another one, or to find
        the work packages you deferred. Returns the standard list envelope:
        ``items`` of ``{id, remind_at, note, work_package, creator}`` plus
        ``pagination`` and ``notes``.

        Pitfalls. Reminders are personal — this only ever shows the ones the
        authenticated account created, never a colleague's, and there is no way
        to list someone else's. It only shows reminders that are still
        **upcoming**: once one has fired (or was completed) OpenProject drops it
        from this collection, so an empty result does not mean nothing was ever
        scheduled. The work package each reminder points at is in
        ``work_package``; a reminder is not a work package and its id is not one.

        Cross-references: ``set_work_package_reminder`` creates, moves or deletes
        one; ``list_notifications`` shows what OpenProject has actually notified
        you about, including fired reminders; ``get_work_package`` opens the
        ticket a reminder points at.
        """
        context = _shared.get_tool_context()
        payload = await context.client.get_json(
            "reminders", params={"pageSize": REMINDERS_PAGE_SIZE}
        )
        collection = hal.collection(payload)
        rows = [_reminder_row(element) for element in collection]

        notes = [REMINDER_SCOPE_NOTE]
        if collection.total > len(rows):
            notes.append(
                f"{collection.total} upcoming reminders exist; the first {len(rows)} are "
                "listed here."
            )
        return _shared.envelope_from_collection(
            collection, rows, page=1, page_size=REMINDERS_PAGE_SIZE, notes=notes
        )

    @mcp.tool(
        name="execute_custom_action",
        tags=_shared.tool_tags(_shared.GROUP_WP_COLLABORATION, _shared.WRITE),
        annotations=_shared.write_annotations(title="Execute custom action"),
    )
    @_shared.tool_errors
    async def execute_custom_action(
        custom_action_id: Annotated[
            int,
            Field(
                description="Id of the custom action to run. Get it from "
                "get_work_package(id=..., include=['custom_actions']), which lists the actions "
                "this instance defines AND this work package currently qualifies for — the "
                "names are instance-specific, so never guess an id."
            ),
        ],
        work_package_id: Annotated[
            int,
            Field(
                description="Work package to run the action on. It must be the one the action "
                "was listed for; conditions are re-checked server-side."
            ),
        ],
        lock_version: Annotated[
            int | None,
            Field(
                description="The work package's current lockVersion, from get_work_package. "
                "Omit it and the current value is read first — safe, one extra request. A stale "
                "value returns a conflict carrying the fresh one."
            ),
        ] = None,
    ) -> WorkPackageRow:
        """Run an instance-defined one-click action on a work package.

        Custom actions are shortcuts an administrator configured — "Accept and
        assign to me", "Reject", "Move to review" — that apply several field
        changes at once, sometimes under conditions (role, status, project).
        Use one when ``get_work_package(include=['custom_actions'])`` offers it,
        instead of reproducing its effects field by field. Returns the updated
        work package row (subject, type, status, priority, assignee, project,
        dates, percentage_done, updated_at).

        Pitfalls. The action decides what changes; this tool cannot influence it,
        and OpenProject does not report which fields it touched — compare the
        returned row with what you read before, or call ``get_work_package``
        again for the full detail (including the new ``lock_version`` for your
        next update). Availability is per work package: an action listed on one
        ticket may 403 on another because its conditions no longer hold, and a
        422 usually means the resulting work package would be invalid (a
        required field the action leaves empty). Writes are never retried
        automatically — a conflict comes back with the fresh ``lock_version`` so
        you can re-read and decide.

        Cross-references: ``get_work_package(include=['custom_actions'])``
        produces the ids and says which are available right now;
        ``update_work_package`` is the explicit alternative when you know exactly
        which fields to set; ``list_work_package_comments`` shows what the action
        recorded in the journal.
        """
        context = _shared.get_tool_context()
        path = f"work_packages/{work_package_id}"
        # Never default the lock version to 0 (SPEC §4.4): a failed read aborts
        # the write instead of silently clobbering a concurrent edit.
        resolved, snapshot = await resolve_lock_version(context.client, path, supplied=lock_version)
        body = build_write_payload(
            links={"workPackage": link("work_packages", work_package_id)},
            lock_version=resolved,
        )

        try:
            updated = await context.client.post_json(
                f"custom_actions/{custom_action_id}/execute", json=body
            )
        except NotFoundError as exc:
            raise _custom_action_not_found(exc, custom_action_id) from exc
        except ConflictError as conflict:
            fresh: dict[str, Any] | None
            try:
                fresh = await context.client.get_json(path)
            except OpenProjectError:
                fresh = dict(snapshot) if snapshot else None
            raise conflict_from_snapshot(conflict, fresh=fresh, attempted=body) from conflict

        return WorkPackageRow.from_hal(updated)
