"""The helpers Phase 1 tool modules build on, exercised end-to-end.

The decorator tests register throwaway tools on a scratch FastMCP server, so
they prove the contract the way a real tool will meet it: through the protocol,
with a declared output schema.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel

from openproject_mcp.client.errors import (
    NotFoundError,
    OpenProjectError,
    ValidationFailedError,
)
from openproject_mcp.client.hal import collection
from openproject_mcp.projections import ListEnvelope
from openproject_mcp.tools import _shared
from tests.fixtures.hal_payloads import WORK_PACKAGE_COLLECTION


class Row(BaseModel):
    id: int
    subject: str


# --- error decorator ------------------------------------------------------


async def test_tool_errors_returns_the_spec_envelope() -> None:
    mcp: FastMCP = FastMCP("scratch")

    @mcp.tool
    @_shared.tool_errors
    async def failing(work_package_id: int) -> Row:
        """Always fails."""
        raise NotFoundError(
            "Work package 9 not found.",
            http_status=404,
            error_identifier="urn:openproject-org:api:v3:errors:NotFound",
            hint="ids come from search_work_packages",
        )

    async with Client(mcp) as client:
        result = await client.call_tool("failing", {"work_package_id": 9}, raise_on_error=False)

    assert result.is_error
    assert result.structured_content is None, "errors must not set structuredContent"
    envelope = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert envelope == {
        "error": {
            "type": "not_found",
            "http_status": 404,
            "error_identifier": "urn:openproject-org:api:v3:errors:NotFound",
            "message": "Work package 9 not found.",
            "hint": "ids come from search_work_packages",
        }
    }


async def test_tool_errors_preserves_the_success_path_and_schemas() -> None:
    mcp: FastMCP = FastMCP("scratch")

    @mcp.tool
    @_shared.tool_errors
    async def succeeding(work_package_id: int, verbose: bool = False) -> Row:
        """Returns a row."""
        return Row(id=work_package_id, subject="ok")

    async with Client(mcp) as client:
        tool = (await client.list_tools())[0]
        assert tool.description == "Returns a row."
        assert set(tool.inputSchema["properties"]) == {"work_package_id", "verbose"}
        assert tool.inputSchema["required"] == ["work_package_id"]
        assert tool.outputSchema is not None
        result = await client.call_tool("succeeding", {"work_package_id": 3})

    assert not result.is_error
    assert result.structured_content == {"id": 3, "subject": "ok"}


async def test_tool_errors_hides_unexpected_exceptions() -> None:
    mcp: FastMCP = FastMCP("scratch")

    @mcp.tool
    @_shared.tool_errors
    async def exploding() -> Row:
        """Raises something unexpected."""
        raise ZeroDivisionError("secret internals: /Users/someone/token.txt")

    async with Client(mcp) as client:
        result = await client.call_tool("exploding", {}, raise_on_error=False)

    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "token.txt" not in text
    assert "Traceback" not in text
    assert json.loads(text)["error"]["type"] == "unexpected_response"


def test_tool_errors_rejects_sync_functions() -> None:
    with pytest.raises(TypeError):

        @_shared.tool_errors
        def not_async() -> None: ...


def test_error_result_is_a_tool_result() -> None:
    result = _shared.error_result(ValidationFailedError("bad", http_status=422))
    assert isinstance(result, ToolResult)
    assert result.is_error


# --- confirmation guard ---------------------------------------------------


def test_require_confirmation_passes_when_confirmed() -> None:
    _shared.require_confirmation(True, action="delete work package", target="#5")


def test_require_confirmation_raises_a_typed_error() -> None:
    with pytest.raises(OpenProjectError) as excinfo:
        _shared.require_confirmation(
            False,
            action="delete work package",
            target="#5",
            consequence="Comments are removed too.",
        )
    error = excinfo.value
    body = error.to_envelope()["error"]
    assert body["type"] == "confirmation_required"
    assert "http_status" not in body, "a local guard has no upstream status"
    assert "confirm=true" in body["hint"]
    assert "Comments are removed too." in body["hint"]


# --- annotations & tags ---------------------------------------------------


def test_read_annotations() -> None:
    annotations = _shared.read_annotations(title="List work packages", max_result_chars=50_000)
    assert annotations["readOnlyHint"] is True
    assert annotations["destructiveHint"] is False
    assert annotations["openWorldHint"] is True
    assert annotations["anthropic/maxResultSizeChars"] == 50_000


def test_write_annotations() -> None:
    annotations = _shared.write_annotations(title="Create work package")
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is False
    assert "anthropic/requiresUserInteraction" not in annotations


def test_destructive_annotations_always_require_user_interaction() -> None:
    annotations = _shared.destructive_annotations(title="Delete work package")
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True
    assert annotations["anthropic/requiresUserInteraction"] is True


def test_tool_tags() -> None:
    assert _shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.READ) == {
        "work_packages",
        "read",
    }
    assert _shared.tool_tags(_shared.GROUP_PEOPLE, _shared.WRITE, _shared.ADMIN) == {
        "people",
        "write",
        "admin",
    }
    with pytest.raises(ValueError, match="kind tag"):
        _shared.tool_tags(_shared.GROUP_PROJECTS)


async def test_annotations_reach_the_wire() -> None:
    mcp: FastMCP = FastMCP("scratch")

    @mcp.tool(
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.DESTRUCTIVE, _shared.WRITE),
        annotations=_shared.destructive_annotations(title="Delete work package"),
    )
    @_shared.tool_errors
    async def delete_work_package(work_package_id: int, confirm: bool = False) -> Row:
        """Deletes."""
        _shared.require_confirmation(confirm, action="delete", target=str(work_package_id))
        return Row(id=work_package_id, subject="deleted")

    async with Client(mcp) as client:
        tool = (await client.list_tools())[0]
        assert tool.annotations is not None
        dumped: dict[str, Any] = tool.annotations.model_dump()
        assert dumped["destructiveHint"] is True
        assert dumped["anthropic/requiresUserInteraction"] is True

        refused = await client.call_tool(
            "delete_work_package", {"work_package_id": 1}, raise_on_error=False
        )
        assert refused.is_error
        assert json.loads(refused.content[0].text)["error"]["type"] == "confirmation_required"  # type: ignore[union-attr]


# --- list envelope --------------------------------------------------------


def test_build_envelope_defaults_to_fetched_in_full() -> None:
    envelope = _shared.build_envelope([Row(id=1, subject="a"), Row(id=2, subject="b")])
    assert envelope.pagination.total == 2
    assert envelope.pagination.page == 1
    assert envelope.pagination.has_more is False
    assert envelope.groups is None and envelope.sums is None and envelope.notes is None


def test_build_envelope_computes_has_more() -> None:
    envelope = _shared.build_envelope(
        [Row(id=1, subject="a")] * 20, total=137, page=2, page_size=20
    )
    assert envelope.pagination.has_more is True
    last = _shared.build_envelope([Row(id=1, subject="a")] * 17, total=137, page=7, page_size=20)
    assert last.pagination.has_more is False


def test_envelope_from_collection_uses_server_reported_paging() -> None:
    unwrapped = collection(WORK_PACKAGE_COLLECTION)
    envelope = _shared.envelope_from_collection(
        unwrapped,
        [Row(id=1, subject="a"), Row(id=2, subject="b")],
        page=1,
        page_size=100,
        notes=["attachment content not searched on this instance"],
    )
    assert envelope.pagination.total == 137
    assert envelope.pagination.page == 2, "server offset wins over the requested page"
    assert envelope.pagination.page_size == 20
    assert envelope.pagination.has_more is True
    assert envelope.notes == ["attachment content not searched on this instance"]


def test_envelope_converts_sums_and_groups_to_hours() -> None:
    unwrapped = collection(WORK_PACKAGE_COLLECTION)
    envelope = _shared.envelope_from_collection(unwrapped, [], page=1, page_size=20)
    assert envelope.sums == {"estimated_hours": 220.0, "story_points": 41.0}
    assert envelope.groups is not None
    assert envelope.groups[0].value == "In progress"
    assert envelope.groups[0].count == 12
    assert envelope.groups[0].sums == {"estimated_hours": 41.5}


def test_envelope_is_serializable_and_typed() -> None:
    envelope = _shared.build_envelope([Row(id=1, subject="a")], total=1)
    assert isinstance(envelope, ListEnvelope)
    assert json.loads(_shared.envelope_json(envelope))["items"] == [{"id": 1, "subject": "a"}]


def test_group_values_from_link_objects() -> None:
    groups = _shared.normalize_groups(
        [
            {
                "value": {
                    "_links": {"self": {"href": "/api/v3/statuses/7", "title": "In progress"}}
                },
                "count": 4,
            }
        ]
    )
    assert groups is not None
    assert groups[0].value == "In progress"


def test_lifespan_access_outside_a_tool_call_is_a_clear_error() -> None:
    with pytest.raises(RuntimeError, match="tool context"):
        _shared.get_tool_context()
