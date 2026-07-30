"""Protocol tests for ``delete_attachment`` (SPEC §6.4, §5.5).

The destructive guard, the read-before-delete that names the file, and the §4.2
error envelopes (404 before anything is removed, 403 after the read, 422 with
violations), all through the in-memory FastMCP client against respx.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.collab_writes_payloads import (
    ATTACHMENT_DELETE_FORBIDDEN,
    ATTACHMENT_DELETE_VIOLATION,
    ATTACHMENT_ID,
    ATTACHMENT_NOT_FOUND,
    DELETABLE_ATTACHMENT,
    WORK_PACKAGE_ID,
)

ATTACHMENT_PATH = f"attachments/{ATTACHMENT_ID}"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


async def test_delete_attachment_is_registered_as_destructive(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    deleting = tools["delete_attachment"]

    assert deleting.outputSchema is not None
    assert deleting.annotations is not None
    assert deleting.annotations.readOnlyHint is False
    assert deleting.annotations.destructiveHint is True
    assert deleting.annotations.idempotentHint is False
    assert deleting.annotations.model_extra is not None
    assert deleting.annotations.model_extra["anthropic/requiresUserInteraction"] is True
    assert set(deleting.inputSchema["properties"]) == {"attachment_id", "confirm"}
    assert deleting.inputSchema["properties"]["confirm"]["default"] is False

    description = deleting.description or ""
    assert "list_attachments" in description
    assert "no undo" in deleting.inputSchema["properties"]["confirm"]["description"]


async def test_delete_refuses_without_confirmation_and_reads_nothing(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(ATTACHMENT_PATH).mock(
        return_value=httpx.Response(200, json=DELETABLE_ATTACHMENT)
    )
    route = mock_api.delete(ATTACHMENT_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_attachment", {"attachment_id": ATTACHMENT_ID}, raise_on_error=False
    )

    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert read.call_count == 0
    assert route.call_count == 0


async def test_confirmed_delete_names_the_file_and_its_container(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    read = mock_api.get(ATTACHMENT_PATH).mock(
        return_value=httpx.Response(200, json=DELETABLE_ATTACHMENT)
    )
    route = mock_api.delete(ATTACHMENT_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_attachment", {"attachment_id": ATTACHMENT_ID, "confirm": True}
    )

    assert result.structured_content is not None
    assert result.structured_content == {
        "id": ATTACHMENT_ID,
        "deleted": True,
        "file_name": "obsolete-spec.pdf",
        "container": {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"},
        "message": "Attachment obsolete-spec.pdf from Ship the client layer was deleted "
        "permanently.",
    }
    assert read.call_count == 1
    assert route.call_count == 1


async def test_unknown_attachment_fails_before_anything_is_deleted(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ATTACHMENT_PATH).mock(return_value=httpx.Response(404, json=ATTACHMENT_NOT_FOUND))
    route = mock_api.delete(ATTACHMENT_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_attachment",
        {"attachment_id": ATTACHMENT_ID, "confirm": True},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "ids come from" in error["hint"]
    assert route.call_count == 0


async def test_missing_permission_on_the_container_is_reported_as_such(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ATTACHMENT_PATH).mock(return_value=httpx.Response(200, json=DELETABLE_ATTACHMENT))
    mock_api.delete(ATTACHMENT_PATH).mock(
        return_value=httpx.Response(403, json=ATTACHMENT_DELETE_FORBIDDEN)
    )

    result = await mcp_client.call_tool(
        "delete_attachment",
        {"attachment_id": ATTACHMENT_ID, "confirm": True},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert error["http_status"] == 403
    assert "list_permissions" in error["hint"]


async def test_a_refusal_with_violations_surfaces_them(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(ATTACHMENT_PATH).mock(return_value=httpx.Response(200, json=DELETABLE_ATTACHMENT))
    mock_api.delete(ATTACHMENT_PATH).mock(
        return_value=httpx.Response(422, json=ATTACHMENT_DELETE_VIOLATION)
    )

    result = await mcp_client.call_tool(
        "delete_attachment",
        {"attachment_id": ATTACHMENT_ID, "confirm": True},
        raise_on_error=False,
    )

    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {
            "attribute": "status",
            "message": "Attachment cannot be removed while the antivirus scan is running.",
        }
    ]
    assert "violations" in error["hint"]
