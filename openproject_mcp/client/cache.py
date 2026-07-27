"""TTL cache for near-static metadata (SPEC §4.6).

Cached: API root, ``/configuration``, types, statuses, priorities, roles,
per-project types/versions/categories, WP schemas, time-entry activities,
``users/me``, and version-probe results (1 h). **Never cached:** lockVersions,
work packages, or anything else a write can invalidate.

Entries are scoped per credential so a multi-user HTTP deployment cannot serve
one principal's metadata to another. The scope key is a hash of the credential,
never the credential itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = ["TTLCache", "credential_scope"]

T = TypeVar("T")

DEFAULT_SCOPE = "default"


def credential_scope(credential: str | None) -> str:
    """Return a stable, non-reversible cache scope for a credential."""
    if not credential:
        return DEFAULT_SCOPE
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """A small async-safe TTL cache with per-credential key scoping.

    Concurrent misses on the same key are collapsed: the first caller computes,
    the rest await the same result, so a burst of tool calls does not stampede
    the metadata endpoints.
    """

    def __init__(
        self,
        *,
        ttl: float = 300.0,
        max_entries: int = 1024,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.default_ttl = ttl
        self.max_entries = max_entries
        self._time = time_fn
        self._entries: dict[tuple[str, Hashable], _Entry] = {}
        self._locks: dict[tuple[str, Hashable], asyncio.Lock] = {}

    # --- basic operations ------------------------------------------------

    def get(self, key: Hashable, *, scope: str = DEFAULT_SCOPE) -> Any | None:
        """Return the live value for ``key``, or ``None`` when missing/expired."""
        full_key = (scope, key)
        entry = self._entries.get(full_key)
        if entry is None:
            return None
        if entry.expires_at <= self._time():
            self._entries.pop(full_key, None)
            return None
        return entry.value

    def set(
        self,
        key: Hashable,
        value: Any,
        *,
        scope: str = DEFAULT_SCOPE,
        ttl: float | None = None,
    ) -> None:
        """Store a value; ``ttl=0`` stores nothing."""
        effective_ttl = self.default_ttl if ttl is None else ttl
        if effective_ttl <= 0:
            return
        self._evict_if_needed()
        self._entries[(scope, key)] = _Entry(value, self._time() + effective_ttl)

    def invalidate(self, key: Hashable, *, scope: str = DEFAULT_SCOPE) -> None:
        self._entries.pop((scope, key), None)

    def invalidate_scope(self, scope: str) -> None:
        for full_key in [k for k in self._entries if k[0] == scope]:
            self._entries.pop(full_key, None)

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()

    def __len__(self) -> int:
        return len(self._entries)

    # --- memoization -----------------------------------------------------

    async def get_or_set(
        self,
        key: Hashable,
        factory: Callable[[], Awaitable[T]],
        *,
        scope: str = DEFAULT_SCOPE,
        ttl: float | None = None,
        refresh: bool = False,
    ) -> T:
        """Return the cached value or compute, store and return it.

        ``refresh=True`` bypasses the cached value and recomputes — this is the
        ``refresh=true`` parameter the metadata tools expose. A factory that
        raises does not poison the cache.
        """
        full_key = (scope, key)
        if not refresh:
            cached = self.get(key, scope=scope)
            if cached is not None:
                return cached  # type: ignore[return-value]

        lock = self._locks.setdefault(full_key, asyncio.Lock())
        async with lock:
            if not refresh:
                cached = self.get(key, scope=scope)
                if cached is not None:
                    return cached  # type: ignore[return-value]
            value = await factory()
            self.set(key, value, scope=scope, ttl=ttl)
            return value

    # --- internals -------------------------------------------------------

    def _evict_if_needed(self) -> None:
        if len(self._entries) < self.max_entries:
            return
        now = self._time()
        for full_key in [k for k, entry in self._entries.items() if entry.expires_at <= now]:
            self._entries.pop(full_key, None)
        while len(self._entries) >= self.max_entries:
            self._entries.pop(next(iter(self._entries)))
