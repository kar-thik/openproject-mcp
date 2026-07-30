"""FastMCP server assembly (SPEC §3.2, §3.4, §10).

``build_server(settings)`` is the only way a server is created. It is
constructable with **zero environment** — no network, no credential check, no
import-time side effects — because tests and ``--help`` must work on a bare
machine (SPEC §3.4). Anything that needs a live instance happens inside the
lifespan or lazily on first use.

Assembly order:

1. logging configured from settings (stderr, JSON or text);
2. ``FastMCP`` created with the server ``instructions`` (§10);
3. lifespan registered — it creates the pooled client, the metadata cache and
   the :class:`ToolContext` that tools reach via ``_shared.get_tool_context()``;
4. every tool module's ``register(mcp)`` called;
5. tag-based deployment filtering applied (``READ_ONLY``, ``ADMIN_TOOLS``,
   ``DISABLE``).

The tool set is fixed at startup; the server never emits
``tools/list_changed``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from openproject_mcp import __version__, prompts, resources
from openproject_mcp.client.cache import TTLCache
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.config import Settings
from openproject_mcp.observability import configure_logging, get_logger
from openproject_mcp.tools import register_all
from openproject_mcp.tools._shared import ADMIN, DESTRUCTIVE, LIFESPAN_KEY, WRITE, ToolContext

__all__ = ["SERVER_INSTRUCTIONS", "build_server"]

logger = get_logger("server")

SERVER_NAME = "openproject"

SERVER_INSTRUCTIONS = (
    "Manages OpenProject: work packages (search, read, create, update, comments, "
    "attachments), projects, files, git/PR links, time tracking, meetings and "
    "notifications. Ids are discoverable — use search_work_packages, list_projects and "
    "get_project_metadata to turn names into ids before calling anything that consumes "
    "them; never guess an id, a status name or a priority id. list_work_packages returns "
    "open items only unless you pass status_scope; search_work_packages searches all "
    "statuses. Every list result reports total/page/page_size/has_more — page explicitly "
    "rather than assuming you saw everything. Destructive tools require confirm=true and "
    "should be confirmed with the user first. Errors come back as structured JSON with a "
    "hint describing how to correct the call."
)


def build_lifespan(settings: Settings) -> Any:
    """Create the lifespan that owns the HTTP client and metadata cache.

    The version probe is *not* run here: it is lazy and cached for an hour
    (SPEC §4.7), so a server can start (and tests can run) without touching the
    network. The first tool that needs it calls ``ctx.probe()``.
    """

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        client = OpenProjectClient(settings)
        cache = TTLCache(ttl=settings.cache_ttl)
        logger.info(
            "server starting",
            extra={
                "read_only": settings.read_only,
                "admin_tools": settings.admin_tools,
                "disabled_groups": sorted(settings.disabled_groups) or None,
            },
        )
        try:
            yield {LIFESPAN_KEY: ToolContext(client=client, cache=cache, settings=settings)}
        finally:
            await client.aclose()
            cache.clear()
            logger.info("server stopped")

    return lifespan


def apply_tag_filters(mcp: FastMCP, settings: Settings) -> None:
    """Apply deployment filtering by tag (SPEC §3.2, §11).

    * ``OPENPROJECT_MCP_READ_ONLY=1`` drops every ``write``/``destructive``/
      ``admin`` tool.
    * ``admin`` tools stay hidden unless ``OPENPROJECT_MCP_ADMIN_TOOLS=1``.
    * ``OPENPROJECT_MCP_DISABLE=meetings,news`` drops whole group tags to cut
      prompt cost.
    """
    if settings.read_only:
        mcp.disable(tags={WRITE, DESTRUCTIVE, ADMIN})
    elif not settings.admin_tools:
        mcp.disable(tags={ADMIN})
    groups = settings.disabled_groups
    if groups:
        mcp.disable(tags=set(groups))


def build_server(settings: Settings | None = None) -> FastMCP:
    """Build the MCP server.

    Args:
        settings: configuration to use; ``Settings()`` (environment) by default.

    Returns:
        A configured :class:`FastMCP` instance. Nothing has connected to
        OpenProject yet — that happens when the lifespan runs.
    """
    resolved = settings or Settings()
    configure_logging(resolved)

    mcp: FastMCP = FastMCP(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=build_lifespan(resolved),
    )
    register_all(mcp)
    prompts.register(mcp)
    resources.register(mcp)
    apply_tag_filters(mcp, resolved)
    return mcp
