"""Protocol tests for the people & access tools (SPEC §6.11, §9.3).

Everything runs through the in-memory FastMCP client against a respx-mocked
instance, so these assert what a model actually receives: the §9.3 envelope, the
principal kinds, the snake_case ``any_name_attribute`` filter that would 400 if
it were camelCased, the quiet-by-default membership grant, the admin tag gate,
and the structured error envelopes for 404 / 422 / 409 / confirmation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import TEST_URL
from tests.fixtures.people_payloads import (
    CREATED_MEMBERSHIP,
    CURRENT_USER,
    EMPTY_PRINCIPAL_COLLECTION,
    MEMBERSHIP_COLLECTION,
    MEMBERSHIP_CONFLICT_ERROR,
    MEMBERSHIP_FORM_DUPLICATE,
    MEMBERSHIP_FORM_INVALID_ROLE,
    MEMBERSHIP_FORM_OK,
    MEMBERSHIP_ID,
    MEMBERSHIP_VALIDATION_ERROR,
    NOT_FOUND_ERROR,
    PRINCIPAL_COLLECTION,
    PROJECT_BY_IDENTIFIER,
    PROJECT_ID,
    PROJECT_IDENTIFIER,
    ROLE_COLLECTION,
    ROLE_COLLECTION_WITH_PERMISSIONS,
    UPDATED_MEMBERSHIP,
    USER_PRINCIPAL,
)

MEMBERSHIP_WRITE_TOOLS = {"create_membership", "update_membership", "delete_membership"}
PEOPLE_READ_TOOLS = {"search_principals", "get_user", "list_memberships", "list_roles"}
MEMBERSHIP_PATH = f"memberships/{MEMBERSHIP_ID}"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def filters_of(request: httpx.Request) -> list[dict[str, Any]]:
    return json.loads(request.url.params["filters"])


def admin_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", admin_tools=True
    )


@pytest.fixture
async def admin_client() -> AsyncIterator[Client[Any]]:
    """A client on a server that opted into the admin-gated membership writes."""
    async with Client(build_server(admin_settings())) as client:
        yield client


# --- registration and the admin gate --------------------------------------


async def test_read_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    assert set(tools) >= PEOPLE_READ_TOOLS

    search = tools["search_principals"]
    assert search.outputSchema is not None
    assert search.annotations is not None
    assert search.annotations.readOnlyHint is True
    assert search.annotations.destructiveHint is False
    assert set(search.inputSchema["properties"]) == {
        "query",
        "type",
        "member_of_project",
        "status",
        "page",
        "page_size",
    }
    assert all(
        search.inputSchema["properties"][name]["description"]
        for name in search.inputSchema["properties"]
    )

    assert set(tools["get_user"].inputSchema["properties"]) == {"id_or_me"}
    assert set(tools["list_memberships"].inputSchema["properties"]) == {
        "project_id",
        "principal_id",
        "page",
        "page_size",
    }
    assert set(tools["list_roles"].inputSchema["properties"]) == {"include_permissions"}


async def test_search_principals_advertises_itself_as_the_id_source(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    description = tools["search_principals"].description or ""
    assert "id-producing" in description
    assert "create_membership" in description
    assert "never guess" in description


async def test_admin_tag_hides_the_three_membership_writes_by_default() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", admin_tools=False
    )
    async with Client(build_server(settings)) as client:
        listed = {tool.name for tool in await client.list_tools()}

    assert not listed & MEMBERSHIP_WRITE_TOOLS
    assert listed >= PEOPLE_READ_TOOLS


async def test_admin_tools_flag_reveals_exactly_those_three(admin_client: Client[Any]) -> None:
    listed = {tool.name for tool in await admin_client.list_tools()}
    assert listed >= MEMBERSHIP_WRITE_TOOLS
    assert listed >= PEOPLE_READ_TOOLS


async def test_read_only_deployment_drops_the_membership_writes_too() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        url=TEST_URL,
        api_key="test-token",
        admin_tools=True,
        read_only=True,
    )
    async with Client(build_server(settings)) as client:
        listed = {tool.name for tool in await client.list_tools()}
    assert not listed & MEMBERSHIP_WRITE_TOOLS


async def test_membership_write_annotations_are_honest(admin_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await admin_client.list_tools()}

    create = tools["create_membership"]
    assert create.annotations is not None
    assert create.annotations.readOnlyHint is False
    assert create.annotations.destructiveHint is False
    assert create.annotations.idempotentHint is False
    assert set(create.inputSchema["properties"]) == {
        "project_id",
        "principal_id",
        "role_ids",
        "notify_message",
    }
    assert create.inputSchema["required"] == ["project_id", "principal_id", "role_ids"]
    assert "No notification is sent unless `notify_message` is given" in (create.description or "")
    assert "NO email at all" in create.inputSchema["properties"]["notify_message"]["description"]

    update = tools["update_membership"]
    assert update.annotations is not None
    assert update.annotations.idempotentHint is True

    delete = tools["delete_membership"]
    assert delete.annotations is not None
    assert delete.annotations.destructiveHint is True
    assert delete.annotations.model_extra is not None
    assert delete.annotations.model_extra["anthropic/requiresUserInteraction"] is True
    assert set(delete.inputSchema["properties"]) == {"membership_id", "confirm"}


# --- search_principals ----------------------------------------------------


async def test_query_is_sent_as_the_snake_case_name_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("principals").mock(
        return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION)
    )
    await mcp_client.call_tool("search_principals", {"query": "hopper"})

    sent = route.calls[0].request
    assert filters_of(sent) == [{"any_name_attribute": {"operator": "~", "values": ["hopper"]}}]
    assert sent.url.params["offset"] == "1"
    assert sent.url.params["pageSize"] == "20"


async def test_type_and_status_map_onto_the_api_vocabulary(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("principals").mock(
        return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION)
    )
    await mcp_client.call_tool("search_principals", {"type": "placeholder", "status": "locked"})
    assert filters_of(route.calls[0].request) == [
        {"type": {"operator": "=", "values": ["PlaceholderUser"]}},
        {"status": {"operator": "=", "values": ["locked"]}},
    ]


async def test_rows_keep_the_principal_kind_and_only_users_carry_contact_details(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("principals").mock(return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION))
    result = await mcp_client.call_tool("search_principals", {})

    assert result.structured_content is not None
    user, group, placeholder, shielded = result.structured_content["items"]

    assert user == {
        "id": 12,
        "name": "Grace Hopper",
        "type": "user",
        "email": "grace@example.test",
        "login": "ghopper",
        "status": "active",
    }
    assert group["id"] == 5
    assert group["type"] == "group"
    assert group["email"] is None and group["login"] is None and group["status"] is None
    assert placeholder["type"] == "placeholder"
    assert placeholder["id"] == 31
    assert shielded["type"] == "user"
    assert shielded["email"] is None

    assert result.structured_content["pagination"] == {
        "total": 4,
        "page": 1,
        "page_size": 20,
        "has_more": False,
    }
    assert any("allowed to see" in note for note in result.structured_content["notes"])


async def test_member_of_project_identifier_is_resolved_to_a_numeric_id(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    lookup = mock_api.get(f"projects/{PROJECT_IDENTIFIER}").mock(
        return_value=httpx.Response(200, json=PROJECT_BY_IDENTIFIER)
    )
    route = mock_api.get("principals").mock(
        return_value=httpx.Response(200, json=EMPTY_PRINCIPAL_COLLECTION)
    )

    result = await mcp_client.call_tool(
        "search_principals", {"member_of_project": PROJECT_IDENTIFIER, "type": "user"}
    )
    assert result.structured_content is not None
    assert result.structured_content["items"] == []
    assert result.structured_content["pagination"]["has_more"] is False
    assert lookup.call_count == 1
    assert filters_of(route.calls[0].request) == [
        {"type": {"operator": "=", "values": ["User"]}},
        {"member": {"operator": "=", "values": [str(PROJECT_ID)]}},
    ]


async def test_search_page_size_out_of_range_is_rejected_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("principals").mock(
        return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION)
    )
    result = await mcp_client.call_tool(
        "search_principals", {"page_size": 500}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "page_size" in error["message"]
    assert route.call_count == 0


# --- get_user -------------------------------------------------------------


async def test_me_is_passed_through_and_the_avatar_is_dropped(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("users/me").mock(return_value=httpx.Response(200, json=CURRENT_USER))
    result = await mcp_client.call_tool("get_user", {"id_or_me": "me"})

    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content == {
        "id": 1,
        "name": "Ada Lovelace",
        "login": "ada",
        "email": "ada@example.test",
        "admin": True,
        "status": "active",
        "language": "en",
        "created_at": "2024-11-11T10:00:00Z",
        "updated_at": "2026-07-01T09:00:00Z",
    }
    assert "avatar" not in result.structured_content


async def test_numeric_id_is_fetched_by_path(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("users/12").mock(return_value=httpx.Response(200, json=USER_PRINCIPAL))
    result = await mcp_client.call_tool("get_user", {"id_or_me": 12})
    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["id"] == 12
    assert result.structured_content["admin"] is False


async def test_a_name_is_refused_before_any_request(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("users/Grace%20Hopper").mock(
        return_value=httpx.Response(200, json=USER_PRINCIPAL)
    )
    result = await mcp_client.call_tool(
        "get_user", {"id_or_me": "Grace Hopper"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "search_principals" in error["hint"]
    assert route.call_count == 0


async def test_unknown_user_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("users/9999").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))
    result = await mcp_client.call_tool("get_user", {"id_or_me": 9999}, raise_on_error=False)

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "ids come from" in error["hint"]


# --- list_memberships -----------------------------------------------------


async def test_memberships_are_projected_with_principal_kinds_and_roles(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("memberships").mock(return_value=httpx.Response(200, json=MEMBERSHIP_COLLECTION))
    result = await mcp_client.call_tool("list_memberships", {})

    assert result.structured_content is not None
    user_row, group_row = result.structured_content["items"]

    assert user_row["id"] == MEMBERSHIP_ID
    assert user_row["project"] == {"id": PROJECT_ID, "name": "Demo project"}
    assert user_row["principal"] == {"id": 12, "name": "Grace Hopper", "type": "user"}
    assert user_row["roles"] == [{"id": 3, "name": "Member"}]
    assert user_row["created_at"] == "2026-03-02T12:00:00Z"

    assert group_row["principal"]["type"] == "group"
    assert [role["id"] for role in group_row["roles"]] == [3, 4]

    assert result.structured_content["pagination"] == {
        "total": 2,
        "page": 1,
        "page_size": 20,
        "has_more": False,
    }


async def test_membership_filters_are_serialized_with_resolved_ids(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"projects/{PROJECT_IDENTIFIER}").mock(
        return_value=httpx.Response(200, json=PROJECT_BY_IDENTIFIER)
    )
    route = mock_api.get("memberships").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_COLLECTION)
    )
    await mcp_client.call_tool(
        "list_memberships", {"project_id": PROJECT_IDENTIFIER, "principal_id": 12}
    )
    assert filters_of(route.calls[0].request) == [
        {"project": {"operator": "=", "values": [str(PROJECT_ID)]}},
        {"principal": {"operator": "=", "values": ["12"]}},
    ]


# --- create_membership ----------------------------------------------------


async def test_grant_goes_through_the_form_and_sends_no_email_by_default(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    lookup = mock_api.get("principals").mock(
        return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION)
    )
    form = mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    create = mock_api.post("memberships").mock(
        return_value=httpx.Response(201, json=CREATED_MEMBERSHIP)
    )

    result = await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_ID, "principal_id": 12, "role_ids": [3]},
    )

    # The principal kind is read first: the membership form rejects the generic
    # /principals/{id} spelling (ResourceTypeMismatch on 16.6), so the payload
    # must carry the concrete /users/{id} href.
    assert lookup.call_count == 1
    assert filters_of(lookup.calls[0].request) == [{"id": {"operator": "=", "values": ["12"]}}]
    assert form.call_count == 1
    assert create.call_count == 1
    body = json.loads(create.calls[0].request.content)
    assert body["_links"] == {
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}"},
        "principal": {"href": "/api/v3/users/12"},
        "roles": [{"href": "/api/v3/roles/3"}],
    }
    assert body["_meta"] == {"sendNotifications": False}
    assert json.loads(form.calls[0].request.content) == body

    assert result.structured_content is not None
    assert result.structured_content["id"] == 77
    assert result.structured_content["principal"]["type"] == "user"
    assert any("No invitation email" in note for note in result.structured_content["notes"])


async def test_group_principal_gets_a_groups_href(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("principals").mock(return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION))
    mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    create = mock_api.post("memberships").mock(
        return_value=httpx.Response(201, json=CREATED_MEMBERSHIP)
    )

    await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_ID, "principal_id": 5, "role_ids": [3]},
    )
    body = json.loads(create.calls[0].request.content)
    assert body["_links"]["principal"] == {"href": "/api/v3/groups/5"}


async def test_unknown_principal_fails_before_the_form(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("principals").mock(
        return_value=httpx.Response(200, json=EMPTY_PRINCIPAL_COLLECTION)
    )
    form = mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )

    result = await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_ID, "principal_id": 999, "role_ids": [3]},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "search_principals" in error["hint"]
    assert form.call_count == 0


async def test_notify_message_opts_into_the_invitation_email(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("principals").mock(return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION))
    mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    create = mock_api.post("memberships").mock(
        return_value=httpx.Response(201, json=CREATED_MEMBERSHIP)
    )

    result = await admin_client.call_tool(
        "create_membership",
        {
            "project_id": PROJECT_ID,
            "principal_id": 12,
            "role_ids": [3, 3, 4],
            "notify_message": "Welcome aboard.",
        },
    )

    body = json.loads(create.calls[0].request.content)
    assert body["_meta"] == {
        "sendNotifications": True,
        "notificationMessage": {"raw": "Welcome aboard."},
    }
    # Duplicate role ids are collapsed before they reach the API.
    assert body["_links"]["roles"] == [
        {"href": "/api/v3/roles/3"},
        {"href": "/api/v3/roles/4"},
    ]
    assert result.structured_content is not None
    assert result.structured_content["notes"] is None


async def test_project_identifier_is_resolved_before_the_grant(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    lookup = mock_api.get(f"projects/{PROJECT_IDENTIFIER}").mock(
        return_value=httpx.Response(200, json=PROJECT_BY_IDENTIFIER)
    )
    mock_api.get("principals").mock(return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION))
    mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    create = mock_api.post("memberships").mock(
        return_value=httpx.Response(201, json=CREATED_MEMBERSHIP)
    )

    await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_IDENTIFIER, "principal_id": 12, "role_ids": [3]},
    )
    assert lookup.call_count == 1
    body = json.loads(create.calls[0].request.content)
    assert body["_links"]["project"] == {"href": f"/api/v3/projects/{PROJECT_ID}"}


async def test_form_validation_errors_block_the_write_and_list_allowed_roles(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("principals").mock(return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION))
    mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_INVALID_ROLE)
    )
    create = mock_api.post("memberships").mock(
        return_value=httpx.Response(201, json=CREATED_MEMBERSHIP)
    )

    result = await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_ID, "principal_id": 12, "role_ids": [999]},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "roles", "message": "Roles is not set to one of the allowed values."}
    ]
    assert "Member, Reader" in error["hint"]
    assert create.call_count == 0, "the membership must not be created after a failed form"


async def test_duplicate_membership_is_reported_from_the_form(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("principals").mock(return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION))
    mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_DUPLICATE)
    )
    create = mock_api.post("memberships").mock(
        return_value=httpx.Response(201, json=CREATED_MEMBERSHIP)
    )

    result = await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_ID, "principal_id": 12, "role_ids": [3]},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["violations"] == [
        {"attribute": "principal", "message": "User has already been taken."}
    ]
    assert "update_membership" in error["hint"]
    assert create.call_count == 0


async def test_upstream_422_carries_its_violations(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("principals").mock(return_value=httpx.Response(200, json=PRINCIPAL_COLLECTION))
    mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    mock_api.post("memberships").mock(
        return_value=httpx.Response(422, json=MEMBERSHIP_VALIDATION_ERROR)
    )

    result = await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_ID, "principal_id": 12, "role_ids": [3]},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "roles", "message": "Roles is not set to one of the allowed values."}
    ]
    assert "violations" in error["hint"]


async def test_empty_role_ids_never_reach_the_api(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("memberships/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    result = await admin_client.call_tool(
        "create_membership",
        {"project_id": PROJECT_ID, "principal_id": 12, "role_ids": []},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "list_roles" in error["hint"]
    assert form.call_count == 0


# --- update_membership ----------------------------------------------------


async def test_roles_are_replaced_through_the_form_then_patched(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{MEMBERSHIP_PATH}/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    patch = mock_api.patch(MEMBERSHIP_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_MEMBERSHIP)
    )

    result = await admin_client.call_tool(
        "update_membership", {"membership_id": MEMBERSHIP_ID, "role_ids": [4, 9]}
    )

    assert form.call_count == 1
    body = json.loads(patch.calls[0].request.content)
    assert body["_links"] == {"roles": [{"href": "/api/v3/roles/4"}, {"href": "/api/v3/roles/9"}]}
    assert body["_meta"] == {"sendNotifications": False}
    assert "lockVersion" not in body

    assert result.structured_content is not None
    assert [role["id"] for role in result.structured_content["roles"]] == [4, 9]


async def test_patch_conflict_is_reported_as_a_conflict(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{MEMBERSHIP_PATH}/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    mock_api.patch(MEMBERSHIP_PATH).mock(
        return_value=httpx.Response(409, json=MEMBERSHIP_CONFLICT_ERROR)
    )

    result = await admin_client.call_tool(
        "update_membership",
        {"membership_id": MEMBERSHIP_ID, "role_ids": [4]},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert "updated by somebody else" in error["message"]
    assert error["hint"]


async def test_update_refuses_an_empty_role_set(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{MEMBERSHIP_PATH}/form").mock(
        return_value=httpx.Response(200, json=MEMBERSHIP_FORM_OK)
    )
    result = await admin_client.call_tool(
        "update_membership",
        {"membership_id": MEMBERSHIP_ID, "role_ids": []},
        raise_on_error=False,
    )
    assert error_of(result)["type"] == "invalid_input"
    assert form.call_count == 0


# --- delete_membership ----------------------------------------------------


async def test_revoking_without_confirmation_deletes_nothing(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(MEMBERSHIP_PATH).mock(return_value=httpx.Response(204))
    result = await admin_client.call_tool(
        "delete_membership", {"membership_id": MEMBERSHIP_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert route.call_count == 0


async def test_confirmed_revocation_calls_delete(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(MEMBERSHIP_PATH).mock(return_value=httpx.Response(204))
    result = await admin_client.call_tool(
        "delete_membership", {"membership_id": MEMBERSHIP_ID, "confirm": True}
    )
    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["deleted"] is True
    assert result.structured_content["id"] == MEMBERSHIP_ID


async def test_revoking_an_unknown_membership_returns_not_found(
    admin_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete("memberships/4321").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))
    result = await admin_client.call_tool(
        "delete_membership",
        {"membership_id": 4321, "confirm": True},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404


# --- list_roles -----------------------------------------------------------


async def test_roles_are_fetched_in_full_with_has_more_false(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("roles").mock(return_value=httpx.Response(200, json=ROLE_COLLECTION))
    result = await mcp_client.call_tool("list_roles", {})

    assert route.calls[0].request.url.params["pageSize"] == "100"
    assert result.structured_content is not None
    assert result.structured_content["items"] == [
        {"id": 3, "name": "Member", "permissions": None},
        {"id": 4, "name": "Reader", "permissions": None},
        {"id": 9, "name": "Project admin", "permissions": None},
    ]
    assert result.structured_content["pagination"] == {
        "total": 3,
        "page": 1,
        "page_size": 3,
        "has_more": False,
    }
    assert result.structured_content["notes"] is None


async def test_include_permissions_adds_the_arrays_and_a_cost_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("roles").mock(
        return_value=httpx.Response(200, json=ROLE_COLLECTION_WITH_PERMISSIONS)
    )
    result = await mcp_client.call_tool("list_roles", {"include_permissions": True})

    assert result.structured_content is not None
    assert result.structured_content["items"][0]["permissions"] == [
        "view_work_packages",
        "edit_work_packages",
        "add_work_packages",
    ]
    assert any("permission arrays are long" in note for note in result.structured_content["notes"])


async def test_missing_permission_arrays_are_admitted_not_faked(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("roles").mock(return_value=httpx.Response(200, json=ROLE_COLLECTION))
    result = await mcp_client.call_tool("list_roles", {"include_permissions": True})

    assert result.structured_content is not None
    assert all(item["permissions"] is None for item in result.structured_content["items"])
    assert any("does not expose" in note for note in result.structured_content["notes"])


async def test_roles_endpoint_failure_surfaces_the_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("roles").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))
    result = await mcp_client.call_tool("list_roles", {}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
