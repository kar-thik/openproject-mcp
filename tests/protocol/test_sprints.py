"""Protocol tests for the sprint tools (SPEC §6.10).

Sprints are standalone ``Sprint`` objects since OpenProject 17.3 — not versions.
The regression this module is named after is the wire-shape split: ``finishDate``
(not ``endDate``), ``definingWorkspace`` (not ``definingProject``) and the status
URN (not a version's open/locked/closed string). Sprint writes are deferred — the
public API documents no sprint write endpoint — and the registration test pins
that only the two read tools exist.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.projects_versions_payloads import (
    SPRINT,
    SPRINT_ID,
    SPRINT_IN_PLANNING,
    SPRINT_NOT_FOUND,
    version_collection,
)


def _structured(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def _envelope(result: Any) -> dict[str, Any]:
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


# --- registration ---------------------------------------------------------


async def test_sprint_tools_are_registered_as_read_only(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    assert "create_sprint" not in tools
    assert "update_sprint" not in tools
    assert "delete_sprint" not in tools

    listing = tools["list_sprints"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert set(listing.inputSchema["properties"]) == {"project_id", "page", "page_size"}
    assert set((listing.meta or {})["fastmcp"]["tags"]) == {"versions", "read"}

    reading = tools["get_sprint"]
    assert reading.annotations is not None
    assert reading.annotations.readOnlyHint is True
    assert set(reading.inputSchema["properties"]) == {"sprint_id"}


# --- listing --------------------------------------------------------------


async def test_project_scoped_listing_is_fetched_in_full(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("projects/7/sprints").mock(
        return_value=httpx.Response(200, json=version_collection([SPRINT, SPRINT_IN_PLANNING]))
    )

    structured = _structured(await mcp_client.call_tool("list_sprints", {"project_id": 7}))

    assert route.called
    assert structured["pagination"] == {"total": 2, "page": 1, "page_size": 2, "has_more": False}
    assert structured["notes"] is None
    first, second = structured["items"]
    assert first["id"] == SPRINT_ID
    assert first["name"] == "Sprint 11"
    assert first["start_date"] == "2026-02-02"
    assert first["finish_date"] == "2026-02-09", "wire finishDate lands, not a version endDate"
    assert "end_date" not in first, "sprint rows carry no version end_date"
    assert first["workspace"] == {"id": 7, "name": "Demo project"}
    assert first["status"] == "active", "parsed from the status URN tail"
    assert first["description"] == "Payments hardening.", "markdown raw, html dropped"
    assert second["status"] == "in_planning"


async def test_instance_wide_listing_sends_offset_and_page_size(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("sprints").mock(
        return_value=httpx.Response(
            200,
            json={
                **version_collection([SPRINT], total=40),
                "pageSize": 20,
                "offset": 2,
            },
        )
    )

    structured = _structured(
        await mcp_client.call_tool("list_sprints", {"page": 2, "page_size": 20})
    )

    params = route.calls[0].request.url.params
    assert params["offset"] == "2"
    assert params["pageSize"] == "20"
    assert structured["pagination"]["total"] == 40
    assert structured["pagination"]["has_more"] is True


async def test_project_scoped_unknown_project_points_at_list_projects(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/999/sprints").mock(
        return_value=httpx.Response(404, json=SPRINT_NOT_FOUND)
    )

    result = await mcp_client.call_tool(
        "list_sprints", {"project_id": 999}, raise_on_error=False
    )

    error = _envelope(result)
    assert error["type"] == "not_found"
    assert "list_projects" in error["hint"]


# --- reading --------------------------------------------------------------


async def test_get_sprint_returns_full_detail(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"sprints/{SPRINT_ID}").mock(
        return_value=httpx.Response(200, json=SPRINT)
    )

    structured = _structured(await mcp_client.call_tool("get_sprint", {"sprint_id": SPRINT_ID}))

    assert structured["id"] == SPRINT_ID
    assert structured["finish_date"] == "2026-02-09"
    assert structured["workspace"] == {"id": 7, "name": "Demo project"}
    assert structured["status"] == "active"
    assert structured["created_at"] == "2026-02-06T09:30:49.157Z"
    assert structured["updated_at"] == "2026-02-10T09:53:21.620Z"


async def test_get_unknown_sprint_points_at_list_sprints(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("sprints/999").mock(return_value=httpx.Response(404, json=SPRINT_NOT_FOUND))

    result = await mcp_client.call_tool(
        "get_sprint", {"sprint_id": 999}, raise_on_error=False
    )

    error = _envelope(result)
    assert error["type"] == "not_found"
    assert "list_sprints" in error["hint"]


# --- legacy interplay -----------------------------------------------------


async def test_new_shape_sprint_objects_are_skipped_by_list_versions(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """``list_versions(include_sprints=True)`` merges legacy backlogs versions only."""
    from tests.fixtures.projects_versions_payloads import SPRINT_ONLY, VERSION

    mock_api.get("projects/7/versions").mock(
        return_value=httpx.Response(200, json=version_collection([VERSION]))
    )
    mock_api.get("projects/7/sprints").mock(
        return_value=httpx.Response(
            200, json=version_collection([SPRINT_ONLY, SPRINT])
        )
    )

    structured = _structured(
        await mcp_client.call_tool(
            "list_versions", {"project_id": 7, "include_sprints": True}
        )
    )

    by_id = {item["id"]: item for item in structured["items"]}
    assert SPRINT_ONLY["id"] in by_id, "legacy _type Version rows still merge"
    assert by_id[SPRINT_ONLY["id"]]["source"] == "sprint"
    assert SPRINT_ID not in by_id, "new _type Sprint objects are not VersionRows"
    assert structured["notes"] is not None
    assert any("list_sprints" in note for note in structured["notes"])
