"""Shared helpers for tool modules — read this before writing a tool.

Everything a tool needs that is not domain logic lives here: access to the
lifespan-scoped client/cache/settings, the error decorator that turns the typed
taxonomy into a structured MCP error, the list-envelope builder, the
confirmation guard for destructive tools, and the annotation/tag helpers.

The shape of a tool
-------------------

::

    from openproject_mcp.projections import ListEnvelope, WorkPackageRow
    from openproject_mcp.tools import _shared

    def register(mcp: FastMCP) -> None:
        @mcp.tool(
            name="list_work_packages",
            tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.READ),
            annotations=_shared.read_annotations(title="List work packages"),
        )
        @_shared.tool_errors
        async def list_work_packages(
            project: str | None = None,
            page: int = 1,
            page_size: int = 20,
        ) -> ListEnvelope[WorkPackageRow]:
            \"\"\"One-paragraph description the model reads. ...\"\"\"
            ctx = _shared.get_tool_context()
            payload = await ctx.client.get_json("work_packages", params=...)
            collection = hal.collection(payload)
            rows = [_row(element) for element in collection]
            return _shared.envelope_from_collection(
                collection, rows, page=page, page_size=page_size
            )

Rules that the helpers enforce for you:

* ``@_shared.tool_errors`` must be the **innermost** decorator (directly above
  the function, below ``@mcp.tool``). It catches every
  :class:`~openproject_mcp.client.errors.OpenProjectError`, returns
  ``ToolResult(is_error=True)`` whose text content is the SPEC §4.2 JSON
  envelope, and never lets a traceback escape.
* Return a Pydantic model (or ``ListEnvelope[...]``) — never a string. FastMCP
  derives ``outputSchema`` from the return annotation and fills
  ``structuredContent`` automatically.
* List tools return :func:`envelope_from_collection` / :func:`build_envelope`,
  always, even for small fetched-in-full collections.
* Destructive tools call :func:`require_confirmation` as their first statement
  and use :func:`destructive_annotations`.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp.server.dependencies import get_context
from fastmcp.tools.base import ToolResult

from openproject_mcp.client.cache import TTLCache
from openproject_mcp.client.errors import (
    ConfirmationRequiredError,
    InputValidationError,
    OpenProjectError,
    UnexpectedResponseError,
)
from openproject_mcp.client.filters import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, to_snake_name
from openproject_mcp.client.hal import HalCollection, as_object, collection, duration_hours
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.config import Settings
from openproject_mcp.observability import correlation_scope, get_logger
from openproject_mcp.projections import Group, ListEnvelope, Pagination
from openproject_mcp.version_probe import InstanceProbe, get_probe

__all__ = [
    "ADMIN",
    "CACHE_KEY_CONFIGURATION",
    "DESTRUCTIVE",
    "FETCH_ALL_CAP",
    "GROUP_ATTACHMENTS",
    "GROUP_BUDGETS",
    "GROUP_DOCUMENTS",
    "GROUP_GIT",
    "GROUP_MEETINGS",
    "GROUP_METADATA",
    "GROUP_NEWS",
    "GROUP_NOTIFICATIONS",
    "GROUP_PEOPLE",
    "GROUP_PROJECTS",
    "GROUP_QUERIES",
    "GROUP_REPORTING",
    "GROUP_TIME_ENTRIES",
    "GROUP_VERSIONS",
    "GROUP_WIKI",
    "GROUP_WORK_PACKAGES",
    "GROUP_WP_COLLABORATION",
    "LIFESPAN_KEY",
    "READ",
    "WRITE",
    "ToolContext",
    "build_envelope",
    "collect_all",
    "destructive_annotations",
    "envelope_from_collection",
    "envelope_json",
    "error_result",
    "fetch_all_envelope",
    "get_configuration",
    "get_tool_context",
    "normalize_groups",
    "normalize_sums",
    "read_annotations",
    "report_progress",
    "require_confirmation",
    "require_first_page_for_fetch_all",
    "tool_errors",
    "tool_tags",
    "write_annotations",
]

logger = get_logger("tools")

#: Key under which :class:`ToolContext` is stored in the FastMCP lifespan dict.
LIFESPAN_KEY = "openproject"

#: The one cache key for ``GET /configuration`` (SPEC §4.6). Several tools read
#: that document — attachment size limits, page-size options — and they must
#: share the entry rather than each paying for its own round trip. Use
#: :func:`get_configuration`; the constant is exported for cache invalidation.
CACHE_KEY_CONFIGURATION = "configuration"

# --- tags (SPEC §3.2) ------------------------------------------------------

READ = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"
ADMIN = "admin"

GROUP_WORK_PACKAGES = "work_packages"
GROUP_WP_COLLABORATION = "wp_collaboration"
GROUP_ATTACHMENTS = "attachments"
GROUP_PROJECTS = "projects"
GROUP_METADATA = "metadata"
GROUP_GIT = "git_activity"
GROUP_QUERIES = "queries"
GROUP_NOTIFICATIONS = "notifications"
GROUP_TIME_ENTRIES = "time_entries"
GROUP_VERSIONS = "versions"
GROUP_PEOPLE = "people"
GROUP_MEETINGS = "meetings"
GROUP_NEWS = "news"
GROUP_DOCUMENTS = "documents"
GROUP_BUDGETS = "budgets"
GROUP_WIKI = "wiki"
GROUP_REPORTING = "reporting"


def tool_tags(group: str, *kinds: str) -> set[str]:
    """Tags for a tool: one group tag plus its kind tags.

    Deployment filtering keys off these (SPEC §3.2): ``READ_ONLY`` drops
    ``write``/``destructive``/``admin``, ``ADMIN_TOOLS=0`` drops ``admin``, and
    ``OPENPROJECT_MCP_DISABLE=meetings,news`` drops whole groups.

    ``tool_tags(GROUP_WORK_PACKAGES, WRITE)`` → ``{"work_packages", "write"}``
    ``tool_tags(GROUP_PEOPLE, WRITE, ADMIN)`` → ``{"people", "write", "admin"}``
    """
    if not kinds:
        raise ValueError("A tool needs at least one kind tag: READ, WRITE, DESTRUCTIVE or ADMIN")
    return {group, *kinds}


# --- annotations (SPEC §5.4) ----------------------------------------------


def read_annotations(
    *,
    title: str | None = None,
    idempotent: bool = True,
    max_result_chars: int | None = None,
) -> dict[str, Any]:
    """Annotations for a read tool.

    ``max_result_chars`` sets ``anthropic/maxResultSizeChars`` — use it on known
    large reads (comment threads, report data, ``run_query``).
    """
    annotations: dict[str, Any] = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": idempotent,
        "openWorldHint": True,
    }
    if title:
        annotations["title"] = title
    if max_result_chars is not None:
        annotations["anthropic/maxResultSizeChars"] = max_result_chars
    return annotations


def write_annotations(
    *,
    title: str | None = None,
    idempotent: bool = False,
    requires_user_interaction: bool = False,
) -> dict[str, Any]:
    """Annotations for a non-destructive write (create/update/add)."""
    annotations: dict[str, Any] = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": idempotent,
        "openWorldHint": True,
    }
    if title:
        annotations["title"] = title
    if requires_user_interaction:
        annotations["anthropic/requiresUserInteraction"] = True
    return annotations


def destructive_annotations(
    *,
    title: str | None = None,
    idempotent: bool = False,
) -> dict[str, Any]:
    """Annotations for a destructive tool.

    Always sets ``anthropic/requiresUserInteraction`` — every 🗑 tool needs it
    (SPEC §5.4) — and must be paired with :func:`require_confirmation` in the
    body and the ``destructive`` tag.
    """
    annotations: dict[str, Any] = {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": idempotent,
        "openWorldHint": True,
        "anthropic/requiresUserInteraction": True,
    }
    if title:
        annotations["title"] = title
    return annotations


# --- lifespan access -------------------------------------------------------


@dataclass(slots=True)
class ToolContext:
    """Everything a tool call needs from the server process.

    Created once in the FastMCP lifespan and reached from inside a tool with
    :func:`get_tool_context`.
    """

    client: OpenProjectClient
    cache: TTLCache
    settings: Settings

    async def probe(self, *, refresh: bool = False) -> InstanceProbe:
        """The instance feature probe (SPEC §4.7): lazy, cached one hour."""
        return await get_probe(self.client, self.cache, refresh=refresh)

    @property
    def scope(self) -> str:
        """Cache scope for the current credential."""
        return self.client.scope


def get_tool_context() -> ToolContext:
    """Return the :class:`ToolContext` for the running tool call.

    Raises:
        RuntimeError: when called outside a tool invocation (no lifespan).
    """
    try:
        mcp_context = get_context()
    except RuntimeError as exc:
        raise RuntimeError(
            "OpenProject tool context is unavailable outside a tool call. "
            "Tools must be invoked through the MCP server, not imported and awaited directly."
        ) from exc
    context = mcp_context.lifespan_context.get(LIFESPAN_KEY)
    if not isinstance(context, ToolContext):
        raise RuntimeError(
            "OpenProject tool context is unavailable — the server lifespan did not run. "
            "Build the server with build_server(settings) so the lifespan can create the "
            "HTTP client and cache."
        )
    return context


async def get_configuration(ctx: ToolContext, *, refresh: bool = False) -> dict[str, Any]:
    """The cached ``GET /configuration`` document (SPEC §4.6).

    It carries ``maximumAttachmentFileSize`` and ``perPageOptions``: near-static
    instance settings, so the whole document is cached once under
    :data:`CACHE_KEY_CONFIGURATION` for every tool that needs a piece of it.
    """

    async def fetch() -> dict[str, Any]:
        return await ctx.client.get_json("configuration")

    return await ctx.cache.get_or_set(
        CACHE_KEY_CONFIGURATION, fetch, scope=ctx.scope, refresh=refresh
    )


async def report_progress(
    progress: float, total: float | None = None, message: str | None = None
) -> None:
    """Emit an MCP progress notification, if the client asked for them.

    Paging aggregations and large downloads call this between iterations to
    reset client idle timeouts (SPEC §5.9). Safe to call unconditionally.
    """
    try:
        context = get_context()
    except RuntimeError:
        return
    await context.report_progress(progress, total, message)


# --- error handling (SPEC §4.2) -------------------------------------------


def error_result(exc: OpenProjectError) -> ToolResult:
    """Build the MCP error result for a taxonomy exception.

    ``structuredContent`` is deliberately left unset: MCP requires it to match
    the declared ``outputSchema``, which describes the success shape only.
    """
    return ToolResult(content=exc.to_json(), is_error=True)


def tool_errors[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R | ToolResult]]:
    """Decorator: typed errors in, structured MCP errors out.

    Place it directly above the tool function (below ``@mcp.tool``). It

    * binds a correlation id so the tool call and its upstream requests share
      one id in the logs (SPEC §12);
    * logs tool name, duration and outcome — never arguments;
    * converts every :class:`OpenProjectError` into ``ToolResult(is_error=True)``
      whose text content is the ``{"error": {...}}`` envelope;
    * converts an unexpected exception into the same envelope with type
      ``unexpected_response`` and logs the traceback to stderr, so a bug never
      leaks a stack trace into the conversation.

    The wrapped function keeps its signature and return annotation, so FastMCP
    still derives ``inputSchema``/``outputSchema`` from it.
    """
    if not inspect.iscoroutinefunction(func):
        raise TypeError("tool_errors only wraps async tool functions")

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | ToolResult:
        started = time.perf_counter()
        with correlation_scope() as correlation_id:
            try:
                result = await func(*args, **kwargs)
            except OpenProjectError as exc:
                _log_outcome(func, started, correlation_id, exc.error_type)
                return error_result(exc)
            except Exception as exc:
                logger.exception(
                    "tool raised an unexpected exception",
                    extra={"tool": func.__name__, "correlation_id": correlation_id},
                )
                _log_outcome(func, started, correlation_id, "unexpected_error")
                return error_result(
                    UnexpectedResponseError(
                        f"{func.__name__} failed unexpectedly: {type(exc).__name__}.",
                        hint=(
                            "This is a bug in the MCP server, not in your request. "
                            "Retry once; if it persists, report it with the tool name."
                        ),
                    )
                )
            _log_outcome(func, started, correlation_id, "ok")
            return result

    return wrapper


def _log_outcome(
    func: Callable[..., Any], started: float, correlation_id: str, outcome: str
) -> None:
    logger.info(
        "tool call",
        extra={
            "tool": func.__name__,
            "outcome": outcome,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "correlation_id": correlation_id,
        },
    )


# --- destructive guard (SPEC §5.5) ----------------------------------------


def require_confirmation(
    confirm: bool,
    *,
    action: str,
    target: str | None = None,
    consequence: str | None = None,
) -> None:
    """Refuse a destructive call that was not explicitly confirmed.

    Call this as the first statement of every 🗑 tool::

        require_confirmation(
            confirm,
            action="delete work package",
            target=f"#{id}",
            consequence="The work package and its comments are permanently removed.",
        )

    Raises:
        ConfirmationRequiredError: turned into a structured tool error by
            :func:`tool_errors`, telling the model to re-call with
            ``confirm=true`` after checking with the user.
    """
    if confirm:
        return
    subject = f"{action} {target}" if target else action
    raise ConfirmationRequiredError(
        f"Refusing to {subject} without confirmation.",
        hint=(
            (consequence + " " if consequence else "")
            + "Ask the user to confirm, then call again with confirm=true."
        ),
    )


# --- list envelope (SPEC §9.3) --------------------------------------------

#: Wire sum keys → the snake_case names we surface, with hour conversion.
_SUM_KEYS: dict[str, str] = {
    "estimatedTime": "estimated_hours",
    "derivedEstimatedTime": "derived_estimated_hours",
    "remainingTime": "remaining_hours",
    "derivedRemainingTime": "derived_remaining_hours",
    "spentTime": "spent_hours",
    "storyPoints": "story_points",
    "laborCosts": "labor_costs",
    "materialCosts": "material_costs",
    "overallCosts": "overall_costs",
}
_DURATION_SUM_KEYS = frozenset(
    {
        "estimatedTime",
        "derivedEstimatedTime",
        "remainingTime",
        "derivedRemainingTime",
        "spentTime",
    }
)


def normalize_sums(raw: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Convert an API ``totalSums`` object into snake_case float hours.

    ``{"estimatedTime": "PT7H30M"}`` → ``{"estimated_hours": 7.5}``. Values that
    are neither numbers nor ISO durations are dropped rather than guessed at.
    """
    if not raw:
        return None
    normalized: dict[str, float] = {}
    for key, value in raw.items():
        name = _SUM_KEYS.get(key, to_snake_name(key))
        if key in _DURATION_SUM_KEYS or isinstance(value, str):
            hours = duration_hours(value)
            if hours is not None:
                normalized[name] = hours
        elif isinstance(value, int | float) and not isinstance(value, bool):
            normalized[name] = float(value)
    return normalized or None


def _group_value(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    entry = as_object(raw)
    if entry is not None:
        for key in ("name", "title", "subject", "value"):
            candidate = entry.get(key)
            if isinstance(candidate, str):
                return candidate
        links = as_object(entry.get("_links"))
        self_link = as_object(links.get("self")) if links is not None else None
        if self_link is not None:
            title = self_link.get("title")
            if isinstance(title, str):
                return title
        return None
    return str(raw)


def normalize_groups(raw: Sequence[Mapping[str, Any]] | None) -> list[Group] | None:
    """Convert API ``groups`` entries into :class:`Group` models.

    Groups are computed server-side over the **full** filtered set, independent
    of pagination — never sum pages to reproduce them.
    """
    if not raw:
        return None
    groups = [
        Group(
            value=_group_value(entry.get("value")),
            count=int(entry.get("count") or 0),
            sums=normalize_sums(as_object(entry.get("sums"))),
        )
        for entry in raw
    ]
    return groups or None


def build_envelope(
    items: Sequence[Any],
    *,
    total: int | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    groups: Sequence[Mapping[str, Any]] | Sequence[Group] | None = None,
    sums: Mapping[str, Any] | None = None,
    notes: Iterable[str] | None = None,
) -> ListEnvelope[Any]:
    """Build the universal list envelope (SPEC §9.3).

    Args:
        items: the projections for this page.
        total: server-reported total; defaults to ``len(items)`` for
            fetched-in-full collections.
        page: 1-based page number that was requested.
        page_size: page size that was requested.
        groups: raw API ``groups`` entries or ready-made :class:`Group` models.
        sums: raw API ``totalSums``; converted to float hours.
        notes: G5 degradation markers ("attachment content not searched…").

    ``has_more`` is derived from ``total`` and the page window, so a
    fetched-in-full collection reports ``has_more: false`` without special
    casing at the call site.
    """
    resolved_total = len(items) if total is None else int(total)
    effective_size = max(int(page_size or DEFAULT_PAGE_SIZE), 1)
    effective_page = max(int(page or 1), 1)
    seen = (effective_page - 1) * effective_size + len(items)
    normalized_groups: list[Group] | None = None
    if groups:
        ready = [item for item in groups if isinstance(item, Group)]
        normalized_groups = (
            ready
            if len(ready) == len(groups)
            else normalize_groups([item for item in groups if not isinstance(item, Group)])
        )
    note_list = [note for note in (notes or []) if note]
    return ListEnvelope[Any](
        items=list(items),
        pagination=Pagination(
            total=resolved_total,
            page=effective_page,
            page_size=effective_size,
            has_more=seen < resolved_total,
        ),
        groups=normalized_groups or None,
        sums=normalize_sums(sums),
        notes=note_list or None,
    )


def envelope_from_collection(
    collection: HalCollection,
    items: Sequence[Any],
    *,
    page: int | None = None,
    page_size: int | None = None,
    notes: Iterable[str] | None = None,
) -> ListEnvelope[Any]:
    """Build the envelope from an unwrapped HAL collection.

    Prefers the server's own ``offset`` (a 1-based page number) and
    ``pageSize`` over what was requested, so the envelope reports what actually
    came back when the instance clamps ``pageSize`` (``apiv3_max_page_size``).
    """
    resolved_page = collection.offset or page or 1
    resolved_size = collection.page_size or page_size or DEFAULT_PAGE_SIZE
    return build_envelope(
        items,
        total=collection.total,
        page=resolved_page,
        page_size=resolved_size,
        groups=collection.groups or None,
        sums=collection.total_sums or None,
        notes=notes,
    )


def envelope_json(envelope: ListEnvelope[Any]) -> str:
    """Serialize an envelope — handy in tests and for text fallbacks."""
    return json.dumps(envelope.model_dump(exclude_none=True), default=str)


#: ``fetch_all`` stops after this many aggregated items (G1: caps are visible,
#: never silent). Five max-size pages on a default instance.
FETCH_ALL_CAP = 500


async def collect_all(
    get_page: Callable[[int, int], Awaitable[Mapping[str, Any]]],
    *,
    cap: int = FETCH_ALL_CAP,
    label: str = "fetching all pages",
) -> tuple[list[dict[str, Any]], HalCollection, list[str]]:
    """Fetch page after page until the collection — or the cap — is exhausted.

    ``get_page(page, page_size)`` performs one GET and returns the raw payload;
    the loop asks for max-size pages and advances until the server total, an
    empty page, or ``cap`` stops it. Returns ``(elements, last_chunk, notes)``:
    the raw HAL elements across every fetched page (trimmed to ``cap``), the
    last unwrapped chunk (its ``total``/``groups``/``total_sums`` speak for the
    full filtered set), and the cap note when one applies.
    """
    elements: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = collection(await get_page(page, MAX_PAGE_SIZE))
        elements.extend(chunk.elements)
        if not chunk.elements or len(elements) >= chunk.total or len(elements) >= cap:
            break
        await report_progress(
            len(elements),
            float(min(chunk.total, cap)),
            f"{label} ({len(elements)} of {chunk.total})",
        )
        # Yield to the event loop between pages so a cancelled tool call stops
        # here instead of paging on to the cap.
        await asyncio.sleep(0)
        page += 1
    notes: list[str] = []
    if chunk.total > cap and len(elements) >= cap:
        notes.append(
            f"fetch_all stopped at {cap} of {chunk.total} matching items — "
            "narrow the filters to see the rest."
        )
    return elements[:cap], chunk, notes


def require_first_page_for_fetch_all(fetch_all: bool, page: int) -> None:
    """``fetch_all`` reads every page itself — a page number contradicts it."""
    if fetch_all and page != 1:
        raise InputValidationError(
            "fetch_all and page are mutually exclusive.",
            hint="fetch_all aggregates every page itself; drop page or set fetch_all=false.",
        )


def fetch_all_envelope(
    rows: Sequence[Any],
    last_chunk: HalCollection,
    *,
    notes: Iterable[str] | None = None,
) -> ListEnvelope[Any]:
    """Envelope for a ``fetch_all`` result: one logical page holding everything.

    ``has_more`` still tells the truth — it is true exactly when the cap left
    part of the filtered set unfetched.
    """
    return build_envelope(
        rows,
        total=last_chunk.total,
        page=1,
        page_size=max(len(rows), 1),
        groups=last_chunk.groups or None,
        sums=last_chunk.total_sums or None,
        notes=notes,
    )
