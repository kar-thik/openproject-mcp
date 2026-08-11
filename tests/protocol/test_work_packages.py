"""End-to-end protocol tests for the six work-package core tools (SPEC §6.2).

Every test drives the real FastMCP server over the in-memory transport with
``respx`` standing in for OpenProject, so what is asserted is what a client
actually receives: the outgoing query string, the structured content, and the
JSON error envelope on the failure paths.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from openproject_mcp.tools import attachments as attachments_module
from tests.fixtures.work_packages_payloads import (
    ATTACHMENTS_COLLECTION,
    CHILDREN_COLLECTION,
    CONFLICT_BODY,
    CREATE_FORM_INVALID_STATUS,
    CREATE_FORM_OK,
    CREATED_WORK_PACKAGE,
    GROUPED_LIST,
    NOT_FOUND_BODY,
    PRIORITIES,
    PROJECT,
    RELATIONS_COLLECTION,
    SEARCH_RESULT,
    STATUSES,
    TYPES,
    UPDATE_FORM_OK,
    UPDATED_WORK_PACKAGE,
    WATCHERS_COLLECTION,
    WORK_PACKAGE_DETAIL,
    WORK_PACKAGE_SCHEMA_5_1,
)

WP_PATH = "work_packages/1234"
SCHEMA_PATH = "work_packages/schemas/5-1"


def _filters(request: httpx.Request) -> list[dict[str, Any]]:
    return json.loads(request.url.params["filters"])


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _envelope(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)["error"]


def _structured(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


# --- search ---------------------------------------------------------------


async def test_quick_search_uses_typeahead_and_searches_every_status(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = mock_api.get("work_packages").mock(return_value=httpx.Response(200, json=SEARCH_RESULT))

    result = await mcp_client.call_tool("search_work_packages", {"query": "client layer"})

    structured = _structured(result)
    assert structured["pagination"] == {
        "total": 2,
        "page": 1,
        "page_size": 20,
        "has_more": False,
    }
    assert structured["items"][0]["id"] == 1234
    assert structured["items"][0]["status"] == {"id": 7, "name": "In progress"}
    assert structured["items"][1]["status"] == {"id": 12, "name": "Closed"}
    assert structured.get("notes") is None, "quick mode makes no degradation claim"

    assert _filters(route.calls[0].request) == [
        {"status": {"operator": "*", "values": []}},
        {"typeahead": {"operator": "**", "values": ["client layer"]}},
    ]


async def test_fulltext_search_uses_the_search_filter_and_reports_attachment_scope(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = mock_api.get("projects/demo/work_packages").mock(
        return_value=httpx.Response(200, json=SEARCH_RESULT)
    )

    result = await mcp_client.call_tool(
        "search_work_packages",
        {
            "query": "retries",
            "project_id": "demo",
            "mode": "fulltext",
            "status_scope": "open",
            "page_size": 50,
        },
    )

    structured = _structured(result)
    assert structured["notes"] == [
        "fulltext searches subject, description, comments and searchable custom fields; "
        "attachment content and filenames are included only when this instance's PostgreSQL "
        "full-text index is populated, which the API does not expose"
    ]
    request = route.calls[0].request
    assert _filters(request) == [
        {"status": {"operator": "o", "values": []}},
        {"search": {"operator": "**", "values": ["retries"]}},
    ]
    assert request.url.params["pageSize"] == "50"


async def test_search_rejects_an_empty_query_locally(mcp_client: Client[Any]) -> None:
    result = await mcp_client.call_tool(
        "search_work_packages", {"query": "   "}, raise_on_error=False
    )
    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "invalid_input"
    assert "http_status" not in error


# --- list -----------------------------------------------------------------


async def test_list_composes_the_typed_filter_set_with_groups_and_sums(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = mock_api.get("projects/demo/work_packages").mock(
        return_value=httpx.Response(200, json=GROUPED_LIST)
    )

    result = await mcp_client.call_tool(
        "list_work_packages",
        {
            "project": "demo",
            "query": "retry",
            "status_scope": "all",
            "type_ids": [1],
            "priority_ids": [9],
            "assignee": ["me"],
            "due_before": "2026-08-31",
            "created_since": "2026-07-01",
            "percentage_done_min": 25,
            "percentage_done_max": 90,
            "raw_filters": [{"name": "customField12", "operator": "=", "values": ["4"]}],
            "sort_by": [["due_date", "asc"]],
            "group_by": "status",
            "show_sums": True,
        },
    )

    structured = _structured(result)
    assert structured["pagination"] == {
        "total": 34,
        "page": 1,
        "page_size": 20,
        "has_more": True,
    }
    assert structured["groups"] == [
        {"value": "In progress", "count": 12, "sums": {"estimated_hours": 41.5}},
        {"value": "New", "count": 22, "sums": {"estimated_hours": 78.5}},
    ]
    assert structured["sums"] == {"estimated_hours": 120.0, "story_points": 21.0}
    assert structured["items"][0]["assignee"] is None

    request = route.calls[0].request
    assert _filters(request) == [
        {"status": {"operator": "*", "values": []}},
        {"search": {"operator": "**", "values": ["retry"]}},
        {"type": {"operator": "=", "values": ["1"]}},
        {"priority": {"operator": "=", "values": ["9"]}},
        {"assignee": {"operator": "=", "values": ["me"]}},
        {"dueDate": {"operator": "<>d", "values": ["", "2026-08-31"]}},
        {"createdAt": {"operator": "<>d", "values": ["2026-07-01", ""]}},
        {"percentageDone": {"operator": ">=", "values": ["25"]}},
        {"percentageDone": {"operator": "<=", "values": ["90"]}},
        {"customField12": {"operator": "=", "values": ["4"]}},
    ]
    assert request.url.params["sortBy"] == '[["dueDate","asc"]]'
    assert request.url.params["groupBy"] == "status"
    assert request.url.params["showSums"] == "true"


async def test_list_defaults_to_open_and_status_ids_override_the_scope(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = mock_api.get("work_packages").mock(return_value=httpx.Response(200, json=GROUPED_LIST))

    await mcp_client.call_tool("list_work_packages", {})
    assert _filters(route.calls[0].request)[0] == {"status": {"operator": "o", "values": []}}

    await mcp_client.call_tool(
        "list_work_packages", {"status_scope": "open", "status_ids": [7, 12]}
    )
    assert _filters(route.calls[1].request)[0] == {
        "status": {"operator": "=", "values": ["7", "12"]}
    }


async def test_list_unassigned_and_top_level_use_the_no_value_operator(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = mock_api.get("work_packages").mock(return_value=httpx.Response(200, json=GROUPED_LIST))

    await mcp_client.call_tool("list_work_packages", {"assignee": ["none"], "top_level_only": True})

    assert _filters(route.calls[0].request)[1:] == [
        {"assignee": {"operator": "!*", "values": []}},
        {"parent": {"operator": "!*", "values": []}},
    ]


async def test_list_rejects_an_unknown_sort_key_with_the_allowed_set(
    mcp_client: Client[Any],
) -> None:
    result = await mcp_client.call_tool(
        "list_work_packages", {"sort_by": [["deadline", "asc"]]}, raise_on_error=False
    )
    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "invalid_input"
    assert "due_date" in error["hint"]


async def test_list_rejects_contradictory_hierarchy_filters(mcp_client: Client[Any]) -> None:
    result = await mcp_client.call_tool(
        "list_work_packages", {"parent_id": 12, "top_level_only": True}, raise_on_error=False
    )
    assert result.is_error
    assert _envelope(result)["type"] == "invalid_input"


# --- get ------------------------------------------------------------------


async def test_get_returns_detail_with_custom_fields_and_availability(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))

    structured = _structured(await mcp_client.call_tool("get_work_package", {"id": 1234}))

    assert structured["subject"] == "Ship the client layer"
    assert structured["description"] == "Pooled httpx client with retries."
    assert structured["lock_version"] == 7
    assert structured["parent"] == {"id": 1000, "name": "Epic"}
    assert structured["estimated_hours"] == 7.5
    assert structured["spent_hours"] == 2.25
    assert structured["available"] == {"dev_links": True, "meetings": False, "files": True}
    assert structured["custom_fields"] == [
        {
            "key": "customField7",
            "name": "Ticket URL",
            "type": "string",
            "value": "https://tickets.example.com/OP-1",
            "value_ids": None,
        },
        {
            "key": "customField9",
            "name": "Reviewers",
            "type": "user",
            "value": ["Grace Hopper", "Alan Turing"],
            "value_ids": [12, 13],
        },
        {
            "key": "customField12",
            "name": "Severity",
            "type": "list",
            "value": "High",
            "value_ids": [4],
        },
    ]
    assert structured["relations"] is None, "includes are opt-in"


async def test_get_caps_every_include_at_twenty_and_points_at_the_full_listing(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    children = mock_api.get("work_packages").mock(
        return_value=httpx.Response(200, json=CHILDREN_COLLECTION)
    )
    mock_api.get(f"{WP_PATH}/relations").mock(
        return_value=httpx.Response(200, json=RELATIONS_COLLECTION)
    )
    mock_api.get(f"{WP_PATH}/watchers").mock(
        return_value=httpx.Response(200, json=WATCHERS_COLLECTION)
    )
    mock_api.get(f"{WP_PATH}/attachments").mock(
        return_value=httpx.Response(200, json=ATTACHMENTS_COLLECTION)
    )

    structured = _structured(
        await mcp_client.call_tool(
            "get_work_package",
            {
                "id": 1234,
                "include": [
                    "children",
                    "relations",
                    "watchers",
                    "attachments",
                    "custom_actions",
                ],
            },
        )
    )

    assert len(structured["children"]["items"]) == 20
    assert structured["children"]["truncated"] is True
    assert structured["children"]["total"] == 42
    assert structured["children"]["more_via"] == (
        "list_work_packages(parent_id=1234, status_scope='all')"
    )

    assert structured["relations"]["truncated"] is False
    assert structured["relations"]["items"][0] == {
        "id": 55,
        "type": "blocks",
        "reverse_type": "blocked",
        "lag": None,
        "description": "waiting on the client layer",
        "from_work_package": {"id": 1234, "name": "Ship the client"},
        "to_work_package": {"id": 1300, "name": "Ship the tools"},
    }
    assert structured["watchers"]["items"] == [{"id": 12, "name": "Grace Hopper"}]
    assert structured["attachments"]["items"][0]["file_name"] == "spec.pdf"
    assert structured["custom_actions"]["items"] == [
        {"id": 3, "name": "Send to QA"},
        {"id": 4, "name": "Escalate"},
    ]

    # The children include is a real filtered query, always with an explicit status filter.
    assert _filters(children.calls[0].request) == [
        {"status": {"operator": "*", "values": []}},
        {"parent": {"operator": "=", "values": ["1234"]}},
    ]


async def test_get_degrades_when_a_sub_resource_is_forbidden(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.get(f"{WP_PATH}/relations").mock(
        return_value=httpx.Response(403, json={"message": "You are not authorized."})
    )

    structured = _structured(
        await mcp_client.call_tool("get_work_package", {"id": 1234, "include": ["relations"]})
    )

    assert structured["relations"] is None
    assert structured["notes"] == ["relations unavailable: You are not authorized."]


async def test_get_missing_work_package_returns_the_not_found_envelope(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get("work_packages/999").mock(return_value=httpx.Response(404, json=NOT_FOUND_BODY))

    result = await mcp_client.call_tool("get_work_package", {"id": 999}, raise_on_error=False)

    assert result.is_error
    assert result.structured_content is None, "errors never set structuredContent"
    error = _envelope(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "search_work_packages" not in error["message"]
    assert error["hint"]


async def test_get_resolves_a_semantic_identifier_and_keeps_includes_numeric(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    """17.x semantic ids ('QMS-42') fetch by the given key; includes use the self id."""
    mock_api.get("work_packages/QMS-42").mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL)
    )
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    relations = mock_api.get(f"{WP_PATH}/relations").mock(
        return_value=httpx.Response(200, json=RELATIONS_COLLECTION)
    )

    structured = _structured(
        await mcp_client.call_tool("get_work_package", {"id": "QMS-42", "include": ["relations"]})
    )

    assert structured["id"] == 1234
    assert relations.call_count == 1, "includes must address the numeric self id"


async def test_get_semantic_identifier_miss_hints_at_the_numeric_id(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get("work_packages/NOPE-1").mock(return_value=httpx.Response(404, json=NOT_FOUND_BODY))

    result = await mcp_client.call_tool("get_work_package", {"id": "NOPE-1"}, raise_on_error=False)

    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "not_found"
    assert "Semantic identifiers" in error["hint"]
    assert "numeric id" in error["hint"]


# --- create ---------------------------------------------------------------


@pytest.fixture
def create_routes(mock_api: respx.MockRouter) -> dict[str, respx.Route]:
    """Everything ``create_work_package`` resolves before it writes."""
    return {
        "project": mock_api.get("projects/demo").mock(
            return_value=httpx.Response(200, json=PROJECT)
        ),
        "types": mock_api.get("types").mock(return_value=httpx.Response(200, json=TYPES)),
        "statuses": mock_api.get("statuses").mock(return_value=httpx.Response(200, json=STATUSES)),
        "priorities": mock_api.get("priorities").mock(
            return_value=httpx.Response(200, json=PRIORITIES)
        ),
        "schema": mock_api.get(SCHEMA_PATH).mock(
            return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1)
        ),
    }


async def test_create_resolves_names_claims_attachments_and_keeps_form_defaults(
    mock_api: respx.MockRouter,
    create_routes: dict[str, respx.Route],
    mcp_client: Client[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded: list[str] = []

    async def fake_upload(
        ctx: Any, file_path: str, file_name: str | None = None, description: str | None = None
    ) -> int:
        uploaded.append(file_path)
        return 91

    monkeypatch.setattr(
        attachments_module, "upload_uncontainered_attachment", fake_upload, raising=False
    )

    form = mock_api.post("work_packages/form").mock(
        return_value=httpx.Response(200, json=CREATE_FORM_OK)
    )
    commit = mock_api.post("work_packages").mock(
        return_value=httpx.Response(201, json=CREATED_WORK_PACKAGE)
    )

    structured = _structured(
        await mcp_client.call_tool(
            "create_work_package",
            {
                "project": "demo",
                "type": "Task",
                "subject": "Write the tools layer",
                "description": "Six work-package tools.",
                "status": "In progress",
                "priority": "High",
                "assignee": "12",
                "estimated_hours": 7.5,
                "custom_fields": {"Severity": "High"},
                "attachment_paths": ["/tmp/spec.pdf"],
            },
        )
    )

    assert uploaded == ["/tmp/spec.pdf"]
    assert structured["id"] == 1500
    assert structured["lock_version"] == 0
    assert structured["status"] == {"id": 7, "name": "In progress"}
    assert structured["custom_fields"][1]["value"] == "High"

    form_body = _body(form.calls[0].request)
    assert form_body["_links"]["status"] == {"href": "/api/v3/statuses/7"}
    assert form_body["_links"]["priority"] == {"href": "/api/v3/priorities/9"}
    assert form_body["_links"]["type"] == {"href": "/api/v3/types/1"}
    assert form_body["_links"]["project"] == {"href": "/api/v3/projects/5"}
    assert form_body["_links"]["customField12"] == {"href": "/api/v3/custom_options/4"}
    assert form_body["estimatedTime"] == "PT7H30M"

    commit_body = _body(commit.calls[0].request)
    assert commit_body["subject"] == "Write the tools layer"
    assert commit_body["scheduleManually"] is False, "form defaults survive the merge"
    assert commit_body["_links"]["attachments"] == [{"href": "/api/v3/attachments/91"}]
    assert commit_body["_links"]["status"] == {"href": "/api/v3/statuses/7"}
    assert commit.calls[0].request.url.params["notify"] == "true"


async def test_create_surfaces_form_validation_errors_with_allowed_values(
    mock_api: respx.MockRouter,
    create_routes: dict[str, respx.Route],
    mcp_client: Client[Any],
) -> None:
    mock_api.post("work_packages/form").mock(
        return_value=httpx.Response(200, json=CREATE_FORM_INVALID_STATUS)
    )
    commit = mock_api.post("work_packages").mock(
        return_value=httpx.Response(201, json=CREATED_WORK_PACKAGE)
    )

    result = await mcp_client.call_tool(
        "create_work_package",
        {
            "project": "demo",
            "type": "Task",
            "subject": "Write the tools layer",
            "status": "Closed",
        },
        raise_on_error=False,
    )

    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["error_identifier"] == (
        "urn:openproject-org:api:v3:errors:PropertyConstraintViolation"
    )
    assert error["violations"] == [
        {"attribute": "status", "message": "Status is not set to one of the allowed values."}
    ]
    assert error["hint"] == "Allowed values for status: New, In progress."
    assert not commit.called, "a failed form never commits"


async def test_create_rejects_an_unknown_status_name_before_any_write(
    mock_api: respx.MockRouter,
    create_routes: dict[str, respx.Route],
    mcp_client: Client[Any],
) -> None:
    form = mock_api.post("work_packages/form").mock(
        return_value=httpx.Response(200, json=CREATE_FORM_OK)
    )

    result = await mcp_client.call_tool(
        "create_work_package",
        {
            "project": "demo",
            "type": "Task",
            "subject": "Write the tools layer",
            "status": "Wontfix",
        },
        raise_on_error=False,
    )

    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "invalid_input"
    assert "Closed" in error["hint"] and "In progress" in error["hint"]
    assert not form.called


async def test_create_rejects_milestone_dates_mixed_with_ranges(
    mcp_client: Client[Any],
) -> None:
    result = await mcp_client.call_tool(
        "create_work_package",
        {
            "project": "demo",
            "type": "Milestone",
            "subject": "Launch",
            "date": "2026-09-01",
            "due_date": "2026-09-02",
        },
        raise_on_error=False,
    )
    assert result.is_error
    assert _envelope(result)["type"] == "invalid_input"


# --- update ---------------------------------------------------------------


async def test_update_clears_the_assignee_and_echoes_the_lock_version(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get("statuses").mock(return_value=httpx.Response(200, json=STATUSES))
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    form = mock_api.post(f"{WP_PATH}/form").mock(
        return_value=httpx.Response(200, json=UPDATE_FORM_OK)
    )
    patch = mock_api.patch(WP_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_WORK_PACKAGE)
    )

    structured = _structured(
        await mcp_client.call_tool(
            "update_work_package",
            {
                "id": 1234,
                "subject": "Ship the client layer (v2)",
                "assignee": None,
                "status": "Closed",
                "notify": False,
            },
        )
    )

    assert structured["assignee"] is None
    assert structured["status"] == {"id": 12, "name": "Closed"}
    assert structured["lock_version"] == 8

    assert form.called, "status changes go through the form so transitions are validated"
    body = _body(patch.calls[0].request)
    assert body["lockVersion"] == 7
    assert body["subject"] == "Ship the client layer (v2)"
    assert body["_links"]["assignee"] == {"href": None}, "clearing sends a null href"
    assert body["_links"]["status"] == {"href": "/api/v3/statuses/12"}
    assert patch.calls[0].request.url.params["notify"] == "false"


async def test_update_leaves_omitted_fields_alone(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    patch = mock_api.patch(WP_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_WORK_PACKAGE)
    )

    await mcp_client.call_tool("update_work_package", {"id": 1234, "percentage_done": 60})

    body = _body(patch.calls[0].request)
    assert set(body) == {"percentageDone", "lockVersion"}


async def test_update_with_nothing_to_change_fails_locally(mcp_client: Client[Any]) -> None:
    result = await mcp_client.call_tool("update_work_package", {"id": 1234}, raise_on_error=False)
    assert result.is_error
    assert _envelope(result)["type"] == "invalid_input"


async def test_update_with_custom_fields_reads_the_work_package_once(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    """The schema link and the lock version must come from the same snapshot."""
    read = mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    patch = mock_api.patch(WP_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_WORK_PACKAGE)
    )

    await mcp_client.call_tool(
        "update_work_package", {"id": 1234, "custom_fields": {"Severity": "Low"}}
    )

    assert read.call_count == 1
    body = _body(patch.calls[0].request)
    assert body["lockVersion"] == 7
    assert body["_links"]["customField12"] == {"href": "/api/v3/custom_options/5"}


async def test_update_conflict_carries_the_fresh_version_and_the_diff(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.post(f"{WP_PATH}/form").mock(return_value=httpx.Response(200, json=UPDATE_FORM_OK))
    mock_api.patch(WP_PATH).mock(return_value=httpx.Response(409, json=CONFLICT_BODY))

    result = await mcp_client.call_tool(
        "update_work_package",
        {"id": 1234, "subject": "Ship the client layer (v2)", "lock_version": 3},
        raise_on_error=False,
    )

    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert error["lock_version"] == 7, "the fresh version comes from the re-read"
    assert error["current"]["subject"] == "Ship the client layer"
    assert error["conflicting_fields"]["subject"] == {
        "attempted": "Ship the client layer (v2)",
        "current": "Ship the client layer",
    }
    assert "lock_version" in error["hint"]


async def test_update_rejects_a_non_numeric_assignee(mcp_client: Client[Any]) -> None:
    result = await mcp_client.call_tool(
        "update_work_package", {"id": 1234, "assignee": "me"}, raise_on_error=False
    )
    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "invalid_input"
    assert "get_instance_info" in error["hint"]


# --- delete ---------------------------------------------------------------


async def test_delete_without_confirmation_refuses_and_explains(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = mock_api.delete(WP_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool("delete_work_package", {"id": 1234}, raise_on_error=False)

    assert result.is_error
    error = _envelope(result)
    assert error["type"] == "confirmation_required"
    assert "http_status" not in error
    assert "confirm=true" in error["hint"]
    assert "permanently" in error["hint"]
    assert not route.called, "nothing is deleted without confirmation"


async def test_delete_with_confirmation_deletes(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    route = mock_api.delete(WP_PATH).mock(return_value=httpx.Response(204))

    structured = _structured(
        await mcp_client.call_tool("delete_work_package", {"id": 1234, "confirm": True})
    )

    assert structured == {
        "id": 1234,
        "deleted": True,
        "message": "Work package #1234 was deleted permanently.",
    }
    assert route.called


# --- registration contract ------------------------------------------------


async def test_annotations_and_schemas_are_honest(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    expected = {
        "search_work_packages",
        "list_work_packages",
        "get_work_package",
        "create_work_package",
        "update_work_package",
        "delete_work_package",
    }
    assert expected <= set(tools)

    for name in ("search_work_packages", "list_work_packages", "get_work_package"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False

    for name in ("create_work_package", "update_work_package"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False

    deletion = tools["delete_work_package"].annotations
    assert deletion is not None
    dumped: dict[str, Any] = deletion.model_dump()
    assert dumped["destructiveHint"] is True
    assert dumped["anthropic/requiresUserInteraction"] is True

    for name in expected:
        assert tools[name].outputSchema is not None, f"{name} must declare an outputSchema"
        assert tools[name].description, f"{name} must carry a description"


async def test_get_surfaces_the_project_phase_reference(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    """16.1+ instances link the phase; the ref survives, hrefs do not."""
    import copy

    payload = copy.deepcopy(WORK_PACKAGE_DETAIL)
    payload["_links"]["projectPhase"] = {
        "href": "/api/v3/project_phases/12",
        "title": "Executing",
    }
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=payload))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))

    structured = _structured(await mcp_client.call_tool("get_work_package", {"id": 1234}))

    assert structured["project_phase"] == {"id": 12, "name": "Executing"}


async def test_get_leaves_project_phase_null_when_the_instance_lacks_it(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL))
    mock_api.get(SCHEMA_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA_5_1))

    structured = _structured(await mcp_client.call_tool("get_work_package", {"id": 1234}))

    assert structured["project_phase"] is None
