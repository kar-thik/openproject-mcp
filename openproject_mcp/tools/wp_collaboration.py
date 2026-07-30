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

Journal details are rendered sentences upstream ("Status changed from New to In
progress"). :func:`_parse_detail` turns them back into ``{field, from, to}``,
preferring the markup OpenProject emits alongside the plain text because a value
may itself contain the separator words.
"""

from __future__ import annotations

import html as html_text
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import InputValidationError, ValidationFailedError
from openproject_mcp.client.payloads import (
    build_write_payload,
    formattable_field,
    link,
    links_payload,
)
from openproject_mcp.projections import ListEnvelope, Ref, RelationRow
from openproject_mcp.tools import _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "ActivityDetail",
    "ActivityEntry",
    "RelationDeletionResult",
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
        description="True when the comment was cut to max_comment_chars (guarantee G1).",
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
