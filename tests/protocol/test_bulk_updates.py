"""Batch safety: no writes during preview, preflight all, preserve per-item outcomes."""

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


def routes(api: respx.MockRouter, id: int) -> dict[str, respx.Route]:
    wp = deepcopy(WORK_PACKAGE_DETAIL)
    wp["id"] = id
    api.get("work_packages/schemas/5-1").respond(200, json=WORK_PACKAGE_SCHEMA_5_1)
    return {
        "get": api.get(f"work_packages/{id}").respond(200, json=wp),
        "form": api.post(f"work_packages/{id}/form").respond(200, json=UPDATE_FORM_OK),
        "patch": api.patch(f"work_packages/{id}").respond(200, json={**wp, "lockVersion": 8}),
    }


async def test_preview_then_apply(mock_api: respx.MockRouter, mcp_client: Client[Any]) -> None:
    first, second = routes(mock_api, 1234), routes(mock_api, 1235)
    updates = [
        {"id": 1234, "changes": {"subject": "Next release"}},
        {"id": 1235, "changes": {"assignee": None}},
    ]
    result = await mcp_client.call_tool("bulk_update_work_packages", {"updates": updates})
    preview = result.structured_content
    assert preview["dry_run"] and preview["applied"] == 0
    assert not first["patch"].called and not second["patch"].called
    assert preview["items"][0]["changes"] == [
        {"field": "subject", "before": "Ship the client layer", "after": "Next release"}
    ]
    assert preview["items"][1]["changes"][0]["after"] is None
    for update, item in zip(updates, preview["items"], strict=True):
        update["lock_version"] = item["lock_version"]
    result = await mcp_client.call_tool(
        "bulk_update_work_packages", {"updates": updates, "dry_run": False, "notify": False}
    )
    assert result.structured_content["applied"] == 2
    assert not result.structured_content["partial_failure"]
    assert all(item["lock_version"] == 8 for item in result.structured_content["items"])
    for item in (first, second):
        request = item["patch"].calls[0].request
        assert json.loads(request.content)["lockVersion"] == 7
        assert request.url.params["notify"] == "false"


async def test_invalid_item_blocks_whole_apply(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    first, second = routes(mock_api, 1234), routes(mock_api, 1235)
    second["form"].respond(403, json={"message": "Forbidden"})
    result = await mcp_client.call_tool(
        "bulk_update_work_packages",
        {
            "dry_run": False,
            "updates": [
                {"id": id, "lock_version": 7, "changes": {"subject": "Next"}} for id in (1234, 1235)
            ],
        },
    )
    assert [r["status"] for r in result.structured_content["items"]] == ["blocked", "invalid"]
    assert result.structured_content["applied"] == 0
    assert not first["patch"].called and not second["patch"].called


async def test_preview_lock_is_required_and_stale_lock_blocks_apply(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    update = {"id": 1234, "changes": {"subject": "Next"}}
    result = await mcp_client.call_tool(
        "bulk_update_work_packages", {"updates": [update], "dry_run": False}, raise_on_error=False
    )
    assert result.is_error and not mock_api.calls
    route = routes(mock_api, 1234)
    result = await mcp_client.call_tool(
        "bulk_update_work_packages", {"updates": [{**update, "lock_version": 6}], "dry_run": False}
    )
    item = result.structured_content["items"][0]
    assert item["error"]["type"] == "conflict"
    assert item["error"]["lock_version"] == 7
    assert not route["form"].called and not route["patch"].called


@pytest.mark.parametrize("failure,status", [(409, "conflict"), (403, "failed"), (500, "unknown")])
async def test_execution_failure_preserves_successes_and_does_not_retry(
    mock_api: respx.MockRouter, mcp_client: Client[Any], failure: int, status: str
) -> None:
    first, second, third = (routes(mock_api, id) for id in (1234, 1235, 1236))
    second["patch"].respond(failure, json={"message": "Rejected"})
    result = await mcp_client.call_tool(
        "bulk_update_work_packages",
        {
            "dry_run": False,
            "updates": [
                {"id": id, "lock_version": 7, "changes": {"subject": "Next"}}
                for id in (1234, 1235, 1236)
            ],
        },
    )
    payload = result.structured_content
    assert payload["applied"] == 2 and payload["partial_failure"]
    assert [r["status"] for r in payload["items"]] == ["updated", status, "updated"]
    assert all(route["patch"].call_count == 1 for route in (first, second, third))
    # Every form precedes the first PATCH.
    methods = [call.request.method for call in mock_api.calls]
    assert methods.index("PATCH") > max(i for i, method in enumerate(methods) if method == "POST")


async def test_lost_response_is_unknown(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = routes(mock_api, 1234)
    route["patch"].mock(side_effect=httpx.ReadTimeout("lost response"))
    result = await mcp_client.call_tool(
        "bulk_update_work_packages",
        {
            "dry_run": False,
            "updates": [{"id": 1234, "lock_version": 7, "changes": {"subject": "Next"}}],
        },
    )
    assert result.structured_content["items"][0]["status"] == "unknown"
    assert route["patch"].call_count == 1


@pytest.mark.parametrize(
    "updates",
    [
        [],
        [{"id": 1, "changes": {}}] * 51,
        [{"id": 1, "changes": {"subject": "A"}}, {"id": 1, "changes": {"subject": "B"}}],
        [{"id": 1, "changes": {"bogus": "ignored?"}}],
    ],
)
async def test_invalid_batch_shape_never_contacts_api(
    mock_api: respx.MockRouter, mcp_client: Client[Any], updates: list[dict[str, Any]]
) -> None:
    result = await mcp_client.call_tool(
        "bulk_update_work_packages", {"updates": updates}, raise_on_error=False
    )
    assert result.is_error and not mock_api.calls
