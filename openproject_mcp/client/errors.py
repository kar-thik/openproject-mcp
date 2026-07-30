"""Typed error taxonomy and the structured envelope tools return (SPEC §4.2).

Every failure an agent can encounter is one of these exceptions. They carry
enough machine-usable detail (``http_status``, ``error_identifier``,
``violations``) plus an always-English ``hint`` telling the model what to do
next. :func:`error_from_response` turns any non-2xx httpx response into the
right one; :func:`error_from_transport` does the same for connection failures.

Upstream response bodies are never echoed verbatim (guarantee G4): non-JSON
bodies (HTML error pages, proxy notices) are discarded and replaced with a
generic message.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import httpx

from openproject_mcp.client.hal import as_array, as_object, as_objects

__all__ = [
    "AttachmentQuarantinedError",
    "AttachmentTooLargeError",
    "AuthenticationError",
    "ConfirmationRequiredError",
    "ConflictError",
    "InputValidationError",
    "NetworkError",
    "NotFoundError",
    "OpenProjectError",
    "PermissionDeniedError",
    "RateLimitedError",
    "UnexpectedResponseError",
    "UpstreamServerError",
    "ValidationFailedError",
    "Violation",
    "error_from_response",
    "error_from_transport",
    "hint_for_status",
    "parse_retry_after",
    "violations_from_form",
]

MULTIPLE_ERRORS_IDENTIFIER = "urn:openproject-org:api:v3:errors:MultipleErrors"

#: status → hint shown to the model. Preserved (and corrected) from the old
#: server's one genuinely good idea.
STATUS_HINTS: dict[int, str] = {
    400: (
        "OpenProject rejected the request parameters. This is usually an unknown filter "
        "name, an operator the filter does not support, or an invalid sort key. Check the "
        "filter/sort names against get_work_package_schema or get_project_metadata."
    ),
    401: (
        "Authentication failed. Check OPENPROJECT_API_KEY (an 'API' access token from "
        "My account -> Access tokens) or the OAuth token, and that the account is not locked."
    ),
    403: (
        "The authenticated user lacks permission for this action in this project. Use "
        "list_permissions to see what the current user may do, or ask an administrator "
        "for the required role."
    ),
    404: (
        "Not found. Either the id does not exist (ids come from the matching search_/list_ "
        "tool), the resource was deleted, or the OpenProject module providing this endpoint "
        "is not enabled on this instance."
    ),
    409: (
        "The resource changed since it was read (stale lock_version). Re-read the resource "
        "and retry the write with the fresh lock_version."
    ),
    422: (
        "Validation failed. Fix the attributes listed in 'violations'; use "
        "get_work_package_schema (or the create/update form) to see required fields and "
        "allowed values."
    ),
    429: (
        "Rate limited by OpenProject. Wait for 'retry_after' seconds before retrying; "
        "reads are retried automatically a few times, writes are not."
    ),
}

_SERVER_ERROR_HINT = (
    "OpenProject returned a server error. The response body is withheld deliberately. "
    "Retry after a short delay; if it persists the instance or a module is unhealthy."
)
_NETWORK_HINT = (
    "Could not reach the OpenProject instance. Check OPENPROJECT_URL for typos, DNS/VPN "
    "reachability, HTTPS_PROXY/ALL_PROXY, and TLS trust (OPENPROJECT_MCP_CA_BUNDLE for a "
    "private CA)."
)
_UNEXPECTED_HINT = (
    "OpenProject returned an unexpected status. Verify the endpoint is supported by this "
    "instance version (get_instance_info reports it)."
)


class Violation(dict[str, str]):
    """A single field-level validation problem: ``{"attribute", "message"}``.

    A plain dict subclass so it serializes as-is into the error envelope while
    still being constructible positionally.
    """

    def __init__(self, attribute: str | None, message: str) -> None:
        super().__init__()
        if attribute:
            self["attribute"] = attribute
        self["message"] = message


class OpenProjectError(Exception):
    """Base of the taxonomy. Carries everything the envelope needs."""

    #: wire value of ``error.type`` in the envelope
    error_type: ClassVar[str] = "openproject_error"

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        error_identifier: str | None = None,
        hint: str | None = None,
        violations: Sequence[Mapping[str, str]] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.error_identifier = error_identifier
        self.hint = hint
        self.violations: list[dict[str, str]] = [dict(item) for item in violations or []]
        self.extra: dict[str, Any] = dict(extra or {})

    def to_envelope(self) -> dict[str, Any]:
        """Return the JSON-serializable ``{"error": {...}}`` envelope (SPEC §4.2)."""
        error: dict[str, Any] = {"type": self.error_type}
        if self.http_status is not None:
            error["http_status"] = self.http_status
        if self.error_identifier:
            error["error_identifier"] = self.error_identifier
        error["message"] = self.message
        if self.violations:
            error["violations"] = self.violations
        if self.hint:
            error["hint"] = self.hint
        error.update(self.extra)
        return {"error": error}

    def to_json(self) -> str:
        return json.dumps(self.to_envelope(), default=str)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.http_status!r}, message={self.message!r})"


class AuthenticationError(OpenProjectError):
    """401 — missing or rejected credentials."""

    error_type: ClassVar[str] = "authentication_failed"


class PermissionDeniedError(OpenProjectError):
    """403 — authenticated but not allowed."""

    error_type: ClassVar[str] = "permission_denied"


class NotFoundError(OpenProjectError):
    """404 — unknown id, deleted resource, or a module that is not installed."""

    error_type: ClassVar[str] = "not_found"


class ValidationFailedError(OpenProjectError):
    """422 (and 400) — the payload or query was rejected by OpenProject."""

    error_type: ClassVar[str] = "validation_failed"


class ConflictError(OpenProjectError):
    """409 — optimistic locking conflict; carries a fresh snapshot (SPEC §4.4)."""

    error_type: ClassVar[str] = "conflict"

    def __init__(
        self,
        message: str,
        *,
        lock_version: int | None = None,
        current: Mapping[str, Any] | None = None,
        conflicting_fields: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.lock_version = lock_version
        self.current = dict(current or {})
        self.conflicting_fields = dict(conflicting_fields or {})
        if lock_version is not None:
            self.extra["lock_version"] = lock_version
        if self.current:
            self.extra["current"] = self.current
        if self.conflicting_fields:
            self.extra["conflicting_fields"] = self.conflicting_fields


class RateLimitedError(OpenProjectError):
    """429 — carries ``retry_after`` seconds when the server sent one."""

    error_type: ClassVar[str] = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
        if retry_after is not None:
            self.extra["retry_after"] = retry_after


class UpstreamServerError(OpenProjectError):
    """5xx — body deliberately discarded (G4)."""

    error_type: ClassVar[str] = "upstream_server_error"


class NetworkError(OpenProjectError):
    """DNS / TLS / timeout / proxy failures — never reached OpenProject."""

    error_type: ClassVar[str] = "network_error"


class UnexpectedResponseError(OpenProjectError):
    """A status outside the taxonomy (405, 415, …) — keeps the taxonomy closed."""

    error_type: ClassVar[str] = "unexpected_response"


class InputValidationError(OpenProjectError):
    """Local, pre-flight rejection: bad operator, unknown sort key, unknown field.

    Not an upstream failure, so ``http_status`` stays ``None``. Raised by the
    filter builder, payload builders and tool argument checks so that bad input
    never becomes a wasted round trip (guarantee G2).
    """

    error_type: ClassVar[str] = "invalid_input"


class ConfirmationRequiredError(InputValidationError):
    """A destructive tool was called without ``confirm=true`` (SPEC §5.5)."""

    error_type: ClassVar[str] = "confirmation_required"


class AttachmentQuarantinedError(OpenProjectError):
    """The antivirus scanner quarantined the file; the bytes are unreachable.

    Not an HTTP failure — the attachment metadata reads fine and says so — which
    is why it is its own taxonomy member rather than a 403.
    """

    error_type: ClassVar[str] = "attachment_quarantined"


class AttachmentTooLargeError(InputValidationError):
    """A size cap refused the transfer before or during it.

    Raised locally against the instance's ``maximumAttachmentFileSize`` (upload)
    or ``OPENPROJECT_MCP_MAX_DOWNLOAD_MB`` (download), so no bytes are wasted.
    """

    error_type: ClassVar[str] = "attachment_too_large"


def hint_for_status(status: int) -> str:
    """Return the English hint for an HTTP status."""
    if status in STATUS_HINTS:
        return STATUS_HINTS[status]
    if status >= 500:
        return _SERVER_ERROR_HINT
    return _UNEXPECTED_HINT


def _json_body(response: httpx.Response) -> Mapping[str, Any] | None:
    """Parse a JSON error body, or ``None`` for HTML/empty/garbage bodies."""
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return None
    return as_object(payload)


def _message_from(body: Mapping[str, Any] | None, response: httpx.Response) -> str:
    if body:
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return f"OpenProject returned HTTP {response.status_code} for {response.request.url.path}."


def _violations_from_body(body: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Extract ``[{attribute, message}]`` from a v3 error body.

    Handles both the single PropertyConstraintViolation shape
    (``_embedded.details.attribute``) and MultipleErrors
    (``_embedded.errors[]``).
    """
    if not body:
        return []
    embedded = as_object(body.get("_embedded"))
    if embedded is None:
        return []

    raw_errors = as_array(embedded.get("errors"))
    if raw_errors is not None:
        collected = [_single_violation(item) for item in as_objects(raw_errors)]
        return [item for item in collected if item]

    single = _single_violation(body)
    return [single] if single else []


def _single_violation(body: Mapping[str, Any]) -> dict[str, str]:
    message = body.get("message")
    if not isinstance(message, str):
        return {}
    attribute: str | None = None
    embedded = as_object(body.get("_embedded"))
    details = as_object(embedded.get("details")) if embedded is not None else None
    if details is not None:
        candidate = details.get("attribute")
        if isinstance(candidate, str):
            attribute = candidate
    return Violation(attribute, message)


def violations_from_form(validation_errors: Mapping[str, Any]) -> list[dict[str, str]]:
    """Turn a form response's ``_embedded.validationErrors`` into violations.

    Form endpoints answer 200 with per-attribute error objects keyed by the
    attribute name (SPEC §4.5); this converts them to the same shape a 422 body
    produces so callers raise one consistent error.
    """
    violations: list[dict[str, str]] = []
    for attribute, raw in validation_errors.items():
        error = as_object(raw)
        if error is None:
            continue
        message = error.get("message")
        if isinstance(message, str):
            nested = _single_violation(error)
            violations.append(Violation(nested.get("attribute", attribute), message))
    return violations


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header: delta-seconds or an HTTP date."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(seconds, 0.0)
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    now = datetime.datetime.now(tz=target.tzinfo or datetime.UTC)
    return max((target - now).total_seconds(), 0.0)


def error_from_response(response: httpx.Response) -> OpenProjectError:
    """Map a non-2xx httpx response onto the taxonomy.

    The response body is consumed only if it is JSON; HTML/text bodies are
    dropped so upstream markup never reaches the model (G4).
    """
    status = response.status_code
    body = _json_body(response)
    identifier = body.get("errorIdentifier") if body else None
    identifier = identifier if isinstance(identifier, str) else None
    message = _message_from(body, response)
    hint = hint_for_status(status)

    if status == 401:
        return AuthenticationError(
            message, http_status=status, error_identifier=identifier, hint=hint
        )
    if status == 403:
        return PermissionDeniedError(
            message, http_status=status, error_identifier=identifier, hint=hint
        )
    if status == 404:
        return NotFoundError(message, http_status=status, error_identifier=identifier, hint=hint)
    if status == 409:
        return ConflictError(message, http_status=status, error_identifier=identifier, hint=hint)
    if status == 429:
        return RateLimitedError(
            message,
            retry_after=parse_retry_after(response.headers.get("retry-after")),
            http_status=status,
            error_identifier=identifier,
            hint=hint,
        )
    if status in (400, 422):
        return ValidationFailedError(
            message,
            http_status=status,
            error_identifier=identifier,
            hint=hint,
            violations=_violations_from_body(body),
        )
    if status >= 500:
        return UpstreamServerError(
            f"OpenProject returned HTTP {status} for {response.request.url.path}.",
            http_status=status,
            hint=hint,
        )
    return UnexpectedResponseError(
        message, http_status=status, error_identifier=identifier, hint=hint
    )


def error_from_transport(exc: httpx.HTTPError) -> NetworkError:
    """Map an httpx transport failure onto :class:`NetworkError`."""
    kind = type(exc).__name__
    detail = str(exc).strip() or kind
    return NetworkError(f"Could not reach OpenProject ({kind}): {detail}", hint=_NETWORK_HINT)
