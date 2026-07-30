"""News tools: read and write project announcements (SPEC §6.13).

Lands here:

=======================  ======  ==========================================
Tool                     Phase   Endpoint(s)
=======================  ======  ==========================================
🔍 ``list_news``         3       ``GET /news`` (+ ``project_id`` filter)
🔍 ``get_news``          3       ``GET /news/{id}``
✏️ ``create_news``       3       ``POST /news``
✏️ ``update_news``       3       ``PATCH /news/{id}``
🗑 ``delete_news``       3       ``DELETE /news/{id}``
=======================  ======  ==========================================

Non-negotiables for this module:

* **No form pre-flight.** The news resource mounts create and update directly;
  there is no ``/news/form`` to ask first, which is why SPEC §6.13 lists a bare
  ``POST``/``PATCH`` where ``create_version`` lists ``form → POST``. Rejections
  therefore arrive as the API's own 422 and every hint lives here.
* **The project filter is spelled ``project_id`` and takes integers.** It is one
  of the filter names OpenProject really does keep snake_case, and its values go
  through an integer strategy — a project *identifier* is not an integer and
  makes the whole query invalid. Identifiers are resolved to the numeric id
  before anything is sent, for the filter and for the create payload's link.
* News carries **no** ``lockVersion``, so the update is a plain PATCH that sends
  only the attributes the caller passed; fabricating a lock version would be
  worse than sending none (SPEC §4.4).
* News is a core resource, but every project switches the module on or off, and
  that absence is invisible on the wire: a project with news disabled simply
  contributes no rows and refuses writes with 403 (the ``manage_news``/
  ``view_news`` permissions belong to the module). Both cases are reported
  in-band — an empty page carries a note (G5) rather than being read back as
  "this project has made no announcements".
* Only ``title``, ``summary`` and ``description`` are writable. A news entry
  cannot be moved to another project after creation, and the author is always
  the authenticated account.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    PermissionDeniedError,
    UnexpectedResponseError,
)
from openproject_mcp.client.filters import (
    DEFAULT_PAGE_SIZE,
    Filter,
    FilterType,
    Op,
    normalize_value,
    pagination_params,
    register_filter_type,
    serialize_filters,
    validate_operator,
)
from openproject_mcp.client.payloads import build_write_payload, formattable_field, link
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    DESTRUCTIVE,
    GROUP_NEWS,
    READ,
    WRITE,
    ToolContext,
    destructive_annotations,
    envelope_from_collection,
    get_tool_context,
    read_annotations,
    require_confirmation,
    tool_errors,
    tool_tags,
    write_annotations,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "KEEP",
    "NewsDeletionResult",
    "NewsDetail",
    "NewsRow",
    "register",
]

#: Sentinel for update parameters that can be *cleared* as well as left alone.
KEEP = "__unchanged__"

#: Resource key for the news endpoint's own filter set.
NEWS_RESOURCE = "news"

#: The wire name of the news project filter. OpenProject keeps this one
#: snake_case, so it is built by hand: the shared snake→camel mapping would send
#: ``projectId`` and the override table it consults is not this module's to edit.
PROJECT_FILTER = "project_id"

#: Newest first, which is what "the latest news" means. The news endpoint's sort
#: keys are snake_case too (``id``, ``created_at``, ``updated_at``), so this is a
#: literal rather than something ``serialize_sort_by`` could produce.
NEWEST_FIRST = '[["created_at","desc"]]'

#: Links OpenProject renders only for accounts holding ``manage_news``.
MANAGE_LINKS = ("updateImmediately", "delete")

PROJECT_EMPTY_NOTE = (
    "no news is visible in project {project}: news only shows up where the project has the "
    "news module enabled and this account holds the 'view news' permission, so an empty page "
    "is not proof that the project published nothing"
)
INSTANCE_EMPTY_NOTE = (
    "no news is visible to this account anywhere on the instance: news only shows up in "
    "projects that have the news module enabled and where you hold the 'view news' permission"
)

WRITE_PERMISSION_HINT = (
    "Creating, changing and deleting news needs the 'manage news' permission in that project, "
    "which only exists while the project has the news module enabled. Check the project's "
    "modules with get_project(id_or_identifier=...), or ask a project administrator."
)


class NewsRow(BaseModel):
    """One news entry as list results return it — headline and summary only."""

    id: int | str | None = Field(
        default=None,
        description="News id — what get_news, update_news and delete_news consume.",
    )
    title: str | None = Field(default=None, description="Headline of the announcement.")
    summary: str | None = Field(
        default=None,
        description="Short teaser OpenProject shows under the headline; may be empty. The "
        "full body is NOT here — read it with get_news(news_id=...).",
    )
    project: Ref | None = Field(
        default=None, description="Project the announcement belongs to ({id, name})."
    )
    author: Ref | None = Field(
        default=None, description="User who published it ({id, name}); set by the server."
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC publication timestamp.")
    can_manage: bool = Field(
        default=False,
        description="True when this account may change or delete this entry (OpenProject "
        "renders the update/delete links only with the 'manage news' permission). False "
        "means update_news and delete_news would fail with 403.",
    )


class NewsDetail(NewsRow):
    """A single news entry with its full body, as get/create/update return it."""

    description: str | None = Field(
        default=None,
        description="The announcement body as markdown (raw); html is dropped. Empty string "
        "when the entry has only a headline and summary.",
    )
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class NewsDeletionResult(BaseModel):
    """Outcome of ``delete_news``."""

    id: int = Field(description="Id of the news entry that was deleted.")
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    message: str = Field(description="Human-readable confirmation.")


# --- projections ----------------------------------------------------------


def _can_manage(payload: Mapping[str, Any]) -> bool:
    """Whether the manage-gated links are rendered on this entry."""
    links = hal.as_object(payload.get("_links"))
    if links is None:
        return False
    return any(name in links for name in MANAGE_LINKS)


def _news_row(payload: Mapping[str, Any]) -> NewsRow:
    summary = payload.get("summary")
    return NewsRow(
        id=hal.self_id(payload),
        title=payload.get("title"),
        summary=summary if isinstance(summary, str) else None,
        project=Ref.from_hal(payload, "project"),
        author=Ref.from_hal(payload, "author"),
        created_at=payload.get("createdAt"),
        can_manage=_can_manage(payload),
    )


def _news_detail(payload: Mapping[str, Any]) -> NewsDetail:
    row = _news_row(payload)
    return NewsDetail(
        **row.model_dump(),
        description=hal.formattable(payload.get("description")),
        updated_at=payload.get("updatedAt"),
    )


# --- input helpers --------------------------------------------------------


def _project_filter(project_id: int) -> Filter:
    """The news project filter, built with its upstream snake_case wire name."""
    validate_operator(PROJECT_FILTER, Op.EQ, NEWS_RESOURCE)
    return Filter(name=PROJECT_FILTER, operator=Op.EQ, values=[normalize_value(project_id)])


def _positive_id(name: str, value: int) -> int:
    if value <= 0:
        raise InputValidationError(
            f"{name}={value} is not a valid id.",
            hint=f"{name} must be a positive integer. Ids come from the matching list_ tool.",
        )
    return value


async def _project_numeric_id(ctx: ToolContext, value: int | str) -> int:
    """Resolve a project id or URL identifier to the numeric id news needs.

    Both places a project reaches the API here take the numeric id: the
    ``project_id`` filter runs through an integer strategy that rejects anything
    else, and a ``_links.project`` href built from an identifier does not resolve
    to a project. So ``"demo"`` costs one lookup here instead of a confusing 400
    or 422 there.
    """
    if isinstance(value, bool):
        raise InputValidationError(
            "project_id must be a numeric id or the URL identifier string.",
            hint="Find both with list_projects.",
        )
    if isinstance(value, int):
        return _positive_id("project_id", value)
    text = value.strip()
    if not text:
        raise InputValidationError(
            "project_id is empty.",
            hint="Pass the numeric project id or its URL identifier; list_projects has both.",
        )
    if text.isdigit():
        return _positive_id("project_id", int(text))

    payload = await ctx.client.get_json(f"projects/{quote(text, safe='')}")
    raw = payload.get("id")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    resolved = hal.self_id(payload)
    if isinstance(resolved, int):
        return resolved
    raise UnexpectedResponseError(
        f"Project {text!r} was found but reported no numeric id.",
        hint="Pass the numeric project id from list_projects instead of the identifier.",
    )


def _news_not_found(exc: NotFoundError, news_id: int) -> NotFoundError:
    return NotFoundError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=(
            f"No news entry with id {news_id} that this account can see. Ids come from "
            "list_news(project_id=...), never from the headline. A 404 also covers 'the entry "
            "exists in a project whose news you may not read' — the news module can be "
            "switched off per project."
        ),
    )


def _news_forbidden(exc: PermissionDeniedError) -> PermissionDeniedError:
    return PermissionDeniedError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=WRITE_PERMISSION_HINT,
    )


def register(mcp: FastMCP) -> None:
    """Register the news tools (SPEC §6.13)."""
    # The news endpoint has its own filter set; teach the validator its one name
    # instead of editing the shared table (SPEC §9.1). It is list_optional
    # upstream, so '*' and '!*' are accepted alongside '=' and '!'.
    register_filter_type(PROJECT_FILTER, FilterType.LIST_OPTIONAL, NEWS_RESOURCE)

    @mcp.tool(
        name="list_news",
        tags=tool_tags(GROUP_NEWS, READ),
        annotations=read_annotations(title="List news"),
    )
    @tool_errors
    async def list_news(
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric project id (or URL identifier, resolved for you) to list only "
                    "that project's announcements. Comes from list_projects. Omit it for "
                    "every announcement visible to you across the instance."
                )
            ),
        ] = None,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="News entries per page (max 100).")
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[NewsRow]:
        """List project news — the announcements a team publishes on its project overview.

        Use it to answer "what was announced recently", to find the id of an entry before
        reading, editing or deleting it, or to check whether a report was already published.
        Results come back newest first (sorted by creation date descending).

        Returns the standard list envelope: ``items`` of ``{id, title, summary, project,
        author, created_at, can_manage}`` plus ``pagination`` and ``notes``. Rows carry the
        short ``summary`` only — the full markdown body is fetched per entry with
        ``get_news(news_id=...)``, which keeps a long announcement out of a listing.
        ``can_manage`` tells you in advance whether ``update_news``/``delete_news`` would be
        allowed for that row.

        Pitfalls: news is only visible where the project has the news module enabled and this
        account holds the 'view news' permission, and neither absence is an error — an empty
        page carries a ``notes`` entry saying so, and reporting "this project has no
        announcements" without reading it would be wrong. The project scope is matched by
        numeric id; an identifier is resolved with one extra lookup, so an unknown identifier
        fails as not_found rather than silently listing the whole instance.

        Cross-references: ``get_news`` for the full body of one entry; ``create_news`` to
        publish one; ``list_projects`` for project ids; work-package discussion lives in
        ``list_work_package_comments``, not here.
        """
        ctx = get_tool_context()
        params: dict[str, Any] = dict(pagination_params(page, page_size))
        params["sortBy"] = NEWEST_FIRST

        numeric_project: int | None = None
        if project_id is not None:
            numeric_project = await _project_numeric_id(ctx, project_id)
            serialized = serialize_filters([_project_filter(numeric_project)])
            if serialized is not None:
                params["filters"] = serialized

        payload = await ctx.client.get_json("news", params=params)
        collection = hal.collection(payload)
        rows = [_news_row(element) for element in collection]

        notes: list[str] = []
        if not rows and collection.total == 0:
            notes.append(
                PROJECT_EMPTY_NOTE.format(project=numeric_project)
                if numeric_project is not None
                else INSTANCE_EMPTY_NOTE
            )
        return envelope_from_collection(
            collection, rows, page=page, page_size=page_size, notes=notes or None
        )

    @mcp.tool(
        name="get_news",
        tags=tool_tags(GROUP_NEWS, READ),
        annotations=read_annotations(title="Get news entry"),
    )
    @tool_errors
    async def get_news(
        news_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric news id from list_news. It is the same id as in the UI's "
                    "/news/{id} URL; the headline is not an id."
                )
            ),
        ],
    ) -> NewsDetail:
        """Read one news entry in full, including the markdown body.

        Use it after ``list_news`` when the summary is not enough — this adds ``description``,
        the announcement's complete text as markdown (html is dropped), plus ``updated_at``.

        Returns ``{id, title, summary, description, project, author, created_at, updated_at,
        can_manage}``. ``author`` is the account that published the entry and cannot be
        changed; ``can_manage`` says whether editing or deleting it would be permitted.

        Pitfalls: a 404 here means "no such entry, or you may not read news in its project" —
        the news module is enabled per project, so a missing entry is not always a wrong id.
        Comments people left on the announcement are not exposed by API v3 and are not
        included.

        Cross-references: ``list_news`` produces the id; ``update_news`` changes the text;
        ``delete_news`` removes the entry for good.
        """
        ctx = get_tool_context()
        try:
            payload = await ctx.client.get_json(f"news/{news_id}")
        except NotFoundError as exc:
            raise _news_not_found(exc, news_id) from exc
        return _news_detail(payload)

    @mcp.tool(
        name="create_news",
        tags=tool_tags(GROUP_NEWS, WRITE),
        annotations=write_annotations(title="Create news"),
    )
    @tool_errors
    async def create_news(
        project_id: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric id (or URL identifier, resolved for you) of the project to "
                    "publish in. The project cannot be changed afterwards, and it must have "
                    "the news module enabled."
                )
            ),
        ],
        title: Annotated[
            str,
            Field(
                description=(
                    "Headline, required, up to 256 characters. This is what readers see in "
                    "the project overview and in notification digests."
                )
            ),
        ],
        summary: Annotated[
            str | None,
            Field(
                description=(
                    "Optional teaser shown under the headline, up to 255 characters. Plain "
                    "text, not markdown. Omit for none."
                )
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description=(
                    "The announcement body as markdown — headings, lists and links all "
                    "render. Omit for a headline-only entry."
                )
            ),
        ] = None,
    ) -> NewsDetail:
        """Publish a news announcement in a project.

        Use it for release notes, a weekly report, a maintenance window — anything the whole
        project should see on its overview page. The author is the authenticated account and
        is set by the server; project members watching the project are notified.

        Returns the created entry ``{id, title, summary, description, project, author,
        created_at, updated_at, can_manage}``; the ``id`` is what ``update_news`` and
        ``delete_news`` consume.

        Pitfalls: this needs the 'manage news' permission in that project, which exists only
        while the project has the news module enabled — a 403 is about the account or the
        module, never about the text. ``title`` is required and rejected when blank (checked
        here, before the request). There is no draft state: the entry is public to everyone
        who can view the project the moment it is created. News is not a work package — for
        something that needs assigning and tracking use ``create_work_package`` instead.

        Cross-references: ``list_news`` to see what is already published (and to avoid
        duplicates); ``update_news`` to correct an entry afterwards; ``list_projects`` for
        the project id.
        """
        ctx = get_tool_context()
        if not title or not title.strip():
            raise InputValidationError(
                "title is empty.",
                hint="Pass the headline, e.g. 'Release 2.1 is live'. News requires a title.",
            )

        numeric_project = await _project_numeric_id(ctx, project_id)
        attributes: dict[str, Any] = {"title": title.strip()}
        if summary is not None:
            attributes["summary"] = summary
        if description is not None:
            attributes["description"] = formattable_field(description)

        payload = build_write_payload(attributes, {"project": link("projects", numeric_project)})
        try:
            created = await ctx.client.post_json("news", json=payload)
        except PermissionDeniedError as exc:
            raise _news_forbidden(exc) from exc
        return _news_detail(created)

    @mcp.tool(
        name="update_news",
        tags=tool_tags(GROUP_NEWS, WRITE),
        annotations=write_annotations(title="Update news"),
    )
    @tool_errors
    async def update_news(
        news_id: Annotated[
            int,
            Field(description="Numeric news id from list_news or get_news."),
        ],
        title: Annotated[
            str | None,
            Field(
                description=(
                    "New headline, up to 256 characters. Omit to leave it alone; it cannot "
                    "be cleared, because news requires a title."
                )
            ),
        ] = None,
        summary: Annotated[
            str | None,
            Field(
                description=(
                    "New teaser (plain text, up to 255 characters); REPLACES the existing "
                    "one. Pass null or an empty string to clear it. Omit the parameter "
                    "entirely (the default) to leave it untouched."
                )
            ),
        ] = KEEP,
        description: Annotated[
            str | None,
            Field(
                description=(
                    "New markdown body; REPLACES the existing text rather than appending to "
                    "it, so read the current one with get_news first if you mean to extend "
                    "it. Pass null or an empty string to clear it. Omit to leave it "
                    "untouched."
                )
            ),
        ] = KEEP,
    ) -> NewsDetail:
        """Correct or rewrite a published news entry.

        Use it to fix a headline, refresh a weekly report in place, or clear a stale teaser.
        Only the parameters you pass are sent, so a concurrent edit to another field survives.

        Returns the updated entry in the same shape as ``get_news``.

        Pitfalls: ``summary`` and ``description`` REPLACE the stored text — there is no
        append. The entry's project and author are fixed at creation and cannot be updated
        here; publish a new entry instead. News carries no ``lockVersion`` upstream, so there
        is nothing to echo and no lock parameter: a simultaneous edit by somebody else is
        silently overwritten, which is why reading with ``get_news`` first is worth it. The
        'manage news' permission is required, so a 403 is about the account or a disabled
        news module.

        Cross-references: ``get_news`` for the current text and for ``can_manage``;
        ``delete_news`` when the entry should disappear entirely; ``create_news`` to publish
        a follow-up instead of rewriting history.
        """
        ctx = get_tool_context()

        attributes: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise InputValidationError(
                    "title is empty.",
                    hint="Omit title to leave it unchanged; news cannot have a blank title.",
                )
            attributes["title"] = title.strip()
        if summary != KEEP:
            attributes["summary"] = summary or ""
        if description != KEEP:
            attributes["description"] = formattable_field(description or "")

        if not attributes:
            raise InputValidationError(
                "update_news was called with nothing to change.",
                hint="Pass at least one of title, summary or description.",
            )

        # Plain PATCH: news has no lockVersion, and inventing one would be worse than
        # sending none (SPEC §4.4).
        try:
            updated = await ctx.client.patch_json(
                f"news/{news_id}", json=build_write_payload(attributes)
            )
        except NotFoundError as exc:
            raise _news_not_found(exc, news_id) from exc
        except PermissionDeniedError as exc:
            raise _news_forbidden(exc) from exc
        return _news_detail(updated)

    @mcp.tool(
        name="delete_news",
        tags=tool_tags(GROUP_NEWS, WRITE, DESTRUCTIVE),
        annotations=destructive_annotations(title="Delete news"),
    )
    @tool_errors
    async def delete_news(
        news_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric news id to delete. Read it back with get_news first — headlines "
                    "repeat across projects, ids do not."
                )
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true. Ask the user first: there is no undo. Calling with "
                    "confirm=false returns a confirmation_required error and deletes nothing."
                )
            ),
        ] = False,
    ) -> NewsDeletionResult:
        """Permanently delete a news entry.

        Use it for an announcement published by mistake or in the wrong project. For an
        outdated but real announcement, ``update_news`` is usually the better answer: the
        entry stays part of the project's record.

        Returns a small confirmation once OpenProject accepts the deletion.

        Pitfalls: the entry and every comment left on it are removed for good — API v3 offers
        no undo and no trash. The 'manage news' permission is required, so a 403 is about the
        account or a disabled news module; a 404 means the id is wrong or its news is not
        visible to you.

        Cross-references: ``list_news``/``get_news`` for the id and for ``can_manage``;
        ``update_news`` for the reversible alternative.
        """
        require_confirmation(
            confirm,
            action="delete news entry",
            target=f"#{news_id}",
            consequence=(
                "The announcement and the comments people left on it are removed for good; "
                "the deletion cannot be undone through the API."
            ),
        )
        ctx = get_tool_context()
        try:
            await ctx.client.delete(f"news/{news_id}")
        except NotFoundError as exc:
            raise _news_not_found(exc, news_id) from exc
        except PermissionDeniedError as exc:
            raise _news_forbidden(exc) from exc

        return NewsDeletionResult(
            id=news_id,
            deleted=True,
            message=f"News entry {news_id} was deleted, together with its comments.",
        )
