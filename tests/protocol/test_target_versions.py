"""Version assignments across the legacy and 17.8 API dialects."""

import json
from copy import deepcopy
from typing import Any

import pytest
import respx
from fastmcp import Client

from tests.fixtures.work_packages_payloads import (
    CREATE_FORM_OK,
    UPDATE_FORM_OK,
    WORK_PACKAGE_DETAIL,
    WORK_PACKAGE_SCHEMA_5_1,
)


@pytest.mark.parametrize("modern,ids", [(False, [3]), (False, []), (True, [3, 4]), (True, [])])
async def test_read_and_update_versions(
    mock_api: respx.MockRouter, mcp_client: Client[Any], modern: bool, ids: list[int]
) -> None:
    current = deepcopy(WORK_PACKAGE_DETAIL)
    schema = deepcopy(WORK_PACKAGE_SCHEMA_5_1)
    if modern:
        schema["targetVersions"] = {"type": "[]Version", "writable": True}
        current["_links"]["targetVersions"] = [
            {"href": f"/api/v3/versions/{i}", "title": f"Release {i}"} for i in ids
        ]
    else:
        current["_links"]["version"] = {"href": f"/api/v3/versions/{ids[0]}" if ids else None}
    mock_api.get("work_packages/1234").respond(200, json=current)
    mock_api.get("work_packages/schemas/5-1").respond(200, json=schema)
    form = mock_api.post("work_packages/1234/form").respond(200, json=UPDATE_FORM_OK)
    patch = mock_api.patch("work_packages/1234").respond(200, json=current)
    result = await mcp_client.call_tool("update_work_package", {"id": 1234, "target_versions": ids})
    assert not result.is_error
    assert [v["id"] for v in result.structured_content["target_versions"]] == ids
    assert (result.structured_content["version"] is None) == (len(ids) != 1)
    for route in (form, patch):
        links = json.loads(route.calls[0].request.content)["_links"]
        if modern:
            assert links == {"targetVersions": [{"href": f"/api/v3/versions/{i}"} for i in ids]}
        else:
            assert links == {"version": {"href": f"/api/v3/versions/{ids[0]}" if ids else None}}


async def test_create_alias_uses_new_schema_and_drops_echoed_legacy_field(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    schema = {"targetVersions": {"type": "[]Version", "writable": True}}
    mock_api.get("work_packages/schemas/5-1").respond(200, json=schema)
    form_body = deepcopy(CREATE_FORM_OK)
    form_body["_embedded"]["payload"].setdefault("_links", {})["version"] = {"href": None}
    form = mock_api.post("work_packages/form").respond(200, json=form_body)
    create = mock_api.post("work_packages").respond(201, json=WORK_PACKAGE_DETAIL)
    await mcp_client.call_tool(
        "create_work_package", {"project": 5, "type": "1", "subject": "Release", "version": "3"}
    )
    for route in (form, create):
        links = json.loads(route.calls[0].request.content)["_links"]
        assert links["targetVersions"] == [{"href": "/api/v3/versions/3"}]
        assert "version" not in links


@pytest.mark.parametrize(
    "arguments",
    [
        {"target_versions": [3, 4]},
        {"target_versions": [], "version": "3"},
    ],
)
async def test_legacy_multi_and_conflicting_arguments_never_write(
    mock_api: respx.MockRouter, mcp_client: Client[Any], arguments: dict[str, Any]
) -> None:
    mock_api.get("work_packages/1234").respond(200, json=WORK_PACKAGE_DETAIL)
    mock_api.get("work_packages/schemas/5-1").respond(200, json=WORK_PACKAGE_SCHEMA_5_1)
    result = await mcp_client.call_tool(
        "update_work_package", {"id": 1234, **arguments}, raise_on_error=False
    )
    assert result.is_error
    assert all(c.request.method == "GET" for c in mock_api.calls)


async def test_omitted_versions_are_untouched(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get("work_packages/1234").respond(200, json=WORK_PACKAGE_DETAIL)
    mock_api.get("work_packages/schemas/5-1").respond(200, json=WORK_PACKAGE_SCHEMA_5_1)
    mock_api.post("work_packages/1234/form").respond(200, json=UPDATE_FORM_OK)
    patch = mock_api.patch("work_packages/1234").respond(200, json=WORK_PACKAGE_DETAIL)
    await mcp_client.call_tool("update_work_package", {"id": 1234, "subject": "Renamed"})
    assert "_links" not in json.loads(patch.calls[0].request.content)


# --- sprint assignment ------------------------------------------------------


async def test_update_assigns_and_clears_a_sprint(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    current = deepcopy(WORK_PACKAGE_DETAIL)
    schema = {**deepcopy(WORK_PACKAGE_SCHEMA_5_1), "sprint": {"type": "Sprint", "writable": True}}
    current["_links"]["sprint"] = {"href": "/api/v3/sprints/101", "title": "Sprint 11"}
    mock_api.get("work_packages/1234").respond(200, json=current)
    mock_api.get("work_packages/schemas/5-1").respond(200, json=schema)
    mock_api.post("work_packages/1234/form").respond(200, json=UPDATE_FORM_OK)
    patch = mock_api.patch("work_packages/1234").respond(200, json=current)

    result = await mcp_client.call_tool("update_work_package", {"id": 1234, "sprint": "101"})
    assert not result.is_error
    assert result.structured_content["sprint"] == {"id": 101, "name": "Sprint 11"}
    links = json.loads(patch.calls[0].request.content)["_links"]
    assert links == {"sprint": {"href": "/api/v3/sprints/101"}}

    result = await mcp_client.call_tool("update_work_package", {"id": 1234, "sprint": None})
    links = json.loads(patch.calls[1].request.content)["_links"]
    assert links == {"sprint": {"href": None}}
    assert result.structured_content["sprint"] == {"id": 101, "name": "Sprint 11"}


async def test_update_sprint_without_schema_support_fails_before_writing(
    mock_api: respx.MockRouter, mcp_client: Client[Any]
) -> None:
    mock_api.get("work_packages/1234").respond(200, json=WORK_PACKAGE_DETAIL)
    mock_api.get("work_packages/schemas/5-1").respond(200, json=WORK_PACKAGE_SCHEMA_5_1)
    result = await mcp_client.call_tool(
        "update_work_package", {"id": 1234, "sprint": "101"}, raise_on_error=False
    )
    assert result.is_error
    assert all(c.request.method == "GET" for c in mock_api.calls)


async def test_create_assigns_a_sprint(mock_api: respx.MockRouter, mcp_client: Client[Any]) -> None:
    schema = {**deepcopy(WORK_PACKAGE_SCHEMA_5_1), "sprint": {"type": "Sprint", "writable": True}}
    mock_api.get("projects/5").respond(
        200, json={"_type": "Project", "id": 5, "_links": {"self": {"href": "/api/v3/projects/5"}}}
    )
    mock_api.get("types").respond(
        200,
        json={
            "_type": "Collection",
            "total": 1,
            "count": 1,
            "_embedded": {
                "elements": [
                    {
                        "_type": "Type",
                        "id": 1,
                        "name": "Task",
                        "_links": {"self": {"href": "/api/v3/types/1"}},
                    }
                ]
            },
        },
    )
    mock_api.get("work_packages/schemas/5-1").respond(200, json=schema)
    form = mock_api.post("work_packages/form").respond(200, json=CREATE_FORM_OK)
    created = deepcopy(WORK_PACKAGE_DETAIL)
    created["_links"]["sprint"] = {"href": "/api/v3/sprints/101", "title": "Sprint 11"}
    create = mock_api.post("work_packages").respond(201, json=created)
    result = await mcp_client.call_tool(
        "create_work_package",
        {"project": 5, "type": "1", "subject": "Planned", "sprint": "101"},
    )
    assert not result.is_error
    assert result.structured_content["sprint"] == {"id": 101, "name": "Sprint 11"}
    for route in (form, create):
        links = json.loads(route.calls[0].request.content)["_links"]
        assert links["sprint"] == {"href": "/api/v3/sprints/101"}
