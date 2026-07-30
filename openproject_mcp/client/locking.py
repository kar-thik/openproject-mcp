"""Optimistic locking done right (SPEC §4.4).

Every OpenProject PATCH must echo the resource's current ``lockVersion``. The
rules encoded here, and the reason this module exists at all:

* Use the caller-supplied ``lock_version`` when given; otherwise GET the
  resource and echo what it reports.
* **A fetch failure aborts the write.** Never default to ``0`` — that is how
  the previous server silently clobbered concurrent edits.
* A 409 becomes a :class:`ConflictError` carrying the *fresh* ``lock_version``,
  a compact snapshot, and the fields that actually differ, so the model can
  retry deliberately instead of blindly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openproject_mcp.client.errors import (
    ConflictError,
    OpenProjectError,
    UnexpectedResponseError,
)
from openproject_mcp.client.hal import as_object, formattable
from openproject_mcp.client.http import OpenProjectClient

__all__ = [
    "conflict_from_snapshot",
    "conflicting_fields",
    "extract_lock_version",
    "patch_with_lock",
    "resolve_lock_version",
]

#: Fields worth echoing back in a conflict snapshot — small and human-legible.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "subject",
    "startDate",
    "dueDate",
    "date",
    "percentageDone",
    "updatedAt",
    "name",
)


def extract_lock_version(payload: Mapping[str, Any]) -> int | None:
    """Read ``lockVersion`` from a resource body."""
    value = payload.get("lockVersion")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


async def resolve_lock_version(
    client: OpenProjectClient,
    path: str,
    *,
    supplied: int | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Return the lock version to echo, plus the snapshot it came from.

    Args:
        path: resource path relative to the API root, e.g. ``work_packages/12``.
        supplied: a caller-provided ``lock_version``; used as-is when present
            (no extra round trip).

    Returns:
        ``(lock_version, snapshot)`` — ``snapshot`` is ``None`` when the caller
        supplied the version.

    Raises:
        OpenProjectError: if the resource cannot be read. The write must not
            proceed.
    """
    if supplied is not None:
        return supplied, None

    current = await client.get_json(path)
    lock_version = extract_lock_version(current)
    if lock_version is None:
        raise UnexpectedResponseError(
            f"Could not read lockVersion for {path}; refusing to write.",
            hint=(
                "The resource did not report a lockVersion, so a safe update is impossible. "
                "Re-read the resource and pass lock_version explicitly if you are sure."
            ),
        )
    return lock_version, current


def conflicting_fields(
    attempted: Mapping[str, Any] | None,
    fresh: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Compact diff of the attributes the caller tried to write.

    Only attributes present in the attempted payload are compared, so the
    result stays small: ``{"subject": {"attempted": "...", "current": "..."}}``.
    """
    if not attempted or not fresh:
        return {}
    diff: dict[str, dict[str, Any]] = {}
    for key, attempted_value in attempted.items():
        if key in ("lockVersion", "_links"):
            continue
        if key not in fresh:
            continue
        current_value = fresh[key]
        if _comparable(current_value) != _comparable(attempted_value):
            diff[key] = {
                "attempted": _comparable(attempted_value),
                "current": _comparable(current_value),
            }
    return diff


def _comparable(value: Any) -> Any:
    """Normalize formattable fields so a diff compares text with text."""
    mapping = as_object(value)
    if mapping is not None and "raw" in mapping:
        return formattable(mapping)
    return value


def snapshot_of(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """A few identifying fields of the current resource for the error envelope."""
    if not payload:
        return {}
    snapshot: dict[str, Any] = {}
    for field in SNAPSHOT_FIELDS:
        if field in payload:
            snapshot[field] = _comparable(payload[field])
    return snapshot


def conflict_from_snapshot(
    original: OpenProjectError,
    *,
    fresh: Mapping[str, Any] | None,
    attempted: Mapping[str, Any] | None = None,
) -> ConflictError:
    """Shape a 409 into a :class:`ConflictError` with the fresh state attached."""
    lock_version = extract_lock_version(fresh) if fresh else None
    return ConflictError(
        original.message,
        http_status=original.http_status or 409,
        error_identifier=original.error_identifier,
        hint=original.hint,
        lock_version=lock_version,
        current=snapshot_of(fresh),
        conflicting_fields=conflicting_fields(attempted, fresh),
    )


async def patch_with_lock(
    client: OpenProjectClient,
    path: str,
    payload: Mapping[str, Any],
    *,
    lock_version: int | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """PATCH a resource with correct lockVersion handling.

    Reads the current lock version when the caller did not supply one (aborting
    the write if that read fails), sends the PATCH, and re-fetches on 409 so the
    resulting :class:`ConflictError` carries the fresh version and a diff.

    Returns:
        The updated resource body.
    """
    resolved, snapshot = await resolve_lock_version(client, path, supplied=lock_version)
    body = dict(payload)
    body["lockVersion"] = resolved

    try:
        return await client.patch_json(path, json=body, params=params)
    except ConflictError as conflict:
        fresh: dict[str, Any] | None
        try:
            fresh = await client.get_json(path)
        except OpenProjectError:
            # The re-read failed too; return what we know rather than masking
            # the conflict with a second, less useful error.
            fresh = dict(snapshot) if snapshot else None
        raise conflict_from_snapshot(conflict, fresh=fresh, attempted=body) from conflict
