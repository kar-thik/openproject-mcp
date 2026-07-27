"""Structured logging: stderr only, JSON lines on demand, credentials redacted."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.config import Settings
from openproject_mcp.observability import (
    LOGGER_NAME,
    REDACTED,
    configure_logging,
    correlation_scope,
    current_correlation_id,
    get_logger,
    redact_headers,
)
from tests.conftest import TEST_URL


@pytest.fixture(autouse=True)
def restore_logging() -> None:
    """Leave the root server logger clean for other tests."""
    logger = logging.getLogger(LOGGER_NAME)
    handlers = list(logger.handlers)
    level = logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


def test_logs_go_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_level="INFO"))  # type: ignore[call-arg]
    get_logger("test").info("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello" in captured.err


def test_json_format_emits_one_object_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_format="json"))  # type: ignore[call-arg]
    get_logger("test").info("upstream request", extra={"status": 200, "path": "work_packages"})
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["message"] == "upstream request"
    assert payload["status"] == 200
    assert payload["path"] == "work_packages"
    assert payload["level"] == "INFO"


def test_text_format_appends_extra_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_format="text"))  # type: ignore[call-arg]
    get_logger("test").info("upstream request", extra={"status": 404})
    assert "status=404" in capsys.readouterr().err


def test_configure_logging_is_idempotent() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    configure_logging(settings)
    configure_logging(settings)
    assert len(logging.getLogger(LOGGER_NAME).handlers) == 1


def test_correlation_scope_binds_and_restores() -> None:
    assert current_correlation_id() is None
    with correlation_scope() as first:
        assert current_correlation_id() == first
        with correlation_scope("fixed") as second:
            assert second == "fixed"
            assert current_correlation_id() == "fixed"
        assert current_correlation_id() == first
    assert current_correlation_id() is None


def test_correlation_id_reaches_the_record(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_format="json"))  # type: ignore[call-arg]
    with correlation_scope("abc123"):
        get_logger("test").info("tool call")
    assert json.loads(capsys.readouterr().err.strip())["correlation_id"] == "abc123"


@pytest.mark.parametrize(
    "header", ["Authorization", "authorization", "Cookie", "Proxy-Authorization"]
)
def test_redact_headers_hides_credentials(header: str) -> None:
    redacted = redact_headers({header: "Basic c2VjcmV0", "Accept": "application/hal+json"})
    assert redacted[header] == REDACTED
    assert redacted["Accept"] == "application/hal+json"


async def test_info_logs_never_contain_the_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(_env_file=None, url=TEST_URL, api_key="super-secret")  # type: ignore[call-arg]
    configure_logging(settings)
    with respx.mock(base_url=f"{TEST_URL}/api/v3") as router:
        router.get("work_packages").mock(return_value=httpx.Response(200, json={"total": 0}))
        client = OpenProjectClient(settings)
        try:
            await client.get_json("work_packages", params={"filters": "[secret filter]"})
        finally:
            await client.aclose()

    logs = capsys.readouterr().err
    assert "super-secret" not in logs
    assert "secret filter" not in logs, "filter values are not logged at INFO"
    assert "work_packages" in logs


async def test_debug_body_logging_still_redacts_authorization(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        url=TEST_URL,
        api_key="super-secret",
        log_bodies=True,
        log_level="DEBUG",
    )
    configure_logging(settings)
    with respx.mock(base_url=f"{TEST_URL}/api/v3") as router:
        router.post("work_packages").mock(return_value=httpx.Response(201, json={"id": 1}))
        client = OpenProjectClient(settings)
        try:
            await client.post_json("work_packages", json={"subject": "hello"})
        finally:
            await client.aclose()

    logs = capsys.readouterr().err
    assert "subject" in logs, "bodies are logged when LOG_BODIES is on at DEBUG"
    assert "super-secret" not in logs
    assert REDACTED in logs
