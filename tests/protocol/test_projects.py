"""Protocol tests for the project tools (SPEC §6.6).

Everything runs through the in-memory FastMCP client against the real server
build, so registration, annotations, the §9.3 envelope and the §4.2 error
envelope are all exercised the way a client sees them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from tests.fixtures.hal_payloads import MULTIPLE_ERRORS
from tests.fixtures.projects_metadata_payloads import (
    PROJECT,
    PROJECT_COLLECTION,
    PROJECT_STATUS_FILTER_ERROR,
)


def error_envelope(result: Any) -> dict[str, Any]:
    """The parsed ``{"error": {...}}`` payload of a failed tool call."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


async def test_project_tools_are_registered_as_reads(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    for name in ("list_projects", "get_project"):
        tool = tools[name]
        assert tool.outputSchema is not None
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.description


async def test_list_projects_pages_and_sends_the_search_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("projects").mock(return_value=httpx.Response(200, json=PROJECT_COLLECTION))

    result = await mcp_client.call_tool(
        "list_projects", {"search": "  demo  ", "page": 2, "page_size": 20}
    )

    structured = result.structured_content
    assert structured is not None
    assert structured["pagination"] == {"total": 42, "page": 2, "page_size": 20, "has_more": True}
    assert structured["items"][0] == {
        "id": 7,
        "identifier": "demo-project",
        "name": "Demo project",
        "active": True,
        "public": False,
        "parent": {"id": 3, "name": "Customer work"},
        "status_code": "at_risk",
    }
    assert structured["items"][1]["parent"] is None
    assert structured["items"][1]["status_code"] is None
    assert structured["items"][1]["active"] is False

    sent = route.calls[0].request.url
    assert json.loads(sent.params["filters"]) == [
        {"name_and_identifier": {"operator": "~", "values": ["demo"]}},
        {"active": {"operator": "=", "values": ["t"]}},
    ]
    assert sent.params["offset"] == "2"
    assert sent.params["pageSize"] == "20"


async def test_list_projects_scopes_to_direct_children_and_favorites(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("projects").mock(return_value=httpx.Response(200, json=PROJECT_COLLECTION))

    await mcp_client.call_tool(
        "list_projects",
        {"parent_id": 3, "favorites_only": True, "active": None, "sort_by": [["name", "asc"]]},
    )

    sent = route.calls[0].request.url
    assert json.loads(sent.params["filters"]) == [
        {"parent_id": {"operator": "=", "values": ["3"]}},
        {"favored": {"operator": "=", "values": ["t"]}},
    ]
    assert sent.params["sortBy"] == '[["name","asc"]]'


async def test_list_projects_maps_snake_case_sort_keys(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("projects").mock(return_value=httpx.Response(200, json=PROJECT_COLLECTION))

    await mcp_client.call_tool("list_projects", {"sort_by": [["created_at", "desc"]]})

    assert route.calls[0].request.url.params["sortBy"] == '[["createdAt","desc"]]'


async def test_list_projects_rejects_an_unknown_sort_key(mcp_client: Client[Any]) -> None:
    result = await mcp_client.call_tool(
        "list_projects", {"sort_by": [["priority", "asc"]]}, raise_on_error=False
    )

    error = error_envelope(result)
    assert error["type"] == "invalid_input"
    assert "priority" in error["message"]
    assert "identifier" in error["hint"]


async def test_favorites_only_explains_an_instance_that_lacks_the_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects").mock(
        return_value=httpx.Response(400, json=PROJECT_STATUS_FILTER_ERROR)
    )

    result = await mcp_client.call_tool(
        "list_projects", {"favorites_only": True}, raise_on_error=False
    )

    error = error_envelope(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 400
    assert "favorites_only" in error["hint"]


async def test_list_projects_surfaces_violations_from_a_422(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects").mock(return_value=httpx.Response(422, json=MULTIPLE_ERRORS))

    result = await mcp_client.call_tool("list_projects", {}, raise_on_error=False)

    error = error_envelope(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["error_identifier"].endswith("MultipleErrors")
    assert error["violations"] == [
        {"attribute": "subject", "message": "Subject can't be blank."},
        {"attribute": "type", "message": "Type is not set to one of the allowed values."},
    ]
    assert "violations" in error["hint"]


async def test_get_project_accepts_the_string_identifier(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("projects/demo-project").mock(
        return_value=httpx.Response(200, json=PROJECT)
    )

    result = await mcp_client.call_tool("get_project", {"id_or_identifier": "demo-project"})

    structured = result.structured_content
    assert structured is not None
    assert structured["id"] == 7
    assert structured["identifier"] == "demo-project"
    assert structured["status_code"] == "at_risk"
    assert structured["description"] == "The customer-facing demo."
    assert structured["status_explanation"] == "Sprint 4 slipped by two days."
    assert structured["parent"] == {"id": 3, "name": "Customer work"}
    assert structured["created_at"] == "2026-01-05T08:30:00Z"
    assert structured["updated_at"] == "2026-07-20T11:00:00Z"
    assert route.call_count == 1


async def test_get_project_accepts_a_numeric_id(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/7").mock(return_value=httpx.Response(200, json=PROJECT))

    result = await mcp_client.call_tool("get_project", {"id_or_identifier": 7})

    assert result.structured_content is not None
    assert result.structured_content["name"] == "Demo project"


@pytest.mark.parametrize(
    ("value", "path", "expected_phrase"),
    [
        (4242, "projects/4242", "numeric id"),
        ("no-such-slug", "projects/no-such-slug", "URL slug"),
    ],
)
async def test_get_project_404_hint_distinguishes_id_from_identifier(
    mcp_client: Client[Any],
    mock_api: respx.MockRouter,
    value: int | str,
    path: str,
    expected_phrase: str,
) -> None:
    mock_api.get(path).mock(
        return_value=httpx.Response(
            404, json={"message": "The requested resource could not be found."}
        )
    )

    result = await mcp_client.call_tool(
        "get_project", {"id_or_identifier": value}, raise_on_error=False
    )

    error = error_envelope(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert expected_phrase in error["hint"]
    assert "list_projects" in error["hint"]


async def test_get_project_conflict_status_becomes_a_structured_conflict(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/7").mock(
        return_value=httpx.Response(409, json={"message": "The resource has changed."})
    )

    result = await mcp_client.call_tool(
        "get_project", {"id_or_identifier": 7}, raise_on_error=False
    )

    error = error_envelope(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert "lock_version" in error["hint"]


async def test_get_project_rejects_a_blank_identifier(mcp_client: Client[Any]) -> None:
    result = await mcp_client.call_tool(
        "get_project", {"id_or_identifier": "   "}, raise_on_error=False
    )

    error = error_envelope(result)
    assert error["type"] == "invalid_input"
    assert "list_projects" in error["hint"]
