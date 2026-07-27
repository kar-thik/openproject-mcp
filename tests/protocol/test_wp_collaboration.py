"""Protocol tests for the Phase 1 collaboration tools (SPEC §6.3, §4.7, G1/G2).

Everything runs through the in-memory FastMCP client against a respx-mocked
instance, so these assert what a model actually receives: the §9.3 envelope,
the per-item truncation markers, the parsed journal details, the version gate on
internal comments, and the structured error envelopes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import API_BASE, TEST_URL
from tests.fixtures.hal_payloads import API_ROOT
from tests.fixtures.wp_collaboration_payloads import (
    COMMENT_CONFLICT_ERROR,
    COMMENT_VALIDATION_ERROR,
    CREATED_COMMENT_ACTIVITY,
    CREATED_INTERNAL_COMMENT_ACTIVITY,
    FIELD_CHANGE_ACTIVITY,
    FOREIGN_ACTIVITY,
    JOURNAL_ELEMENTS,
    LONG_COMMENT_ACTIVITY,
    LONG_COMMENT_TEXT,
    WORK_PACKAGE_ID,
    WORK_PACKAGE_JOURNAL,
    WORK_PACKAGE_NOT_FOUND,
    activity_collection,
)

ACTIVITIES_PATH = f"work_packages/{WORK_PACKAGE_ID}/activities"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def route_probe(mock_api: respx.MockRouter, core_version: str = "17.7.1") -> None:
    """Route the two calls ``ctx.probe()`` makes (SPEC §4.7).

    The API root has to be routed by full URL: with a router base_url, a
    relative pattern of ``""`` matches every path.
    """
    mock_api.get(url=f"{API_BASE}/").mock(
        return_value=httpx.Response(200, json={**API_ROOT, "coreVersion": core_version})
    )
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json={"total": 0}))


def server_for(core_version: str, mock_api: respx.MockRouter) -> FastMCP:
    route_probe(mock_api, core_version)
    return build_server(
        Settings(_env_file=None, url=TEST_URL, api_key="test-token")  # type: ignore[call-arg]
    )


# --- registration ---------------------------------------------------------


async def test_both_phase_one_tools_are_registered(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_work_package_comments"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert listing.annotations.model_extra is not None
    assert listing.annotations.model_extra["anthropic/maxResultSizeChars"] == 100_000
    assert set(listing.inputSchema["properties"]) == {
        "id",
        "page",
        "page_size",
        "max_comment_chars",
        "activity_id",
    }
    assert listing.inputSchema["properties"]["activity_id"]["description"]

    adding = tools["add_work_package_comment"]
    assert adding.outputSchema is not None
    assert adding.annotations is not None
    assert adding.annotations.readOnlyHint is False
    assert adding.annotations.destructiveHint is False
    assert adding.annotations.idempotentHint is False
    assert set(adding.inputSchema["properties"]) == {"id", "comment", "notify", "internal"}


async def test_description_admits_the_endpoint_is_unpaginated(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    description = tools["list_work_package_comments"].description or ""
    assert "unpaginated" in description
    assert "oldest first" in description
    assert "add_work_package_comment" in description


# --- listing: pagination over the unpaginated journal ---------------------


async def test_pagination_is_computed_client_side_over_the_whole_journal(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_JOURNAL)
    )

    first = await mcp_client.call_tool(
        "list_work_package_comments", {"id": WORK_PACKAGE_ID, "page": 1, "page_size": 10}
    )
    assert first.structured_content is not None
    assert first.structured_content["pagination"] == {
        "total": len(JOURNAL_ELEMENTS),
        "page": 1,
        "page_size": 10,
        "has_more": True,
    }
    assert [item["id"] for item in first.structured_content["items"]] == [
        element["id"] for element in JOURNAL_ELEMENTS[:10]
    ]
    assert any("unpaginated" in note for note in first.structured_content["notes"])

    last = await mcp_client.call_tool(
        "list_work_package_comments", {"id": WORK_PACKAGE_ID, "page": 3, "page_size": 10}
    )
    assert last.structured_content is not None
    assert last.structured_content["pagination"] == {
        "total": 23,
        "page": 3,
        "page_size": 10,
        "has_more": False,
    }
    assert len(last.structured_content["items"]) == 3
    assert [item["internal"] for item in last.structured_content["items"]] == [False, True, False]

    past_the_end = await mcp_client.call_tool(
        "list_work_package_comments", {"id": WORK_PACKAGE_ID, "page": 9, "page_size": 10}
    )
    assert past_the_end.structured_content is not None
    assert past_the_end.structured_content["items"] == []
    assert past_the_end.structured_content["pagination"]["has_more"] is False
    assert past_the_end.structured_content["pagination"]["total"] == 23

    # No pagination is sent upstream: the endpoint does not support it.
    for call in route.calls:
        assert "offset" not in call.request.url.params
        assert "pageSize" not in call.request.url.params


async def test_page_size_out_of_range_is_rejected_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_JOURNAL)
    )
    result = await mcp_client.call_tool(
        "list_work_package_comments",
        {"id": WORK_PACKAGE_ID, "page_size": 500},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "page_size" in error["message"]
    assert route.call_count == 0


# --- listing: projection, truncation, details -----------------------------


async def test_comment_and_field_change_entries_are_projected(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(
            200, json=activity_collection([FIELD_CHANGE_ACTIVITY, *JOURNAL_ELEMENTS[1:3]])
        )
    )
    result = await mcp_client.call_tool(
        "list_work_package_comments", {"id": WORK_PACKAGE_ID, "page_size": 2}
    )
    assert result.structured_content is not None
    changes, comment = result.structured_content["items"]

    assert changes["kind"] == "field_change"
    assert changes["comment"] is None
    assert changes["author"] == {"id": 12, "name": "Grace Hopper"}
    assert changes["work_package"] == {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"}
    assert changes["version"] == 2
    assert changes["created_at"] == "2026-07-02T09:15:00Z"
    assert changes["internal"] is False

    assert comment["kind"] == "comment"
    assert comment["comment"] == "Retries are in. Review when you get a chance."
    assert comment["truncated"] is False
    assert comment["comment_length"] is None
    assert comment["details"][0] == {
        "field": "Progress (%)",
        "from": "20",
        "to": "40",
        "text": "Progress (%) changed from 20 to 40",
    }


async def test_details_are_parsed_into_field_from_to(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(200, json=activity_collection([FIELD_CHANGE_ACTIVITY]))
    )
    result = await mcp_client.call_tool("list_work_package_comments", {"id": WORK_PACKAGE_ID})
    assert result.structured_content is not None
    details = result.structured_content["items"][0]["details"]

    assert details[0] == {
        "field": "Status",
        "from": "New",
        "to": "In progress",
        "text": "Status changed from New to In progress",
    }
    assert details[1] == {
        "field": "Start date",
        "from": None,
        "to": "2026-07-01",
        "text": "Start date set to 2026-07-01",
    }
    assert details[2] == {
        "field": "Assignee",
        "from": "Grace Hopper",
        "to": None,
        "text": "Assignee deleted (Grace Hopper)",
    }
    assert details[3] == {
        "field": "Description",
        "from": None,
        "to": None,
        "text": "Description updated",
    }

    # The rendered markup, not the plain sentence, decides where a value that
    # contains " to " is split.
    assert details[4]["from"] == "Ship to prod"
    assert details[4]["to"] == "Ship to staging"


async def test_unparseable_detail_keeps_its_text(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    activity = {
        **FIELD_CHANGE_ACTIVITY,
        "details": [{"format": "custom", "raw": "Journal entry 3 was aggregated", "html": ""}],
    }
    mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(200, json=activity_collection([activity]))
    )
    result = await mcp_client.call_tool("list_work_package_comments", {"id": WORK_PACKAGE_ID})
    assert result.structured_content is not None
    assert result.structured_content["items"][0]["details"] == [
        {"field": None, "from": None, "to": None, "text": "Journal entry 3 was aggregated"}
    ]


async def test_long_comments_are_truncated_with_a_marker(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(200, json=activity_collection([LONG_COMMENT_ACTIVITY]))
    )
    result = await mcp_client.call_tool(
        "list_work_package_comments", {"id": WORK_PACKAGE_ID, "max_comment_chars": 200}
    )
    assert result.structured_content is not None
    entry = result.structured_content["items"][0]

    assert entry["truncated"] is True
    assert entry["comment_length"] == len(LONG_COMMENT_TEXT)
    assert entry["comment"] == LONG_COMMENT_TEXT[:200]
    assert any("activity_id" in note for note in result.structured_content["notes"])
    assert any(str(entry["id"]) in note for note in result.structured_content["notes"])


async def test_activity_id_returns_one_entry_uncapped(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    journal = mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_JOURNAL)
    )
    mock_api.get("activities/599").mock(
        return_value=httpx.Response(200, json=LONG_COMMENT_ACTIVITY)
    )

    result = await mcp_client.call_tool(
        "list_work_package_comments",
        {"id": WORK_PACKAGE_ID, "activity_id": 599, "max_comment_chars": 200},
    )
    assert result.structured_content is not None
    assert result.structured_content["pagination"] == {
        "total": 1,
        "page": 1,
        "page_size": 1,
        "has_more": False,
    }
    entry = result.structured_content["items"][0]
    assert entry["truncated"] is False
    assert entry["comment"] == LONG_COMMENT_TEXT
    assert journal.call_count == 0


async def test_activity_from_another_work_package_is_refused(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("activities/777").mock(return_value=httpx.Response(200, json=FOREIGN_ACTIVITY))
    result = await mcp_client.call_tool(
        "list_work_package_comments",
        {"id": WORK_PACKAGE_ID, "activity_id": 777},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "4321" in error["message"]
    assert "list_work_package_comments(id=4321" in error["hint"]


async def test_unknown_work_package_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(404, json=WORK_PACKAGE_NOT_FOUND)
    )
    result = await mcp_client.call_tool(
        "list_work_package_comments", {"id": WORK_PACKAGE_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "ids come from" in error["hint"]


# --- writing --------------------------------------------------------------


async def test_comment_is_posted_with_the_formattable_body(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_COMMENT_ACTIVITY)
    )
    result = await mcp_client.call_tool(
        "add_work_package_comment",
        {"id": WORK_PACKAGE_ID, "comment": "Deployed to staging."},
    )
    assert result.structured_content is not None
    assert result.structured_content["id"] == 610
    assert result.structured_content["kind"] == "comment"
    assert result.structured_content["author"] == {"id": 1, "name": "Ada Lovelace"}
    assert result.structured_content["internal"] is False

    request = route.calls[0].request
    assert json.loads(request.content) == {
        "comment": {"format": "markdown", "raw": "Deployed to staging."}
    }
    assert request.url.params["notify"] == "true"


async def test_notify_false_is_sent_as_a_query_parameter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_COMMENT_ACTIVITY)
    )
    await mcp_client.call_tool(
        "add_work_package_comment",
        {"id": WORK_PACKAGE_ID, "comment": "Bookkeeping.", "notify": False},
    )
    assert route.calls[0].request.url.params["notify"] == "false"


async def test_blank_comment_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_COMMENT_ACTIVITY)
    )
    result = await mcp_client.call_tool(
        "add_work_package_comment",
        {"id": WORK_PACKAGE_ID, "comment": "   "},
        raise_on_error=False,
    )
    assert error_of(result)["type"] == "invalid_input"
    assert route.call_count == 0


async def test_validation_failure_carries_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(422, json=COMMENT_VALIDATION_ERROR)
    )
    result = await mcp_client.call_tool(
        "add_work_package_comment",
        {"id": WORK_PACKAGE_ID, "comment": "x" * 10},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "comment", "message": "Comment is too long (maximum is 65536 characters)."}
    ]
    assert "violations" in error["hint"]


async def test_conflict_is_reported_as_a_conflict(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(409, json=COMMENT_CONFLICT_ERROR)
    )
    result = await mcp_client.call_tool(
        "add_work_package_comment",
        {"id": WORK_PACKAGE_ID, "comment": "Racing another editor."},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert "updated" in error["message"]
    assert error["hint"]


# --- the internal-comment version gate (SPEC §4.7, G2) --------------------


@pytest.mark.parametrize("core_version", ["16.0.0", "17.7.1"])
async def test_internal_comment_is_posted_on_openproject_16_and_newer(
    core_version: str, mock_api: respx.MockRouter
) -> None:
    server = server_for(core_version, mock_api)
    route = mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_INTERNAL_COMMENT_ACTIVITY)
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "add_work_package_comment",
            {
                "id": WORK_PACKAGE_ID,
                "comment": "Internal: escalate to the account manager.",
                "internal": True,
            },
        )

    assert result.structured_content is not None
    assert result.structured_content["internal"] is True
    assert json.loads(route.calls[0].request.content)["internal"] is True


@pytest.mark.parametrize("core_version", ["14.6.1", "15.4.0"])
async def test_internal_comment_hard_errors_on_older_openproject(
    core_version: str, mock_api: respx.MockRouter
) -> None:
    server = server_for(core_version, mock_api)
    route = mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_INTERNAL_COMMENT_ACTIVITY)
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "add_work_package_comment",
            {"id": WORK_PACKAGE_ID, "comment": "Should not be published.", "internal": True},
            raise_on_error=False,
        )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert core_version in error["message"]
    assert "16.0" in error["message"]
    assert "publish the comment anyway" in error["hint"]
    assert route.call_count == 0, "the comment must not be posted publicly"


async def test_public_comment_does_not_pay_for_the_probe(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    root = mock_api.get(url=f"{API_BASE}/").mock(return_value=httpx.Response(200, json=API_ROOT))
    route = mock_api.post(ACTIVITIES_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_COMMENT_ACTIVITY)
    )
    await mcp_client.call_tool(
        "add_work_package_comment", {"id": WORK_PACKAGE_ID, "comment": "No probe needed."}
    )
    assert root.call_count == 0
    assert "internal" not in json.loads(route.calls[0].request.content)
