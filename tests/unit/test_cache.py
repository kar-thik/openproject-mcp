"""TTL cache: expiry, per-credential isolation, refresh, single-flight."""

from __future__ import annotations

import asyncio
import contextlib

from openproject_mcp.client.cache import TTLCache, credential_scope


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_values_expire_after_ttl() -> None:
    clock = FakeClock()
    cache = TTLCache(ttl=300.0, time_fn=clock)
    cache.set("types", ["Task"])
    assert cache.get("types") == ["Task"]

    clock.advance(299.0)
    assert cache.get("types") == ["Task"]

    clock.advance(2.0)
    assert cache.get("types") is None


def test_per_entry_ttl_overrides_default() -> None:
    clock = FakeClock()
    cache = TTLCache(ttl=300.0, time_fn=clock)
    cache.set("probe", {"v": 1}, ttl=3600.0)
    clock.advance(3000.0)
    assert cache.get("probe") == {"v": 1}
    clock.advance(700.0)
    assert cache.get("probe") is None


def test_zero_ttl_stores_nothing() -> None:
    cache = TTLCache(ttl=0.0)
    cache.set("types", ["Task"])
    assert cache.get("types") is None


def test_scopes_isolate_credentials() -> None:
    cache = TTLCache(ttl=300.0)
    alice = credential_scope("alice-token")
    bob = credential_scope("bob-token")
    cache.set("users/me", {"id": 1}, scope=alice)

    assert cache.get("users/me", scope=alice) == {"id": 1}
    assert cache.get("users/me", scope=bob) is None
    assert cache.get("users/me") is None


def test_credential_scope_never_contains_the_secret() -> None:
    scope = credential_scope("super-secret-token")
    assert "super-secret-token" not in scope
    assert scope == credential_scope("super-secret-token")
    assert scope != credential_scope("other-token")
    assert credential_scope(None) == credential_scope("")


def test_invalidate_and_clear() -> None:
    cache = TTLCache(ttl=300.0)
    cache.set("a", 1)
    cache.set("b", 2, scope="s")
    cache.invalidate("a")
    assert cache.get("a") is None
    assert cache.get("b", scope="s") == 2
    cache.invalidate_scope("s")
    assert cache.get("b", scope="s") is None
    cache.set("c", 3)
    cache.clear()
    assert len(cache) == 0


async def test_get_or_set_calls_factory_once() -> None:
    cache = TTLCache(ttl=300.0)
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "value"

    assert await cache.get_or_set("k", factory) == "value"
    assert await cache.get_or_set("k", factory) == "value"
    assert calls == 1


async def test_get_or_set_refresh_bypasses_the_cache() -> None:
    cache = TTLCache(ttl=300.0)
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_set("k", factory) == 1
    assert await cache.get_or_set("k", factory, refresh=True) == 2
    assert await cache.get_or_set("k", factory) == 2


async def test_concurrent_misses_collapse_to_one_factory_call() -> None:
    cache = TTLCache(ttl=300.0)
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "value"

    results = await asyncio.gather(*(cache.get_or_set("k", factory) for _ in range(5)))
    assert results == ["value"] * 5
    assert calls == 1


async def test_factory_failure_does_not_poison_the_cache() -> None:
    cache = TTLCache(ttl=300.0)

    async def failing() -> str:
        raise RuntimeError("upstream down")

    async def working() -> str:
        return "value"

    with contextlib.suppress(RuntimeError):
        await cache.get_or_set("k", failing)
    assert await cache.get_or_set("k", working) == "value"


def test_eviction_keeps_the_cache_bounded() -> None:
    cache = TTLCache(ttl=300.0, max_entries=8)
    for index in range(50):
        cache.set(f"key-{index}", index)
    assert len(cache) <= 8
