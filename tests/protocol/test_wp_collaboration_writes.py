"""Protocol tests for the Phase 2 collaboration writes (SPEC §6.3).

Comment editing, watchers and relations, exercised through the in-memory FastMCP
client against a respx-mocked instance: the wire bodies that leave this server,
the projections a model receives, the local validation that never becomes a
request, and the §4.2 error envelopes (404, 422 with violations, 409,
confirmation_required).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.collab_writes_payloads import (
    ACTIVITY_ID,
    ACTIVITY_NOT_FOUND,
    COMMENT_EDIT_VALIDATION_ERROR,
    COMMENT_SHAPE_REJECTED,
    EDITABLE_COMMENT_ACTIVITY,
    EDITED_COMMENT_ACTIVITY,
    EDITED_COMMENT_TEXT,
    FIELD_CHANGE_ACTIVITY_ID,
    FIELD_CHANGE_ONLY_ACTIVITY,
    FOLLOWS_RELATION,
    OTHER_WORK_PACKAGE_ID,
    RELATES_RELATION,
    RELATION_CONFLICT,
    RELATION_ID,
    RELATION_LAG_VIOLATION,
    RELATION_NOT_FOUND,
    UPDATED_RELATION,
    USER_ID,
    WATCHER_NOT_ALLOWED,
    WATCHER_NOT_FOUND,
    WATCHER_USER,
    WORK_PACKAGE_ID,
)

ACTIVITY_PATH = f"activities/{ACTIVITY_ID}"
WATCHERS_PATH = f"work_packages/{WORK_PACKAGE_ID}/watchers"
WATCHER_PATH = f"{WATCHERS_PATH}/{USER_ID}"
RELATIONS_PATH = f"work_packages/{WORK_PACKAGE_ID}/relations"
RELATION_PATH = f"relations/{RELATION_ID}"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


# --- registration ---------------------------------------------------------


async def test_phase_two_write_tools_are_registered(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    editing = tools["edit_work_package_comment"]
    assert editing.outputSchema is not None
    assert editing.annotations is not None
    assert editing.annotations.readOnlyHint is False
    assert editing.annotations.destructiveHint is False
    assert editing.annotations.idempotentHint is True
    assert set(editing.inputSchema["properties"]) == {"activity_id", "comment"}

    adding = tools["add_work_package_watcher"]
    assert set(adding.inputSchema["properties"]) == {"work_package_id", "user_id"}
    assert adding.annotations is not None
    assert adding.annotations.readOnlyHint is False

    removing = tools["remove_work_package_watcher"]
    assert set(removing.inputSchema["properties"]) == {"work_package_id", "user_id"}

    creating = tools["create_work_package_relation"]
    assert set(creating.inputSchema["properties"]) == {
        "from_id",
        "to_id",
        "type",
        "lag",
        "description",
    }
    assert creating.annotations is not None
    assert creating.annotations.idempotentHint is False

    updating = tools["update_work_package_relation"]
    assert set(updating.inputSchema["properties"]) == {
        "relation_id",
        "type",
        "lag",
        "description",
    }

    deleting = tools["delete_work_package_relation"]
    assert set(deleting.inputSchema["properties"]) == {"relation_id", "confirm"}
    assert deleting.annotations is not None
    assert deleting.annotations.destructiveHint is True
    assert deleting.annotations.model_extra is not None
    assert deleting.annotations.model_extra["anthropic/requiresUserInteraction"] is True


async def test_relation_type_is_a_closed_enum_in_the_schema(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    schema = tools["create_work_package_relation"].inputSchema["properties"]["type"]
    allowed = schema.get("enum") or schema.get("const")
    assert allowed == [
        "relates",
        "precedes",
        "follows",
        "blocks",
        "blocked",
        "duplicates",
        "duplicated",
        "includes",
        "partof",
        "requires",
        "required",
    ]


async def test_descriptions_carry_the_pitfalls_and_cross_references(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    editing = tools["edit_work_package_comment"].description or ""
    assert "field-change" in editing
    assert "list_work_package_comments" in editing

    creating = tools["create_work_package_relation"].description or ""
    assert "update_work_package(parent_id=" in creating
    assert "409" in creating

    updating = tools["update_work_package_relation"].description or ""
    assert "no lock version" in updating.lower()


# --- edit_work_package_comment --------------------------------------------


async def test_comment_edit_reads_the_entry_then_patches_it(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(ACTIVITY_PATH).mock(
        return_value=httpx.Response(200, json=EDITABLE_COMMENT_ACTIVITY)
    )
    write = mock_api.patch(ACTIVITY_PATH).mock(
        return_value=httpx.Response(200, json=EDITED_COMMENT_ACTIVITY)
    )

    result = await mcp_client.call_tool(
        "edit_work_package_comment",
        {"activity_id": ACTIVITY_ID, "comment": EDITED_COMMENT_TEXT},
    )

    assert result.structured_content is not None
    assert result.structured_content["id"] == ACTIVITY_ID
    assert result.structured_content["kind"] == "comment"
    assert result.structured_content["comment"] == EDITED_COMMENT_TEXT
    assert result.structured_content["updated_at"] == "2026-07-26T11:30:00Z"
    assert result.structured_content["author"] == {"id": 1, "name": "Ada Lovelace"}

    assert read.call_count == 1
    assert json.loads(write.calls[0].request.content) == {"comment": EDITED_COMMENT_TEXT}


async def test_comment_edit_falls_back_to_the_formattable_shape(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ACTIVITY_PATH).mock(
        return_value=httpx.Response(200, json=EDITABLE_COMMENT_ACTIVITY)
    )
    write = mock_api.patch(ACTIVITY_PATH).mock(
        side_effect=[
            httpx.Response(400, json=COMMENT_SHAPE_REJECTED),
            httpx.Response(200, json=EDITED_COMMENT_ACTIVITY),
        ]
    )

    result = await mcp_client.call_tool(
        "edit_work_package_comment",
        {"activity_id": ACTIVITY_ID, "comment": EDITED_COMMENT_TEXT},
    )

    assert result.structured_content is not None
    assert result.structured_content["comment"] == EDITED_COMMENT_TEXT
    assert write.call_count == 2
    assert json.loads(write.calls[0].request.content) == {"comment": EDITED_COMMENT_TEXT}
    assert json.loads(write.calls[1].request.content) == {
        "comment": {"format": "markdown", "raw": EDITED_COMMENT_TEXT}
    }


async def test_comment_edit_reports_the_first_rejection_when_both_shapes_fail(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ACTIVITY_PATH).mock(
        return_value=httpx.Response(200, json=EDITABLE_COMMENT_ACTIVITY)
    )
    write = mock_api.patch(ACTIVITY_PATH).mock(
        side_effect=[
            httpx.Response(422, json=COMMENT_EDIT_VALIDATION_ERROR),
            httpx.Response(400, json=COMMENT_SHAPE_REJECTED),
        ]
    )

    result = await mcp_client.call_tool(
        "edit_work_package_comment",
        {"activity_id": ACTIVITY_ID, "comment": "x" * 10},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "comment", "message": "Comment is too long (maximum is 65536 characters)."}
    ]
    assert "violations" in error["hint"]
    assert write.call_count == 2


async def test_comment_edit_refuses_a_field_change_entry(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    path = f"activities/{FIELD_CHANGE_ACTIVITY_ID}"
    mock_api.get(path).mock(return_value=httpx.Response(200, json=FIELD_CHANGE_ONLY_ACTIVITY))
    write = mock_api.patch(path).mock(
        return_value=httpx.Response(200, json=EDITED_COMMENT_ACTIVITY)
    )

    result = await mcp_client.call_tool(
        "edit_work_package_comment",
        {"activity_id": FIELD_CHANGE_ACTIVITY_ID, "comment": "Not allowed."},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "field-change journal entry" in error["message"]
    assert "kind='comment'" in error["hint"]
    assert "update_work_package" in error["hint"]
    assert write.call_count == 0


async def test_comment_edit_rejects_blank_text_before_reading_anything(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(ACTIVITY_PATH).mock(
        return_value=httpx.Response(200, json=EDITABLE_COMMENT_ACTIVITY)
    )
    result = await mcp_client.call_tool(
        "edit_work_package_comment",
        {"activity_id": ACTIVITY_ID, "comment": "   "},
        raise_on_error=False,
    )
    assert error_of(result)["type"] == "invalid_input"
    assert read.call_count == 0


async def test_comment_edit_unknown_activity_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ACTIVITY_PATH).mock(return_value=httpx.Response(404, json=ACTIVITY_NOT_FOUND))
    write = mock_api.patch(ACTIVITY_PATH).mock(
        return_value=httpx.Response(200, json=EDITED_COMMENT_ACTIVITY)
    )

    result = await mcp_client.call_tool(
        "edit_work_package_comment",
        {"activity_id": ACTIVITY_ID, "comment": "Nobody home."},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "ids come from" in error["hint"]
    assert write.call_count == 0


# --- watchers -------------------------------------------------------------


async def test_watcher_is_added_with_a_user_link(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(WATCHERS_PATH).mock(return_value=httpx.Response(201, json=WATCHER_USER))

    result = await mcp_client.call_tool(
        "add_work_package_watcher",
        {"work_package_id": WORK_PACKAGE_ID, "user_id": USER_ID},
    )

    assert result.structured_content is not None
    assert result.structured_content["work_package_id"] == WORK_PACKAGE_ID
    assert result.structured_content["user"] == {"id": USER_ID, "name": "Grace Hopper"}
    assert result.structured_content["watching"] is True
    assert result.structured_content["changed"] is True
    assert "Grace Hopper" in result.structured_content["message"]

    assert json.loads(route.calls[0].request.content) == {
        "_links": {"user": {"href": f"/api/v3/users/{USER_ID}"}}
    }


async def test_adding_an_existing_watcher_reports_that_nothing_changed(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(WATCHERS_PATH).mock(return_value=httpx.Response(200, json=WATCHER_USER))

    result = await mcp_client.call_tool(
        "add_work_package_watcher",
        {"work_package_id": WORK_PACKAGE_ID, "user_id": USER_ID},
    )

    assert result.structured_content is not None
    assert result.structured_content["watching"] is True
    assert result.structured_content["changed"] is False
    assert "already watched" in result.structured_content["message"]


async def test_watcher_rejected_by_the_instance_carries_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(WATCHERS_PATH).mock(return_value=httpx.Response(422, json=WATCHER_NOT_ALLOWED))

    result = await mcp_client.call_tool(
        "add_work_package_watcher",
        {"work_package_id": WORK_PACKAGE_ID, "user_id": USER_ID},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "user", "message": "User is not allowed to view this work package."}
    ]


async def test_watcher_is_removed_and_the_result_is_honest_about_it(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(WATCHER_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "remove_work_package_watcher",
        {"work_package_id": WORK_PACKAGE_ID, "user_id": USER_ID},
    )

    assert result.structured_content is not None
    assert result.structured_content["watching"] is False
    assert result.structured_content["changed"] is None
    assert result.structured_content["user"] == {"id": USER_ID, "name": None}
    assert route.call_count == 1


async def test_removing_an_unknown_watcher_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(WATCHER_PATH).mock(return_value=httpx.Response(404, json=WATCHER_NOT_FOUND))

    result = await mcp_client.call_tool(
        "remove_work_package_watcher",
        {"work_package_id": WORK_PACKAGE_ID, "user_id": USER_ID},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404


# --- create_work_package_relation -----------------------------------------


async def test_relation_is_created_with_the_to_link_and_projected(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(RELATIONS_PATH).mock(
        return_value=httpx.Response(201, json=FOLLOWS_RELATION)
    )

    result = await mcp_client.call_tool(
        "create_work_package_relation",
        {
            "from_id": WORK_PACKAGE_ID,
            "to_id": OTHER_WORK_PACKAGE_ID,
            "type": "follows",
            "lag": 2,
            "description": "Start once the design is signed off.",
        },
    )

    assert result.structured_content is not None
    assert result.structured_content == {
        "id": RELATION_ID,
        "type": "follows",
        "reverse_type": "precedes",
        "from_work_package": {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"},
        "to_work_package": {"id": OTHER_WORK_PACKAGE_ID, "name": "Design the client layer"},
        "lag": 2,
        "description": "Start once the design is signed off.",
    }

    assert json.loads(route.calls[0].request.content) == {
        "type": "follows",
        "lag": 2,
        "description": "Start once the design is signed off.",
        "_links": {"to": {"href": f"/api/v3/work_packages/{OTHER_WORK_PACKAGE_ID}"}},
    }


async def test_relation_without_lag_or_description_sends_neither(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(RELATIONS_PATH).mock(
        return_value=httpx.Response(201, json=RELATES_RELATION)
    )

    result = await mcp_client.call_tool(
        "create_work_package_relation",
        {"from_id": WORK_PACKAGE_ID, "to_id": OTHER_WORK_PACKAGE_ID, "type": "relates"},
    )

    assert result.structured_content is not None
    assert result.structured_content["lag"] is None
    assert result.structured_content["description"] is None
    assert json.loads(route.calls[0].request.content) == {
        "type": "relates",
        "_links": {"to": {"href": f"/api/v3/work_packages/{OTHER_WORK_PACKAGE_ID}"}},
    }


async def test_lag_on_a_non_scheduling_relation_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(RELATIONS_PATH).mock(
        return_value=httpx.Response(201, json=RELATES_RELATION)
    )

    result = await mcp_client.call_tool(
        "create_work_package_relation",
        {
            "from_id": WORK_PACKAGE_ID,
            "to_id": OTHER_WORK_PACKAGE_ID,
            "type": "blocks",
            "lag": 3,
        },
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "follows" in error["message"] and "precedes" in error["message"]
    assert "blocks" in error["message"]
    assert route.call_count == 0


async def test_a_relation_to_itself_is_refused_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(RELATIONS_PATH).mock(
        return_value=httpx.Response(201, json=RELATES_RELATION)
    )

    result = await mcp_client.call_tool(
        "create_work_package_relation",
        {"from_id": WORK_PACKAGE_ID, "to_id": WORK_PACKAGE_ID, "type": "relates"},
        raise_on_error=False,
    )

    assert error_of(result)["type"] == "invalid_input"
    assert route.call_count == 0


async def test_an_unknown_relation_type_is_rejected_by_the_schema(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post(RELATIONS_PATH).mock(
        return_value=httpx.Response(201, json=RELATES_RELATION)
    )

    result = await mcp_client.call_tool(
        "create_work_package_relation",
        {"from_id": WORK_PACKAGE_ID, "to_id": OTHER_WORK_PACKAGE_ID, "type": "parent"},
        raise_on_error=False,
    )

    assert result.is_error
    assert route.call_count == 0


async def test_relation_validation_failure_carries_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(RELATIONS_PATH).mock(
        return_value=httpx.Response(422, json=RELATION_LAG_VIOLATION)
    )

    result = await mcp_client.call_tool(
        "create_work_package_relation",
        {
            "from_id": WORK_PACKAGE_ID,
            "to_id": OTHER_WORK_PACKAGE_ID,
            "type": "follows",
            "lag": -1,
        },
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "lag", "message": "Lag must be a number greater than or equal to 0"}
    ]
    assert "violations" in error["hint"]


async def test_an_existing_relation_between_the_pair_is_a_conflict(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(RELATIONS_PATH).mock(return_value=httpx.Response(409, json=RELATION_CONFLICT))

    result = await mcp_client.call_tool(
        "create_work_package_relation",
        {"from_id": WORK_PACKAGE_ID, "to_id": OTHER_WORK_PACKAGE_ID, "type": "relates"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert error["hint"]


# --- update_work_package_relation -----------------------------------------


async def test_relation_update_patches_only_what_was_given(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(RELATION_PATH).mock(return_value=httpx.Response(200, json=FOLLOWS_RELATION))
    route = mock_api.patch(RELATION_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_RELATION)
    )

    result = await mcp_client.call_tool(
        "update_work_package_relation",
        {"relation_id": RELATION_ID, "description": "Start a week after sign-off."},
    )

    assert result.structured_content is not None
    assert result.structured_content["description"] == "Start a week after sign-off."
    assert json.loads(route.calls[0].request.content) == {
        "description": "Start a week after sign-off."
    }
    # No lockVersion: relations do not carry one.
    assert "lockVersion" not in json.loads(route.calls[0].request.content)
    assert read.call_count == 0


async def test_updating_only_the_lag_reads_the_current_type_first(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(RELATION_PATH).mock(return_value=httpx.Response(200, json=FOLLOWS_RELATION))
    route = mock_api.patch(RELATION_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_RELATION)
    )

    result = await mcp_client.call_tool(
        "update_work_package_relation", {"relation_id": RELATION_ID, "lag": 5}
    )

    assert result.structured_content is not None
    assert result.structured_content["lag"] == 5
    assert read.call_count == 1
    assert json.loads(route.calls[0].request.content) == {"lag": 5}


async def test_lag_on_a_relation_that_cannot_schedule_is_refused_after_the_read(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("relations/651").mock(return_value=httpx.Response(200, json=RELATES_RELATION))
    route = mock_api.patch("relations/651").mock(
        return_value=httpx.Response(200, json=UPDATED_RELATION)
    )

    result = await mcp_client.call_tool(
        "update_work_package_relation",
        {"relation_id": 651, "lag": 4},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "relates" in error["message"]
    assert route.call_count == 0


async def test_relation_update_without_any_field_is_refused(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(RELATION_PATH).mock(return_value=httpx.Response(200, json=FOLLOWS_RELATION))
    route = mock_api.patch(RELATION_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_RELATION)
    )

    result = await mcp_client.call_tool(
        "update_work_package_relation", {"relation_id": RELATION_ID}, raise_on_error=False
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "delete_work_package_relation" in error["hint"]
    assert read.call_count == 0
    assert route.call_count == 0


async def test_updating_an_unknown_relation_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(RELATION_PATH).mock(return_value=httpx.Response(404, json=RELATION_NOT_FOUND))

    result = await mcp_client.call_tool(
        "update_work_package_relation",
        {"relation_id": RELATION_ID, "type": "relates"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "ids come from" in error["hint"]


# --- delete_work_package_relation -----------------------------------------


async def test_relation_delete_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(RELATION_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_work_package_relation", {"relation_id": RELATION_ID}, raise_on_error=False
    )

    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert route.call_count == 0


async def test_relation_is_deleted_once_confirmed(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(RELATION_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_work_package_relation", {"relation_id": RELATION_ID, "confirm": True}
    )

    assert result.structured_content is not None
    assert result.structured_content["id"] == RELATION_ID
    assert result.structured_content["deleted"] is True
    assert route.call_count == 1


async def test_deleting_an_unknown_relation_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(RELATION_PATH).mock(return_value=httpx.Response(404, json=RELATION_NOT_FOUND))

    result = await mcp_client.call_tool(
        "delete_work_package_relation",
        {"relation_id": RELATION_ID, "confirm": True},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
