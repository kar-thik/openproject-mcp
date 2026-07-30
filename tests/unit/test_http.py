"""Client behavior under respx: retries, write safety, redirects, error mapping."""

from __future__ import annotations

import httpx
import pytest
import respx

from openproject_mcp.client.errors import (
    NetworkError,
    NotFoundError,
    RateLimitedError,
    UnexpectedResponseError,
    UpstreamServerError,
    ValidationFailedError,
)
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.config import Settings
from tests.conftest import API_BASE, TEST_URL
from tests.fixtures.hal_payloads import VALIDATION_ERROR, WORK_PACKAGE


async def test_get_json_returns_the_body(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/1234").mock(return_value=httpx.Response(200, json=WORK_PACKAGE))
    assert (await op_client.get_json("work_packages/1234"))["id"] == 1234


async def test_base_url_targets_the_api_root(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("work_packages/1").mock(return_value=httpx.Response(200, json={}))
    await op_client.get_json("work_packages/1")
    assert str(route.calls.last.request.url) == f"{API_BASE}/work_packages/1"


async def test_auth_uses_basic_apikey_scheme(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("users/me").mock(return_value=httpx.Response(200, json={}))
    await op_client.get_json("users/me")
    header = route.calls.last.request.headers["authorization"]
    assert header.startswith("Basic ")
    import base64

    assert base64.b64decode(header.removeprefix("Basic ")).decode() == "apikey:test-token"


async def test_params_are_sent_as_query_parameters(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("work_packages").mock(return_value=httpx.Response(200, json={}))
    await op_client.get_json(
        "work_packages",
        params={"filters": '[{"status":{"operator":"o","values":[]}}]', "offset": 2},
    )
    request_url = route.calls.last.request.url
    assert request_url.params["offset"] == "2"
    assert request_url.params["filters"] == '[{"status":{"operator":"o","values":[]}}]'


# --- retries --------------------------------------------------------------


async def test_read_retries_on_429_and_honors_retry_after(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, sleep_calls: list[float]
) -> None:
    route = mock_api.get("work_packages").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
            httpx.Response(200, json={"total": 0}),
        ]
    )
    assert await op_client.get_json("work_packages") == {"total": 0}
    assert route.call_count == 2
    assert sleep_calls == [7.0]


async def test_read_retries_on_503_with_exponential_backoff(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, sleep_calls: list[float]
) -> None:
    route = mock_api.get("work_packages").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(200, json={"total": 1}),
        ]
    )
    assert await op_client.get_json("work_packages") == {"total": 1}
    assert route.call_count == 3
    assert sleep_calls == [0.5, 1.0]


async def test_read_gives_up_after_max_attempts(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, sleep_calls: list[float]
) -> None:
    route = mock_api.get("work_packages").mock(return_value=httpx.Response(503))
    with pytest.raises(UpstreamServerError):
        await op_client.get_json("work_packages")
    assert route.call_count == 3
    assert len(sleep_calls) == 2


async def test_huge_retry_after_is_reported_instead_of_slept_on(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, sleep_calls: list[float]
) -> None:
    mock_api.get("work_packages").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"})
    )
    with pytest.raises(RateLimitedError) as excinfo:
        await op_client.get_json("work_packages")
    assert excinfo.value.retry_after == 3600.0
    assert sleep_calls == []


async def test_transport_errors_are_retried_then_mapped(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, sleep_calls: list[float]
) -> None:
    route = mock_api.get("work_packages").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(NetworkError):
        await op_client.get_json("work_packages")
    assert route.call_count == 3
    assert len(sleep_calls) == 2


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
async def test_writes_are_never_retried(
    method: str,
    op_client: OpenProjectClient,
    mock_api: respx.MockRouter,
    sleep_calls: list[float],
) -> None:
    route = mock_api.request(method, "work_packages").mock(return_value=httpx.Response(503))
    with pytest.raises(UpstreamServerError):
        await op_client.request(method, "work_packages", json={})
    assert route.call_count == 1
    assert sleep_calls == []


async def test_write_rate_limit_is_not_retried_either(
    op_client: OpenProjectClient, mock_api: respx.MockRouter, sleep_calls: list[float]
) -> None:
    route = mock_api.post("work_packages").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    with pytest.raises(RateLimitedError):
        await op_client.post_json("work_packages", json={})
    assert route.call_count == 1
    assert sleep_calls == []


# --- error mapping --------------------------------------------------------


async def test_404_maps_to_not_found(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/9").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(NotFoundError):
        await op_client.get_json("work_packages/9")


async def test_422_maps_to_validation_failed_with_violations(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.post("work_packages").mock(return_value=httpx.Response(422, json=VALIDATION_ERROR))
    with pytest.raises(ValidationFailedError) as excinfo:
        await op_client.post_json("work_packages", json={})
    assert excinfo.value.violations[0]["attribute"] == "subject"


async def test_non_json_body_is_a_typed_error(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages").mock(return_value=httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(UnexpectedResponseError):
        await op_client.get_json("work_packages")


async def test_delete_accepts_empty_204(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.delete("work_packages/1").mock(return_value=httpx.Response(204))
    assert await op_client.delete("work_packages/1") == {}


# --- redirect hygiene -----------------------------------------------------


async def test_authorization_is_stripped_on_cross_origin_redirect(settings: Settings) -> None:
    with respx.mock(assert_all_called=True) as router:
        origin = router.get(f"{API_BASE}/attachments/5/content").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://files.example.com/blob?sig=abc"}
            )
        )
        storage = router.get("https://files.example.com/blob").mock(
            return_value=httpx.Response(200, content=b"file bytes")
        )
        client = OpenProjectClient(settings, sleep=_no_sleep, jitter=lambda _: 0.0)
        try:
            response = await client.request("GET", "attachments/5/content")
        finally:
            await client.aclose()

    assert response.content == b"file bytes"
    assert "authorization" in origin.calls.last.request.headers
    assert "authorization" not in storage.calls.last.request.headers


async def test_authorization_survives_same_origin_redirect(settings: Settings) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{API_BASE}/attachments/5/content").mock(
            return_value=httpx.Response(
                302, headers={"Location": f"{TEST_URL}/api/v3/attachments/5/download"}
            )
        )
        final = router.get(f"{API_BASE}/attachments/5/download").mock(
            return_value=httpx.Response(200, content=b"file bytes")
        )
        client = OpenProjectClient(settings, sleep=_no_sleep, jitter=lambda _: 0.0)
        try:
            await client.request("GET", "attachments/5/content")
        finally:
            await client.aclose()

    assert "authorization" in final.calls.last.request.headers


# --- streaming ------------------------------------------------------------


async def test_stream_yields_the_body_and_maps_errors(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("attachments/5/content").mock(
        return_value=httpx.Response(200, content=b"0123456789")
    )
    chunks = []
    async with op_client.stream("GET", "attachments/5/content") as response:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
    assert b"".join(chunks) == b"0123456789"

    mock_api.get("attachments/6/content").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(NotFoundError):
        async with op_client.stream("GET", "attachments/6/content"):
            pass


# --- headers --------------------------------------------------------------


async def test_accept_language_is_forwarded_when_configured() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        url=TEST_URL,
        api_key="test-token",
        accept_language="vi",
    )
    with respx.mock(base_url=API_BASE, assert_all_called=True) as router:
        route = router.get("users/me").mock(return_value=httpx.Response(200, json={}))
        client = OpenProjectClient(settings, sleep=_no_sleep, jitter=lambda _: 0.0)
        try:
            await client.get_json("users/me")
        finally:
            await client.aclose()
    assert route.calls.last.request.headers["accept-language"] == "vi"


async def _no_sleep(seconds: float) -> None:
    return None
