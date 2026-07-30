"""Per-request bearer auth on the HTTP transport, driven at the ASGI level.

These tests exercise the exact app ``--transport http`` serves
(``build_server(settings).http_app()``) through ``httpx.ASGITransport`` — no
sockets, no network. They pin the SPEC §11 contract: with
``OPENPROJECT_MCP_AUTH_TOKENS`` set, every request to the MCP endpoint must
present ``Authorization: Bearer <token>`` with a configured token or be
rejected with 401 + ``WWW-Authenticate``; with ``OPENPROJECT_MCP_INSECURE=1``
and no tokens the endpoint stays open; stdio and the in-memory client are
unaffected either way.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp import Client

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import TEST_URL

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "auth-test-client", "version": "0.0.0"},
    },
}


def _settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", **overrides
    )


async def _post_initialize(
    settings: Settings, headers: dict[str, str] | None = None
) -> httpx.Response:
    """POST an MCP ``initialize`` to the HTTP app and return the response."""
    app = build_server(settings).http_app()
    request_headers = {"Accept": "application/json, text/event-stream"}
    request_headers.update(headers or {})
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                app.state.path, json=INITIALIZE_REQUEST, headers=request_headers
            )
    return response


async def test_missing_authorization_is_rejected_with_www_authenticate() -> None:
    settings = _settings(auth_tokens="secret-token")
    app = build_server(settings).http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                app.state.path,
                json=INITIALIZE_REQUEST,
                headers={"Accept": "application/json, text/event-stream"},
            )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_wrong_token_is_rejected() -> None:
    settings = _settings(auth_tokens="secret-token")
    response = await _post_initialize(settings, {"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_valid_token_initializes() -> None:
    settings = _settings(auth_tokens="secret-token")
    response = await _post_initialize(settings, {"Authorization": "Bearer secret-token"})
    assert response.status_code == 200
    # A 200 alone could carry a JSON-RPC error; pin a real initialize result.
    assert "protocolVersion" in response.text
    assert "serverInfo" in response.text


async def test_every_configured_token_works() -> None:
    settings = _settings(auth_tokens="alpha, beta")
    for token in ("alpha", "beta"):
        response = await _post_initialize(settings, {"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, f"token {token!r} should authenticate"


async def test_insecure_mode_without_tokens_stays_open() -> None:
    settings = _settings(insecure=True)
    response = await _post_initialize(settings)
    assert response.status_code == 200


async def test_raw_token_without_bearer_scheme_is_rejected() -> None:
    settings = _settings(auth_tokens="secret-token")
    response = await _post_initialize(settings, {"Authorization": "secret-token"})
    assert response.status_code == 401


async def test_empty_token_list_rejects_every_request() -> None:
    """``AUTH_TOKENS=","`` must never yield an open endpoint."""
    settings = _settings(auth_tokens=",")
    for headers in (None, {"Authorization": "Bearer anything"}, {"Authorization": "Bearer "}):
        response = await _post_initialize(settings, headers)
        assert response.status_code == 401


async def test_other_methods_require_a_token_too() -> None:
    """GET/DELETE/HEAD on the MCP endpoint are gated exactly like POST."""
    settings = _settings(auth_tokens="secret-token")
    app = build_server(settings).http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            for method in ("GET", "DELETE", "HEAD"):
                response = await client.request(
                    method,
                    app.state.path,
                    headers={"Accept": "application/json, text/event-stream"},
                )
                assert response.status_code == 401, method


async def test_http_app_exposes_only_the_mcp_route() -> None:
    """A route added later (e.g. via a custom route) would bypass bearer auth."""
    settings = _settings(auth_tokens="secret-token")
    app = build_server(settings).http_app()
    assert [getattr(route, "path", None) for route in app.routes] == [app.state.path]


async def test_in_memory_client_is_unaffected_by_auth_tokens() -> None:
    """The in-memory transport bypasses HTTP middleware: no token required."""
    settings = _settings(auth_tokens="secret-token", admin_tools=True)
    async with Client(build_server(settings)) as client:
        assert await client.ping()
        listed = {tool.name for tool in await client.list_tools()}
    assert len(listed) == 72
