"""Argument completion for the prompts and resource templates (SPEC §10).

Clients that render prompts as slash commands, or resource templates as
pickers, ask ``completion/complete`` for candidates while the user types. The
server answers for the arguments with a closed or searchable domain:

* ``project`` on ``weekly_report``, ``daily_standup`` and ``groom_backlog``, and
  ``identifier`` on ``openproject://project/{identifier}`` — project identifiers
  (URL slugs) whose name or identifier contains the typed text, active projects
  only, at most :data:`PROJECT_CANDIDATES`.
* ``locale`` on ``weekly_report`` — the rendered locales.

Anything else gets no candidates. A failed OpenProject read also yields none:
completion is a typing aid, and an error there would only interrupt the user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import mcp_types

from openproject_mcp.client import hal
from openproject_mcp.client.errors import OpenProjectError
from openproject_mcp.client.filters import Op, make_filter, serialize_filters, serialize_sort_by
from openproject_mcp.observability import get_logger
from openproject_mcp.prompts import LOCALES
from openproject_mcp.resources import PROJECT_URI
from openproject_mcp.tools._shared import get_tool_context

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["PROJECT_CANDIDATES", "PROJECT_PROMPTS", "register"]

#: Prompts whose ``project`` argument completes to project identifiers.
PROJECT_PROMPTS = frozenset({"weekly_report", "daily_standup", "groom_backlog"})

#: Most project identifiers offered per request; one OpenProject page.
PROJECT_CANDIDATES = 20

#: Seconds a project lookup is reused, so typing does not cost one request per key.
PROJECT_LOOKUP_TTL = 30.0

_PROJECTS_RESOURCE = "projects"

logger = get_logger(__name__)


async def _project_identifiers(typed: str) -> list[str]:
    ctx = get_tool_context()
    text = typed.strip()
    filters = [make_filter("active", Op.EQ, [True], resource=_PROJECTS_RESOURCE)]
    if text:
        filters.append(
            make_filter("name_and_identifier", Op.CONTAINS, [text], resource=_PROJECTS_RESOURCE)
        )
    params: dict[str, Any] = {
        "filters": serialize_filters(filters),
        "pageSize": PROJECT_CANDIDATES,
        "sortBy": serialize_sort_by([("name", "asc")]),
    }

    async def fetch() -> list[str]:
        payload = await ctx.client.get_json("projects", params=params)
        identifiers = (element.get("identifier") for element in hal.collection(payload))
        return [identifier for identifier in identifiers if isinstance(identifier, str)]

    return await ctx.cache.get_or_set(
        ("completion", "projects", text.casefold()),
        fetch,
        scope=ctx.scope,
        ttl=PROJECT_LOOKUP_TTL,
    )


def _wants_project(
    ref: mcp_types.PromptReference | mcp_types.ResourceTemplateReference, name: str
) -> bool:
    if isinstance(ref, mcp_types.PromptReference):
        return ref.name in PROJECT_PROMPTS and name == "project"
    return ref.uri == PROJECT_URI and name == "identifier"


def register(mcp: FastMCP) -> None:
    """Register the server's single completion handler."""

    @mcp.completion
    async def complete(
        ref: mcp_types.PromptReference | mcp_types.ResourceTemplateReference,
        argument: mcp_types.CompletionArgument,
        context: mcp_types.CompletionContext | None,
    ) -> list[str]:
        if _wants_project(ref, argument.name):
            try:
                return await _project_identifiers(argument.value)
            except OpenProjectError as exc:
                logger.info("project completion failed", extra={"error": exc.error_type})
                return []
        if (
            isinstance(ref, mcp_types.PromptReference)
            and ref.name == "weekly_report"
            and argument.name == "locale"
        ):
            typed = argument.value.casefold()
            return [locale for locale in LOCALES if locale.casefold().startswith(typed)]
        return []
