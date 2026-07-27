"""Shared test fixtures. No test in this suite touches the network.

Fixtures Phase 1 will use:

``settings``      a zero-environment :class:`Settings` pointing at ``TEST_URL``
``mock_api``      a ``respx`` router rooted at the API base; define routes with
                  relative paths (``mock_api.get("work_packages")``)
``op_client``     an :class:`OpenProjectClient` whose backoff sleeps are recorded
                  instead of awaited (see ``sleep_calls``)
``mcp_server``    ``build_server(settings)``
``mcp_client``    an in-memory ``fastmcp.Client`` connected to it
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from fastmcp import Client, FastMCP

from openproject_mcp.client.cache import TTLCache
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.config import Settings
from openproject_mcp.server import build_server

TEST_URL = "https://openproject.test"
API_BASE = f"{TEST_URL}/api/v3"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's real OpenProject environment out of the tests."""
    for key in list(os.environ):
        if key.startswith("OPENPROJECT"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Fully-specified settings that never read a ``.env`` file."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        url=TEST_URL,
        api_key="test-token",
        cache_ttl=300.0,
        max_retries=3,
    )


@pytest.fixture
def api_base() -> str:
    return API_BASE


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    """A respx router rooted at ``/api/v3``; unmatched requests fail loudly."""
    with respx.mock(base_url=API_BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
def sleep_calls() -> list[float]:
    """Backoff delays the client would have slept for."""
    return []


@pytest.fixture
async def op_client(
    settings: Settings, sleep_calls: list[float]
) -> AsyncIterator[OpenProjectClient]:
    """A client with deterministic, instant retries."""

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    client = OpenProjectClient(settings, sleep=fake_sleep, jitter=lambda _: 0.0)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def cache() -> TTLCache:
    return TTLCache(ttl=300.0)


@pytest.fixture
def mcp_server(settings: Settings) -> FastMCP:
    return build_server(settings)


@pytest.fixture
async def mcp_client(mcp_server: FastMCP) -> AsyncIterator[Client[Any]]:
    async with Client(mcp_server) as client:
        yield client
