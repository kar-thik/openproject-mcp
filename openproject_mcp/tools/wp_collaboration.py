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
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import InputValidationError
from openproject_mcp.client.payloads import formattable_field
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools import _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["ActivityDetail", "ActivityEntry", "register"]

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
        default_factory=list,
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
    markup = entry.get("html") if isinstance(entry, Mapping) else None
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

    return ActivityDetail(
        field=_clean(field),
        from_=_clean(from_value),
        to=_clean(to_value),
        text=text,
    )


def _details_of(element: Mapping[str, Any]) -> list[ActivityDetail]:
    raw = element.get("details")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    return [_parse_detail(item) for item in raw]


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
