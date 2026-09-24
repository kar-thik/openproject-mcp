"""Tool profiles and ``enable_tool_group`` over the in-memory FastMCP client.

``OPENPROJECT_MCP_PROFILE=core`` hides the module-backed groups at startup;
``enable_tool_group`` lifts one for the calling session only and must never
bypass ``READ_ONLY``, the admin gate or ``OPENPROJECT_MCP_DISABLE``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.messages import MessageHandler

from openproject_mcp.config import Settings
from openproject_mcp.groups import ALL_GROUPS, CORE_PROFILE_HIDDEN_GROUPS
from openproject_mcp.server import CORE_PROFILE_INSTRUCTIONS, SERVER_INSTRUCTIONS, build_server
from tests.conftest import TEST_URL
from tests.protocol.test_server import (
    ADMIN_TOOLS_SET,
    DEFAULT_TOOLS,
    MEETINGS_GROUP_TOOLS,
    MEETINGS_RECURRING_GROUP_TOOLS,
    NEWS_GROUP_TOOLS,
    PROMPT_NAMES,
    RESOURCE_TEMPLATES,
    WRITE_TOOLS,
)

REPORTING_PROMPTS = {"weekly_report", "daily_standup"}
MEETINGS_READ_TOOLS = MEETINGS_GROUP_TOOLS - WRITE_TOOLS


class ListChangedCounter(MessageHandler):
    """Counts ``notifications/tools/list_changed`` received by one client."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_list_changed = 0

    async def on_tool_list_changed(self, message: mcp.types.ToolListChangedNotification) -> None:
        self.tool_list_changed += 1


def _settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", **overrides
    )


async def _hidden_group_tools() -> set[str]:
    """Default-visible tools whose tags touch a core-hidden group."""
    server = build_server(_settings())
    return {
        tool.name for tool in await server.list_tools() if tool.tags & CORE_PROFILE_HIDDEN_GROUPS
    }


async def _names(client: Client[Any]) -> set[str]:
    return {tool.name for tool in await client.list_tools()}


def _error(result: Any) -> dict[str, Any]:
    assert result.is_error
    envelope: dict[str, Any] = json.loads(result.content[0].text)
    return envelope["error"]


async def test_core_profile_lists_the_core_tools_prompts_and_resources() -> None:
    hidden = await _hidden_group_tools()
    assert hidden >= MEETINGS_GROUP_TOOLS | NEWS_GROUP_TOOLS
    async with Client(build_server(_settings(profile="core"))) as client:
        listed = await _names(client)
        prompts = {prompt.name for prompt in await client.list_prompts()}
        templates = {t.uriTemplate for t in await client.list_resource_templates()}
    assert listed == DEFAULT_TOOLS - hidden
    assert "enable_tool_group" in listed
    assert prompts == PROMPT_NAMES - REPORTING_PROMPTS
    assert templates == RESOURCE_TEMPLATES


async def test_enable_tool_group_reveals_the_group_and_notifies_the_session() -> None:
    counter = ListChangedCounter()
    async with Client(build_server(_settings(profile="core")), message_handler=counter) as client:
        before = await _names(client)
        result = await client.call_tool("enable_tool_group", {"group": "meetings"})
        after = await _names(client)

    assert not result.is_error
    structured = result.structured_content
    assert structured is not None
    assert structured["status"] == "enabled"
    assert structured["title"] == "Meetings"
    assert structured["notes"] == []
    assert set(structured["tools_enabled"]) == after - before == MEETINGS_GROUP_TOOLS
    assert after >= MEETINGS_GROUP_TOOLS
    assert counter.tool_list_changed >= 1


async def test_unknown_group_is_invalid_input_naming_the_valid_groups() -> None:
    async with Client(build_server(_settings(profile="core"))) as client:
        result = await client.call_tool(
            "enable_tool_group", {"group": "meeting"}, raise_on_error=False
        )
    error = _error(result)
    assert error["type"] == "invalid_input"
    for group in ALL_GROUPS:
        assert group in error["hint"]


@pytest.mark.parametrize("group", ["meetings", "meetings_recurring"])
async def test_operator_disabled_group_cannot_be_re_enabled(group: str) -> None:
    counter = ListChangedCounter()
    server = build_server(_settings(profile="core", disable="meetings"))
    async with Client(server, message_handler=counter) as client:
        before = await _names(client)
        result = await client.call_tool("enable_tool_group", {"group": group}, raise_on_error=False)
        after = await _names(client)
    error = _error(result)
    assert error["type"] == "invalid_input"
    assert "OPENPROJECT_MCP_DISABLE" in error["hint"]
    assert after == before
    assert counter.tool_list_changed == 0


async def test_full_profile_reports_already_visible_without_notifying() -> None:
    counter = ListChangedCounter()
    async with Client(build_server(_settings()), message_handler=counter) as client:
        before = await _names(client)
        result = await client.call_tool("enable_tool_group", {"group": "meetings"})
        after = await _names(client)
    structured = result.structured_content
    assert structured is not None
    assert structured["status"] == "already_visible"
    assert structured["tools_enabled"] == []
    assert after == before == DEFAULT_TOOLS
    assert counter.tool_list_changed == 0


async def test_enabling_a_group_never_bypasses_read_only() -> None:
    async with Client(build_server(_settings(profile="core", read_only=True))) as client:
        before = await _names(client)
        result = await client.call_tool("enable_tool_group", {"group": "meetings"})
        after = await _names(client)
    structured = result.structured_content
    assert structured is not None
    assert after - before == MEETINGS_READ_TOOLS
    assert len(MEETINGS_READ_TOOLS) == 4
    assert not after & WRITE_TOOLS
    assert any("READ_ONLY" in note for note in structured["notes"])


async def test_enabling_every_hidden_group_never_reveals_admin_tools() -> None:
    async with Client(build_server(_settings(profile="core"))) as client:
        for group in sorted(CORE_PROFILE_HIDDEN_GROUPS):
            await client.call_tool("enable_tool_group", {"group": group})
        listed = await _names(client)
    assert listed == DEFAULT_TOOLS
    assert not listed & ADMIN_TOOLS_SET


async def test_enabled_group_is_scoped_to_the_calling_session() -> None:
    server = build_server(_settings(profile="core"))
    other_counter = ListChangedCounter()
    async with Client(server) as first, Client(server, message_handler=other_counter) as second:
        baseline = await _names(second)
        await first.call_tool("enable_tool_group", {"group": "meetings"})
        assert await _names(first) >= MEETINGS_GROUP_TOOLS
        assert await _names(second) == baseline
        assert not baseline & MEETINGS_GROUP_TOOLS
    assert other_counter.tool_list_changed == 0


async def test_disabled_sub_tag_stays_hidden_when_the_parent_is_enabled() -> None:
    server = build_server(_settings(profile="core", disable="meetings_recurring"))
    async with Client(server) as client:
        result = await client.call_tool("enable_tool_group", {"group": "meetings"})
        listed = await _names(client)
    structured = result.structured_content
    assert structured is not None
    expected = MEETINGS_GROUP_TOOLS - MEETINGS_RECURRING_GROUP_TOOLS
    assert set(structured["tools_enabled"]) == expected
    assert len(expected) == 11
    assert not listed & MEETINGS_RECURRING_GROUP_TOOLS
    assert structured["notes"] == ["meetings_recurring stays hidden (OPENPROJECT_MCP_DISABLE)."]


async def test_unknown_disable_names_warn_and_the_server_still_lists(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The package logger does not propagate, so listen on the server logger directly.
    server_logger = logging.getLogger("openproject_mcp.server")
    server_logger.addHandler(caplog.handler)
    try:
        server = build_server(_settings(disable="meeting,news"))
    finally:
        server_logger.removeHandler(caplog.handler)
    warnings = [r for r in caplog.records if "unknown groups" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].__dict__["unknown"] == ["meeting"]
    async with Client(server) as client:
        assert await _names(client) == DEFAULT_TOOLS - NEWS_GROUP_TOOLS


async def test_core_profile_appends_the_instructions_hint() -> None:
    async with Client(build_server(_settings(profile="core"))) as client:
        core = client.initialize_result.instructions
    async with Client(build_server(_settings())) as client:
        full = client.initialize_result.instructions
    assert core == SERVER_INSTRUCTIONS + CORE_PROFILE_INSTRUCTIONS
    assert core is not None and core.endswith(CORE_PROFILE_INSTRUCTIONS)
    assert "enable_tool_group" in CORE_PROFILE_INSTRUCTIONS
    assert full == SERVER_INSTRUCTIONS


async def test_every_registered_tool_carries_a_known_group_tag() -> None:
    server: FastMCP = build_server(_settings(admin_tools=True))
    tools = await server.list_tools()
    assert len(tools) == 89
    for tool in tools:
        assert tool.tags & ALL_GROUPS, f"{tool.name} has no group tag: {sorted(tool.tags)}"
    assert CORE_PROFILE_HIDDEN_GROUPS <= ALL_GROUPS
