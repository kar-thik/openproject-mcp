"""Protocol tests for the saved-query tools (SPEC §6.7, §9.2, §9.3).

Everything goes through the in-memory FastMCP client against a respx-mocked
instance: the wire filter a project scope produces, the run-on-read semantics of
``GET /queries/{id}``, the §9.3 envelope with server-side groups and sums, the
readable rendering of a stored filter set, and the error envelopes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.git_queries_payloads import (
    NOT_FOUND_ERROR,
    PROJECT_QUERY,
    QUERY_COLLECTION,
    QUERY_ID,
    QUERY_INVALID_FILTER_ERROR,
    QUERY_WITH_RESULTS,
    hal_collection,
)

QUERY_PATH = f"queries/{QUERY_ID}"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


# --- registration ---------------------------------------------------------


async def test_both_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_queries"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert set(listing.inputSchema["properties"]) == {"project_id", "page", "page_size"}

    running = tools["run_query"]
    assert running.annotations is not None
    assert running.annotations.readOnlyHint is True
    assert running.annotations.model_extra is not None
    assert running.annotations.model_extra["anthropic/maxResultSizeChars"] == 100_000
    assert set(running.inputSchema["properties"]) == {
        "query_id",
        "page",
        "page_size",
        "override_filters",
    }
    description = running.description or ""
    assert "run on read" in description
    assert "list_queries" in description
    assert "replaces the stored filters" in description.lower()


# --- list_queries ---------------------------------------------------------


async def test_queries_are_listed_with_the_standard_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("queries").mock(return_value=httpx.Response(200, json=QUERY_COLLECTION))

    result = await mcp_client.call_tool("list_queries", {})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["pagination"] == {"total": 2, "page": 1, "page_size": 20, "has_more": False}
    assert payload["items"][0] == {
        "id": QUERY_ID,
        "name": "Sprint board",
        "project": {"id": 5, "name": "Platform"},
        "public": True,
        "starred": True,
        "updated_at": "2026-07-01T11:30:00Z",
    }
    assert payload["items"][1]["project"] is None, "a global query has no project"
    assert payload["items"][1]["public"] is False
    assert any("global queries" in note for note in payload["notes"])

    params = route.calls[0].request.url.params
    assert params["offset"] == "1"
    assert params["pageSize"] == "20"
    assert "filters" not in params


async def test_project_scope_sends_the_queries_project_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("queries").mock(
        return_value=httpx.Response(200, json=hal_collection([PROJECT_QUERY], total=1))
    )

    result = await mcp_client.call_tool("list_queries", {"project_id": 5, "page_size": 50})
    assert result.structured_content is not None
    assert result.structured_content["items"][0]["id"] == QUERY_ID
    assert result.structured_content["notes"] is None

    params = route.calls[0].request.url.params
    assert json.loads(params["filters"]) == [{"project": {"operator": "=", "values": ["5"]}}]
    assert params["pageSize"] == "50"


# --- run_query ------------------------------------------------------------


async def test_running_a_query_returns_rows_groups_sums_and_the_definition(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(QUERY_PATH).mock(return_value=httpx.Response(200, json=QUERY_WITH_RESULTS))

    result = await mcp_client.call_tool("run_query", {"query_id": QUERY_ID})
    assert result.structured_content is not None
    payload = result.structured_content

    # The stored page size wins when the caller does not override it.
    assert "offset" not in route.calls[0].request.url.params
    assert "pageSize" not in route.calls[0].request.url.params
    assert payload["pagination"] == {"total": 37, "page": 1, "page_size": 2, "has_more": True}

    assert payload["items"][0] == {
        "id": 1234,
        "subject": "Ship the client layer",
        "type": {"id": 1, "name": "Task"},
        "status": {"id": 7, "name": "In progress"},
        "priority": {"id": 8, "name": "Normal"},
        "assignee": {"id": 12, "name": "Grace Hopper"},
        "project": {"id": 5, "name": "Platform"},
        "start_date": "2026-07-01",
        "due_date": "2026-07-31",
        "percentage_done": 40,
        "updated_at": "2026-07-06T09:00:00Z",
    }

    # Groups and sums come from the server, over the full result set.
    assert payload["groups"] == [
        {"value": "In progress", "count": 12, "sums": {"estimated_hours": 41.5}},
        {"value": "New", "count": 25, "sums": {"estimated_hours": 100.0}},
    ]
    assert payload["sums"] == {"estimated_hours": 141.5, "story_points": 55.0}
    assert payload["notes"] is None

    query = payload["query"]
    assert query["id"] == QUERY_ID
    assert query["name"] == "Sprint board"
    assert query["project"] == {"id": 5, "name": "Platform"}
    assert query["group_by"] == "Status"
    assert query["sort_by"] == ["Finish date asc"]
    assert query["display_sums"] is True
    assert query["filters"] == [
        "Status open",
        "Assignee is (OR) Grace Hopper, Ada Lovelace",
        "Finish date between (open), 2026-08-01",
    ]


async def test_paging_overrides_the_stored_query_properties(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(QUERY_PATH).mock(return_value=httpx.Response(200, json=QUERY_WITH_RESULTS))

    await mcp_client.call_tool("run_query", {"query_id": QUERY_ID, "page": 3, "page_size": 50})

    params = route.calls[0].request.url.params
    # OpenProject's offset is a 1-based page number, not a record offset.
    assert params["offset"] == "3"
    assert params["pageSize"] == "50"


async def test_override_filters_replace_the_stored_ones_and_say_so(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(QUERY_PATH).mock(return_value=httpx.Response(200, json=QUERY_WITH_RESULTS))

    result = await mcp_client.call_tool(
        "run_query",
        {
            "query_id": QUERY_ID,
            "override_filters": [
                {"name": "status", "operator": "o", "values": []},
                {"name": "customField12", "operator": "=", "values": ["4"]},
            ],
        },
    )
    assert result.structured_content is not None

    assert json.loads(route.calls[0].request.url.params["filters"]) == [
        {"status": {"operator": "o", "values": []}},
        {"customField12": {"operator": "=", "values": ["4"]}},
    ]
    assert any("replaced the stored filters" in note for note in result.structured_content["notes"])
    # The stored definition is still reported, so the swap is visible.
    assert result.structured_content["query"]["filters"][0] == "Status open"


async def test_an_empty_override_runs_the_query_unfiltered(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(QUERY_PATH).mock(return_value=httpx.Response(200, json=QUERY_WITH_RESULTS))
    await mcp_client.call_tool("run_query", {"query_id": QUERY_ID, "override_filters": []})
    assert route.calls[0].request.url.params["filters"] == "[]"


async def test_an_impossible_filter_operator_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(QUERY_PATH).mock(return_value=httpx.Response(200, json=QUERY_WITH_RESULTS))
    result = await mcp_client.call_tool(
        "run_query",
        {
            "query_id": QUERY_ID,
            "override_filters": [{"name": "subject", "operator": "o", "values": []}],
        },
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "subject" in error["message"]
    assert "allowed operators" in error["hint"].lower()
    assert route.call_count == 0


async def test_a_query_without_embedded_results_degrades_with_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(QUERY_PATH).mock(return_value=httpx.Response(200, json=PROJECT_QUERY))

    result = await mcp_client.call_tool("run_query", {"query_id": QUERY_ID})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["items"] == []
    assert payload["pagination"] == {"total": 0, "page": 1, "page_size": 20, "has_more": False}
    assert any("without embedded results" in note for note in payload["notes"])
    assert payload["query"]["name"] == "Sprint board"


async def test_unknown_query_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("queries/999").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    result = await mcp_client.call_tool("run_query", {"query_id": 999}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "ids come from" in error["hint"]


async def test_a_rejected_filter_set_surfaces_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(QUERY_PATH).mock(return_value=httpx.Response(422, json=QUERY_INVALID_FILTER_ERROR))

    result = await mcp_client.call_tool(
        "run_query",
        {
            "query_id": QUERY_ID,
            "override_filters": [{"name": "assignee", "operator": "=", "values": ["nobody"]}],
        },
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "filters", "message": "Assignee filter has invalid values."}
    ]
    assert "violations" in error["hint"]
