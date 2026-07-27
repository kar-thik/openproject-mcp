"""Attachment tools end to end over the in-memory FastMCP client (SPEC §6.4, §7).

Everything here goes through the real protocol: ``build_server`` + ``Client`` +
``respx``, so registration, argument validation, the §9.3 envelope, the error
envelopes and the download/upload wire format are all asserted as a client sees
them. The one exception is :func:`upload_uncontainered_attachment`, which is not
an MCP tool and is unit-tested directly at the bottom of this file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from openproject_mcp.client.cache import TTLCache
from openproject_mcp.client.errors import InputValidationError, UnexpectedResponseError
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from openproject_mcp.tools import attachments
from openproject_mcp.tools._shared import ToolContext
from tests.conftest import TEST_URL
from tests.fixtures.attachments_payloads import (
    ALLOWLIST_ERROR,
    ATTACHMENT,
    ATTACHMENT_COLLECTION,
    ATTACHMENT_HUGE,
    ATTACHMENT_IMAGE,
    ATTACHMENT_PENDING_SCAN,
    ATTACHMENT_QUARANTINED,
    ATTACHMENT_TRAVERSAL,
    COMMENT_ATTACHMENT_COLLECTION,
    CONFIGURATION,
    PDF_BYTES,
    PNG_1X1,
)


def _settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", **overrides
    )


def _error(result: Any) -> dict[str, Any]:
    """The §4.2 error envelope carried in a failed tool result's text content."""
    assert result.is_error
    payload = json.loads(result.content[0].text)
    return payload["error"]


def _local_file(directory: Path, name: str, payload: bytes) -> str:
    path = directory / name
    path.write_bytes(payload)
    return str(path)


# --- registration ----------------------------------------------------------


async def test_tools_are_registered_with_honest_annotations(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    for name in ("list_attachments", "download_attachment", "upload_attachment"):
        assert name in tools, f"{name} is not registered"
        assert tools[name].outputSchema is not None
        assert tools[name].description

    for name in ("list_attachments", "download_attachment"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False

    upload = tools["upload_attachment"].annotations
    assert upload is not None
    assert upload.readOnlyHint is False
    # Repeating a download makes a second file ("name (2).ext"), so it is not idempotent.
    assert tools["download_attachment"].annotations.idempotentHint is False  # type: ignore[union-attr]


# --- list_attachments ------------------------------------------------------


async def test_list_attachments_returns_the_full_collection_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/1234/attachments").mock(
        return_value=httpx.Response(200, json=ATTACHMENT_COLLECTION)
    )

    result = await mcp_client.call_tool(
        "list_attachments", {"container_type": "work_package", "container_id": 1234}
    )

    structured = result.structured_content
    assert structured is not None
    assert structured["pagination"] == {"total": 2, "page": 1, "page_size": 2, "has_more": False}
    first, second = structured["items"]
    assert first == {
        "id": 42,
        "file_name": "spec.pdf",
        "size_bytes": 12,
        "content_type": "application/pdf",
        "description": "The signed spec",
        "author": {"id": 12, "name": "Grace Hopper"},
        "created_at": "2026-07-20T09:15:00Z",
        "status": "uploaded",
    }
    assert second["id"] == 43
    assert second["status"] == "scanned"


async def test_list_attachments_maps_comment_to_the_activities_mount(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("activities/9001/attachments").mock(
        return_value=httpx.Response(200, json=COMMENT_ATTACHMENT_COLLECTION)
    )

    result = await mcp_client.call_tool(
        "list_attachments", {"container_type": "comment", "container_id": 9001}
    )

    assert route.called
    assert result.structured_content is not None
    assert result.structured_content["items"][0]["id"] == 42
    assert result.structured_content["pagination"]["has_more"] is False


async def test_list_attachments_404_returns_the_error_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("meetings/77/attachments").mock(
        return_value=httpx.Response(
            404,
            json={"message": "The requested resource could not be found."},
            headers={"content-type": "application/json"},
        )
    )

    result = await mcp_client.call_tool(
        "list_attachments",
        {"container_type": "meeting", "container_id": 77},
        raise_on_error=False,
    )

    error = _error(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "module" in error["hint"]


async def test_list_attachments_rejects_an_unknown_container_type(
    mcp_client: Client[Any],
) -> None:
    with pytest.raises(Exception) as excinfo:
        await mcp_client.call_tool(
            "list_attachments", {"container_type": "forum_post", "container_id": 1}
        )
    assert "forum_post" in str(excinfo.value)


# --- download_attachment ---------------------------------------------------


async def test_download_streams_to_disk_and_reports_sha256(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    payload = b"x" * (200 * 1024)
    mock_api.get("attachments/42").mock(
        return_value=httpx.Response(200, json={**ATTACHMENT, "fileSize": len(payload)})
    )
    content = mock_api.get("attachments/42/content").mock(
        return_value=httpx.Response(
            200,
            content=payload,
            headers={"content-disposition": 'attachment; filename="spec.pdf"'},
        )
    )
    updates: list[tuple[float, float | None, str | None]] = []

    async def progress(value: float, total: float | None, message: str | None) -> None:
        updates.append((value, total, message))

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 42, "save_dir": str(tmp_path)},
            progress_handler=progress,
        )

    structured = result.structured_content
    assert structured is not None
    saved = Path(structured["path"])
    assert saved == tmp_path / "spec.pdf"
    assert saved.read_bytes() == payload
    assert structured["file_name"] == "spec.pdf"
    assert structured["size_bytes"] == len(payload)
    assert structured["content_type"] == "application/pdf"
    assert structured["sha256"] == hashlib.sha256(payload).hexdigest()
    assert structured.get("notes") is None
    assert content.called
    # Progress is emitted per chunk, so a 200 KiB body reports more than once.
    assert len(updates) > 1
    assert updates[-1][0] == len(payload)
    assert updates[-1][1] == len(payload)


async def test_download_uniquifies_a_colliding_file_name(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    (tmp_path / "spec.pdf").write_bytes(b"older download")
    mock_api.get("attachments/42").mock(return_value=httpx.Response(200, json=ATTACHMENT))
    mock_api.get("attachments/42/content").mock(return_value=httpx.Response(200, content=PDF_BYTES))

    async with Client(mcp_server) as client:
        first = await client.call_tool(
            "download_attachment", {"attachment_id": 42, "save_dir": str(tmp_path)}
        )
        second = await client.call_tool(
            "download_attachment", {"attachment_id": 42, "save_dir": str(tmp_path)}
        )

    assert first.structured_content is not None
    assert second.structured_content is not None
    assert first.structured_content["file_name"] == "spec (2).pdf"
    assert second.structured_content["file_name"] == "spec (3).pdf"
    assert (tmp_path / "spec.pdf").read_bytes() == b"older download"
    assert (tmp_path / "spec (2).pdf").read_bytes() == PDF_BYTES
    assert "already existed" in first.structured_content["notes"][0]


async def test_download_neutralizes_traversal_in_the_file_name(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/46").mock(return_value=httpx.Response(200, json=ATTACHMENT_TRAVERSAL))
    mock_api.get("attachments/46/content").mock(return_value=httpx.Response(200, content=b"root:"))
    target = tmp_path / "downloads"

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment", {"attachment_id": 46, "save_dir": str(target)}
        )

    assert result.structured_content is not None
    saved = Path(result.structured_content["path"])
    assert saved == target / "passwd"
    assert saved.parent == target
    assert not (tmp_path.parent / "etc").exists()


async def test_download_refuses_a_file_larger_than_the_cap_before_streaming(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/47").mock(return_value=httpx.Response(200, json=ATTACHMENT_HUGE))
    content = mock_api.get("attachments/47/content").mock(
        return_value=httpx.Response(200, content=b"never fetched")
    )

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 47, "save_dir": str(tmp_path)},
            raise_on_error=False,
        )

    error = _error(result)
    assert error["type"] == "attachment_too_large"
    assert error["size_bytes"] == ATTACHMENT_HUGE["fileSize"]
    assert "OPENPROJECT_MCP_MAX_DOWNLOAD_MB" in error["hint"]
    assert not content.called
    assert list(tmp_path.iterdir()) == []


async def test_download_aborts_mid_stream_and_removes_the_partial_file(
    mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    oversized = b"y" * (1024 * 1024 + 4096)
    # The metadata under-reports the size, so only the streaming guard can catch it.
    mock_api.get("attachments/42").mock(
        return_value=httpx.Response(200, json={**ATTACHMENT, "fileSize": 10})
    )
    mock_api.get("attachments/42/content").mock(return_value=httpx.Response(200, content=oversized))

    server = build_server(_settings(max_download_mb=1))
    async with Client(server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 42, "save_dir": str(tmp_path)},
            raise_on_error=False,
        )

    error = _error(result)
    assert error["type"] == "attachment_too_large"
    assert "while streaming" in error["message"]
    assert list(tmp_path.iterdir()) == []


async def test_download_aborts_when_the_wall_clock_deadline_passes(
    mcp_server: Any,
    mock_api: respx.MockRouter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments, "DOWNLOAD_DEADLINE_SECONDS", 0.0)
    mock_api.get("attachments/42").mock(return_value=httpx.Response(200, json=ATTACHMENT))
    mock_api.get("attachments/42/content").mock(return_value=httpx.Response(200, content=PDF_BYTES))

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 42, "save_dir": str(tmp_path)},
            raise_on_error=False,
        )

    error = _error(result)
    assert error["type"] == "network_error"
    assert list(tmp_path.iterdir()) == []


async def test_download_refuses_a_quarantined_attachment(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/44").mock(
        return_value=httpx.Response(200, json=ATTACHMENT_QUARANTINED)
    )
    content = mock_api.get("attachments/44/content").mock(
        return_value=httpx.Response(200, content=b"malware")
    )

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 44, "save_dir": str(tmp_path)},
            raise_on_error=False,
        )

    error = _error(result)
    assert error["type"] == "attachment_quarantined"
    assert error["attachment_id"] == 44
    assert error["file_name"] == "invoice.exe"
    assert "administrator" in error["hint"]
    assert not content.called
    assert list(tmp_path.iterdir()) == []


async def test_download_401_explains_the_unfinished_virus_scan(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/45").mock(
        return_value=httpx.Response(200, json=ATTACHMENT_PENDING_SCAN)
    )
    mock_api.get("attachments/45/content").mock(
        return_value=httpx.Response(
            401, json={"message": "You did not provide the correct credentials."}
        )
    )

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 45, "save_dir": str(tmp_path)},
            raise_on_error=False,
        )

    error = _error(result)
    assert error["type"] == "authentication_failed"
    assert error["http_status"] == 401
    assert "antivirus scan" in error["hint"]
    assert "prescan" in error["hint"]
    assert list(tmp_path.iterdir()) == []


async def test_download_follows_the_presigned_redirect_without_leaking_credentials(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/42").mock(return_value=httpx.Response(200, json=ATTACHMENT))
    origin = mock_api.get("attachments/42/content").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://files.example.com/blob?signature=abc"}
        )
    )
    storage = mock_api.get("https://files.example.com/blob").mock(
        return_value=httpx.Response(200, content=PDF_BYTES)
    )

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment", {"attachment_id": 42, "save_dir": str(tmp_path)}
        )

    assert result.structured_content is not None
    assert Path(result.structured_content["path"]).read_bytes() == PDF_BYTES
    assert "authorization" in origin.calls.last.request.headers
    assert "authorization" not in storage.calls.last.request.headers
    assert "cookie" not in storage.calls.last.request.headers


async def test_download_returns_an_inline_image_block(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/43").mock(return_value=httpx.Response(200, json=ATTACHMENT_IMAGE))
    mock_api.get("attachments/43/content").mock(return_value=httpx.Response(200, content=PNG_1X1))

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 43, "save_dir": str(tmp_path), "return_image": True},
        )

    images = [block for block in result.content if block.type == "image"]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert images[0].data
    structured = result.structured_content
    assert structured is not None
    assert structured["file_name"] == "screenshot.png"
    assert structured["sha256"] == hashlib.sha256(PNG_1X1).hexdigest()
    assert (tmp_path / "screenshot.png").read_bytes() == PNG_1X1


async def test_download_notes_why_a_non_image_was_not_shown(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/42").mock(return_value=httpx.Response(200, json=ATTACHMENT))
    mock_api.get("attachments/42/content").mock(return_value=httpx.Response(200, content=PDF_BYTES))

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 42, "save_dir": str(tmp_path), "return_image": True},
        )

    assert [block for block in result.content if block.type == "image"] == []
    assert result.structured_content is not None
    assert "not an image" in result.structured_content["notes"][0]


async def test_download_notes_when_an_image_is_too_large_to_inline(
    mcp_server: Any, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    oversized = PNG_1X1 + b"\x00" * (attachments.IMAGE_INLINE_MAX_BYTES)
    mock_api.get("attachments/43").mock(
        return_value=httpx.Response(200, json={**ATTACHMENT_IMAGE, "fileSize": len(oversized)})
    )
    mock_api.get("attachments/43/content").mock(return_value=httpx.Response(200, content=oversized))

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 43, "save_dir": str(tmp_path), "return_image": True},
        )

    assert [block for block in result.content if block.type == "image"] == []
    assert result.structured_content is not None
    assert "inline limit" in result.structured_content["notes"][0]
    assert (tmp_path / "screenshot.png").stat().st_size == len(oversized)


async def test_download_rejects_a_relative_save_dir(
    mcp_server: Any, mock_api: respx.MockRouter
) -> None:
    mock_api.get("attachments/42").mock(return_value=httpx.Response(200, json=ATTACHMENT))
    content = mock_api.get("attachments/42/content").mock(
        return_value=httpx.Response(200, content=PDF_BYTES)
    )

    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "download_attachment",
            {"attachment_id": 42, "save_dir": "./downloads"},
            raise_on_error=False,
        )

    error = _error(result)
    assert error["type"] == "invalid_input"
    assert "absolute path" in error["message"]
    assert not content.called


async def test_download_falls_back_to_the_configured_download_dir(
    mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("attachments/42").mock(return_value=httpx.Response(200, json=ATTACHMENT))
    mock_api.get("attachments/42/content").mock(return_value=httpx.Response(200, content=PDF_BYTES))
    configured = tmp_path / "configured" / "nested"

    server = build_server(_settings(download_dir=configured))
    async with Client(server) as client:
        result = await client.call_tool("download_attachment", {"attachment_id": 42})

    assert result.structured_content is not None
    assert Path(result.structured_content["path"]) == configured / "spec.pdf"


# --- upload_attachment -----------------------------------------------------


async def test_upload_sends_exactly_two_multipart_parts(
    mcp_client: Client[Any], mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("configuration").mock(return_value=httpx.Response(200, json=CONFIGURATION))
    route = mock_api.post("work_packages/1234/attachments").mock(
        return_value=httpx.Response(201, json=ATTACHMENT)
    )
    source = _local_file(tmp_path, "local-name.pdf", PDF_BYTES)

    result = await mcp_client.call_tool(
        "upload_attachment",
        {
            "container_type": "work_package",
            "container_id": 1234,
            "file_path": source,
            "file_name": "spec.pdf",
            "description": "The signed spec",
        },
    )

    assert result.structured_content is not None
    assert result.structured_content["id"] == 42
    assert result.structured_content["file_name"] == "spec.pdf"

    body = route.calls.last.request.read()
    content_type = route.calls.last.request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    assert body.count(b"Content-Disposition: form-data;") == 2
    assert b'Content-Disposition: form-data; name="metadata"' in body
    assert b"Content-Type: application/json" in body
    assert b'{"fileName": "spec.pdf", "description": {"raw": "The signed spec"}}' in body
    # The metadata fileName wins; the part filename is cosmetic and never the path's own.
    assert b'name="file"; filename="spec.pdf"' in body
    assert b"local-name.pdf" not in body
    assert PDF_BYTES in body


async def test_upload_defaults_the_stored_name_to_the_file_basename(
    mcp_client: Client[Any], mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("configuration").mock(return_value=httpx.Response(200, json=CONFIGURATION))
    route = mock_api.post("activities/9001/attachments").mock(
        return_value=httpx.Response(201, json=ATTACHMENT)
    )
    source = _local_file(tmp_path, "notes.txt", b"hello")

    await mcp_client.call_tool(
        "upload_attachment",
        {"container_type": "comment", "container_id": 9001, "file_path": source},
    )

    body = route.calls.last.request.read()
    assert b'{"fileName": "notes.txt"}' in body
    assert b"description" not in body


async def test_upload_refuses_a_file_over_the_instance_limit(
    mcp_client: Client[Any], mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("configuration").mock(return_value=httpx.Response(200, json=CONFIGURATION))
    route = mock_api.post("work_packages/1234/attachments").mock(
        return_value=httpx.Response(201, json=ATTACHMENT)
    )
    source = _local_file(tmp_path, "big.bin", b"z" * 2048)

    result = await mcp_client.call_tool(
        "upload_attachment",
        {"container_type": "work_package", "container_id": 1234, "file_path": source},
        raise_on_error=False,
    )

    error = _error(result)
    assert error["type"] == "attachment_too_large"
    assert "2048 bytes" in error["message"]
    assert "1024 bytes" in error["message"]
    assert not route.called


async def test_upload_fails_locally_when_the_file_is_missing(
    mcp_client: Client[Any], mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    configuration = mock_api.get("configuration").mock(
        return_value=httpx.Response(200, json=CONFIGURATION)
    )

    result = await mcp_client.call_tool(
        "upload_attachment",
        {
            "container_type": "work_package",
            "container_id": 1234,
            "file_path": str(tmp_path / "gone.pdf"),
        },
        raise_on_error=False,
    )

    error = _error(result)
    assert error["type"] == "invalid_input"
    assert "No such file" in error["message"]
    assert "Nothing was uploaded" in error["hint"]
    assert not configuration.called


async def test_upload_422_surfaces_violations_and_the_allowlist_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("configuration").mock(return_value=httpx.Response(200, json=CONFIGURATION))
    mock_api.post("work_packages/1234/attachments").mock(
        return_value=httpx.Response(
            422, json=ALLOWLIST_ERROR, headers={"content-type": "application/json"}
        )
    )
    source = _local_file(tmp_path, "payload.exe", b"MZ")

    result = await mcp_client.call_tool(
        "upload_attachment",
        {"container_type": "work_package", "container_id": 1234, "file_path": source},
        raise_on_error=False,
    )

    error = _error(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "file", "message": "File is not of an allowed content type."}
    ]
    assert "attachment_whitelist" in error["hint"]
    assert "Nothing was stored" in error["hint"]


async def test_upload_caches_the_configuration_between_calls(
    mcp_client: Client[Any], mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    configuration = mock_api.get("configuration").mock(
        return_value=httpx.Response(200, json=CONFIGURATION)
    )
    mock_api.post("work_packages/1234/attachments").mock(
        return_value=httpx.Response(201, json=ATTACHMENT)
    )
    source = _local_file(tmp_path, "notes.txt", b"hello")
    arguments = {
        "container_type": "work_package",
        "container_id": 1234,
        "file_path": source,
    }

    await mcp_client.call_tool("upload_attachment", arguments)
    await mcp_client.call_tool("upload_attachment", arguments)

    assert configuration.call_count == 1


# --- upload_uncontainered_attachment (not a tool; unit-tested directly) ----


@pytest.fixture
def tool_context(settings: Settings, op_client: OpenProjectClient) -> ToolContext:
    return ToolContext(client=op_client, cache=TTLCache(ttl=300.0), settings=settings)


async def test_uncontainered_upload_returns_the_new_attachment_id(
    tool_context: ToolContext, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("configuration").mock(return_value=httpx.Response(200, json=CONFIGURATION))
    route = mock_api.post("attachments").mock(return_value=httpx.Response(201, json=ATTACHMENT))
    source = _local_file(tmp_path, "local-name.pdf", PDF_BYTES)

    attachment_id = await attachments.upload_uncontainered_attachment(
        tool_context, source, file_name="spec.pdf", description="claimed on create"
    )

    assert attachment_id == 42
    body = route.calls.last.request.read()
    assert body.count(b"Content-Disposition: form-data;") == 2
    assert b'{"fileName": "spec.pdf", "description": {"raw": "claimed on create"}}' in body
    assert b'name="file"; filename="spec.pdf"' in body


async def test_uncontainered_upload_preflights_the_size_limit(
    tool_context: ToolContext, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("configuration").mock(return_value=httpx.Response(200, json=CONFIGURATION))
    route = mock_api.post("attachments").mock(return_value=httpx.Response(201, json=ATTACHMENT))
    source = _local_file(tmp_path, "big.bin", b"z" * 2048)

    with pytest.raises(attachments.AttachmentTooLargeError) as excinfo:
        await attachments.upload_uncontainered_attachment(tool_context, source)

    assert excinfo.value.error_type == "attachment_too_large"
    assert not route.called


async def test_uncontainered_upload_rejects_a_directory(
    tool_context: ToolContext, tmp_path: Path
) -> None:
    with pytest.raises(InputValidationError) as excinfo:
        await attachments.upload_uncontainered_attachment(tool_context, str(tmp_path))

    assert "not a regular file" in excinfo.value.message


async def test_uncontainered_upload_flags_a_response_without_an_id(
    tool_context: ToolContext, mock_api: respx.MockRouter, tmp_path: Path
) -> None:
    mock_api.get("configuration").mock(return_value=httpx.Response(200, json=CONFIGURATION))
    mock_api.post("attachments").mock(
        return_value=httpx.Response(201, json={"_type": "Attachment"})
    )
    source = _local_file(tmp_path, "notes.txt", b"hello")

    with pytest.raises(UnexpectedResponseError) as excinfo:
        await attachments.upload_uncontainered_attachment(tool_context, source)

    assert "no attachment id" in excinfo.value.message
