"""Protocol tests for the time-tracking tools (SPEC §6.9, §4.7, §9.3, G1/G2).

Everything runs through the in-memory FastMCP client against a respx-mocked
instance. The interesting parts: the version-probed work-package filter name
(both spellings), ISO-duration conversion in both directions, the capped summing
path, the form-driven activity resolution, the lock-version decision on update,
and the structured error envelopes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client, FastMCP

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import API_BASE, TEST_URL
from tests.fixtures.hal_payloads import API_ROOT
from tests.fixtures.notifications_time_payloads import (
    CREATED_TIME_ENTRY,
    INVALID_FILTER_ERROR,
    PROJECT_ID,
    TIME_ENTRY_CONFLICT,
    TIME_ENTRY_FORM,
    TIME_ENTRY_FORM_DEVELOPMENT,
    TIME_ENTRY_FORM_INVALID,
    TIME_ENTRY_ID,
    TIME_ENTRY_NOT_FOUND,
    TIME_ENTRY_PAGE,
    TIME_ENTRY_UPDATED,
    TIME_ENTRY_VALIDATION_ERROR,
    TIME_ENTRY_WITH_LOCK,
    WORK_PACKAGE_ID,
    time_entry,
    time_entry_collection,
)

ENTRY_PATH = f"time_entries/{TIME_ENTRY_ID}"

#: The exact filter the §4.7 probe sends, so its route never swallows a real one.
PROBE_FILTERS = json.dumps(
    [{"entityId": {"operator": "=", "values": ["1"]}}], separators=(",", ":")
)


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def filters_of(request: httpx.Request) -> list[dict[str, Any]]:
    return json.loads(request.url.params["filters"])


def route_probe(mock_api: respx.MockRouter, *, legacy: bool = False) -> None:
    """Route the two calls ``ctx.probe()`` makes (SPEC §4.7).

    Registered before any other ``time_entries`` route so respx resolves the
    probe's own request here and everything else to the test's own route. The
    API root has to be routed by full URL: with a router base_url, a relative
    pattern of ``""`` matches every path.
    """
    mock_api.get(url=f"{API_BASE}/").mock(return_value=httpx.Response(200, json=API_ROOT))
    probe = mock_api.get("time_entries", params={"filters": PROBE_FILTERS})
    if legacy:
        probe.mock(return_value=httpx.Response(400, json=INVALID_FILTER_ERROR))
    else:
        probe.mock(
            return_value=httpx.Response(200, json=time_entry_collection([], total=0, page_size=1))
        )


def fresh_server() -> FastMCP:
    """A server with its own lifespan cache, so a probe result is not reused."""
    return build_server(
        Settings(_env_file=None, url=TEST_URL, api_key="test-token")  # type: ignore[call-arg]
    )


def paged_entries(total: int, *, page_size: int = 100) -> Any:
    """A respx side effect that serves ``total`` entries in pages of 100.

    Half the entries are booked on Development and half on Management so the
    per-activity subtotals have something to get wrong.
    """

    def responder(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        start = (offset - 1) * page_size
        window = [
            time_entry(
                2000 + index,
                hours="PT1H",
                activity_id=3 if index % 2 == 0 else 1,
                activity_name="Development" if index % 2 == 0 else "Management",
            )
            for index in range(start, min(start + page_size, total))
        ]
        return httpx.Response(
            200,
            json=time_entry_collection(window, total=total, offset=offset, page_size=page_size),
        )

    return responder


# --- registration ---------------------------------------------------------


async def test_the_four_time_tools_are_registered(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_time_entries"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert set(listing.inputSchema["properties"]) == {
        "work_package_id",
        "project_id",
        "user",
        "from_date",
        "to_date",
        "activity_id",
        "sum_hours",
        "page",
        "page_size",
    }

    logging_tool = tools["log_time"]
    assert logging_tool.annotations is not None
    assert logging_tool.annotations.readOnlyHint is False
    assert logging_tool.annotations.idempotentHint is False
    assert set(logging_tool.inputSchema["required"]) == {"hours", "spent_on"}

    updating = tools["update_time_entry"]
    assert updating.annotations is not None
    assert updating.annotations.destructiveHint is False

    deleting = tools["delete_time_entry"]
    assert deleting.annotations is not None
    assert deleting.annotations.destructiveHint is True
    assert deleting.annotations.model_extra is not None
    assert deleting.annotations.model_extra["anthropic/requiresUserInteraction"] is True
    assert set(deleting.inputSchema["properties"]) == {"time_entry_id", "confirm"}


async def test_descriptions_carry_the_pitfalls_and_cross_references(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_time_entries"].description or ""
    assert "log_time" in listing
    assert "child" in listing
    assert "2000 entries" in listing

    logging_description = tools["log_time"].description or ""
    assert "not** idempotent" in logging_description
    # Derived-progress instances make percentageDone read-only: no promises.
    assert "estimate or progress" in logging_description
    assert "percentage is read-only" in logging_description
    assert "get_project_metadata" in logging_description

    assert "update_time_entry" in (tools["delete_time_entry"].description or "")


# --- listing: the probed work-package filter (SPEC §4.7) ------------------


async def test_work_package_scoping_uses_entity_id_on_current_instances(
    mock_api: respx.MockRouter,
) -> None:
    route_probe(mock_api)
    route = mock_api.get("time_entries").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_PAGE)
    )

    async with Client(fresh_server()) as client:
        result = await client.call_tool("list_time_entries", {"work_package_id": WORK_PACKAGE_ID})

    assert filters_of(route.calls[0].request) == [
        {"entityId": {"operator": "=", "values": ["1234"]}},
        {"entityType": {"operator": "=", "values": ["WorkPackage"]}},
    ]
    assert result.structured_content is not None
    assert result.structured_content.get("notes") is None


async def test_work_package_scoping_falls_back_to_the_legacy_filter(
    mock_api: respx.MockRouter,
) -> None:
    route_probe(mock_api, legacy=True)
    route = mock_api.get("time_entries").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_PAGE)
    )

    async with Client(fresh_server()) as client:
        result = await client.call_tool("list_time_entries", {"work_package_id": WORK_PACKAGE_ID})

    assert filters_of(route.calls[0].request) == [
        {"workPackage": {"operator": "=", "values": ["1234"]}}
    ]
    assert result.structured_content is not None
    assert any("workPackage" in note for note in result.structured_content["notes"])


# --- listing: filters, projection, envelope -------------------------------


async def test_project_user_date_and_activity_filters_are_combined(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("time_entries").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_PAGE)
    )
    await mcp_client.call_tool(
        "list_time_entries",
        {
            "project_id": PROJECT_ID,
            "user": "me",
            "from_date": "2026-07-01",
            "to_date": "2026-07-31",
            "activity_id": 3,
        },
    )
    assert filters_of(route.calls[0].request) == [
        {"project": {"operator": "=", "values": ["7"]}},
        {"user": {"operator": "=", "values": ["me"]}},
        {"spentOn": {"operator": "<>d", "values": ["2026-07-01", "2026-07-31"]}},
        {"activity": {"operator": "=", "values": ["3"]}},
    ]


async def test_an_open_ended_date_range_leaves_the_other_bound_empty(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("time_entries").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_PAGE)
    )
    await mcp_client.call_tool("list_time_entries", {"from_date": "2026-07-01"})
    assert filters_of(route.calls[0].request) == [
        {"spentOn": {"operator": "<>d", "values": ["2026-07-01", ""]}}
    ]


async def test_a_malformed_date_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("time_entries").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_PAGE)
    )
    result = await mcp_client.call_tool(
        "list_time_entries", {"from_date": "01/07/2026"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "YYYY-MM-DD" in error["hint"]
    assert route.call_count == 0


async def test_rows_report_float_hours_and_compact_refs(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json=TIME_ENTRY_PAGE))
    result = await mcp_client.call_tool("list_time_entries", {})
    assert result.structured_content is not None
    long_entry, short_entry, project_entry = result.structured_content["items"]

    assert long_entry == {
        "id": TIME_ENTRY_ID,
        "hours": 7.5,
        "spent_on": "2026-07-20",
        "comment": "Client layer work.",
        "user": {"id": 1, "name": "Ada Lovelace"},
        "activity": {"id": 3, "name": "Development"},
        "work_package": {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"},
        "project": {"id": PROJECT_ID, "name": "Client layer"},
    }
    assert short_entry["hours"] == 1.25
    # Project-level entries are supported and simply have no work package.
    assert project_entry["work_package"] is None
    assert project_entry["hours"] == 2.0

    assert result.structured_content["pagination"] == {
        "total": 3,
        "page": 1,
        "page_size": 20,
        "has_more": False,
    }


async def test_unknown_work_package_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("time_entries").mock(return_value=httpx.Response(404, json=TIME_ENTRY_NOT_FOUND))
    result = await mcp_client.call_tool(
        "list_time_entries", {"project_id": 999}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "ids come from" in error["hint"]


# --- listing: the summing path (G1) ---------------------------------------


async def test_sum_hours_pages_through_everything_and_groups_by_activity(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("time_entries").mock(side_effect=paged_entries(230))

    result = await mcp_client.call_tool(
        "list_time_entries", {"project_id": PROJECT_ID, "sum_hours": True}
    )

    assert route.call_count == 3
    assert [call.request.url.params["pageSize"] for call in route.calls] == ["100", "100", "100"]
    assert [call.request.url.params["offset"] for call in route.calls] == ["1", "2", "3"]

    assert result.structured_content is not None
    assert result.structured_content["sums"] == {"total_hours": 230.0}
    groups = {group["value"]: group for group in result.structured_content["groups"]}
    assert groups["Development"]["count"] == 115
    assert groups["Development"]["sums"] == {"total_hours": 115.0}
    assert groups["Management"]["count"] == 115

    # 'items' is still one page; the totals cover everything.
    assert len(result.structured_content["items"]) == 20
    assert result.structured_content["pagination"] == {
        "total": 230,
        "page": 1,
        "page_size": 20,
        "has_more": True,
    }
    assert any("not just this page" in note for note in result.structured_content["notes"])


async def test_sum_hours_reports_the_cap_instead_of_lying(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("time_entries").mock(side_effect=paged_entries(2500))

    result = await mcp_client.call_tool(
        "list_time_entries", {"project_id": PROJECT_ID, "sum_hours": True, "page_size": 5}
    )

    assert route.call_count == 20, "stops at the 2000-entry cap"
    assert result.structured_content is not None
    assert result.structured_content["sums"] == {"total_hours": 2000.0}
    assert result.structured_content["pagination"]["total"] == 2500
    assert any("first 2000 of 2500" in note for note in result.structured_content["notes"]), (
        result.structured_content["notes"]
    )


async def test_sum_hours_on_an_empty_match_set_is_zero_not_an_error(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("time_entries").mock(
        return_value=httpx.Response(200, json=time_entry_collection([], total=0, page_size=100))
    )
    result = await mcp_client.call_tool("list_time_entries", {"sum_hours": True})

    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["sums"] == {"total_hours": 0.0}
    assert result.structured_content["items"] == []


# --- logging time ---------------------------------------------------------


async def test_log_time_needs_a_work_package_or_a_project(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("time_entries/form").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_FORM)
    )
    result = await mcp_client.call_tool(
        "log_time", {"hours": 1.5, "spent_on": "2026-07-21"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "work_package_id" in error["message"]
    assert form.call_count == 0


async def test_hours_are_converted_to_an_iso_duration(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("time_entries/form").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_FORM_DEVELOPMENT)
    )
    create = mock_api.post("time_entries").mock(
        return_value=httpx.Response(201, json=CREATED_TIME_ENTRY)
    )

    result = await mcp_client.call_tool(
        "log_time",
        {
            "hours": 1.5,
            "spent_on": "2026-07-21",
            "work_package_id": WORK_PACKAGE_ID,
            "activity": 3,
            "comment": "Wrote the retry policy.",
        },
    )

    assert form.call_count == 1, "a numeric activity needs no name resolution round trip"
    body = json.loads(create.calls[0].request.content)
    assert body["hours"] == "PT1H30M"
    assert body["spentOn"] == "2026-07-21"
    assert body["comment"] == {"raw": "Wrote the retry policy."}
    assert body["_links"]["workPackage"] == {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"}
    assert body["_links"]["activity"] == {"href": "/api/v3/time_entries/activities/3"}
    # The form's instance defaults survive the merge.
    assert body["ongoing"] is False

    assert result.structured_content is not None
    assert result.structured_content["id"] == 91
    assert result.structured_content["hours"] == 1.5
    assert result.structured_content["activity"] == {"id": 3, "name": "Development"}


async def test_a_quarter_hour_becomes_minutes_only(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("time_entries/form").mock(return_value=httpx.Response(200, json=TIME_ENTRY_FORM))
    create = mock_api.post("time_entries").mock(
        return_value=httpx.Response(201, json=CREATED_TIME_ENTRY)
    )
    await mcp_client.call_tool(
        "log_time", {"hours": 0.25, "spent_on": "2026-07-21", "project_id": PROJECT_ID}
    )
    assert json.loads(create.calls[0].request.content)["hours"] == "PT15M"


async def test_an_activity_name_is_resolved_against_the_form(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("time_entries/form").mock(
        side_effect=[
            httpx.Response(200, json=TIME_ENTRY_FORM),
            httpx.Response(200, json=TIME_ENTRY_FORM_DEVELOPMENT),
        ]
    )
    create = mock_api.post("time_entries").mock(
        return_value=httpx.Response(201, json=CREATED_TIME_ENTRY)
    )

    await mcp_client.call_tool(
        "log_time",
        {
            "hours": 2,
            "spent_on": "2026-07-21",
            "work_package_id": WORK_PACKAGE_ID,
            "activity": "development",
        },
    )

    assert form.call_count == 2
    revalidated = json.loads(form.calls[1].request.content)
    assert revalidated["_links"]["activity"] == {"href": "/api/v3/time_entries/activities/3"}
    assert json.loads(create.calls[0].request.content)["_links"]["activity"] == {
        "href": "/api/v3/time_entries/activities/3"
    }


async def test_an_unknown_activity_lists_the_valid_ones_and_writes_nothing(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("time_entries/form").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_FORM)
    )
    create = mock_api.post("time_entries").mock(
        return_value=httpx.Response(201, json=CREATED_TIME_ENTRY)
    )

    result = await mcp_client.call_tool(
        "log_time",
        {
            "hours": 1,
            "spent_on": "2026-07-21",
            "work_package_id": WORK_PACKAGE_ID,
            "activity": "Coding",
        },
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "Development (3)" in error["hint"]
    assert "get_project_metadata" in error["hint"]
    assert form.call_count == 1
    assert create.call_count == 0


async def test_form_validation_errors_become_a_typed_422(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("time_entries/form").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_FORM_INVALID)
    )
    create = mock_api.post("time_entries").mock(
        return_value=httpx.Response(201, json=CREATED_TIME_ENTRY)
    )

    result = await mcp_client.call_tool(
        "log_time",
        {"hours": 1, "spent_on": "2026-01-05", "project_id": PROJECT_ID},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "spentOn", "message": "Date is locked and cannot be edited."}
    ]
    assert error["hint"]
    assert create.call_count == 0, "the form rejected it, so nothing was created"


async def test_zero_hours_is_refused_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("time_entries/form").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_FORM)
    )
    result = await mcp_client.call_tool(
        "log_time",
        {"hours": 0, "spent_on": "2026-07-21", "project_id": PROJECT_ID},
        raise_on_error=False,
    )
    assert error_of(result)["type"] == "invalid_input"
    assert form.call_count == 0


# --- updating -------------------------------------------------------------


async def test_update_echoes_the_lock_version_when_the_instance_reports_one(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(ENTRY_PATH).mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_WITH_LOCK)
    )
    patch = mock_api.patch(ENTRY_PATH).mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_UPDATED)
    )

    result = await mcp_client.call_tool(
        "update_time_entry",
        {"time_entry_id": TIME_ENTRY_ID, "hours": 2, "comment": "Corrected."},
    )

    assert read.call_count == 1
    body = json.loads(patch.calls[0].request.content)
    assert body["lockVersion"] == 3
    assert body["hours"] == "PT2H"
    assert body["comment"] == {"raw": "Corrected."}
    assert result.structured_content is not None
    assert result.structured_content["hours"] == 2.0
    assert result.structured_content["lock_version"] == 4


async def test_update_omits_the_lock_version_when_the_resource_has_none(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    unversioned = time_entry(TIME_ENTRY_ID, hours="PT7H30M")
    mock_api.get(ENTRY_PATH).mock(return_value=httpx.Response(200, json=unversioned))
    patch = mock_api.patch(ENTRY_PATH).mock(
        return_value=httpx.Response(200, json=time_entry(TIME_ENTRY_ID, spent_on="2026-07-22"))
    )

    result = await mcp_client.call_tool(
        "update_time_entry", {"time_entry_id": TIME_ENTRY_ID, "spent_on": "2026-07-22"}
    )

    body = json.loads(patch.calls[0].request.content)
    assert "lockVersion" not in body
    assert body == {"spentOn": "2026-07-22"}
    assert result.structured_content is not None
    assert result.structured_content["spent_on"] == "2026-07-22"
    assert result.structured_content["lock_version"] is None


async def test_update_resolves_an_activity_name_through_the_entry_form(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ENTRY_PATH).mock(return_value=httpx.Response(200, json=TIME_ENTRY_WITH_LOCK))
    form = mock_api.post(f"{ENTRY_PATH}/form").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_FORM)
    )
    patch = mock_api.patch(ENTRY_PATH).mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_UPDATED)
    )

    await mcp_client.call_tool(
        "update_time_entry", {"time_entry_id": TIME_ENTRY_ID, "activity": "Specification"}
    )

    assert form.call_count == 1
    body = json.loads(patch.calls[0].request.content)
    assert body["_links"]["activity"] == {"href": "/api/v3/time_entries/activities/4"}


async def test_update_with_nothing_to_change_is_refused_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(ENTRY_PATH).mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_WITH_LOCK)
    )
    result = await mcp_client.call_tool(
        "update_time_entry", {"time_entry_id": TIME_ENTRY_ID}, raise_on_error=False
    )
    assert error_of(result)["type"] == "invalid_input"
    assert read.call_count == 0


async def test_a_concurrent_edit_comes_back_as_a_conflict_with_fresh_state(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ENTRY_PATH).mock(
        side_effect=[
            httpx.Response(200, json=TIME_ENTRY_WITH_LOCK),
            httpx.Response(200, json=TIME_ENTRY_UPDATED),
        ]
    )
    mock_api.patch(ENTRY_PATH).mock(return_value=httpx.Response(409, json=TIME_ENTRY_CONFLICT))

    result = await mcp_client.call_tool(
        "update_time_entry", {"time_entry_id": TIME_ENTRY_ID, "hours": 3}, raise_on_error=False
    )

    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert error["lock_version"] == 4, "the fresh version to retry with"
    assert error["conflicting_fields"]["hours"] == {"attempted": "PT3H", "current": "PT2H"}
    assert error["current"]["updatedAt"] == "2026-07-20T17:00:00Z"


async def test_a_rejected_update_carries_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ENTRY_PATH).mock(return_value=httpx.Response(200, json=TIME_ENTRY_WITH_LOCK))
    mock_api.patch(ENTRY_PATH).mock(
        return_value=httpx.Response(422, json=TIME_ENTRY_VALIDATION_ERROR)
    )

    result = await mcp_client.call_tool(
        "update_time_entry", {"time_entry_id": TIME_ENTRY_ID, "hours": 0.02}, raise_on_error=False
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "hours", "message": "Hours must be greater than 0."}
    ]
    assert "violations" in error["hint"]


# --- deleting -------------------------------------------------------------


async def test_delete_without_confirmation_deletes_nothing(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(ENTRY_PATH).mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool(
        "delete_time_entry", {"time_entry_id": TIME_ENTRY_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert route.call_count == 0


async def test_confirmed_delete_removes_the_entry(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(ENTRY_PATH).mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool(
        "delete_time_entry", {"time_entry_id": TIME_ENTRY_ID, "confirm": True}
    )
    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content == {
        "id": TIME_ENTRY_ID,
        "deleted": True,
        "message": f"Time entry #{TIME_ENTRY_ID} was deleted permanently.",
    }


async def test_deleting_an_unknown_entry_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete("time_entries/999999").mock(
        return_value=httpx.Response(404, json=TIME_ENTRY_NOT_FOUND)
    )
    result = await mcp_client.call_tool(
        "delete_time_entry", {"time_entry_id": 999999, "confirm": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
