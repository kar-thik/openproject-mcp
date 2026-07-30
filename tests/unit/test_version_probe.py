"""Version probe: root parsing, version gates, and the documented fallbacks."""

from __future__ import annotations

import httpx
import pytest
import respx

from openproject_mcp.client.cache import TTLCache
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.version_probe import (
    InstanceProbe,
    cached_capabilities_context,
    get_probe,
    parse_version,
    probe_capabilities_context,
    probe_from_root,
    probe_time_entry_filter,
)
from tests.conftest import API_BASE
from tests.fixtures.hal_payloads import API_ROOT

INVALID_FILTER = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Filters Entity id is not a valid filter.",
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("17.7.1", (17, 7, 1)),
        ("14.6", (14, 6, 0)),
        ("17.7.1-dev", (17, 7, 1)),
        ("16", (16, 0, 0)),
        (None, None),
        ("unknown", None),
    ],
)
def test_parse_version(value: str | None, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(value) == expected


def test_feature_gates_from_root() -> None:
    probe = probe_from_root(API_ROOT)
    assert probe.core_version == "17.7.1"
    assert probe.instance_name == "Test OpenProject"
    assert probe.supports_internal_comments
    assert probe.supports_emoji_reactions
    assert probe.supports_project_favorites


def test_feature_gates_on_14_lts() -> None:
    probe = probe_from_root({**API_ROOT, "coreVersion": "14.6.1"})
    assert not probe.supports_internal_comments
    assert not probe.supports_emoji_reactions
    assert not probe.supports_project_favorites


def test_unknown_version_degrades_explicitly() -> None:
    probe = probe_from_root({"instanceName": "Mystery"})
    assert probe.core_version is None
    assert not probe.supports_internal_comments
    assert probe.notes and "core version" in probe.notes[0]


async def test_time_entry_filter_prefers_entity_id(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json={"total": 0}))
    assert await probe_time_entry_filter(op_client) == "entityId"


async def test_time_entry_filter_falls_back_on_400(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("time_entries").mock(return_value=httpx.Response(400, json=INVALID_FILTER))
    assert await probe_time_entry_filter(op_client) == "workPackage"


async def test_time_entry_filter_result_is_cached(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, cache: TTLCache
) -> None:
    route = mock_api.get("time_entries").mock(return_value=httpx.Response(400, json=INVALID_FILTER))
    assert await probe_time_entry_filter(op_client, cache) == "workPackage"
    assert await probe_time_entry_filter(op_client, cache) == "workPackage"
    assert route.call_count == 1


async def test_capabilities_context_falls_back_to_w_prefix(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("capabilities").mock(return_value=httpx.Response(400, json=INVALID_FILTER))
    assert await probe_capabilities_context(op_client, 7) == "w"


async def test_capabilities_context_keeps_p_prefix(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("capabilities").mock(return_value=httpx.Response(200, json={"total": 0}))
    assert await probe_capabilities_context(op_client, 7) == "p"


async def test_capabilities_context_lands_on_the_probe_tools_see(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, cache: TTLCache
) -> None:
    """The prefix is discovered later than the probe, so it is overlaid on read."""
    mock_api.get(url=f"{API_BASE}/").mock(return_value=httpx.Response(200, json=API_ROOT))
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json={"total": 0}))
    mock_api.get("capabilities").mock(return_value=httpx.Response(400, json=INVALID_FILTER))

    assert (await get_probe(op_client, cache)).capabilities_context_prefix is None

    assert await probe_capabilities_context(op_client, 7, cache) == "w"
    assert cached_capabilities_context(cache, op_client.scope) == "w"
    assert (await get_probe(op_client, cache)).capabilities_context_prefix == "w"


def test_an_unprobed_capabilities_context_is_none(cache: TTLCache) -> None:
    assert cached_capabilities_context(cache, "scope") is None
    assert cached_capabilities_context(None, "scope") is None


async def test_get_probe_runs_lazily_and_caches(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, cache: TTLCache
) -> None:
    root_route = mock_api.get(url=f"{API_BASE}/").mock(
        return_value=httpx.Response(200, json=API_ROOT)
    )
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json={"total": 0}))

    probe = await get_probe(op_client, cache)
    assert isinstance(probe, InstanceProbe)
    assert probe.time_entry_work_package_filter == "entityId"

    await get_probe(op_client, cache)
    assert root_route.call_count == 1


async def test_unreachable_instance_yields_notes_not_an_exception(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, cache: TTLCache
) -> None:
    mock_api.get(url=f"{API_BASE}/").mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )
    probe = await get_probe(op_client, cache)
    assert probe.core_version is None
    assert probe.notes and "probe unavailable" in probe.notes[0]
