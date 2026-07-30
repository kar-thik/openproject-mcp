"""Protocol tests for the notification tools (SPEC §6.8, §9.3, G1/G5).

Everything runs through the in-memory FastMCP client against a respx-mocked
instance, so these assert what a model actually receives: the §9.3 envelope, the
polymorphic resource projection, the exact filters that reach the wire, the
Enterprise hint on ``dateAlert``, and the structured error envelopes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.notifications_time_payloads import (
    DATE_ALERT_FILTER_ERROR,
    MARK_VALIDATION_ERROR,
    NOTIFICATION_MENTIONED,
    NOTIFICATION_NOT_FOUND,
    NOTIFICATION_PAGE,
    NOTIFICATION_WIKI,
    PROJECT_ID,
    WORK_PACKAGE_ID,
    notification_collection,
)


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def filters_of(request: httpx.Request) -> list[dict[str, Any]]:
    """The decoded ``filters`` query parameter of an outgoing request."""
    return json.loads(request.url.params["filters"])


# --- registration ---------------------------------------------------------


async def test_the_three_notification_tools_are_registered(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_notifications"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert set(listing.inputSchema["properties"]) == {
        "unread_only",
        "reason",
        "project_id",
        "page",
        "page_size",
    }

    marking = tools["mark_notifications"]
    assert marking.annotations is not None
    assert marking.annotations.readOnlyHint is False
    assert marking.annotations.destructiveHint is False
    assert marking.annotations.idempotentHint is True
    assert set(marking.inputSchema["properties"]) == {"ids", "read"}
    assert "ids" in marking.inputSchema["required"]

    mark_all = tools["mark_all_notifications_read"]
    assert mark_all.annotations is not None
    assert mark_all.annotations.readOnlyHint is False
    assert set(mark_all.inputSchema["properties"]) == {"reason", "project_id"}
    # The safe direction only: there is no "read" parameter to flip.
    assert "read" not in mark_all.inputSchema["properties"]


async def test_descriptions_state_the_blast_radius_and_the_enterprise_gate(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_notifications"].description or ""
    assert "mark_notifications" in listing
    assert "does **not** mark it read" in listing

    mark_all = tools["mark_all_notifications_read"].description or ""
    assert "every unread notification" in mark_all
    assert "no undo" in mark_all
    assert "mark_notifications" in mark_all

    reason_description = tools["list_notifications"].inputSchema["properties"]["reason"][
        "description"
    ]
    assert "Enterprise" in reason_description


# --- listing --------------------------------------------------------------


async def test_unread_only_sends_the_boolean_read_ian_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("notifications").mock(
        return_value=httpx.Response(200, json=NOTIFICATION_PAGE)
    )
    result = await mcp_client.call_tool("list_notifications", {})

    assert filters_of(route.calls[0].request) == [{"readIAN": {"operator": "=", "values": ["f"]}}]
    assert route.calls[0].request.url.params["offset"] == "1"
    assert route.calls[0].request.url.params["pageSize"] == "20"

    assert result.structured_content is not None
    assert result.structured_content["pagination"] == {
        "total": 37,
        "page": 1,
        "page_size": 20,
        "has_more": True,
    }


async def test_unread_only_false_sends_no_read_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("notifications").mock(
        return_value=httpx.Response(200, json=NOTIFICATION_PAGE)
    )
    await mcp_client.call_tool("list_notifications", {"unread_only": False})
    assert "filters" not in route.calls[0].request.url.params


async def test_reason_and_project_filters_are_combined(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("notifications").mock(
        return_value=httpx.Response(200, json=NOTIFICATION_PAGE)
    )
    await mcp_client.call_tool(
        "list_notifications",
        {"reason": "mentioned", "project_id": PROJECT_ID, "page": 2, "page_size": 5},
    )
    assert filters_of(route.calls[0].request) == [
        {"readIAN": {"operator": "=", "values": ["f"]}},
        {"reason": {"operator": "=", "values": ["mentioned"]}},
        {"project": {"operator": "=", "values": ["7"]}},
    ]
    assert route.calls[0].request.url.params["offset"] == "2"
    assert route.calls[0].request.url.params["pageSize"] == "5"


async def test_rows_project_actor_project_and_polymorphic_resource(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("notifications").mock(return_value=httpx.Response(200, json=NOTIFICATION_PAGE))
    result = await mcp_client.call_tool("list_notifications", {})
    assert result.structured_content is not None
    mentioned, date_alert, wiki = result.structured_content["items"]

    assert mentioned == {
        "id": 4711,
        "reason": "mentioned",
        "read": False,
        "created_at": "2026-07-20T08:30:00Z",
        "actor": {"id": 12, "name": "Grace Hopper"},
        "project": {"id": PROJECT_ID, "name": "Client layer"},
        "resource": {
            "id": WORK_PACKAGE_ID,
            "type": "WorkPackage",
            "title": "Ship the client layer",
        },
    }

    # System-generated notifications have no actor at all.
    assert date_alert["actor"] is None
    assert date_alert["reason"] == "dateAlert"

    # A non-work-package subject keeps its own type, from the inlined resource.
    assert wiki["read"] is True
    assert wiki["resource"] == {"id": 44, "type": "WikiPage", "title": "Release checklist"}


async def test_empty_inbox_returns_the_envelope_with_has_more_false(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("notifications").mock(
        return_value=httpx.Response(200, json=notification_collection([], total=0))
    )
    result = await mcp_client.call_tool("list_notifications", {})
    assert result.structured_content is not None
    assert result.structured_content["items"] == []
    assert result.structured_content["pagination"] == {
        "total": 0,
        "page": 1,
        "page_size": 20,
        "has_more": False,
    }


async def test_date_alert_rejection_names_the_enterprise_gate(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("notifications").mock(
        return_value=httpx.Response(400, json=DATE_ALERT_FILTER_ERROR)
    )
    result = await mcp_client.call_tool(
        "list_notifications", {"reason": "dateAlert"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 400
    assert error["message"] == "Filters Reason filter has invalid values."
    assert "date_alerts" in error["hint"]
    assert "Enterprise" in error["hint"]


async def test_other_reasons_keep_the_generic_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("notifications").mock(
        return_value=httpx.Response(400, json=DATE_ALERT_FILTER_ERROR)
    )
    result = await mcp_client.call_tool(
        "list_notifications", {"reason": "mentioned"}, raise_on_error=False
    )
    assert "date_alerts" not in error_of(result)["hint"]


# --- marking specific ids -------------------------------------------------


async def test_marking_ids_is_one_bulk_request_with_an_id_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("notifications/read_ian").mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool("mark_notifications", {"ids": [4711, 4712, 4711]})

    assert route.call_count == 1
    assert filters_of(route.calls[0].request) == [
        {"id": {"operator": "=", "values": ["4711", "4712"]}}
    ]
    # The empty JSON body is what makes httpx send a Content-Type header;
    # without one OpenProject answers 406 before marking anything.
    assert route.calls[0].request.headers["content-type"] == "application/json"
    assert route.calls[0].request.content == b"{}"
    assert result.structured_content is not None
    assert result.structured_content["marked"] == 2
    assert result.structured_content["read"] is True
    assert result.structured_content["ids"] == [4711, 4712]
    assert "read" in result.structured_content["message"]


async def test_marking_unread_uses_the_unread_endpoint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.post("notifications/read_ian").mock(return_value=httpx.Response(204))
    unread = mock_api.post("notifications/unread_ian").mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool("mark_notifications", {"ids": [4711], "read": False})

    assert read.call_count == 0
    assert unread.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["read"] is False
    assert "unread" in result.structured_content["message"]


async def test_empty_ids_never_reach_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("notifications/read_ian").mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool("mark_notifications", {"ids": []}, raise_on_error=False)

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "mark_all_notifications_read" in error["hint"]
    assert route.call_count == 0


async def test_too_many_ids_is_refused_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("notifications/read_ian").mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool(
        "mark_notifications", {"ids": list(range(1, 300))}, raise_on_error=False
    )
    assert error_of(result)["type"] == "invalid_input"
    assert route.call_count == 0


async def test_unknown_notification_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("notifications/read_ian").mock(
        return_value=httpx.Response(404, json=NOTIFICATION_NOT_FOUND)
    )
    result = await mcp_client.call_tool(
        "mark_notifications", {"ids": [999999]}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "ids come from" in error["hint"]


async def test_rejected_mark_carries_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("notifications/read_ian").mock(
        return_value=httpx.Response(422, json=MARK_VALIDATION_ERROR)
    )
    result = await mcp_client.call_tool("mark_notifications", {"ids": [4711]}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "ids", "message": "Ids is not a valid notification filter value."}
    ]
    assert "violations" in error["hint"]


# --- the mass operation ---------------------------------------------------


async def test_marking_all_counts_first_then_clears_the_inbox(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    count = mock_api.get("notifications").mock(
        return_value=httpx.Response(
            200, json=notification_collection([NOTIFICATION_MENTIONED], total=12, page_size=1)
        )
    )
    bulk = mock_api.post("notifications/read_ian").mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool("mark_all_notifications_read", {})

    assert filters_of(count.calls[0].request) == [{"readIAN": {"operator": "=", "values": ["f"]}}]
    assert count.calls[0].request.url.params["pageSize"] == "1"
    assert filters_of(bulk.calls[0].request) == [{"readIAN": {"operator": "=", "values": ["f"]}}]
    # json={} keeps the Content-Type header that OpenProject requires (406 otherwise).
    assert bulk.calls[0].request.headers["content-type"] == "application/json"
    assert bulk.calls[0].request.content == b"{}"

    assert result.structured_content is not None
    assert result.structured_content["marked"] == 12
    assert result.structured_content["read"] is True
    assert result.structured_content["ids"] is None


async def test_marking_all_narrows_by_reason_and_project(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("notifications").mock(
        return_value=httpx.Response(
            200, json=notification_collection([NOTIFICATION_WIKI], total=3, page_size=1)
        )
    )
    bulk = mock_api.post("notifications/read_ian").mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "mark_all_notifications_read", {"reason": "watched", "project_id": "client-layer"}
    )

    assert filters_of(bulk.calls[0].request) == [
        {"readIAN": {"operator": "=", "values": ["f"]}},
        {"reason": {"operator": "=", "values": ["watched"]}},
        {"project": {"operator": "=", "values": ["client-layer"]}},
    ]
    assert result.structured_content is not None
    assert result.structured_content["marked"] == 3
    assert "reason=watched" in result.structured_content["message"]
    assert "project=client-layer" in result.structured_content["message"]


async def test_marking_all_with_date_alert_gets_the_enterprise_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("notifications").mock(
        return_value=httpx.Response(400, json=DATE_ALERT_FILTER_ERROR)
    )
    bulk = mock_api.post("notifications/read_ian").mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "mark_all_notifications_read", {"reason": "dateAlert"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert "date_alerts" in error["hint"]
    assert bulk.call_count == 0, "nothing may be marked when the filter was rejected"
