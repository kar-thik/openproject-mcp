"""Protocol tests for ``list_permissions`` (SPEC §6.1, §4.7, G1/G4/G5).

The corrections this tool exists for are all asserted here: the principal filter
carries the NUMERIC current-user id (the capabilities API has no ``"me"``), the
context filter is ``g``/``p{id}`` with the ``w{id}`` fallback cached after the
first rejection, every capability page is read (with the cap reported), and the
description carries the API's own "only a subset of actions is exposed" caveat.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.projects_versions_payloads import (
    CAPABILITIES_FILTER_ERROR,
    CURRENT_USER,
    GLOBAL_CAPABILITIES,
    PROJECT_CAPABILITIES,
    PROJECT_CONTEXT_HREF,
    PROJECT_NOT_FOUND,
    capability,
    capability_collection,
)

CAPABILITY_PROJECT_ID = 12

PROJECT_12: dict[str, Any] = {
    "_type": "Project",
    "id": CAPABILITY_PROJECT_ID,
    "identifier": "demo-project",
    "name": "Demo project",
    "_links": {"self": {"href": PROJECT_CONTEXT_HREF, "title": "Demo project"}},
}


def error_of(result: Any) -> dict[str, Any]:
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def filters_of(request: httpx.Request) -> dict[str, dict[str, Any]]:
    """The filter array of a request, flattened to ``{name: {operator, values}}``."""
    raw = json.loads(request.url.params["filters"])
    return {name: body for entry in raw for name, body in entry.items()}


def route_me(mock_api: respx.MockRouter) -> respx.Route:
    return mock_api.get("users/me").mock(return_value=httpx.Response(200, json=CURRENT_USER))


# --- registration ---------------------------------------------------------


async def test_list_permissions_is_registered_as_a_read_tool(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    tool = tools["list_permissions"]

    assert tool.outputSchema is not None
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert set(tool.inputSchema["properties"]) == {"project_id", "permission"}
    assert set((tool.meta or {})["fastmcp"]["tags"]) == {"metadata", "read"}


async def test_description_carries_the_subset_caveat_and_the_no_me_rule(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    description = tools["list_permissions"].description or ""
    assert "SUBSET" in description
    assert "is not proof" in description
    assert '"me"' in description
    assert "numeric id" in description


# --- the global context ---------------------------------------------------


async def test_global_permissions_filter_by_the_numeric_principal_id(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    me = route_me(mock_api)
    route = mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection(GLOBAL_CAPABILITIES))
    )

    result = await mcp_client.call_tool("list_permissions", {})

    assert me.call_count == 1
    filters = filters_of(route.calls[0].request)
    assert filters["principal"] == {"operator": "=", "values": ["1"]}
    assert filters["context"] == {"operator": "=", "values": ["g"]}
    assert route.calls[0].request.url.params["pageSize"] == "100"
    assert route.calls[0].request.url.params["offset"] == "1"

    assert result.structured_content is not None
    content = result.structured_content
    assert content["principal"] == {"id": 1, "name": "Ada Lovelace"}
    assert content["capability_count"] == 2
    assert content["items"] == [
        {
            "id": "global",
            "context": "global",
            "project": None,
            "actions": ["projects/create", "users/read"],
        }
    ]
    assert content["pagination"] == {"total": 1, "page": 1, "page_size": 1, "has_more": False}
    assert any("subset" in note for note in content["notes"])
    assert content["check"] is None


async def test_the_current_user_is_resolved_once_and_reused_from_cache(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    me = route_me(mock_api)
    mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection(GLOBAL_CAPABILITIES))
    )

    await mcp_client.call_tool("list_permissions", {})
    await mcp_client.call_tool("list_permissions", {})

    assert me.call_count == 1


# --- a project context ----------------------------------------------------


async def test_project_permissions_use_the_p_prefix_and_group_by_context(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    route = mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection(PROJECT_CAPABILITIES))
    )

    result = await mcp_client.call_tool("list_permissions", {"project_id": CAPABILITY_PROJECT_ID})

    assert filters_of(route.calls[0].request)["context"] == {"operator": "=", "values": ["p12"]}
    assert result.structured_content is not None
    row = result.structured_content["items"][0]
    assert row["id"] == "project:12"
    assert row["context"] == "project"
    assert row["project"] == {"id": 12, "name": "Demo project"}
    # The action name keeps its resource half — a naive href parse would say "create".
    assert row["actions"] == [
        "memberships/create",
        "versions/manage",
        "work_packages/create",
    ]


async def test_a_project_identifier_is_resolved_to_its_numeric_id_first(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    project = mock_api.get("projects/demo-project").mock(
        return_value=httpx.Response(200, json=PROJECT_12)
    )
    route = mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection(PROJECT_CAPABILITIES))
    )

    await mcp_client.call_tool("list_permissions", {"project_id": "demo-project"})

    assert project.call_count == 1
    assert filters_of(route.calls[0].request)["context"]["values"] == ["p12"]


async def test_an_unknown_project_identifier_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    mock_api.get("projects/ghost-project").mock(
        return_value=httpx.Response(404, json=PROJECT_NOT_FOUND)
    )
    capabilities = mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection([]))
    )

    result = await mcp_client.call_tool(
        "list_permissions", {"project_id": "ghost-project"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "ids come from" in error["hint"]
    assert capabilities.call_count == 0


# --- the p{id} → w{id} fallback (SPEC §4.7) -------------------------------


async def test_a_rejected_context_prefix_is_retried_as_w_and_then_cached(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    route = mock_api.get("capabilities").mock(
        side_effect=[
            httpx.Response(400, json=CAPABILITIES_FILTER_ERROR),
            httpx.Response(200, json=capability_collection(PROJECT_CAPABILITIES)),
            httpx.Response(200, json=capability_collection(PROJECT_CAPABILITIES)),
        ]
    )

    first = await mcp_client.call_tool("list_permissions", {"project_id": CAPABILITY_PROJECT_ID})
    assert first.structured_content is not None
    assert first.structured_content["capability_count"] == 3

    assert route.call_count == 2
    assert filters_of(route.calls[0].request)["context"]["values"] == ["p12"]
    assert filters_of(route.calls[1].request)["context"]["values"] == ["w12"]

    # The working prefix is remembered, so the second call does not pay for the probe.
    await mcp_client.call_tool("list_permissions", {"project_id": CAPABILITY_PROJECT_ID})
    assert route.call_count == 3
    assert filters_of(route.calls[2].request)["context"]["values"] == ["w12"]


async def test_a_rejection_of_both_prefixes_surfaces_with_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    mock_api.get("capabilities").mock(
        side_effect=[
            httpx.Response(400, json=CAPABILITIES_FILTER_ERROR),
            httpx.Response(400, json=CAPABILITIES_FILTER_ERROR),
        ]
    )

    result = await mcp_client.call_tool(
        "list_permissions", {"project_id": CAPABILITY_PROJECT_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 400
    assert error["violations"] == [
        {"attribute": "filters", "message": "Filters Context filter has invalid values."}
    ]
    assert error["hint"]


# --- paging and the cap (G1) ---------------------------------------------


async def test_every_page_is_read_and_the_cap_is_reported(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    page = [capability(f"resource{index}/read") for index in range(100)]
    route = mock_api.get("capabilities").mock(
        side_effect=[
            httpx.Response(200, json=capability_collection(page, total=600, offset=number))
            for number in range(1, 7)
        ]
    )

    result = await mcp_client.call_tool("list_permissions", {})

    # 500 of 600 read, then stopped at the cap — five pages, not six.
    assert route.call_count == 5
    assert [call.request.url.params["offset"] for call in route.calls] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert result.structured_content is not None
    assert result.structured_content["capability_count"] == 500
    assert any("capped at 500 of 600" in note for note in result.structured_content["notes"])


async def test_paging_stops_when_the_server_runs_out_of_elements(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    route = mock_api.get("capabilities").mock(
        side_effect=[
            httpx.Response(
                200,
                json=capability_collection(
                    [capability(f"resource{index}/read") for index in range(100)],
                    total=137,
                    offset=1,
                ),
            ),
            httpx.Response(
                200,
                json=capability_collection(
                    [capability(f"other{index}/read") for index in range(37)],
                    total=137,
                    offset=2,
                ),
            ),
        ]
    )

    result = await mcp_client.call_tool("list_permissions", {})

    assert route.call_count == 2
    assert result.structured_content is not None
    assert result.structured_content["capability_count"] == 137
    assert not any("capped" in note for note in result.structured_content["notes"])


# --- the permission predicate --------------------------------------------


async def test_permission_predicate_reports_where_the_action_is_granted(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection(PROJECT_CAPABILITIES))
    )

    result = await mcp_client.call_tool(
        "list_permissions",
        {"project_id": CAPABILITY_PROJECT_ID, "permission": "memberships/create"},
    )
    assert result.structured_content is not None
    assert result.structured_content["check"] == {
        "checked": "memberships/create",
        "allowed": True,
        "granted_in": ["project:12"],
    }
    # The full listing is still returned alongside the predicate.
    assert result.structured_content["items"][0]["actions"]


async def test_permission_predicate_is_false_when_the_action_is_absent(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_me(mock_api)
    mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection(PROJECT_CAPABILITIES))
    )

    result = await mcp_client.call_tool(
        "list_permissions",
        {"project_id": CAPABILITY_PROJECT_ID, "permission": "/Projects/Delete"},
    )
    assert result.structured_content is not None
    assert result.structured_content["check"] == {
        "checked": "projects/delete",
        "allowed": False,
        "granted_in": [],
    }


async def test_a_blank_permission_is_rejected_before_any_request(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    me = route_me(mock_api)
    capabilities = mock_api.get("capabilities").mock(
        return_value=httpx.Response(200, json=capability_collection(GLOBAL_CAPABILITIES))
    )

    result = await mcp_client.call_tool(
        "list_permissions", {"permission": "  "}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "work_packages/create" in error["hint"]
    assert me.call_count == 0
    assert capabilities.call_count == 0
