"""Real API contracts through an in-memory MCP client; only disposable fixture credentials."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from pydantic import SecretStr

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server

pytestmark = pytest.mark.integration


@pytest.fixture
async def live() -> AsyncIterator[tuple[Client[Any], dict[str, Any]]]:
    fixture_path = os.environ.get("OP_MCP_SMOKE_FIXTURE")
    if not fixture_path:
        pytest.fail("Use scripts/live_smoke.py to create a disposable fixture first.")
    fixture = json.loads(Path(fixture_path).read_text())
    for key in ("admin_token", "outsider_token"):
        fixture[key] = SecretStr(fixture[key])
    settings = Settings(_env_file=None, url=fixture["url"], api_key=fixture["admin_token"])
    async with Client(build_server(settings)) as client:
        yield client, fixture


async def call(client: Client[Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments, raise_on_error=False)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def create(client: Client[Any], fixture: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return await call(
        client,
        "create_work_package",
        {
            "project": fixture["project_id"],
            "type": str(fixture["type_id"]),
            "subject": "Disposable compatibility check",
            "notify": False,
            **extra,
        },
    )


async def test_instance_and_custom_field_roundtrip(
    live: tuple[Client[Any], dict[str, Any]],
) -> None:
    client, fixture = live
    info = await call(client, "get_instance_info", {})
    assert info["core_version"]
    created = await create(client, fixture, custom_fields={fixture["custom_field"]: "created"})
    assert any(
        f["key"] == fixture["custom_field"] and f["value"] == "created"
        for f in created["custom_fields"]
    )
    updated = await call(
        client,
        "update_work_package",
        {
            "id": created["id"],
            "lock_version": created["lock_version"],
            "custom_fields": {"Smoke marker": "updated"},
            "notify": False,
        },
    )
    assert any(f["value"] == "updated" for f in updated["custom_fields"])


async def test_version_assignment_and_clearing(live: tuple[Client[Any], dict[str, Any]]) -> None:
    client, fixture = live
    versions = fixture["version_ids"]
    created = await create(client, fixture, target_versions=versions[:1])
    assert [v["id"] for v in created["target_versions"]] == versions[:1]
    listed = await call(
        client,
        "list_work_packages",
        {
            "project": fixture["project_id"],
            "version_ids": versions[:1],
            "status_scope": "all",
        },
    )
    assert created["id"] in [wp["id"] for wp in listed["items"]]
    if fixture["multiple_versions"]:
        updated = await call(
            client,
            "update_work_package",
            {
                "id": created["id"],
                "target_versions": versions,
                "notify": False,
            },
        )
        assert {v["id"] for v in updated["target_versions"]} == set(versions)
        assert updated["version"] is None
        for version_id in versions:
            listed = await call(
                client,
                "list_work_packages",
                {
                    "project": fixture["project_id"],
                    "version_ids": [version_id],
                    "status_scope": "all",
                },
            )
            assert created["id"] in [wp["id"] for wp in listed["items"]]
    else:
        result = await client.call_tool(
            "update_work_package",
            {
                "id": created["id"],
                "target_versions": versions,
                "notify": False,
            },
            raise_on_error=False,
        )
        assert result.is_error
    cleared = await call(
        client,
        "update_work_package",
        {
            "id": created["id"],
            "target_versions": [],
            "notify": False,
        },
    )
    assert cleared["target_versions"] == [] and cleared["version"] is None


async def test_batch_preview_apply_and_conflict(live: tuple[Client[Any], dict[str, Any]]) -> None:
    client, fixture = live
    created = await create(client, fixture)
    update = {"id": created["id"], "changes": {"subject": "Applied from preview"}}
    preview = await call(client, "bulk_update_work_packages", {"updates": [update]})
    current = await call(client, "get_work_package", {"id": created["id"]})
    assert current["subject"] == created["subject"]
    update["lock_version"] = preview["items"][0]["lock_version"]
    applied = await call(
        client,
        "bulk_update_work_packages",
        {
            "updates": [update],
            "dry_run": False,
            "notify": False,
        },
    )
    assert applied["applied"] == 1
    stale = await call(
        client,
        "bulk_update_work_packages",
        {
            "updates": [update],
            "dry_run": False,
            "notify": False,
        },
    )
    assert stale["applied"] == 0 and stale["items"][0]["error"]["type"] == "conflict"


async def test_permissions_do_not_grant_outsiders_access(
    live: tuple[Client[Any], dict[str, Any]],
) -> None:
    client, fixture = live
    permissions = await call(client, "list_permissions", {"project_id": fixture["project_id"]})
    assert permissions["capability_count"] > 0
    created = await create(client, fixture)
    settings = Settings(_env_file=None, url=fixture["url"], api_key=fixture["outsider_token"])
    async with Client(build_server(settings)) as outsider:
        for tool, args in [
            ("get_work_package", {"id": created["id"]}),
            ("update_work_package", {"id": created["id"], "subject": "Forbidden"}),
        ]:
            result = await outsider.call_tool(tool, args, raise_on_error=False)
            assert result.is_error
            error = json.loads(result.content[0].text)["error"]
            assert error["http_status"] in (403, 404)


async def test_meeting_api_variant(live: tuple[Client[Any], dict[str, Any]]) -> None:
    client, fixture = live
    args = {
        "project_id": fixture["project_id"],
        "title": "Disposable review",
        "start_time": (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat(),
        "duration_minutes": 30,
    }
    result = await client.call_tool("create_meeting", args, raise_on_error=False)
    if fixture["core_version"].startswith("17.8"):
        assert not result.is_error, result.content
        meeting = result.structured_content
        read = await call(client, "get_meeting", {"meeting_id": meeting["id"]})
        assert read["title"] == args["title"]
        await call(client, "delete_meeting", {"meeting_id": meeting["id"], "confirm": True})
    else:
        assert result.is_error
        error = json.loads(result.content[0].text)["error"]
        assert error["http_status"] in (404, 405)
        assert "version" in error["hint"].lower() or "17." in error["hint"]
