"""The pooled OpenProject API client (SPEC §4.1).

One :class:`httpx.AsyncClient` per server process, created in the FastMCP
lifespan and shared by every tool. It owns:

* auth — HTTP Basic ``apikey:<token>`` (default) or ``Bearer <oauth-token>``,
  held as ``SecretStr`` and never logged;
* retries — **reads only** (guarantee G6): 429 honoring ``Retry-After``,
  502/503/504 and transport errors, exponential backoff with jitter;
* redirect hygiene — ``Authorization`` is stripped on cross-origin redirects,
  which is how attachment downloads reach presigned S3 URLs without leaking
  credentials to the object store;
* error mapping — every non-2xx response becomes a typed error from
  :mod:`openproject_mcp.client.errors`;
* structured request logging to stderr with credentials redacted (SPEC §12).

Paths passed to these methods are relative to the API root:
``"work_packages/123"`` → ``https://host/api/v3/work_packages/123``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from openproject_mcp import __version__
from openproject_mcp.client.cache import credential_scope
from openproject_mcp.client.errors import (
    UnexpectedResponseError,
    error_from_response,
    error_from_transport,
    parse_retry_after,
)
from openproject_mcp.client.hal import as_array, as_object
from openproject_mcp.config import Settings
from openproject_mcp.observability import current_correlation_id, get_logger, redact_headers

__all__ = ["OpenProjectClient"]

logger = get_logger("client")

RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

BASE_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 4.0
#: A Retry-After longer than this is reported to the model instead of slept on.
MAX_HONORED_RETRY_AFTER = 60.0

SleepFn = Callable[[float], Awaitable[None]]
JitterFn = Callable[[float], float]


class BearerAuth(httpx.Auth):
    """``Authorization: Bearer <token>`` for OAuth deployments."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class OpenProjectClient:
    """Async OpenProject API v3 client.

    Args:
        settings: resolved configuration; ``url`` and a credential are required.
        client: inject a pre-built ``httpx.AsyncClient`` (tests, custom transports).
        sleep: injected for tests so backoff does not cost wall-clock time.
        jitter: injected for tests; default is ``random.uniform(0, x)``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn | None = None,
        jitter: JitterFn | None = None,
    ) -> None:
        self.settings = settings
        self.scope = credential_scope(settings.credential_secret)
        self._owns_client = client is None
        self._sleep = sleep or self._default_sleep
        self._jitter: JitterFn = jitter or (lambda upper: random.uniform(0, upper))
        self._client = client or self._build_client(settings)
        self._base_origin = _origin(self._client.base_url)

    # --- construction ----------------------------------------------------

    def _build_client(self, settings: Settings) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/hal+json",
            "User-Agent": f"openproject-mcp/{__version__}",
        }
        if settings.accept_language:
            headers["Accept-Language"] = settings.accept_language
        return httpx.AsyncClient(
            base_url=settings.api_base_url,
            http2=True,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=settings.read_timeout,
                write=settings.write_timeout,
                pool=settings.pool_timeout,
            ),
            limits=httpx.Limits(max_connections=settings.max_connections),
            headers=headers,
            auth=self._build_auth(settings),
            verify=str(settings.ca_bundle) if settings.ca_bundle else True,
            follow_redirects=True,
            event_hooks={"request": [self._strip_auth_on_cross_origin, self._log_outgoing]},
        )

    @staticmethod
    def _build_auth(settings: Settings) -> httpx.Auth | None:
        if settings.api_key is not None:
            return httpx.BasicAuth("apikey", settings.api_key.get_secret_value())
        if settings.oauth_token is not None:
            return BearerAuth(settings.oauth_token.get_secret_value())
        return None

    async def _strip_auth_on_cross_origin(self, request: httpx.Request) -> None:
        """Drop credentials when a redirect leaves the OpenProject origin.

        Attachment downloads 302 to presigned object-store URLs that authorize
        themselves; forwarding Basic credentials there is the classic leak.
        httpx also strips on cross-origin redirects, but this hook makes the
        guarantee explicit and independently testable (SPEC §11).
        """
        if _origin(request.url) != self._base_origin:
            request.headers.pop("Authorization", None)
            request.headers.pop("Cookie", None)

    async def _log_outgoing(self, request: httpx.Request) -> None:
        """DEBUG + ``LOG_BODIES`` only: the real headers, with credentials redacted."""
        if not self.settings.log_bodies or not logger.isEnabledFor(logging.DEBUG):
            return
        logger.debug(
            "upstream request headers",
            extra={
                "correlation_id": current_correlation_id(),
                "method": request.method,
                "path": request.url.path,
                "headers": redact_headers(request.headers),
            },
        )

    # --- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> OpenProjectClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def httpx_client(self) -> httpx.AsyncClient:
        """The underlying client — for streaming and other advanced flows."""
        return self._client

    # --- requests --------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        data: Mapping[str, Any] | None = None,
        files: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
        retry: bool | None = None,
    ) -> httpx.Response:
        """Perform a request, retrying reads and raising typed errors.

        Args:
            method: HTTP verb; anything outside GET/HEAD/OPTIONS is a write and
                is never retried (G6) unless ``retry`` is forced.
            path: path relative to ``/api/v3``.
            retry: override the read/write retry decision. Only pass ``True``
                for verbs you know are idempotent upstream.

        Raises:
            OpenProjectError: one of the taxonomy members, always.
        """
        verb = method.upper()
        retryable = verb in READ_METHODS if retry is None else retry
        max_attempts = self.settings.max_retries if retryable else 1
        request_kwargs: dict[str, Any] = {
            "params": dict(params) if params else None,
            "json": json,
            "data": data,
            "files": files,
            "headers": dict(headers) if headers else None,
        }
        if timeout is not None:
            request_kwargs["timeout"] = timeout

        self._log_payload(verb, path, params=params, body=json)

        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                response = await self._client.request(verb, path, **request_kwargs)
            except httpx.HTTPError as exc:
                self._log_request(verb, path, None, started, attempt, error=type(exc).__name__)
                if retryable and attempt < max_attempts:
                    await self._backoff(attempt)
                    continue
                raise error_from_transport(exc) from exc

            self._log_request(verb, path, response.status_code, started, attempt)

            if response.is_success:
                return response

            if retryable and attempt < max_attempts and response.status_code in RETRYABLE_STATUSES:
                delay = self._retry_delay(response, attempt)
                if delay is not None:
                    await self._sleep(delay)
                    continue

            raise error_from_response(response)

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict[str, Any]:
        response = await self.request("GET", path, params=params, headers=headers, timeout=timeout)
        return _decode_json(response)

    async def post_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        files: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict[str, Any]:
        response = await self.request(
            "POST",
            path,
            json=json,
            params=params,
            data=data,
            files=files,
            headers=headers,
            timeout=timeout,
        )
        return _decode_json(response)

    async def patch_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict[str, Any]:
        response = await self.request(
            "PATCH", path, json=json, params=params, headers=headers, timeout=timeout
        )
        return _decode_json(response)

    async def delete(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict[str, Any]:
        """DELETE a resource; ``204 No Content`` yields an empty dict."""
        response = await self.request(
            "DELETE", path, params=params, headers=headers, timeout=timeout
        )
        return _decode_json(response, allow_empty=True)

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> AsyncGenerator[httpx.Response]:
        """Stream a response body (attachment download).

        Streamed bodies get no read timeout — callers enforce a wall-clock and
        size cap instead (SPEC §4.1/§7.1). Errors are mapped to the taxonomy
        after reading the (small) error body.
        """
        effective_timeout = (
            timeout
            if timeout is not None
            else httpx.Timeout(
                connect=self.settings.connect_timeout,
                read=None,
                write=self.settings.write_timeout,
                pool=self.settings.pool_timeout,
            )
        )
        started = time.perf_counter()
        verb = method.upper()
        try:
            async with self._client.stream(
                verb,
                path,
                params=dict(params) if params else None,
                headers=dict(headers) if headers else None,
                timeout=effective_timeout,
            ) as response:
                self._log_request(verb, path, response.status_code, started, 1)
                if not response.is_success:
                    await response.aread()
                    raise error_from_response(response)
                yield response
        except httpx.HTTPError as exc:
            raise error_from_transport(exc) from exc

    # --- retry helpers ---------------------------------------------------

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before the next attempt, or ``None`` to give up."""
        if response.status_code == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            if retry_after is not None:
                return retry_after if retry_after <= MAX_HONORED_RETRY_AFTER else None
        return self._backoff_delay(attempt)

    def _backoff_delay(self, attempt: int) -> float:
        base = min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
        return base + self._jitter(base / 2)

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(self._backoff_delay(attempt))

    @staticmethod
    async def _default_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    # --- logging ---------------------------------------------------------

    def _log_payload(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        body: Any | None,
    ) -> None:
        """Log query values and the request body — DEBUG + ``LOG_BODIES`` only.

        Filter values and bodies are deliberately absent from INFO logs; this is
        the development-only escape hatch (SPEC §12). Headers are redacted even
        here, so credentials never reach a log record.
        """
        if not self.settings.log_bodies or not logger.isEnabledFor(logging.DEBUG):
            return
        logger.debug(
            "upstream request payload",
            extra={
                "correlation_id": current_correlation_id(),
                "method": method,
                "path": path,
                "params": dict(params) if params else None,
                "body": body,
                "headers": redact_headers(self._client.headers),
            },
        )

    def _log_request(
        self,
        method: str,
        path: str,
        status: int | None,
        started: float,
        attempt: int,
        *,
        error: str | None = None,
    ) -> None:
        """Log one upstream call: no query values, no bodies, no credentials."""
        logger.info(
            "upstream request",
            extra={
                "correlation_id": current_correlation_id(),
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "attempt": attempt,
                "error": error,
            },
        )


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _decode_json(response: httpx.Response, *, allow_empty: bool = False) -> dict[str, Any]:
    """Decode a JSON body into a dict, or raise a typed error."""
    if not response.content:
        if allow_empty or response.status_code == 204:
            return {}
        raise UnexpectedResponseError(
            f"OpenProject returned an empty body for {response.request.url.path}.",
            http_status=response.status_code,
            hint="Expected a JSON document. This usually means a proxy altered the response.",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise UnexpectedResponseError(
            f"OpenProject returned a non-JSON body for {response.request.url.path}.",
            http_status=response.status_code,
            hint="Expected JSON. Check that OPENPROJECT_URL points at the API, not a login page.",
        ) from exc
    body = as_object(payload)
    if body is not None:
        return dict(body)
    array = as_array(payload)
    if array is not None:
        return {"_embedded": {"elements": list(array)}}
    raise UnexpectedResponseError(
        f"OpenProject returned an unexpected JSON type for {response.request.url.path}.",
        http_status=response.status_code,
        hint="Expected a JSON object.",
    )
