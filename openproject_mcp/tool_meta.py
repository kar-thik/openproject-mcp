"""Claude's per-tool vendor keys, delivered where Claude reads them (SPEC §5.4).

Claude Code reads ``anthropic/requiresUserInteraction`` and
``anthropic/maxResultSizeChars`` from a tool's ``_meta`` in ``tools/list``, not
from its ``annotations``; MCP SDK v2 also drops every non-standard key from
``annotations`` on the wire. The annotation helpers in
:mod:`openproject_mcp.tools._shared` still accept these keys next to the
standard hints, so a tool declares everything about itself in one place, and
return a :class:`ToolHints` that keeps them. :class:`ClaudeToolMeta` moves them
into ``_meta`` as the tool list goes out.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool
from pydantic import ConfigDict

VENDOR_PREFIX = "anthropic/"


class ToolHints(mt.ToolAnnotations):
    """Standard tool annotations that also carry ``anthropic/*`` keys until listing."""

    model_config = ConfigDict(extra="allow")


def _split(annotations: mt.ToolAnnotations) -> tuple[mt.ToolAnnotations, dict[str, Any]]:
    extra = annotations.model_extra or {}
    vendor = {key: value for key, value in extra.items() if key.startswith(VENDOR_PREFIX)}
    if not vendor:
        return annotations, {}
    standard = mt.ToolAnnotations.model_validate(annotations.model_dump(exclude=set(vendor)))
    return standard, vendor


def with_vendor_meta(tool: Tool) -> Tool:
    """``tool`` with its ``anthropic/*`` annotation keys moved into ``meta``."""
    if tool.annotations is None:
        return tool
    standard, vendor = _split(tool.annotations)
    if not vendor:
        return tool
    meta = {**(tool.meta or {}), **vendor}
    return tool.model_copy(update={"annotations": standard, "meta": meta})


class ClaudeToolMeta(Middleware):
    """Publish every listed tool's ``anthropic/*`` keys under ``_meta``."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        return [with_vendor_meta(tool) for tool in await call_next(context)]
