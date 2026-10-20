"""Protocol tests for ``get_project_report_data`` (SPEC §6.14, §9.3).

Everything goes through the in-memory FastMCP client against a respx-mocked
instance: the wire filters each report window sends, the server-side
``groupBy=status`` breakdown, the ``isClosed``-driven bucketing (proved against
closed statuses named 'Shipped' and 'Rejected', which no keyword classifier
would catch), the internal caps turning into in-band notes, the degradation of
an unreadable roster and of unreadable time entries, and the error envelopes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from openproject_mcp.tools import reporting
from tests.fixtures.reporting_payloads import (
    FORBIDDEN_ERROR,
    FROM_DATE,
    MEMBERSHIP_COLLECTION,
    NOT_FOUND_ERROR,
    PROJECT,
    PROJECT_ID,
    STATUS_COLLECTION,
    TIME_ENTRY_COLLECTION,
    TO_DATE,
    hal_collection,
    time_entry_element,
    work_package_element,
    work_package_response,
)

WP_PATH = f"projects/{PROJECT_ID}/work_packages"
ARGS = {"project_id": PROJECT_ID, "from_date": FROM_DATE, "to_date": TO_DATE}


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def filters_of(request: httpx.Request) -> list[dict[str, Any]]:
    return json.loads(request.url.params.get("filters", "[]"))


def mock_report_api(mock_api: respx.MockRouter) -> dict[str, respx.Route]:
    """Route every endpoint one full report window touches."""
    return {
        "project": mock_api.get(f"projects/{PROJECT_ID}").mock(
            return_value=httpx.Response(200, json=PROJECT)
        ),
        "statuses": mock_api.get("statuses").mock(
            return_value=httpx.Response(200, json=STATUS_COLLECTION)
        ),
        "work_packages": mock_api.get(WP_PATH).mock(side_effect=work_package_response),
        "time_entries": mock_api.get("time_entries").mock(
            return_value=httpx.Response(200, json=TIME_ENTRY_COLLECTION)
        ),
        "memberships": mock_api.get("memberships").mock(
            return_value=httpx.Response(200, json=MEMBERSHIP_COLLECTION)
        ),
    }


# --- registration ---------------------------------------------------------


async def test_the_tool_is_registered_with_honest_annotations(mcp_client: Client[Any]) -> None:
    tool = next(t for t in await mcp_client.list_tools() if t.name == "get_project_report_data")

    assert tool.outputSchema is not None
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.model_extra is not None
    assert tool.annotations.model_extra["anthropic/maxResultSizeChars"] == 150_000
    assert set(tool.inputSchema["properties"]) == {
        "project_id",
        "from_date",
        "to_date",
        "sections",
        "sample_size",
    }
    assert set(tool.inputSchema["required"]) == {"project_id", "from_date", "to_date"}

    description = tool.description or ""
    assert "isClosed" in description
    assert "list_work_packages" in description
    assert "weekly_report" in description


# --- the aggregation ------------------------------------------------------


async def test_report_data_aggregates_the_whole_window(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_report_api(mock_api)

    result = await mcp_client.call_tool("get_project_report_data", ARGS)
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["project"] == {"id": PROJECT_ID, "name": "Platform"}
    assert payload["from_date"] == FROM_DATE
    assert payload["to_date"] == TO_DATE

    # Counts are the server's totals; rows are the compact projection.
    assert payload["sample_size"] == 20
    assert payload["created"]["state"] == "complete"
    assert payload["created"]["more"] is None
    assert payload["created"]["total"] == 2
    assert payload["created"]["truncated"] is False
    assert [row["id"] for row in payload["created"]["items"]] == [1235, 1236]
    assert payload["created"]["items"][0] == {
        "id": 1235,
        "subject": "Drop sentinel dates",
        "type": {"id": 2, "name": "Bug"},
        "status": {"id": 1, "name": "New"},
        "assignee": None,
        "is_closed": False,
        "due_date": None,
        "created_at": "2026-07-02T07:30:00Z",
        "updated_at": "2026-07-02T07:30:00Z",
    }

    # A row created inside the window is changed inside it too, so the changed
    # window is a superset of the created one.
    assert payload["updated"]["total"] == 4
    assert [row["id"] for row in payload["updated"]["items"]] == [1234, 1236, 1237, 1235]

    # 'Shipped' and 'Rejected' are closed because the API says isClosed, not
    # because their names contain a keyword — they contain none.
    assert payload["closed_updated"]["total"] == 2
    closed_rows = [
        (row["id"], row["status"]["name"], row["is_closed"])
        for row in payload["closed_updated"]["items"]
    ]
    assert closed_rows == [(1236, "Shipped", True), (1237, "Rejected", True)]

    # Open counts come from the server-side groupBy, not from summed pages.
    assert payload["open"]["state"] == "complete"
    assert payload["open"]["total"] == 13
    assert payload["open"]["by_status"] == [
        {"status": {"id": 7, "name": "In progress"}, "count": 4, "is_closed": False},
        {"status": {"id": None, "name": "New"}, "count": 9, "is_closed": False},
    ]

    assert payload["time"]["state"] == "complete"
    assert payload["time"]["total_hours"] == 7.5
    assert payload["time"]["entry_count"] == 3
    assert payload["time"]["total_entries"] == 3
    assert payload["time"]["truncated"] is False
    assert payload["time"]["by_activity"] == [
        {"activity": {"id": 3, "name": "Development"}, "hours": 6.0, "entries": 2},
        {"activity": {"id": 4, "name": "Management"}, "hours": 1.5, "entries": 1},
    ]
    assert payload["time"]["by_user"] == [
        {"user": {"id": 12, "name": "Grace Hopper"}, "hours": 4.0, "entries": 1},
        {"user": {"id": 1, "name": "Ada Lovelace"}, "hours": 3.5, "entries": 2},
    ]

    assert payload["roster"]["state"] == "complete"
    assert payload["roster"]["total"] == 2
    assert payload["roster"]["members"] == [
        {"principal": {"id": 12, "name": "Grace Hopper"}, "roles": ["Member"]},
        {"principal": {"id": 1, "name": "Ada Lovelace"}, "roles": ["Project admin", "Member"]},
    ]

    assert any("isClosed flag" in note for note in payload["notes"])
    assert not any("cap" in note for note in payload["notes"])


async def test_each_window_sends_the_filters_the_spec_requires(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    await mcp_client.call_tool("get_project_report_data", ARGS)

    wp_calls = routes["work_packages"].calls
    created, updated, closed, grouped = (call.request for call in wp_calls)

    assert filters_of(created) == [
        {"status": {"operator": "*", "values": []}},
        {"createdAt": {"operator": "<>d", "values": [FROM_DATE, TO_DATE]}},
    ]
    assert filters_of(updated) == [
        {"status": {"operator": "*", "values": []}},
        {"updatedAt": {"operator": "<>d", "values": [FROM_DATE, TO_DATE]}},
    ]
    # 'closed in window' is the API's own closed operator plus the window.
    assert filters_of(closed) == [
        {"status": {"operator": "c", "values": []}},
        {"updatedAt": {"operator": "<>d", "values": [FROM_DATE, TO_DATE]}},
    ]
    assert filters_of(grouped) == [{"status": {"operator": "o", "values": []}}]
    assert grouped.url.params["groupBy"] == "status"
    # One row is enough: the counts are computed server-side over the full set.
    assert grouped.url.params["pageSize"] == "1"

    time_filters = filters_of(routes["time_entries"].calls[0].request)
    assert time_filters == [
        {"project": {"operator": "=", "values": [str(PROJECT_ID)]}},
        {"spentOn": {"operator": "<>d", "values": [FROM_DATE, TO_DATE]}},
    ]
    assert filters_of(routes["memberships"].calls[0].request) == [
        {"project": {"operator": "=", "values": [str(PROJECT_ID)]}}
    ]


async def test_a_project_identifier_is_accepted_in_the_path(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/platform").mock(return_value=httpx.Response(200, json=PROJECT))
    mock_api.get("statuses").mock(return_value=httpx.Response(200, json=STATUS_COLLECTION))
    route = mock_api.get("projects/platform/work_packages").mock(side_effect=work_package_response)
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json=TIME_ENTRY_COLLECTION))
    mock_api.get("memberships").mock(return_value=httpx.Response(200, json=MEMBERSHIP_COLLECTION))

    result = await mcp_client.call_tool(
        "get_project_report_data",
        {"project_id": "platform", "from_date": FROM_DATE, "to_date": TO_DATE},
    )
    assert result.structured_content is not None
    assert route.call_count == 4
    # The numeric id still drives the time-entry and membership filters.
    assert result.structured_content["project"] == {"id": PROJECT_ID, "name": "Platform"}


# --- paging and caps (G1) -------------------------------------------------


async def test_time_entries_are_paged_and_summed_over_every_page(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    pages = {
        "1": hal_collection(
            [
                time_entry_element(
                    901,
                    "PT4H",
                    spent_on="2026-07-02",
                    activity=(3, "Development"),
                    user=(12, "Grace Hopper"),
                )
            ],
            total=2,
            pageSize=1,
            offset=1,
        ),
        "2": hal_collection(
            [
                time_entry_element(
                    902,
                    "PT2H",
                    spent_on="2026-07-03",
                    activity=(3, "Development"),
                    user=(1, "Ada Lovelace"),
                )
            ],
            total=2,
            pageSize=1,
            offset=2,
        ),
    }

    def paged(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params["offset"]])

    mock_report_api(mock_api)
    route = mock_api.get("time_entries").mock(side_effect=paged)

    result = await mcp_client.call_tool("get_project_report_data", ARGS)
    assert result.structured_content is not None

    assert route.call_count == 2
    # OpenProject's offset is a 1-based page number, not a record offset.
    assert [call.request.url.params["offset"] for call in route.calls] == ["1", "2"]
    assert result.structured_content["time"]["total_hours"] == 6.0
    assert result.structured_content["time"]["entry_count"] == 2
    assert result.structured_content["time"]["truncated"] is False


async def test_a_capped_time_scan_reports_the_cap_in_band(
    mcp_client: Client[Any], mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reporting, "TIME_ENTRY_CAP", 2)
    mock_report_api(mock_api)

    result = await mcp_client.call_tool("get_project_report_data", ARGS)
    assert result.structured_content is not None
    summary = result.structured_content["time"]

    assert summary["state"] == "partial"
    assert summary["truncated"] is True
    assert summary["entry_count"] == 2
    # The total the server reported survives the cap; only the rows are cut.
    assert summary["total_entries"] == 3
    assert summary["total_hours"] == 6.0
    assert summary["more"] == {
        "tool": "list_time_entries",
        "arguments": {
            "project_id": PROJECT_ID,
            "from_date": FROM_DATE,
            "to_date": TO_DATE,
            "sum_hours": True,
        },
    }
    note = next(note for note in result.structured_content["notes"] if note.startswith("time"))
    assert "first 2 of 3" in note
    assert "internal cap 2" in note


async def test_a_sampled_bucket_is_partial_with_an_exact_total_and_a_drill_down(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    result = await mcp_client.call_tool("get_project_report_data", {**ARGS, "sample_size": 1})
    assert result.structured_content is not None
    payload = result.structured_content

    updated = payload["updated"]
    assert updated["state"] == "partial"
    assert updated["truncated"] is True
    assert updated["total"] == 4
    assert len(updated["items"]) == 1
    assert updated["more"] == {
        "tool": "list_work_packages",
        "arguments": {
            "project": PROJECT_ID,
            "status_scope": "all",
            "updated_since": FROM_DATE,
            "updated_until": TO_DATE,
            "sort_by": [["updated_at", "desc"], ["id", "desc"]],
            "page": 2,
            "page_size": 1,
        },
    }
    assert any(note.startswith("updated (partial)") for note in payload["notes"])
    # The sample is one request of sample_size rows in the drill-down's order.
    sample_request = routes["work_packages"].calls[1].request
    assert sample_request.url.params["pageSize"] == "1"
    assert sample_request.url.params["sortBy"] == '[["updatedAt","desc"],["id","desc"]]'


async def test_the_drill_down_is_a_valid_list_work_packages_call(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)
    result = await mcp_client.call_tool("get_project_report_data", {**ARGS, "sample_size": 1})
    assert result.structured_content is not None
    more = result.structured_content["created"]["more"]

    listed = await mcp_client.call_tool(more["tool"], more["arguments"])

    assert not listed.is_error
    request = routes["work_packages"].calls.last.request
    assert filters_of(request) == [
        {"status": {"operator": "*", "values": []}},
        {"createdAt": {"operator": "<>d", "values": [FROM_DATE, TO_DATE]}},
    ]
    assert request.url.params["offset"] == "2"
    assert request.url.params["pageSize"] == "1"


async def test_unrequested_sections_cost_no_requests(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    result = await mcp_client.call_tool("get_project_report_data", {**ARGS, "sections": ["time"]})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["time"]["state"] == "complete"
    for name in ("created", "updated", "closed_updated", "open", "roster"):
        assert payload[name]["state"] == "not_requested"
        assert payload[name]["total"] is None
    assert routes["work_packages"].call_count == 0
    assert routes["statuses"].call_count == 0
    assert routes["memberships"].call_count == 0
    assert not any("isClosed" in note for note in payload["notes"])


async def test_the_largest_sample_stays_inside_the_result_budget(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    long_rows = [
        work_package_element(
            n, "x" * 255, status=(7, "In progress"), assignee=(n, f"Person {n:04d}" * 4)
        )
        for n in range(1, 101)
    ]
    members = [
        {
            "_type": "Membership",
            "id": n,
            "_links": {
                "principal": {"href": f"/api/v3/users/{n}", "title": f"Person {n:04d}" * 4},
                "roles": [{"href": "/api/v3/roles/3", "title": "Member"}],
            },
        }
        for n in range(1, 101)
    ]
    mock_report_api(mock_api)
    mock_api.get(WP_PATH).mock(
        side_effect=lambda request: (
            work_package_response(request)
            if "groupBy" in request.url.params
            else httpx.Response(200, json=hal_collection(long_rows, total=5000))
        )
    )
    mock_api.get("memberships").mock(
        return_value=httpx.Response(200, json=hal_collection(members, total=400))
    )

    result = await mcp_client.call_tool(
        "get_project_report_data", {**ARGS, "sample_size": reporting.MAX_SAMPLE_SIZE}
    )
    assert result.structured_content is not None

    payload = result.structured_content
    size = len(json.dumps(payload, separators=(",", ":")))
    assert size <= reporting.RESULT_BUDGET_CHARS, size
    assert payload["roster"]["state"] == "partial"
    # A trimmed bucket keeps its exact total and a drill-down that lines up with it.
    trimmed = [
        payload[name]
        for name in ("created", "updated", "closed_updated")
        if len(payload[name]["items"]) < reporting.MAX_SAMPLE_SIZE
    ]
    assert trimmed
    for bucket in trimmed:
        assert bucket["total"] == 5000
        assert bucket["more"]["arguments"]["page"] == 2
        assert bucket["more"]["arguments"]["page_size"] == len(bucket["items"])
        assert "result budget" in bucket["reason"]


# --- degradation (G5) -----------------------------------------------------


async def test_an_unreadable_roster_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_report_api(mock_api)
    mock_api.get("memberships").mock(return_value=httpx.Response(403, json=FORBIDDEN_ERROR))

    result = await mcp_client.call_tool("get_project_report_data", ARGS)
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["roster"]["state"] == "unavailable"
    assert payload["roster"]["total"] is None
    assert payload["roster"]["members"] == []
    assert any(note.startswith("roster (unavailable)") for note in payload["notes"])
    # Everything else still came back.
    assert payload["closed_updated"]["total"] == 2
    assert payload["time"]["total_hours"] == 7.5


async def test_unreadable_time_entries_degrade_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    # Time and costs is a per-project module and view_time_entries is its own
    # permission, so a 403 here must not abort the other seven sections.
    mock_report_api(mock_api)
    mock_api.get("time_entries").mock(return_value=httpx.Response(403, json=FORBIDDEN_ERROR))

    result = await mcp_client.call_tool("get_project_report_data", ARGS)
    assert result.structured_content is not None
    payload = result.structured_content

    # Unknown, not zero.
    assert payload["time"]["state"] == "unavailable"
    assert payload["time"]["total_hours"] is None
    assert payload["time"]["total_entries"] is None
    assert payload["time"]["by_user"] == []
    note = next(note for note in payload["notes"] if note.startswith("time (unavailable)"))
    assert "no permission" in note
    # Everything else still came back.
    assert payload["closed_updated"]["total"] == 2
    assert payload["roster"]["members"][0]["principal"] == {"id": 12, "name": "Grace Hopper"}


async def test_a_missing_time_module_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_report_api(mock_api)
    mock_api.get("time_entries").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    result = await mcp_client.call_tool("get_project_report_data", ARGS)
    assert result.structured_content is not None

    assert result.structured_content["time"]["total_hours"] is None
    note = next(
        note for note in result.structured_content["notes"] if note.startswith("time (unavailable)")
    )
    assert "module absent" in note


async def test_an_instance_without_status_groups_says_so(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    def without_groups(request: httpx.Request) -> httpx.Response:
        if "groupBy" in request.url.params:
            return httpx.Response(
                200, json={"_type": "WorkPackageCollection", "total": 13, "count": 0}
            )
        return work_package_response(request)

    mock_report_api(mock_api)
    mock_api.get(WP_PATH).mock(side_effect=without_groups)

    result = await mcp_client.call_tool("get_project_report_data", ARGS)
    assert result.structured_content is not None

    assert result.structured_content["open"]["state"] == "partial"
    assert result.structured_content["open"]["total"] == 13
    assert result.structured_content["open"]["by_status"] == []
    assert any("groupBy=status" in note for note in result.structured_content["notes"])


# --- error paths ----------------------------------------------------------


async def test_a_malformed_date_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    result = await mcp_client.call_tool(
        "get_project_report_data",
        {"project_id": PROJECT_ID, "from_date": "07/01/2026", "to_date": TO_DATE},
        raise_on_error=False,
    )
    error = error_of(result)

    assert error["type"] == "invalid_input"
    assert "from_date" in error["message"]
    assert "YYYY-MM-DD" in error["hint"]
    assert routes["project"].call_count == 0


async def test_an_inverted_window_is_refused_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    result = await mcp_client.call_tool(
        "get_project_report_data",
        {"project_id": PROJECT_ID, "from_date": TO_DATE, "to_date": FROM_DATE},
        raise_on_error=False,
    )
    error = error_of(result)

    assert error["type"] == "invalid_input"
    assert "after" in error["message"]
    assert routes["project"].call_count == 0


async def test_an_unknown_project_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/999").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    result = await mcp_client.call_tool(
        "get_project_report_data",
        {"project_id": 999, "from_date": FROM_DATE, "to_date": TO_DATE},
        raise_on_error=False,
    )
    error = error_of(result)

    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert error["hint"]
