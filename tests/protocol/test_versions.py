"""Protocol tests for the version and sprint tools (SPEC §6.10, §4.5, §9.3, G1/G2/G5).

The regression this module is named after is asserted twice: ``end_date`` must
reach the wire as ``endDate`` on create AND on update (the old server's tool and
client disagreed and the value vanished). The rest covers the form-first flow,
the fetched-in-full envelope, the backlogs degradation note, the KEEP/clear
semantics of the update parameters, and the structured error envelopes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.projects_versions_payloads import (
    CREATED_VERSION,
    SHARED_VERSION,
    SHARED_VERSION_ID,
    SPRINT_ONLY,
    SPRINT_ONLY_ID,
    UPDATED_VERSION,
    VERSION,
    VERSION_FORM,
    VERSION_FORM_DUPLICATE_NAME,
    VERSION_ID,
    VERSION_IN_USE_ERROR,
    VERSION_NOT_FOUND,
    VERSION_UPDATE_FORM,
    version_collection,
)

PROJECT_ID = 7
VERSION_PATH = f"versions/{VERSION_ID}"


def error_of(result: Any) -> dict[str, Any]:
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


# --- registration ---------------------------------------------------------


async def test_all_four_version_tools_are_registered(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_versions"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert set(listing.inputSchema["properties"]) == {"project_id", "include_sprints"}

    creating = tools["create_version"]
    assert creating.annotations is not None
    assert creating.annotations.readOnlyHint is False
    assert creating.annotations.destructiveHint is False
    assert set(creating.inputSchema["properties"]) == {
        "project_id",
        "name",
        "start_date",
        "end_date",
        "description",
        "status",
        "sharing",
    }
    assert creating.inputSchema["properties"]["status"]["anyOf"][0]["enum"] == [
        "open",
        "locked",
        "closed",
    ]

    updating = tools["update_version"]
    assert set(updating.inputSchema["properties"]) == {
        "version_id",
        "name",
        "start_date",
        "end_date",
        "description",
        "status",
        "sharing",
    }

    deleting = tools["delete_version"]
    assert deleting.annotations is not None
    assert deleting.annotations.destructiveHint is True
    assert deleting.annotations.model_extra is not None
    assert deleting.annotations.model_extra["anthropic/requiresUserInteraction"] is True
    assert set((deleting.meta or {})["fastmcp"]["tags"]) == {"versions", "write", "destructive"}


async def test_descriptions_name_the_end_date_field_and_the_backlogs_degradation(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    assert "endDate" in (tools["create_version"].description or "")
    assert "endDate" in (tools["update_version"].description or "")
    listing = tools["list_versions"].description or ""
    assert "backlogs" in listing
    assert (
        "does not fail"
        in (tools["list_versions"].inputSchema["properties"]["include_sprints"]["description"])
    )
    assert "update_version(status='closed')" in (tools["delete_version"].description or "")


# --- listing --------------------------------------------------------------


async def test_instance_wide_listing_asks_for_one_full_page_and_admits_the_cut(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("versions").mock(
        return_value=httpx.Response(
            200,
            json=version_collection([VERSION, SHARED_VERSION], total=137, page_size=100, offset=1),
        )
    )

    result = await mcp_client.call_tool("list_versions", {})

    assert route.calls[0].request.url.params["pageSize"] == "100"
    assert result.structured_content is not None
    content = result.structured_content
    assert content["pagination"] == {
        "total": 137,
        "page": 1,
        "page_size": 100,
        "has_more": True,
    }
    assert any("137 versions exist" in note for note in content["notes"])
    assert content["items"][0] == {
        "id": VERSION_ID,
        "name": "Sprint 12",
        "project": {"id": 7, "name": "Demo project"},
        "status": "open",
        "start_date": "2026-08-01",
        "end_date": "2026-08-14",
        "description": "Hardening sprint.",
        "sharing": "none",
        "source": "version",
    }
    shared = content["items"][1]
    assert shared["project"] == {"id": 3, "name": "Customer work"}
    assert shared["status"] == "closed"
    assert shared["start_date"] is None


async def test_project_listing_is_fetched_in_full(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"projects/{PROJECT_ID}/versions").mock(
        return_value=httpx.Response(200, json=version_collection([VERSION, SHARED_VERSION]))
    )

    result = await mcp_client.call_tool("list_versions", {"project_id": PROJECT_ID})

    assert result.structured_content is not None
    assert result.structured_content["pagination"] == {
        "total": 2,
        "page": 1,
        "page_size": 2,
        "has_more": False,
    }
    assert result.structured_content["notes"] is None


async def test_include_sprints_without_a_project_is_rejected_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("versions").mock(
        return_value=httpx.Response(200, json=version_collection([VERSION]))
    )
    result = await mcp_client.call_tool(
        "list_versions", {"include_sprints": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "project_id" in error["message"]
    assert "/sprints" in error["hint"]
    assert route.call_count == 0


async def test_sprints_are_merged_with_a_source_marker_and_never_duplicated(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"projects/{PROJECT_ID}/versions").mock(
        return_value=httpx.Response(200, json=version_collection([VERSION, SHARED_VERSION]))
    )
    mock_api.get(f"projects/{PROJECT_ID}/sprints").mock(
        return_value=httpx.Response(200, json=version_collection([VERSION, SPRINT_ONLY]))
    )

    result = await mcp_client.call_tool(
        "list_versions", {"project_id": PROJECT_ID, "include_sprints": True}
    )

    assert result.structured_content is not None
    items = result.structured_content["items"]
    assert [(item["id"], item["source"]) for item in items] == [
        (VERSION_ID, "sprint"),
        (SHARED_VERSION_ID, "version"),
        (SPRINT_ONLY_ID, "sprint"),
    ]
    assert result.structured_content["pagination"]["total"] == 3


async def test_a_missing_backlogs_module_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"projects/{PROJECT_ID}/versions").mock(
        return_value=httpx.Response(200, json=version_collection([VERSION]))
    )
    mock_api.get(f"projects/{PROJECT_ID}/sprints").mock(
        return_value=httpx.Response(404, json=VERSION_NOT_FOUND)
    )

    result = await mcp_client.call_tool(
        "list_versions", {"project_id": PROJECT_ID, "include_sprints": True}
    )

    assert result.structured_content is not None
    assert len(result.structured_content["items"]) == 1
    assert any("backlogs module" in note for note in result.structured_content["notes"])


# --- create ---------------------------------------------------------------


async def test_create_version_sends_end_date_as_endDate_through_the_form_flow(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("versions/form").mock(return_value=httpx.Response(200, json=VERSION_FORM))
    create = mock_api.post("versions").mock(return_value=httpx.Response(201, json=CREATED_VERSION))

    result = await mcp_client.call_tool(
        "create_version",
        {
            "project_id": PROJECT_ID,
            "name": "Sprint 14",
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "description": "Payments hardening.",
            "sharing": "descendants",
        },
    )

    assert form.call_count == 1
    sent = body_of(form)
    # The regression this tool exists for: end_date must land as endDate.
    assert sent["endDate"] == "2026-09-30"
    assert sent["startDate"] == "2026-09-01"
    assert "effectiveDate" not in sent
    assert "dueDate" not in sent
    assert sent["description"] == {"format": "markdown", "raw": "Payments hardening."}
    assert sent["sharing"] == "descendants"
    assert sent["_links"] == {"definingProject": {"href": "/api/v3/projects/7"}}

    committed = body_of(create)
    assert committed["endDate"] == "2026-09-30"
    # The form's default status is kept, our sharing wins over the form's echo.
    assert committed["status"] == "open"
    assert committed["sharing"] == "descendants"

    assert result.structured_content is not None
    assert result.structured_content["id"] == 61
    assert result.structured_content["end_date"] == "2026-09-30"
    assert result.structured_content["project"] == {"id": 7, "name": "Demo project"}
    assert result.structured_content["created_at"] == "2026-07-26T09:10:00Z"


async def test_create_version_surfaces_form_violations_and_never_commits(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("versions/form").mock(
        return_value=httpx.Response(200, json=VERSION_FORM_DUPLICATE_NAME)
    )
    create = mock_api.post("versions").mock(return_value=httpx.Response(201, json=CREATED_VERSION))

    result = await mcp_client.call_tool(
        "create_version",
        {"project_id": PROJECT_ID, "name": "Sprint 12"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [{"attribute": "name", "message": "Name has already been taken."}]
    assert "unique within the defining project" in error["hint"]
    assert create.call_count == 0


async def test_create_version_rejects_a_malformed_date_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("versions/form").mock(return_value=httpx.Response(200, json=VERSION_FORM))
    result = await mcp_client.call_tool(
        "create_version",
        {"project_id": PROJECT_ID, "name": "Sprint 14", "end_date": "30-09-2026"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "end_date" in error["message"]
    assert "YYYY-MM-DD" in error["hint"]
    assert form.call_count == 0


# --- update ---------------------------------------------------------------


async def test_update_version_patches_without_a_lock_version_and_can_clear_the_end_date(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{VERSION_PATH}/form").mock(
        return_value=httpx.Response(200, json=VERSION_UPDATE_FORM)
    )
    patch = mock_api.patch(VERSION_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_VERSION)
    )
    read = mock_api.get(VERSION_PATH).mock(return_value=httpx.Response(200, json=VERSION))

    result = await mcp_client.call_tool(
        "update_version",
        {"version_id": VERSION_ID, "status": "closed", "end_date": None},
    )

    assert form.call_count == 1
    assert body_of(form) == {"status": "closed", "endDate": None}
    assert body_of(patch) == {"status": "closed", "endDate": None}
    assert "lockVersion" not in body_of(patch)
    assert read.call_count == 0

    assert result.structured_content is not None
    assert result.structured_content["status"] == "closed"
    assert result.structured_content["end_date"] is None


async def test_update_version_leaves_untouched_dates_out_of_the_payload(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{VERSION_PATH}/form").mock(
        return_value=httpx.Response(200, json=VERSION_UPDATE_FORM)
    )
    patch = mock_api.patch(VERSION_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_VERSION)
    )

    await mcp_client.call_tool(
        "update_version", {"version_id": VERSION_ID, "name": "Sprint 12 (extended)"}
    )
    assert body_of(patch) == {"name": "Sprint 12 (extended)"}


async def test_update_version_with_nothing_to_change_never_calls_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{VERSION_PATH}/form").mock(
        return_value=httpx.Response(200, json=VERSION_UPDATE_FORM)
    )
    result = await mcp_client.call_tool(
        "update_version", {"version_id": VERSION_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to change" in error["message"]
    assert form.call_count == 0


async def test_update_version_reports_an_unknown_id_as_not_found(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("versions/9999/form").mock(
        return_value=httpx.Response(404, json=VERSION_NOT_FOUND)
    )
    result = await mcp_client.call_tool(
        "update_version", {"version_id": 9999, "status": "closed"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "list_versions" in error["hint"]


# --- delete ---------------------------------------------------------------


async def test_delete_version_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(VERSION_PATH).mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool(
        "delete_version", {"version_id": VERSION_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert route.call_count == 0


async def test_delete_version_confirms_the_deletion(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(VERSION_PATH).mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool(
        "delete_version", {"version_id": VERSION_ID, "confirm": True}
    )
    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content == {
        "id": VERSION_ID,
        "deleted": True,
        "message": f"Version {VERSION_ID} was deleted; work packages that used it now have none.",
    }


async def test_a_version_still_in_use_fails_with_an_actionable_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(VERSION_PATH).mock(return_value=httpx.Response(422, json=VERSION_IN_USE_ERROR))
    result = await mcp_client.call_tool(
        "delete_version", {"version_id": VERSION_ID, "confirm": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {
            "attribute": "base",
            "message": "Version cannot be deleted because it is in use by work packages.",
        }
    ]
    assert "list_work_packages" in error["hint"]
    assert "update_version(status='closed')" in error["hint"]
    assert str(VERSION_ID) in error["hint"]


async def test_delete_version_reports_an_unknown_id_as_not_found(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete("versions/9999").mock(return_value=httpx.Response(404, json=VERSION_NOT_FOUND))
    result = await mcp_client.call_tool(
        "delete_version", {"version_id": 9999, "confirm": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "not from a sprint number" in error["hint"]
