"""Instance version and feature probing (SPEC §4.7).

The API surface genuinely differs across OpenProject 14 LTS → 17.x. Rather than
assume, we probe — lazily, on first need, cached for an hour — and report the
result so tools degrade explicitly instead of silently (guarantee G5).

What is probed and why:

============================  ===========================================
Feature                       Strategy
============================  ===========================================
core version / instance name  ``GET /`` (API root)
internal comments (≥ 16.0)    version-derived; older servers *ignore* the
                              flag silently, so we hard-error instead (G2)
emoji reactions (≥ 16.0)      version-derived, 404-tolerant at call time
project favorites (≥ 17.0)    version-derived, 404-tolerant at call time
time-entry WP filter name     try ``entityId``; a 400 falls back to the
                              pre-15.x ``workPackage``
capabilities context prefix   try ``p{id}``; a rejection falls back to
                              ``w{id}`` (17.2+ spelling)
============================  ===========================================

Every probe function takes the client explicitly and performs ordinary HTTP,
so all of them are testable under ``respx`` with no live instance.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client.cache import TTLCache
from openproject_mcp.client.errors import OpenProjectError, ValidationFailedError
from openproject_mcp.client.filters import Op, make_filter, serialize_filters
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.config import PROBE_CACHE_TTL

__all__ = [
    "InstanceProbe",
    "get_probe",
    "parse_version",
    "probe_capabilities_context",
    "probe_root",
    "probe_time_entry_filter",
]

CACHE_KEY_ROOT = "probe:root"
CACHE_KEY_PROBE = "probe:instance"
CACHE_KEY_TIME_ENTRY_FILTER = "probe:time_entry_filter"
CACHE_KEY_CAPABILITIES_CONTEXT = "probe:capabilities_context"

INTERNAL_COMMENTS_MIN = (16, 0, 0)
EMOJI_REACTIONS_MIN = (16, 0, 0)
PROJECT_FAVORITES_MIN = (17, 0, 0)

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")

TimeEntryFilterName = Literal["entityId", "workPackage"]
CapabilitiesContextPrefix = Literal["p", "w"]


class InstanceProbe(BaseModel):
    """What this instance supports. Included in ``get_instance_info`` output."""

    core_version: str | None = Field(
        default=None, description="OpenProject core version string, e.g. '17.7.1'."
    )
    instance_name: str | None = Field(default=None, description="Configured instance name.")
    supports_internal_comments: bool = Field(
        default=False, description="Internal (private) work-package comments; OpenProject >= 16."
    )
    supports_emoji_reactions: bool = Field(
        default=False, description="Emoji reactions on activities; OpenProject >= 16."
    )
    supports_project_favorites: bool = Field(
        default=False, description="Project favorite endpoints; OpenProject >= 17."
    )
    time_entry_work_package_filter: TimeEntryFilterName | None = Field(
        default=None,
        description="Filter name for scoping time entries to a work package.",
    )
    capabilities_context_prefix: CapabilitiesContextPrefix | None = Field(
        default=None, description="Project context prefix for the capabilities API."
    )
    notes: list[str] = Field(
        default_factory=list, description="Degradation markers for anything undetectable."
    )


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    """Parse ``"17.7.1"`` / ``"14.6"`` / ``"17.7.1-dev"`` into a comparable tuple."""
    if not value:
        return None
    match = _VERSION_RE.search(value)
    if not match:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor or 0), int(patch or 0))


async def probe_root(
    client: OpenProjectClient,
    cache: TTLCache | None = None,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch and cache the API root document (``GET /api/v3``)."""

    async def fetch() -> dict[str, Any]:
        return await client.get_json("")

    if cache is None:
        return await fetch()
    return await cache.get_or_set(
        CACHE_KEY_ROOT, fetch, scope=client.scope, ttl=PROBE_CACHE_TTL, refresh=refresh
    )


async def probe_time_entry_filter(
    client: OpenProjectClient,
    cache: TTLCache | None = None,
    *,
    refresh: bool = False,
) -> TimeEntryFilterName:
    """Determine the time-entry filter name for a work package.

    Current versions use ``entityId`` (+ ``entityType``); the ``workPackage``
    filter was removed in 2025-05. A 400 (invalid filter) on the probe means we
    are talking to an older instance.
    """

    async def detect() -> TimeEntryFilterName:
        params = {
            "filters": serialize_filters([make_filter("entityId", Op.EQ, ["1"])]),
            "pageSize": 1,
        }
        try:
            await client.get_json("time_entries", params=params)
        except ValidationFailedError:
            return "workPackage"
        except OpenProjectError:
            # Permission or module problems say nothing about the filter name;
            # assume the modern spelling and let the real call report properly.
            return "entityId"
        return "entityId"

    if cache is None:
        return await detect()
    return await cache.get_or_set(
        CACHE_KEY_TIME_ENTRY_FILTER,
        detect,
        scope=client.scope,
        ttl=PROBE_CACHE_TTL,
        refresh=refresh,
    )


async def probe_capabilities_context(
    client: OpenProjectClient,
    project_id: int | str,
    cache: TTLCache | None = None,
    *,
    refresh: bool = False,
) -> CapabilitiesContextPrefix:
    """Determine the capabilities project-context prefix (``p{id}`` vs ``w{id}``)."""

    async def detect() -> CapabilitiesContextPrefix:
        params = {
            "filters": serialize_filters([make_filter("context", Op.EQ, [f"p{project_id}"])]),
            "pageSize": 1,
        }
        try:
            await client.get_json("capabilities", params=params)
        except ValidationFailedError:
            return "w"
        except OpenProjectError:
            return "p"
        return "p"

    if cache is None:
        return await detect()
    return await cache.get_or_set(
        CACHE_KEY_CAPABILITIES_CONTEXT,
        detect,
        scope=client.scope,
        ttl=PROBE_CACHE_TTL,
        refresh=refresh,
    )


def probe_from_root(root: dict[str, Any]) -> InstanceProbe:
    """Derive version-gated feature flags from the API root document."""
    core_version = root.get("coreVersion")
    core_version = core_version if isinstance(core_version, str) else None
    instance_name = root.get("instanceName")
    instance_name = instance_name if isinstance(instance_name, str) else None
    version = parse_version(core_version)

    notes: list[str] = []
    if version is None:
        notes.append(
            "core version not reported by this instance; version-gated features "
            "are treated as unavailable"
        )

    return InstanceProbe(
        core_version=core_version,
        instance_name=instance_name,
        supports_internal_comments=version is not None and version >= INTERNAL_COMMENTS_MIN,
        supports_emoji_reactions=version is not None and version >= EMOJI_REACTIONS_MIN,
        supports_project_favorites=version is not None and version >= PROJECT_FAVORITES_MIN,
        notes=notes,
    )


async def get_probe(
    client: OpenProjectClient,
    cache: TTLCache | None = None,
    *,
    refresh: bool = False,
) -> InstanceProbe:
    """Return the cached instance probe, running it on first need.

    Cached for an hour per credential. Failures do not raise: an unreachable
    root yields a probe whose ``notes`` explain that features could not be
    detected, so a tool can still answer with an honest degradation marker.
    """

    async def run() -> InstanceProbe:
        try:
            root = await probe_root(client, cache, refresh=refresh)
        except OpenProjectError as exc:
            return InstanceProbe(notes=[f"feature probe unavailable: {exc.message}"])
        probe = probe_from_root(root)
        probe.time_entry_work_package_filter = await probe_time_entry_filter(
            client, cache, refresh=refresh
        )
        return probe

    if cache is None:
        return await run()
    return await cache.get_or_set(
        CACHE_KEY_PROBE, run, scope=client.scope, ttl=PROBE_CACHE_TTL, refresh=refresh
    )
