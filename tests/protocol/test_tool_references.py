"""No tool description may point the model at a tool it cannot see.

Docstrings are the descriptions a model reads for every tool call, including
tools that are hidden under the server's default settings. The three
membership-write tools (``create_membership``, ``update_membership``,
``delete_membership``) are tagged ``admin`` and stay invisible unless the
deployment sets ``OPENPROJECT_MCP_ADMIN_TOOLS=1`` (see
``server.apply_tag_filters``). If a default-visible tool names one of them
without saying so, the model is told to call a tool that is not in its tool
list. This test builds a default server and an admin server, and checks every
whole-word tool-name reference a default-visible tool's description or
parameter descriptions make against the tools the admin server adds.
"""

from __future__ import annotations

import re
from typing import Any

from fastmcp import Client

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import TEST_URL

ADMIN_GATE_MARKER = "OPENPROJECT_MCP_ADMIN_TOOLS"
KNOWN_ADMIN_TOOLS = {"create_membership", "update_membership", "delete_membership"}


def _settings(*, admin_tools: bool) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", admin_tools=admin_tools
    )


def _referenced_tool_names(tool: Any, universe: set[str]) -> set[str]:
    """Whole-word matches of any universe tool name in ``tool``'s descriptions."""
    texts = [tool.description or ""]
    for prop in tool.inputSchema.get("properties", {}).values():
        if isinstance(prop, dict):
            texts.append(prop.get("description") or "")
    combined = " ".join(texts)
    return {
        other
        for other in universe
        if other != tool.name and re.search(rf"\b{re.escape(other)}\b", combined)
    }


async def test_default_visible_tools_name_the_gate_when_referencing_admin_tools() -> None:
    async with Client(build_server(_settings(admin_tools=True))) as client:
        universe = {tool.name: tool for tool in await client.list_tools()}
    async with Client(build_server(_settings(admin_tools=False))) as client:
        default_visible = {tool.name for tool in await client.list_tools()}

    hidden_referenced: set[str] = set()
    ungated: list[str] = []
    for name in sorted(default_visible):
        referenced = _referenced_tool_names(universe[name], set(universe))
        hidden = referenced - default_visible
        if not hidden:
            continue
        hidden_referenced |= hidden
        if ADMIN_GATE_MARKER not in (universe[name].description or ""):
            ungated.append(name)

    assert hidden_referenced <= KNOWN_ADMIN_TOOLS, (
        "default-visible tools reference hidden tools outside the known admin gate: "
        f"{sorted(hidden_referenced - KNOWN_ADMIN_TOOLS)}"
    )
    assert not ungated, (
        f"tools reference an admin-gated tool without naming {ADMIN_GATE_MARKER}: {ungated}"
    )
