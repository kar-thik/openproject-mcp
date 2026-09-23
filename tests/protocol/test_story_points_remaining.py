"""Story Points + Remaining work writes on single and bulk updates."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from tests.fixtures.work_packages_payloads import (
    UPDATE_FORM_OK,
    WORK_PACKAGE_DETAIL,
    WORK_PACKAGE_SCHEMA_5_1,
)

WP_PATH = "work_packages/1234"
SCHEMA_PATH = "work_packages/schemas/5-1"


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _structured(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def test_update_writes_story_points_and_remaining_hours(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    updated = deepcopy(WORK_PACKAGE_DETAIL)
    updated["lockVersion"] = 8
    updated["storyPoints"] = 3
    updated["remainingTime"] = "PT1H30M"
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    patch = mock_api.patch(WP_PATH).mock(return_value=httpx.Response(200, json=updated))

    structured = _structured(
        await mcp_client.call_tool(
            "update_work_package",
            {"id": 1234, "story_points": 3, "remaining_hours": 1.5},
        )
    )

    assert structured["story_points"] == 3
    assert structured["remaining_hours"] == 1.5
    body = _body(patch.calls[0].request)
    assert body["storyPoints"] == 3
    assert body["remainingTime"] == "PT1H30M"


@pytest.mark.parametrize(
    ("echoed", "dropped"),
    [({}, True), ({"storyPoints": 3}, False), ({"storyPoints": 5}, True)],
    ids=["absent", "saved", "different"],
)
async def test_update_notes_story_points_openproject_silently_dropped(
    mock_api: respx.MockRouter,
    mcp_client: Client[Any],
    echoed: dict[str, Any],
    dropped: bool,
) -> None:
    """14.x answers 200 but drops storyPoints without Backlogs; the response omits it."""
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    mock_api.patch(WP_PATH).mock(
        return_value=httpx.Response(200, json={**WORK_PACKAGE_DETAIL, **echoed})
    )

    structured = _structured(
        await mcp_client.call_tool("update_work_package", {"id": 1234, "story_points": 3})
    )

    notes = structured["notes"] or []
    assert any("story_points was not saved" in note for note in notes) is dropped
    assert any("Backlogs" in note for note in notes) is dropped


async def test_update_without_story_points_adds_no_drop_note(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    mock_api.patch(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))

    structured = _structured(
        await mcp_client.call_tool("update_work_package", {"id": 1234, "remaining_hours": 2})
    )

    assert not any("story_points" in note for note in structured["notes"] or [])


async def test_update_leaves_story_and_remaining_untouched_when_omitted(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    patch = mock_api.patch(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))

    await mcp_client.call_tool("update_work_package", {"id": 1234, "subject": "Renamed"})

    body = _body(patch.calls[0].request)
    assert "storyPoints" not in body
    assert "remainingTime" not in body


async def test_update_writes_zero_story_points_and_remaining_hours(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    """Zero is a value, not "omitted": both fields must reach the wire."""
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    patch = mock_api.patch(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))

    await mcp_client.call_tool(
        "update_work_package", {"id": 1234, "story_points": 0, "remaining_hours": 0}
    )

    body = _body(patch.calls[0].request)
    assert body["storyPoints"] == 0
    assert body["remainingTime"] == "PT0H"


async def test_get_surfaces_story_points_and_remaining_hours(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    current = deepcopy(WORK_PACKAGE_DETAIL)
    current["storyPoints"] = 5
    current["remainingTime"] = "PT2H"
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=current))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))

    structured = _structured(await mcp_client.call_tool("get_work_package", {"id": 1234}))

    assert structured["story_points"] == 5
    assert structured["remaining_hours"] == 2.0


async def test_bulk_dry_run_shows_story_and_remaining_diffs(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    wp = deepcopy(WORK_PACKAGE_DETAIL)
    mock_api.get(SCHEMA_PATH).respond(200, json=WORK_PACKAGE_SCHEMA_5_1)
    mock_api.get(WP_PATH).respond(200, json=wp)
    mock_api.post(f"{WP_PATH}/form").respond(200, json=UPDATE_FORM_OK)
    patch = mock_api.patch(WP_PATH).respond(
        200, json={**wp, "lockVersion": 8, "storyPoints": 3, "remainingTime": "PT1H30M"}
    )

    preview = await mcp_client.call_tool(
        "bulk_update_work_packages",
        {"updates": [{"id": 1234, "changes": {"story_points": 3, "remaining_hours": 1.5}}]},
    )
    assert not preview.is_error
    fields = {c["field"] for c in preview.structured_content["items"][0]["changes"]}
    assert {"storyPoints", "remainingTime"} <= fields
    assert not patch.called, "dry run must not write"

    updates = [{"id": 1234, "changes": {"story_points": 3, "remaining_hours": 1.5}}]
    for update, item in zip(updates, preview.structured_content["items"], strict=True):
        update["lock_version"] = item["lock_version"]
    result = await mcp_client.call_tool(
        "bulk_update_work_packages", {"updates": updates, "dry_run": False}
    )
    assert not result.is_error
    assert result.structured_content["applied"] == 1
    body = _body(patch.calls[0].request)
    assert body["storyPoints"] == 3
    assert body["remainingTime"] == "PT1H30M"
