"""Protocol tests for the Phase 2 project writes (SPEC §6.6, §4.5, G1/G2/G4).

Everything runs through the in-memory FastMCP client against a respx-mocked
instance, so these assert what a model actually receives: the form-first flow,
the exact wire payloads (``_links.status`` as a project-status href, a
``{"href": null}`` parent detach, no fabricated ``lockVersion``), the *scheduled*
— never "gone" — deletion result, and the structured error envelopes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.projects_versions_payloads import (
    CREATED_PROJECT,
    DELETE_JOB_ACCEPTED,
    JOB_ID,
    PROJECT_CONFLICT,
    PROJECT_CREATE_VALIDATION_ERROR,
    PROJECT_FORM,
    PROJECT_FORM_IDENTIFIER_TAKEN,
    PROJECT_ID,
    PROJECT_NOT_FOUND,
    PROJECT_UPDATE_FORM,
    UPDATED_PROJECT,
)

PROJECT_PATH = f"projects/{PROJECT_ID}"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


# --- registration ---------------------------------------------------------


async def test_the_three_write_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    creating = tools["create_project"]
    assert creating.outputSchema is not None
    assert creating.annotations is not None
    assert creating.annotations.readOnlyHint is False
    assert creating.annotations.destructiveHint is False
    assert creating.annotations.idempotentHint is False
    assert set(creating.inputSchema["properties"]) == {
        "name",
        "identifier",
        "description",
        "parent_id",
        "public",
        "status_code",
    }
    assert creating.inputSchema["properties"]["status_code"]["anyOf"][0]["enum"] == [
        "on_track",
        "at_risk",
        "off_track",
        "not_started",
        "finished",
        "discontinued",
    ]

    updating = tools["update_project"]
    assert updating.annotations is not None
    assert updating.annotations.readOnlyHint is False
    assert set(updating.inputSchema["properties"]) == {
        "id_or_identifier",
        "name",
        "description",
        "public",
        "parent_id",
        "active",
        "status_code",
        "status_explanation",
    }

    deleting = tools["delete_project"]
    assert deleting.annotations is not None
    assert deleting.annotations.destructiveHint is True
    assert deleting.annotations.readOnlyHint is False
    assert deleting.annotations.model_extra is not None
    assert deleting.annotations.model_extra["anthropic/requiresUserInteraction"] is True
    assert set(deleting.inputSchema["properties"]) == {"id_or_identifier", "confirm"}
    assert set((deleting.meta or {})["fastmcp"]["tags"]) == {"projects", "write", "destructive"}


async def test_delete_description_says_scheduled_and_cascading(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    description = tools["delete_project"].description or ""
    assert "ASYNCHRONOUS" in description
    assert "scheduled" in description
    assert "CASCADES" in description
    assert "subproject" in description
    assert "no API-side undo" in description
    assert "update_project(active=false)" in description


# --- create ---------------------------------------------------------------


async def test_create_project_asks_the_form_first_then_commits_the_merged_payload(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("projects/form").mock(return_value=httpx.Response(200, json=PROJECT_FORM))
    create = mock_api.post("projects").mock(return_value=httpx.Response(201, json=CREATED_PROJECT))

    result = await mcp_client.call_tool(
        "create_project",
        {
            "name": "Apollo migration",
            "description": "Move Apollo off the legacy stack.",
            "parent_id": 3,
            "public": False,
            "status_code": "on_track",
        },
    )

    assert form.call_count == 1
    assert create.call_count == 1

    sent = body_of(form)
    assert sent["name"] == "Apollo migration"
    assert sent["description"] == {
        "format": "markdown",
        "raw": "Move Apollo off the legacy stack.",
    }
    assert sent["public"] is False
    assert sent["_links"]["parent"] == {"href": "/api/v3/projects/3"}
    # The status is a link into project_statuses, not a free string and not a
    # work-package status.
    assert sent["_links"]["status"] == {"href": "/api/v3/project_statuses/on_track"}
    assert "identifier" not in sent

    committed = body_of(create)
    # The identifier OpenProject derived in the form is carried into the commit.
    assert committed["identifier"] == "apollo-migration"
    assert committed["name"] == "Apollo migration"
    assert committed["_links"]["status"] == {"href": "/api/v3/project_statuses/on_track"}
    assert "self" not in committed["_links"]

    assert result.structured_content is not None
    assert result.structured_content["id"] == PROJECT_ID
    assert result.structured_content["identifier"] == "apollo-migration"
    assert result.structured_content["status_code"] == "on_track"
    assert result.structured_content["parent"] == {"id": 3, "name": "Customer work"}
    assert result.structured_content["description"] == "Move Apollo off the legacy stack."


async def test_create_project_surfaces_form_validation_errors_and_never_commits(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("projects/form").mock(
        return_value=httpx.Response(200, json=PROJECT_FORM_IDENTIFIER_TAKEN)
    )
    create = mock_api.post("projects").mock(return_value=httpx.Response(201, json=CREATED_PROJECT))

    result = await mcp_client.call_tool(
        "create_project",
        {"name": "Apollo migration", "identifier": "apollo-migration"},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "identifier", "message": "Identifier has already been taken."}
    ]
    assert "unique across the instance" in error["hint"]
    assert create.call_count == 0


async def test_create_project_rejects_a_malformed_identifier_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("projects/form").mock(return_value=httpx.Response(200, json=PROJECT_FORM))
    result = await mcp_client.call_tool(
        "create_project",
        {"name": "Apollo migration", "identifier": "Apollo Migration"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "identifier" in error["message"]
    assert "lowercase" in error["hint"]
    assert form.call_count == 0


async def test_create_project_surfaces_a_422_from_the_commit_with_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("projects/form").mock(return_value=httpx.Response(200, json=PROJECT_FORM))
    mock_api.post("projects").mock(
        return_value=httpx.Response(422, json=PROJECT_CREATE_VALIDATION_ERROR)
    )

    result = await mcp_client.call_tool(
        "create_project", {"name": "Apollo migration"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [{"attribute": "name", "message": "Name can't be blank."}]
    assert error["error_identifier"].endswith("PropertyConstraintViolation")


# --- update ---------------------------------------------------------------


async def test_update_project_patches_only_what_was_asked_and_sends_no_lock_version(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{PROJECT_PATH}/form").mock(
        return_value=httpx.Response(200, json=PROJECT_UPDATE_FORM)
    )
    patch = mock_api.patch(PROJECT_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_PROJECT)
    )
    read = mock_api.get(PROJECT_PATH).mock(return_value=httpx.Response(200, json=UPDATED_PROJECT))

    result = await mcp_client.call_tool(
        "update_project",
        {
            "id_or_identifier": PROJECT_ID,
            "name": "Apollo migration (phase 2)",
            "status_code": "at_risk",
            "status_explanation": "Vendor slipped two weeks.",
            "parent_id": None,
        },
    )

    assert form.call_count == 1
    sent = body_of(patch)
    assert sent == {
        "name": "Apollo migration (phase 2)",
        "statusExplanation": {"format": "markdown", "raw": "Vendor slipped two weeks."},
        "_links": {
            "parent": {"href": None},
            "status": {"href": "/api/v3/project_statuses/at_risk"},
        },
    }
    assert "lockVersion" not in sent
    # Projects carry no lockVersion, so no read-before-write round trip happens.
    assert read.call_count == 0

    assert result.structured_content is not None
    assert result.structured_content["status_code"] == "at_risk"
    assert result.structured_content["parent"] is None
    assert result.structured_content["status_explanation"] == "Vendor slipped two weeks."


async def test_update_project_leaves_the_parent_alone_when_not_mentioned(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{PROJECT_PATH}/form").mock(
        return_value=httpx.Response(200, json=PROJECT_UPDATE_FORM)
    )
    patch = mock_api.patch(PROJECT_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_PROJECT)
    )

    await mcp_client.call_tool("update_project", {"id_or_identifier": PROJECT_ID, "public": True})
    assert body_of(patch) == {"public": True}


async def test_update_project_with_nothing_to_change_never_calls_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{PROJECT_PATH}/form").mock(
        return_value=httpx.Response(200, json=PROJECT_UPDATE_FORM)
    )
    result = await mcp_client.call_tool(
        "update_project", {"id_or_identifier": PROJECT_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to change" in error["message"]
    assert form.call_count == 0


async def test_update_project_reports_an_unknown_identifier_as_not_found(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("projects/ghost-project/form").mock(
        return_value=httpx.Response(404, json=PROJECT_NOT_FOUND)
    )
    result = await mcp_client.call_tool(
        "update_project",
        {"id_or_identifier": "ghost-project", "name": "Whatever"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "URL slug" in error["hint"]


async def test_update_project_passes_a_conflict_through_as_a_conflict(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{PROJECT_PATH}/form").mock(
        return_value=httpx.Response(200, json=PROJECT_UPDATE_FORM)
    )
    mock_api.patch(PROJECT_PATH).mock(return_value=httpx.Response(409, json=PROJECT_CONFLICT))

    result = await mcp_client.call_tool(
        "update_project",
        {"id_or_identifier": PROJECT_ID, "name": "Apollo migration (phase 2)"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    # No lock_version is invented for a resource that has none.
    assert "lock_version" not in error
    assert error["hint"]


# --- delete ---------------------------------------------------------------


async def test_delete_project_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(PROJECT_PATH).mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool(
        "delete_project", {"id_or_identifier": PROJECT_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert "subproject" in error["hint"]
    assert route.call_count == 0


async def test_delete_project_reports_a_scheduled_deletion_with_the_job_id(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(PROJECT_PATH).mock(return_value=httpx.Response(202, json=DELETE_JOB_ACCEPTED))
    result = await mcp_client.call_tool(
        "delete_project", {"id_or_identifier": PROJECT_ID, "confirm": True}
    )
    assert result.structured_content is not None
    assert result.structured_content["scheduled"] is True
    assert result.structured_content["job_id"] == JOB_ID
    assert "scheduled" in result.structured_content["message"]
    assert "deleted" not in result.structured_content


async def test_delete_project_handles_a_204_without_a_job_id(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete("projects/apollo-migration").mock(return_value=httpx.Response(204))
    result = await mcp_client.call_tool(
        "delete_project", {"id_or_identifier": "apollo-migration", "confirm": True}
    )
    assert result.structured_content is not None
    assert result.structured_content["scheduled"] is True
    assert result.structured_content["job_id"] is None
    assert result.structured_content["id"] == "apollo-migration"
    assert "background job" in result.structured_content["message"]


async def test_delete_project_reports_an_unknown_project_as_not_found(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete("projects/999").mock(return_value=httpx.Response(404, json=PROJECT_NOT_FOUND))
    result = await mcp_client.call_tool(
        "delete_project", {"id_or_identifier": 999, "confirm": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "numeric id 999" in error["hint"]
