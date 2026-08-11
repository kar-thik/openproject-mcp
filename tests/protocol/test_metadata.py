"""Protocol tests for the instance, metadata and schema tools (SPEC §6.1, §6.12).

Covers the connection test and its auth failure path, global vs project-scoped
metadata (including time-entry activities discovered from the form, never
hardcoded — G3), the metadata cache and its ``refresh`` bust, and the
custom-field read shape of §6.2.1.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.conftest import API_BASE
from tests.fixtures.hal_payloads import API_ROOT, MULTIPLE_ERRORS, WORK_PACKAGE_SCHEMA
from tests.fixtures.projects_metadata_payloads import (
    CATEGORY_COLLECTION,
    CONFIGURATION,
    CURRENT_USER,
    PRIORITY_COLLECTION,
    PROJECT_TYPE_COLLECTION,
    ROLE_COLLECTION,
    STATUS_COLLECTION,
    TIME_ENTRY_FORM,
    TYPE_COLLECTION,
    VERSION_COLLECTION,
)


def error_envelope(result: Any) -> dict[str, Any]:
    """The parsed ``{"error": {...}}`` payload of a failed tool call."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def mock_instance_endpoints(mock_api: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "root": mock_api.get(url=f"{API_BASE}/").mock(
            return_value=httpx.Response(200, json=API_ROOT)
        ),
        "configuration": mock_api.get("configuration").mock(
            return_value=httpx.Response(200, json=CONFIGURATION)
        ),
        "me": mock_api.get("users/me").mock(return_value=httpx.Response(200, json=CURRENT_USER)),
        "time_entries": mock_api.get("time_entries").mock(
            return_value=httpx.Response(200, json={"total": 0})
        ),
    }


def mock_global_metadata(mock_api: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "types": mock_api.get("types").mock(return_value=httpx.Response(200, json=TYPE_COLLECTION)),
        "statuses": mock_api.get("statuses").mock(
            return_value=httpx.Response(200, json=STATUS_COLLECTION)
        ),
        "priorities": mock_api.get("priorities").mock(
            return_value=httpx.Response(200, json=PRIORITY_COLLECTION)
        ),
        "roles": mock_api.get("roles").mock(return_value=httpx.Response(200, json=ROLE_COLLECTION)),
    }


def mock_project_metadata(mock_api: respx.MockRouter) -> dict[str, respx.Route]:
    routes = mock_global_metadata(mock_api)
    routes["project_types"] = mock_api.get("projects/7/types").mock(
        return_value=httpx.Response(200, json=PROJECT_TYPE_COLLECTION)
    )
    routes["versions"] = mock_api.get("projects/7/versions").mock(
        return_value=httpx.Response(200, json=VERSION_COLLECTION)
    )
    routes["categories"] = mock_api.get("projects/7/categories").mock(
        return_value=httpx.Response(200, json=CATEGORY_COLLECTION)
    )
    routes["form"] = mock_api.post("time_entries/form").mock(
        return_value=httpx.Response(200, json=TIME_ENTRY_FORM)
    )
    return routes


async def test_metadata_tools_are_registered_as_reads(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    for name in ("get_instance_info", "get_project_metadata", "get_work_package_schema"):
        tool = tools[name]
        assert tool.outputSchema is not None
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.description


async def test_get_instance_info_reports_version_limits_user_and_features(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_instance_endpoints(mock_api)

    result = await mcp_client.call_tool("get_instance_info", {})

    structured = result.structured_content
    assert structured is not None
    assert structured["core_version"] == "17.7.1"
    assert structured["instance_name"] == "Test OpenProject"
    assert structured["maximum_attachment_file_size_bytes"] == 5242880
    assert structured["per_page_options"] == [20, 100]
    assert structured["current_user"] == {
        "id": 1,
        "name": "Ada Lovelace",
        "login": "ada",
        "admin": True,
        "email": "ada@openproject.test",
    }
    features = structured["features"]
    assert features["supports_internal_comments"] is True
    assert features["supports_project_favorites"] is True
    assert features["time_entry_work_package_filter"] == "entityId"


async def test_get_instance_info_reauthenticates_on_every_call(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_instance_endpoints(mock_api)

    await mcp_client.call_tool("get_instance_info", {})
    await mcp_client.call_tool("get_instance_info", {})

    assert routes["root"].call_count == 2
    assert routes["me"].call_count == 2
    assert routes["configuration"].call_count == 1


async def test_get_instance_info_401_names_the_credential_to_fix(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(url=f"{API_BASE}/").mock(
        return_value=httpx.Response(
            401, json={"message": "You did not provide the correct credentials."}
        )
    )

    result = await mcp_client.call_tool("get_instance_info", {}, raise_on_error=False)

    error = error_envelope(result)
    assert error["type"] == "authentication_failed"
    assert error["http_status"] == 401
    assert "OPENPROJECT_API_KEY" in error["hint"]


async def test_get_instance_info_network_failure_is_actionable(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(url=f"{API_BASE}/").mock(side_effect=httpx.ConnectError("dns failure"))

    result = await mcp_client.call_tool("get_instance_info", {}, raise_on_error=False)

    error = error_envelope(result)
    assert error["type"] == "network_error"
    assert "OPENPROJECT_URL" in error["hint"]


async def test_global_metadata_never_touches_project_endpoints(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_project_metadata(mock_api)

    result = await mcp_client.call_tool("get_project_metadata", {})

    structured = result.structured_content
    assert structured is not None
    assert structured["project_id"] is None
    assert structured["versions"] is None
    assert structured["categories"] is None
    assert structured["time_entry_activities"] is None
    assert [row["name"] for row in structured["types"]] == ["Task", "Bug", "Milestone"]
    # 17.7+ variant fields surface when the instance sends them, stay null elsewhere.
    assert structured["types"][0]["own_name"] is None
    assert structured["types"][0]["parent"] is None
    assert structured["types"][1]["own_name"] == "Bug"
    assert structured["types"][1]["parent"] == {"id": 1, "name": "Task"}
    assert [row["is_closed"] for row in structured["statuses"]] == [False, False, True]
    assert structured["statuses"][2] == {
        "id": 12,
        "name": "Closed",
        "is_closed": True,
        "is_default": False,
    }
    assert structured["priorities"][0]["is_default"] is True
    assert structured["roles"] == [{"id": 3, "name": "Member"}, {"id": 4, "name": "Project admin"}]

    assert routes["types"].call_count == 1
    assert routes["project_types"].call_count == 0
    assert routes["versions"].call_count == 0
    assert routes["form"].call_count == 0


async def test_project_metadata_adds_scoped_sets_and_form_discovered_activities(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_project_metadata(mock_api)

    result = await mcp_client.call_tool("get_project_metadata", {"project_id": 7})

    structured = result.structured_content
    assert structured is not None
    assert structured["project_id"] == 7
    assert [row["name"] for row in structured["types"]] == ["Task"]
    assert structured["versions"] == [
        {
            "id": 3,
            "name": "Sprint 4",
            "status": "open",
            "start_date": "2026-07-06",
            "end_date": "2026-07-24",
            "sharing": "none",
        }
    ]
    assert structured["categories"] == [
        {"id": 5, "name": "Backend", "default_assignee": {"id": 12, "name": "Grace Hopper"}}
    ]
    assert structured["time_entry_activities"] == [
        {"id": 3, "name": "Development", "is_default": None},
        {"id": 4, "name": "Management", "is_default": None},
    ]
    assert structured["notes"] is None
    assert routes["types"].call_count == 0

    form_body = json.loads(routes["form"].calls[0].request.content)
    assert form_body["_links"]["project"] == {"href": "/api/v3/projects/7"}


async def test_time_entry_activities_degrade_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_project_metadata(mock_api)
    routes["form"].mock(
        return_value=httpx.Response(
            403, json={"message": "You are not authorized to access this resource."}
        )
    )

    result = await mcp_client.call_tool("get_project_metadata", {"project_id": 7})

    structured = result.structured_content
    assert structured is not None
    assert structured["time_entry_activities"] == []
    assert structured["notes"] is not None
    assert "time-entry activities unavailable" in structured["notes"][0]
    assert structured["versions"] is not None


async def test_project_metadata_is_cached_and_refresh_busts_it(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_project_metadata(mock_api)

    await mcp_client.call_tool("get_project_metadata", {"project_id": 7})
    await mcp_client.call_tool("get_project_metadata", {"project_id": 7})

    assert routes["statuses"].call_count == 1
    assert routes["project_types"].call_count == 1
    assert routes["form"].call_count == 1

    await mcp_client.call_tool("get_project_metadata", {"project_id": 7, "refresh": True})

    assert routes["statuses"].call_count == 2
    assert routes["project_types"].call_count == 2
    assert routes["form"].call_count == 2


async def test_project_metadata_404_carries_the_status_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_global_metadata(mock_api)
    mock_api.get("projects/999/types").mock(
        return_value=httpx.Response(
            404, json={"message": "The requested resource could not be found."}
        )
    )

    result = await mcp_client.call_tool(
        "get_project_metadata", {"project_id": 999}, raise_on_error=False
    )

    error = error_envelope(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["hint"]


async def test_project_metadata_surfaces_violations_from_a_422(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("statuses").mock(return_value=httpx.Response(422, json=MULTIPLE_ERRORS))

    result = await mcp_client.call_tool("get_project_metadata", {}, raise_on_error=False)

    error = error_envelope(result)
    assert error["type"] == "validation_failed"
    assert error["violations"][0] == {"attribute": "subject", "message": "Subject can't be blank."}


async def test_work_package_schema_exposes_fields_and_custom_fields(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("work_packages/schemas/7-1").mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA)
    )

    result = await mcp_client.call_tool("get_work_package_schema", {"project_id": 7, "type_id": 1})

    structured = result.structured_content
    assert structured is not None
    assert structured["project_id"] == 7
    assert structured["type_id"] == 1
    assert structured["required_fields"] == ["subject"]
    assert structured["fields"] == [
        {
            "key": "subject",
            "name": "Subject",
            "type": "String",
            "required": True,
            "writable": True,
            "has_default": None,
            "allowed_values": None,
        }
    ]

    custom_fields = {row["key"]: row for row in structured["custom_fields"]}
    # Custom-field types speak the same vocabulary get_work_package reports.
    assert custom_fields["customField12"] == {
        "key": "customField12",
        "name": "Severity",
        "type": "list",
        "required": False,
        "writable": True,
        "options": [{"id": 4, "name": "High"}, {"id": 5, "name": "Low"}],
    }
    assert custom_fields["customField9"]["type"] == "user"
    assert custom_fields["customField9"]["options"] == [
        {"id": 12, "name": "Grace Hopper"},
        {"id": 13, "name": "Alan Turing"},
    ]
    assert custom_fields["customField7"]["options"] is None
    assert custom_fields["customField20"]["writable"] is False
    assert route.call_count == 1


async def test_work_package_schema_is_cached_and_refresh_busts_it(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("work_packages/schemas/7-1").mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_SCHEMA)
    )

    await mcp_client.call_tool("get_work_package_schema", {"project_id": 7, "type_id": 1})
    await mcp_client.call_tool("get_work_package_schema", {"project_id": 7, "type_id": 1})
    assert route.call_count == 1

    await mcp_client.call_tool(
        "get_work_package_schema", {"project_id": 7, "type_id": 1, "refresh": True}
    )
    assert route.call_count == 2


async def test_work_package_schema_404_points_at_get_project_metadata(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/schemas/7-99").mock(
        return_value=httpx.Response(
            404, json={"message": "The requested resource could not be found."}
        )
    )

    result = await mcp_client.call_tool(
        "get_work_package_schema", {"project_id": 7, "type_id": 99}, raise_on_error=False
    )

    error = error_envelope(result)
    assert error["type"] == "not_found"
    assert "get_project_metadata" in error["hint"]
