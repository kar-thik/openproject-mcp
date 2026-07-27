"""Protocol-level checks against the in-memory FastMCP client.

These assert the assembled server: it boots, the handshake carries the
instructions the model reads, the lifespan wires the tool context, the Phase 1
tool set is registered, and every deployment-filter path prunes exactly the
tools it should.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP

from openproject_mcp.client.filters import query_params, status_filter
from openproject_mcp.client.hal import collection, self_id
from openproject_mcp.config import Settings
from openproject_mcp.projections import ListEnvelope, Ref, WorkPackageRow
from openproject_mcp.server import SERVER_INSTRUCTIONS, apply_tag_filters, build_server
from openproject_mcp.tools import TOOL_MODULES
from openproject_mcp.tools._shared import (
    ADMIN,
    DESTRUCTIVE,
    GROUP_WORK_PACKAGES,
    READ,
    WRITE,
    ToolContext,
    envelope_from_collection,
    get_tool_context,
    read_annotations,
    tool_errors,
    tool_tags,
)
from tests.conftest import TEST_URL
from tests.fixtures.hal_payloads import WORK_PACKAGE_COLLECTION


async def test_server_boots_over_the_in_memory_transport(mcp_client: Client[Any]) -> None:
    assert await mcp_client.ping()


async def test_instructions_are_advertised(mcp_client: Client[Any]) -> None:
    instructions = mcp_client.initialize_result.instructions
    assert instructions == SERVER_INSTRUCTIONS
    assert "work packages" in instructions
    assert "confirm" in instructions
    assert "open items only" in instructions


PHASE_1_READ_TOOLS = {
    "search_work_packages",
    "list_work_packages",
    "get_work_package",
    "list_work_package_comments",
    "list_attachments",
    "download_attachment",
    "list_projects",
    "get_project",
    "get_instance_info",
    "get_project_metadata",
    "get_work_package_schema",
}
PHASE_1_WRITE_TOOLS = {
    "create_work_package",
    "update_work_package",
    "delete_work_package",
    "add_work_package_comment",
    "upload_attachment",
}
PHASE_1_TOOLS = PHASE_1_READ_TOOLS | PHASE_1_WRITE_TOOLS
ATTACHMENT_GROUP_TOOLS = {"list_attachments", "download_attachment", "upload_attachment"}


async def test_phase_1_tool_set_is_registered(mcp_client: Client[Any]) -> None:
    listed = {tool.name for tool in await mcp_client.list_tools()}
    assert listed == PHASE_1_TOOLS
    assert await mcp_client.list_resources() == []
    assert await mcp_client.list_prompts() == []


def test_server_builds_with_zero_environment() -> None:
    server = build_server(Settings(_env_file=None))  # type: ignore[call-arg]
    assert server.name == "openproject"


def test_every_tool_module_exposes_register() -> None:
    for module in TOOL_MODULES:
        assert callable(module.register), f"{module.__name__} must expose register(mcp)"


async def test_lifespan_provides_the_tool_context(settings: Settings) -> None:
    server = build_server(settings)
    seen: dict[str, Any] = {}

    @server.tool
    async def _probe_context() -> dict[str, str]:
        """Reports what the lifespan wired up."""
        from openproject_mcp.tools._shared import get_tool_context

        context = get_tool_context()
        seen["context"] = context
        return {"scope": context.scope}

    async with Client(server) as client:
        result = await client.call_tool("_probe_context", {})

    context = seen["context"]
    assert isinstance(context, ToolContext)
    assert context.settings.url == TEST_URL
    assert context.cache.default_ttl == settings.cache_ttl
    assert result.structured_content == {"scope": context.scope}


@pytest.mark.parametrize(
    ("read_only", "admin_tools", "disable", "expected"),
    [
        (False, False, "", {"read_tool", "write_tool", "grouped_tool"}),
        (True, False, "", {"read_tool", "grouped_tool"}),
        (True, True, "", {"read_tool", "grouped_tool"}),
        (False, True, "", {"read_tool", "write_tool", "admin_tool", "grouped_tool"}),
        (False, False, "meetings", {"read_tool", "write_tool"}),
    ],
)
async def test_tag_filtering_paths(
    read_only: bool, admin_tools: bool, disable: str, expected: set[str]
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        url=TEST_URL,
        api_key="test-token",
        read_only=read_only,
        admin_tools=admin_tools,
        disable=disable,
    )
    server: FastMCP = FastMCP("filtered")

    @server.tool(tags=tool_tags("work_packages", READ))
    async def read_tool() -> str:
        """Read."""
        return "ok"

    @server.tool(tags=tool_tags("work_packages", WRITE))
    async def write_tool() -> str:
        """Write."""
        return "ok"

    @server.tool(tags=tool_tags("people", WRITE, ADMIN))
    async def admin_tool() -> str:
        """Admin write."""
        return "ok"

    @server.tool(tags=tool_tags("meetings", READ))
    async def grouped_tool() -> str:
        """Module read."""
        return "ok"

    @server.tool(tags=tool_tags("work_packages", WRITE, DESTRUCTIVE))
    async def destructive_tool() -> str:
        """Destructive."""
        return "ok"

    apply_tag_filters(server, settings)

    async with Client(server) as client:
        listed = {tool.name for tool in await client.list_tools()}

    assert listed == expected | ({"destructive_tool"} if not read_only else set())


async def test_reference_tool_shape_works_end_to_end(
    settings: Settings, mock_api: respx.MockRouter
) -> None:
    """The exact shape Phase 1 tools take, proven through the protocol.

    Copy this: registration with tags/annotations, ``@tool_errors``, the tool
    context from the lifespan, a validated filter query, HAL projection, and the
    §9.3 envelope — plus the error path.
    """
    server = build_server(settings)

    @server.tool(
        name="reference_list_work_packages",
        tags=tool_tags(GROUP_WORK_PACKAGES, READ),
        annotations=read_annotations(title="List work packages"),
    )
    @tool_errors
    async def reference_list_work_packages(
        page: int = 1, page_size: int = 20
    ) -> ListEnvelope[WorkPackageRow]:
        """Lists open work packages."""
        context = get_tool_context()
        payload = await context.client.get_json(
            "work_packages",
            params=query_params(filters=[status_filter("open")], page=page, page_size=page_size),
        )
        unwrapped = collection(payload)
        rows = [
            WorkPackageRow(
                id=self_id(element),
                subject=element.get("subject"),
                status=Ref.from_hal(element, "status"),
                assignee=Ref.from_hal(element, "assignee"),
            )
            for element in unwrapped
        ]
        return envelope_from_collection(unwrapped, rows, page=page, page_size=page_size)

    route = mock_api.get("work_packages").mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_COLLECTION)
    )

    async with Client(server) as client:
        tool = next(
            t for t in await client.list_tools() if t.name == "reference_list_work_packages"
        )
        assert tool.outputSchema is not None
        assert tool.annotations is not None and tool.annotations.readOnlyHint is True

        result = await client.call_tool("reference_list_work_packages", {"page": 2})
        assert not result.is_error
        structured = result.structured_content
        assert structured is not None
        assert structured["pagination"] == {
            "total": 137,
            "page": 2,
            "page_size": 20,
            "has_more": True,
        }
        assert structured["items"][0]["status"] == {"id": 7, "name": "In progress"}
        assert structured["items"][1]["assignee"] is None
        assert structured["sums"]["estimated_hours"] == 220.0

        route.mock(return_value=httpx.Response(404, json={"message": "nope"}))
        failed = await client.call_tool("reference_list_work_packages", {}, raise_on_error=False)

    assert failed.is_error
    envelope = json.loads(failed.content[0].text)  # type: ignore[union-attr]
    assert envelope["error"]["type"] == "not_found"
    assert envelope["error"]["http_status"] == 404
    assert envelope["error"]["hint"]

    sent = route.calls[0].request.url
    assert sent.params["filters"] == '[{"status":{"operator":"o","values":[]}}]'
    assert sent.params["offset"] == "2"


async def test_read_only_server_hides_every_write_tool() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", read_only=True
    )
    async with Client(build_server(settings)) as client:
        listed = {tool.name for tool in await client.list_tools()}
    assert listed == PHASE_1_READ_TOOLS
    assert not listed & PHASE_1_WRITE_TOOLS


async def test_disable_prunes_exactly_the_named_groups() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        url=TEST_URL,
        api_key="test-token",
        disable="meetings,news,attachments",
    )
    async with Client(build_server(settings)) as client:
        listed = {tool.name for tool in await client.list_tools()}
    assert listed == PHASE_1_TOOLS - ATTACHMENT_GROUP_TOOLS
