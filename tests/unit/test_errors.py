"""Error taxonomy: every status maps to the right exception, hint and envelope."""

from __future__ import annotations

import httpx
import pytest

from openproject_mcp.client.errors import (
    AuthenticationError,
    ConflictError,
    NetworkError,
    NotFoundError,
    OpenProjectError,
    PermissionDeniedError,
    RateLimitedError,
    UnexpectedResponseError,
    UpstreamServerError,
    ValidationFailedError,
    error_from_response,
    error_from_transport,
    hint_for_status,
    parse_retry_after,
    violations_from_form,
)
from tests.fixtures.hal_payloads import MULTIPLE_ERRORS, VALIDATION_ERROR

REQUEST = httpx.Request("GET", "https://openproject.test/api/v3/work_packages/1")


def response(status: int, json: object | None = None, headers: dict[str, str] | None = None):
    return httpx.Response(status, json=json, headers=headers, request=REQUEST)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ValidationFailedError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, ValidationFailedError),
        (429, RateLimitedError),
        (500, UpstreamServerError),
        (502, UpstreamServerError),
        (503, UpstreamServerError),
        (504, UpstreamServerError),
        (405, UnexpectedResponseError),
        (415, UnexpectedResponseError),
    ],
)
def test_status_maps_to_taxonomy_member(status: int, expected: type[OpenProjectError]) -> None:
    error = error_from_response(response(status, {"message": "boom"}))
    assert isinstance(error, expected)
    assert error.http_status == status
    assert error.hint, "every mapped error carries an actionable hint"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 500, 503])
def test_hint_table_is_specific_per_status(status: int) -> None:
    hint = hint_for_status(status)
    assert hint
    assert hint == hint_for_status(status)


def test_hints_are_distinct_across_client_statuses() -> None:
    hints = {hint_for_status(status) for status in (401, 403, 404, 409, 422, 429)}
    assert len(hints) == 6


def test_401_hint_names_the_credential_env_var() -> None:
    assert "OPENPROJECT_API_KEY" in hint_for_status(401)


def test_validation_error_extracts_single_violation() -> None:
    error = error_from_response(response(422, VALIDATION_ERROR))
    assert isinstance(error, ValidationFailedError)
    assert error.message == "Subject can't be blank."
    assert error.violations == [{"attribute": "subject", "message": "Subject can't be blank."}]
    assert error.error_identifier and error.error_identifier.endswith("PropertyConstraintViolation")


def test_validation_error_extracts_multiple_violations() -> None:
    error = error_from_response(response(422, MULTIPLE_ERRORS))
    assert [item["attribute"] for item in error.violations] == ["subject", "type"]


def test_envelope_shape_matches_spec() -> None:
    error = error_from_response(response(422, VALIDATION_ERROR))
    envelope = error.to_envelope()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert body["type"] == "validation_failed"
    assert body["http_status"] == 422
    assert body["message"] == "Subject can't be blank."
    assert body["violations"][0]["attribute"] == "subject"
    assert body["hint"]


def test_server_error_body_is_never_echoed() -> None:
    secret_html = "<html><body>stack trace with /etc/passwd</body></html>"
    error = error_from_response(
        httpx.Response(
            500, text=secret_html, headers={"content-type": "text/html"}, request=REQUEST
        )
    )
    assert isinstance(error, UpstreamServerError)
    assert "stack trace" not in error.to_json()


def test_html_body_on_4xx_is_discarded_but_status_kept() -> None:
    error = error_from_response(
        httpx.Response(
            404,
            text="<html>Not Found</html>",
            headers={"content-type": "text/html"},
            request=REQUEST,
        )
    )
    assert isinstance(error, NotFoundError)
    assert "<html>" not in error.message


def test_rate_limit_carries_retry_after() -> None:
    error = error_from_response(response(429, {"message": "slow down"}, {"Retry-After": "12"}))
    assert isinstance(error, RateLimitedError)
    assert error.retry_after == 12.0
    assert error.to_envelope()["error"]["retry_after"] == 12.0


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("", None), ("5", 5.0), ("0", 0.0), ("not-a-date", None)],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_parse_retry_after_accepts_http_date() -> None:
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_conflict_carries_snapshot_fields() -> None:
    error = ConflictError(
        "stale",
        lock_version=9,
        current={"subject": "fresh"},
        conflicting_fields={"subject": {"attempted": "mine", "current": "fresh"}},
    )
    body = error.to_envelope()["error"]
    assert body["lock_version"] == 9
    assert body["current"] == {"subject": "fresh"}
    assert body["conflicting_fields"]["subject"]["current"] == "fresh"


def test_transport_error_becomes_network_error() -> None:
    error = error_from_transport(httpx.ConnectError("nodename nor servname provided"))
    assert isinstance(error, NetworkError)
    assert error.http_status is None
    assert "OPENPROJECT_URL" in (error.hint or "")


def test_form_validation_errors_are_normalized() -> None:
    violations = violations_from_form(
        {
            "subject": {
                "_type": "Error",
                "message": "Subject can't be blank.",
                "_embedded": {"details": {"attribute": "subject"}},
            },
            "dueDate": {"_type": "Error", "message": "Due date must be after start date."},
        }
    )
    assert violations == [
        {"attribute": "subject", "message": "Subject can't be blank."},
        {"attribute": "dueDate", "message": "Due date must be after start date."},
    ]


def test_message_falls_back_to_status_when_body_has_none() -> None:
    error = error_from_response(response(403, {"_type": "Error"}))
    assert "403" in error.message
