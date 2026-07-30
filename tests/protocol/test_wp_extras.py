"""Protocol tests for the Phase 3 collaboration extras (SPEC §6.3, §4.7, G1/G2).

Emoji reactions, personal reminders and custom actions, driven through the
in-memory FastMCP client against a respx-mocked instance: the wire bodies that
leave this server, the projections a model receives, the version gate that keeps
a reaction off a pre-16 instance, the upsert that turns a 409 into an update,
the lock-version handling of a custom action, and the §4.2 error envelopes.
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
from tests.fixtures.wp_extras_payloads import (
    ACTIVITY_ID,
    ACTIVITY_NOT_FOUND,
    CAPPED_REMINDERS,
    CREATED_REMINDER,
    CURRENT_USER_ID,
    CUSTOM_ACTION_CONFLICT,
    CUSTOM_ACTION_FORBIDDEN,
    CUSTOM_ACTION_ID,
    CUSTOM_ACTION_NOT_FOUND,
    HEART,
    LATER_REMIND_AT,
    LOCK_VERSION,
    MY_REMINDERS,
    NO_REMINDERS,
    ONE_REMINDER,
    OTHER_REMINDER_ID,
    OTHER_USER_ID,
    REACTION_ON_NON_COMMENT,
    REACTIONS_AFTER_ADD,
    REACTIONS_AFTER_REMOVE,
    REACTIONS_EMPTY,
    REMIND_AT,
    REMINDER_CONFLICT,
    REMINDER_ID,
    REMINDER_IN_THE_PAST,
    REMINDER_NOTE,
    ROCKET,
    UPDATED_REMINDER,
    WORK_PACKAGE_AFTER_ACTION,
    WORK_PACKAGE_BEFORE_ACTION,
    WORK_PACKAGE_ID,
    WORK_PACKAGE_MOVED_ON,
    WORK_PACKAGE_NOT_FOUND,
)

REACTIONS_PATH = f"activities/{ACTIVITY_ID}/emoji_reactions"
WP_REMINDERS_PATH = f"work_packages/{WORK_PACKAGE_ID}/reminders"
REMINDER_PATH = f"reminders/{REMINDER_ID}"
WORK_PACKAGE_PATH = f"work_packages/{WORK_PACKAGE_ID}"
EXECUTE_PATH = f"custom_actions/{CUSTOM_ACTION_ID}/execute"


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
    """A server with its own lifespan cache, so a probe result is never reused."""
    route_probe(mock_api, core_version)
    return build_server(
        Settings(_env_file=None, url=TEST_URL, api_key="test-token")  # type: ignore[call-arg]
    )


def server_without_version(mock_api: respx.MockRouter) -> FastMCP:
    """A server talking to a root that reports no ``coreVersion``.

    OpenProject renders ``coreVersion`` on the API root to admins only, so this
    is what an ordinary API token sees on any instance, however new. Everything
    else about the root — including the authenticated user — is still there.
    """
    root = {key: value for key, value in API_ROOT.items() if key != "coreVersion"}
    mock_api.get(url=f"{API_BASE}/").mock(return_value=httpx.Response(200, json=root))
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json={"total": 0}))
    return build_server(
        Settings(_env_file=None, url=TEST_URL, api_key="test-token")  # type: ignore[call-arg]
    )


# --- registration ---------------------------------------------------------


async def test_phase_three_collaboration_tools_are_registered(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    reacting = tools["toggle_comment_reaction"]
    assert reacting.outputSchema is not None
    assert reacting.annotations is not None
    assert reacting.annotations.readOnlyHint is False
    assert reacting.annotations.destructiveHint is False
    # A toggle is not idempotent: the second call undoes the first.
    assert reacting.annotations.idempotentHint is False
    assert set(reacting.inputSchema["properties"]) == {"activity_id", "reaction"}

    reminding = tools["set_work_package_reminder"]
    assert reminding.outputSchema is not None
    assert reminding.annotations is not None
    assert reminding.annotations.readOnlyHint is False
    assert reminding.annotations.destructiveHint is False
    assert reminding.annotations.idempotentHint is True
    assert set(reminding.inputSchema["properties"]) == {"work_package_id", "remind_at", "note"}

    listing = tools["list_reminders"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert listing.inputSchema.get("properties", {}) == {}

    executing = tools["execute_custom_action"]
    assert executing.outputSchema is not None
    assert executing.annotations is not None
    assert executing.annotations.readOnlyHint is False
    assert set(executing.inputSchema["properties"]) == {
        "custom_action_id",
        "work_package_id",
        "lock_version",
    }


async def test_reaction_is_a_closed_enum_of_the_eight_openproject_accepts(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    schema = tools["toggle_comment_reaction"].inputSchema["properties"]["reaction"]
    allowed = schema.get("enum") or schema.get("const")
    assert allowed == [
        "thumbs_up",
        "thumbs_down",
        "grinning_face_with_smiling_eyes",
        "confused_face",
        "heart",
        "party_popper",
        "rocket",
        "eyes",
    ]


async def test_descriptions_carry_the_pitfalls_and_cross_references(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    reacting = tools["toggle_comment_reaction"].description or ""
    assert "toggle" in reacting.lower()
    assert "16.0" in reacting
    assert "list_work_package_comments" in reacting

    reminding = tools["set_work_package_reminder"].description or ""
    # Deleting a reminder is the documented null-clearing write, not a 🗑 tool.
    assert "remind_at=null" in reminding
    assert "confirm" in reminding
    assert "one active reminder" in reminding
    assert "list_reminders" in reminding

    listing = tools["list_reminders"].description or ""
    assert "personal" in listing
    assert "upcoming" in listing

    executing = tools["execute_custom_action"].description or ""
    assert "include=['custom_actions']" in executing
    assert "lock_version" in executing


# --- toggle_comment_reaction ----------------------------------------------


async def test_reaction_is_added_and_reported_as_yours(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    route = mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(200, json=REACTIONS_AFTER_ADD)
    )

    result = await mcp_client.call_tool(
        "toggle_comment_reaction", {"activity_id": ACTIVITY_ID, "reaction": "heart"}
    )

    assert json.loads(route.calls[0].request.content) == {"reaction": "heart"}
    assert result.structured_content is not None
    assert result.structured_content["activity_id"] == ACTIVITY_ID
    assert result.structured_content["reaction"] == "heart"
    assert result.structured_content["reacted"] is True
    assert result.structured_content["notes"] == []

    reactions = result.structured_content["reactions"]
    assert [item["reaction"] for item in reactions] == ["heart", "rocket"]
    assert reactions[0]["emoji"] == HEART
    assert reactions[0]["count"] == 2
    assert reactions[0]["first_reaction_at"] == "2026-07-27T09:00:00Z"
    assert reactions[0]["users"] == [
        {"id": CURRENT_USER_ID, "name": "Ada Lovelace"},
        {"id": OTHER_USER_ID, "name": "Grace Hopper"},
    ]
    assert reactions[1]["emoji"] == ROCKET
    assert reactions[1]["count"] == 1
    assert "1 other(s)" in result.structured_content["message"]


async def test_toggling_the_same_reaction_again_removes_it(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(200, json=REACTIONS_AFTER_REMOVE)
    )

    result = await mcp_client.call_tool(
        "toggle_comment_reaction", {"activity_id": ACTIVITY_ID, "reaction": "heart"}
    )

    assert result.structured_content is not None
    assert result.structured_content["reacted"] is False
    heart = result.structured_content["reactions"][0]
    assert heart["count"] == 1
    assert heart["users"] == [{"id": OTHER_USER_ID, "name": "Grace Hopper"}]
    assert "removed" in result.structured_content["message"]


async def test_removing_the_last_reaction_returns_an_honestly_empty_state(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    mock_api.patch(REACTIONS_PATH).mock(return_value=httpx.Response(200, json=REACTIONS_EMPTY))

    result = await mcp_client.call_tool(
        "toggle_comment_reaction", {"activity_id": ACTIVITY_ID, "reaction": "rocket"}
    )

    assert result.structured_content is not None
    assert result.structured_content["reactions"] == []
    assert result.structured_content["reacted"] is False


@pytest.mark.parametrize("core_version", ["16.0.0", "17.7.1"])
async def test_reaction_is_accepted_on_openproject_16_and_newer(
    core_version: str, mock_api: respx.MockRouter
) -> None:
    server = server_for(core_version, mock_api)
    route = mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(200, json=REACTIONS_AFTER_ADD)
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "toggle_comment_reaction", {"activity_id": ACTIVITY_ID, "reaction": "heart"}
        )

    assert result.structured_content is not None
    assert result.structured_content["reacted"] is True
    assert route.call_count == 1


@pytest.mark.parametrize("core_version", ["14.6.1", "15.4.0"])
async def test_reaction_hard_errors_below_openproject_16(
    core_version: str, mock_api: respx.MockRouter
) -> None:
    server = server_for(core_version, mock_api)
    route = mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(200, json=REACTIONS_AFTER_ADD)
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "toggle_comment_reaction",
            {"activity_id": ACTIVITY_ID, "reaction": "heart"},
            raise_on_error=False,
        )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert core_version in error["message"]
    assert "16.0" in error["message"]
    assert "emoji_reactions" in error["hint"]
    assert "get_instance_info" in error["hint"]
    assert route.call_count == 0, "the reaction must not be attempted on a pre-16 instance"


async def test_an_instance_without_a_readable_version_still_attempts_the_reaction(
    mock_api: respx.MockRouter,
) -> None:
    server = server_without_version(mock_api)
    route = mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(200, json=REACTIONS_AFTER_ADD)
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "toggle_comment_reaction", {"activity_id": ACTIVITY_ID, "reaction": "heart"}
        )

    assert result.structured_content is not None
    assert result.structured_content["reacted"] is True
    assert route.call_count == 1


async def test_an_unversioned_instance_without_the_endpoint_gets_the_version_hint_on_404(
    mock_api: respx.MockRouter,
) -> None:
    server = server_without_version(mock_api)
    route = mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(404, json=ACTIVITY_NOT_FOUND)
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "toggle_comment_reaction",
            {"activity_id": ACTIVITY_ID, "reaction": "eyes"},
            raise_on_error=False,
        )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "emoji reactions" in error["hint"]
    assert "get_instance_info" in error["hint"]
    assert route.call_count == 1


async def test_an_unknown_reaction_name_is_rejected_by_the_schema(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    route = mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(200, json=REACTIONS_AFTER_ADD)
    )

    result = await mcp_client.call_tool(
        "toggle_comment_reaction",
        {"activity_id": ACTIVITY_ID, "reaction": "tada"},
        raise_on_error=False,
    )

    assert result.is_error
    assert route.call_count == 0


async def test_reacting_to_an_unknown_activity_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    mock_api.patch(REACTIONS_PATH).mock(return_value=httpx.Response(404, json=ACTIVITY_NOT_FOUND))

    result = await mcp_client.call_tool(
        "toggle_comment_reaction",
        {"activity_id": ACTIVITY_ID, "reaction": "heart"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "list_work_package_comments" in error["hint"]
    assert "emoji reactions" in error["hint"]


async def test_reacting_to_a_field_change_entry_surfaces_the_api_rejection(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    mock_api.patch(REACTIONS_PATH).mock(
        return_value=httpx.Response(400, json=REACTION_ON_NON_COMMENT)
    )

    result = await mcp_client.call_tool(
        "toggle_comment_reaction",
        {"activity_id": ACTIVITY_ID, "reaction": "heart"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 400
    assert "only supported on comments" in error["message"]


# --- set_work_package_reminder --------------------------------------------


async def test_reminder_is_created_when_the_work_package_has_none(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=NO_REMINDERS))
    route = mock_api.post(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_REMINDER)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": REMIND_AT, "note": REMINDER_NOTE},
    )

    assert read.call_count == 1
    assert json.loads(route.calls[0].request.content) == {
        "remindAt": REMIND_AT,
        "note": REMINDER_NOTE,
    }
    assert result.structured_content is not None
    assert result.structured_content["work_package_id"] == WORK_PACKAGE_ID
    assert result.structured_content["action"] == "created"
    assert result.structured_content["reminder"] == {
        "id": OTHER_REMINDER_ID,
        "remind_at": REMIND_AT,
        "note": REMINDER_NOTE,
        "work_package": {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"},
        "creator": {"id": CURRENT_USER_ID, "name": "Ada Lovelace"},
    }
    assert REMIND_AT in result.structured_content["message"]


async def test_an_existing_reminder_is_updated_rather_than_duplicated(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=ONE_REMINDER))
    created = mock_api.post(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_REMINDER)
    )
    route = mock_api.patch(REMINDER_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_REMINDER)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {
            "work_package_id": WORK_PACKAGE_ID,
            "remind_at": LATER_REMIND_AT,
            "note": "Check the staging deploy after the freeze",
        },
    )

    assert created.call_count == 0
    assert json.loads(route.calls[0].request.content) == {
        "remindAt": LATER_REMIND_AT,
        "note": "Check the staging deploy after the freeze",
    }
    assert result.structured_content is not None
    assert result.structured_content["action"] == "updated"
    assert result.structured_content["reminder"]["id"] == REMINDER_ID
    assert result.structured_content["reminder"]["remind_at"] == LATER_REMIND_AT
    assert result.structured_content["reminder"]["note"] == (
        "Check the staging deploy after the freeze"
    )


async def test_a_note_only_call_leaves_the_time_alone(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=ONE_REMINDER))
    route = mock_api.patch(REMINDER_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_REMINDER)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "note": "Check the staging deploy after the freeze"},
    )

    assert json.loads(route.calls[0].request.content) == {
        "note": "Check the staging deploy after the freeze"
    }
    assert result.structured_content is not None
    assert result.structured_content["action"] == "updated"


async def test_a_conflicting_create_is_re_read_and_patched(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(WP_REMINDERS_PATH).mock(
        side_effect=[
            httpx.Response(200, json=NO_REMINDERS),
            httpx.Response(200, json=ONE_REMINDER),
        ]
    )
    created = mock_api.post(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(409, json=REMINDER_CONFLICT)
    )
    route = mock_api.patch(REMINDER_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_REMINDER)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": LATER_REMIND_AT},
    )

    assert created.call_count == 1
    assert read.call_count == 2
    assert json.loads(route.calls[0].request.content) == {"remindAt": LATER_REMIND_AT}
    assert result.structured_content is not None
    assert result.structured_content["action"] == "updated"
    assert result.structured_content["reminder"]["id"] == REMINDER_ID
    assert "already existed" in result.structured_content["message"]


async def test_an_explicit_null_deletes_the_reminder_without_asking_for_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=ONE_REMINDER))
    route = mock_api.delete(REMINDER_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": None},
    )

    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["action"] == "deleted"
    assert result.structured_content["reminder"]["id"] == REMINDER_ID
    assert result.structured_content["reminder"]["remind_at"] == REMIND_AT
    assert "work package itself is unchanged" in result.structured_content["message"]


async def test_clearing_a_reminder_that_is_not_there_reports_unchanged(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=NO_REMINDERS))
    route = mock_api.delete(REMINDER_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": None},
    )

    assert route.call_count == 0
    assert result.structured_content is not None
    assert result.structured_content["action"] == "unchanged"
    assert result.structured_content["reminder"] is None


async def test_a_note_with_an_explicit_null_time_is_refused_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=ONE_REMINDER))
    route = mock_api.delete(REMINDER_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": None, "note": "still needed?"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to attach to" in error["message"]
    assert read.call_count == 0
    assert route.call_count == 0


async def test_a_call_with_nothing_to_set_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=ONE_REMINDER))

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to set" in error["message"]
    assert "remind_at=null" in error["hint"]
    assert read.call_count == 0


async def test_a_naive_datetime_is_refused_before_any_request(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=NO_REMINDERS))
    route = mock_api.post(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_REMINDER)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": "2026-08-03T09:00"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "no timezone" in error["message"]
    assert "2026-08-03T09:00:00Z" in error["hint"]
    assert read.call_count == 0
    assert route.call_count == 0


async def test_a_note_alone_cannot_create_a_missing_reminder(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=NO_REMINDERS))
    route = mock_api.post(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_REMINDER)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "note": "no time given"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "no reminder of yours to update" in error["message"]
    assert "remind_at" in error["hint"]
    assert route.call_count == 0


async def test_a_rejected_reminder_time_carries_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_REMINDERS_PATH).mock(return_value=httpx.Response(200, json=NO_REMINDERS))
    mock_api.post(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(422, json=REMINDER_IN_THE_PAST)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": "2020-01-01T09:00:00Z"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "remindAt", "message": "Remind at must be in the future."}
    ]


async def test_an_unknown_work_package_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(404, json=WORK_PACKAGE_NOT_FOUND)
    )
    route = mock_api.post(WP_REMINDERS_PATH).mock(
        return_value=httpx.Response(201, json=CREATED_REMINDER)
    )

    result = await mcp_client.call_tool(
        "set_work_package_reminder",
        {"work_package_id": WORK_PACKAGE_ID, "remind_at": REMIND_AT},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "ids come from" in error["hint"]
    assert route.call_count == 0


# --- list_reminders -------------------------------------------------------


async def test_reminders_are_listed_in_the_standard_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("reminders").mock(return_value=httpx.Response(200, json=MY_REMINDERS))

    result = await mcp_client.call_tool("list_reminders", {})

    assert route.calls[0].request.url.params["pageSize"] == "100"
    assert result.structured_content is not None
    items = result.structured_content["items"]
    assert [item["id"] for item in items] == [REMINDER_ID, 44]
    assert items[0]["remind_at"] == REMIND_AT
    assert items[0]["note"] == REMINDER_NOTE
    assert items[0]["work_package"] == {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"}
    assert items[1]["work_package"] == {"id": 4321, "name": "Design the client layer"}
    assert result.structured_content["pagination"] == {
        "total": 2,
        "page": 1,
        "page_size": 100,
        "has_more": False,
    }
    assert any("personal" in note for note in result.structured_content["notes"])


async def test_an_empty_reminder_list_still_carries_the_envelope_and_the_scope_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("reminders").mock(return_value=httpx.Response(200, json=NO_REMINDERS))

    result = await mcp_client.call_tool("list_reminders", {})

    assert result.structured_content is not None
    assert result.structured_content["items"] == []
    assert result.structured_content["pagination"]["total"] == 0
    assert result.structured_content["pagination"]["has_more"] is False
    assert any("upcoming" in note for note in result.structured_content["notes"])


async def test_more_reminders_than_one_page_holds_are_reported_not_hidden(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("reminders").mock(return_value=httpx.Response(200, json=CAPPED_REMINDERS))

    result = await mcp_client.call_tool("list_reminders", {})

    assert result.structured_content is not None
    assert result.structured_content["pagination"]["total"] == 137
    assert result.structured_content["pagination"]["has_more"] is True
    assert any("137 upcoming reminders" in note for note in result.structured_content["notes"])


async def test_a_reminder_listing_failure_is_a_structured_error(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("reminders").mock(return_value=httpx.Response(403, json=CUSTOM_ACTION_FORBIDDEN))

    result = await mcp_client.call_tool("list_reminders", {}, raise_on_error=False)

    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert error["http_status"] == 403


# --- execute_custom_action ------------------------------------------------


async def test_custom_action_reads_the_lock_version_then_executes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(WORK_PACKAGE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_BEFORE_ACTION)
    )
    route = mock_api.post(EXECUTE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_AFTER_ACTION)
    )

    result = await mcp_client.call_tool(
        "execute_custom_action",
        {"custom_action_id": CUSTOM_ACTION_ID, "work_package_id": WORK_PACKAGE_ID},
    )

    assert read.call_count == 1
    assert json.loads(route.calls[0].request.content) == {
        "lockVersion": LOCK_VERSION,
        "_links": {"workPackage": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"}},
    }
    assert result.structured_content is not None
    assert result.structured_content == {
        "id": WORK_PACKAGE_ID,
        "subject": "Ship the client layer",
        "type": {"id": 1, "name": "Task"},
        "status": {"id": 12, "name": "Closed"},
        "priority": {"id": 8, "name": "Normal"},
        "assignee": {"id": OTHER_USER_ID, "name": "Grace Hopper"},
        "project": {"id": "demo", "name": "Demo project"},
        "start_date": "2026-07-01",
        "due_date": "2026-07-31",
        "percentage_done": 100,
        "updated_at": "2026-07-30T08:45:00Z",
    }


async def test_a_supplied_lock_version_skips_the_read(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(WORK_PACKAGE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_BEFORE_ACTION)
    )
    route = mock_api.post(EXECUTE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_AFTER_ACTION)
    )

    result = await mcp_client.call_tool(
        "execute_custom_action",
        {
            "custom_action_id": CUSTOM_ACTION_ID,
            "work_package_id": WORK_PACKAGE_ID,
            "lock_version": 3,
        },
    )

    assert read.call_count == 0
    assert json.loads(route.calls[0].request.content)["lockVersion"] == 3
    assert result.structured_content is not None
    assert result.structured_content["status"] == {"id": 12, "name": "Closed"}


async def test_a_stale_lock_version_returns_the_fresh_one_in_the_conflict(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    refresh = mock_api.get(WORK_PACKAGE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_MOVED_ON)
    )
    mock_api.post(EXECUTE_PATH).mock(return_value=httpx.Response(409, json=CUSTOM_ACTION_CONFLICT))

    result = await mcp_client.call_tool(
        "execute_custom_action",
        {
            "custom_action_id": CUSTOM_ACTION_ID,
            "work_package_id": WORK_PACKAGE_ID,
            "lock_version": LOCK_VERSION,
        },
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert error["lock_version"] == LOCK_VERSION + 3
    assert error["current"]["subject"] == "Ship the client layer (renamed)"
    assert refresh.call_count == 1


async def test_a_failed_lock_version_read_aborts_the_execution(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WORK_PACKAGE_PATH).mock(
        return_value=httpx.Response(404, json=WORK_PACKAGE_NOT_FOUND)
    )
    route = mock_api.post(EXECUTE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_AFTER_ACTION)
    )

    result = await mcp_client.call_tool(
        "execute_custom_action",
        {"custom_action_id": CUSTOM_ACTION_ID, "work_package_id": WORK_PACKAGE_ID},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert route.call_count == 0, "a lockVersion that could not be read must never become 0"


async def test_an_unknown_custom_action_points_at_the_include_that_lists_them(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WORK_PACKAGE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_BEFORE_ACTION)
    )
    mock_api.post(EXECUTE_PATH).mock(return_value=httpx.Response(404, json=CUSTOM_ACTION_NOT_FOUND))

    result = await mcp_client.call_tool(
        "execute_custom_action",
        {"custom_action_id": CUSTOM_ACTION_ID, "work_package_id": WORK_PACKAGE_ID},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert f"custom action with id {CUSTOM_ACTION_ID}" in error["hint"]
    assert "include=['custom_actions']" in error["hint"]


async def test_an_action_whose_conditions_no_longer_hold_is_permission_denied(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WORK_PACKAGE_PATH).mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_BEFORE_ACTION)
    )
    mock_api.post(EXECUTE_PATH).mock(return_value=httpx.Response(403, json=CUSTOM_ACTION_FORBIDDEN))

    result = await mcp_client.call_tool(
        "execute_custom_action",
        {"custom_action_id": CUSTOM_ACTION_ID, "work_package_id": WORK_PACKAGE_ID},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert error["http_status"] == 403
