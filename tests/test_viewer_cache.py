"""Test the shared viewer/_cache TTL-dict helper: fresh, stale, recompute,
key isolation, per-call TTL, degrade-silently on store, sync + async twins."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_viewer_cache_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import init  # noqa: E402
from viewer._cache import _acached, _cached, _reset_for_tests  # noqa: E402


def test_fresh_returns_cached_value():
    """Within the TTL window, the cached value is returned without
    calling fetch() a second time."""
    _reset_for_tests()
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return f"v{len(calls)}"

    assert _cached("k1", 1.0, fetch) == "v1"
    assert _cached("k1", 1.0, fetch) == "v1"
    assert len(calls) == 1


def test_stale_recomputes():
    """Past the TTL window, fetch() is called again and the new value
    is cached."""
    _reset_for_tests()
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return f"v{len(calls)}"

    assert _cached("k2", 0.01, fetch) == "v1"
    time.sleep(0.05)
    assert _cached("k2", 0.01, fetch) == "v2"
    assert len(calls) == 2


def test_window_boundary_just_fresh():
    """Right at the TTL boundary, the cached value is still fresh."""
    _reset_for_tests()
    assert _cached("k3", 0.1, lambda: "v") == "v"
    time.sleep(0.05)
    # 0.05s < 0.1s TTL, so still fresh
    assert _cached("k3", 0.1, lambda: "different") == "v"


def test_per_call_ttl_isolates_callers():
    """Same key, different TTLs at different call sites, recompute
    happens as soon as the shorter TTL elapses."""
    _reset_for_tests()
    counter = [0]

    def fetch() -> int:
        counter[0] += 1
        return counter[0]

    # First call: TTL 1.0, fresh for 1 second
    assert _cached("k4", 1.0, fetch) == 1
    # Second call: TTL 0.01 (way shorter), recompute immediately
    time.sleep(0.05)
    assert _cached("k4", 0.01, fetch) == 2


def test_key_isolation():
    """Different keys don't see each other's cached values."""
    _reset_for_tests()
    assert _cached("a", 1.0, lambda: "alpha") == "alpha"
    assert _cached("b", 1.0, lambda: "beta") == "beta"
    assert _cached("a", 1.0, lambda: "WRONG") == "alpha"
    assert _cached("b", 1.0, lambda: "WRONG") == "beta"


def test_degrade_silently_on_store_failure():
    """If the store raises, fetch's value is still returned (the cache
    write is wrapped in try/except)."""
    from viewer import _cache

    _reset_for_tests()
    original_cache = _cache._CACHE

    class _FailingDict:
        def get(self, *args, **kwargs):
            return original_cache.get(*args, **kwargs)

        def __setitem__(self, *args, **kwargs):
            raise RuntimeError("simulated store failure")

        def clear(self):
            original_cache.clear()

    _cache._CACHE = _FailingDict()
    try:
        assert _cached("k5", 1.0, lambda: "still-here") == "still-here"
    finally:
        _cache._CACHE = original_cache


async def test_async_fresh_returns_cached_value():
    """Async twin: within the TTL window, the cached value is returned
    without calling fetch() a second time."""
    _reset_for_tests()
    calls: list[int] = []

    async def fetch() -> str:
        await asyncio.sleep(0)
        calls.append(1)
        return f"v{len(calls)}"

    assert await _acached("a1", 1.0, fetch) == "v1"
    assert await _acached("a1", 1.0, fetch) == "v1"
    assert len(calls) == 1


async def test_async_stale_recomputes():
    """Async twin: past the TTL window, fetch() is called again."""
    _reset_for_tests()
    calls: list[int] = []

    async def fetch() -> str:
        await asyncio.sleep(0)
        calls.append(1)
        return f"v{len(calls)}"

    assert await _acached("a2", 0.01, fetch) == "v1"
    time.sleep(0.05)
    assert await _acached("a2", 0.01, fetch) == "v2"
    assert len(calls) == 2


async def test_async_key_isolation():
    """Async twin: different keys don't see each other's cached values."""
    _reset_for_tests()

    async def fetch_alpha() -> str:
        return "alpha"

    async def fetch_beta() -> str:
        return "beta"

    assert await _acached("ak1", 1.0, fetch_alpha) == "alpha"
    assert await _acached("ak2", 1.0, fetch_beta) == "beta"
    assert await _acached("ak1", 1.0, fetch_alpha) == "alpha"
    assert await _acached("ak2", 1.0, fetch_beta) == "beta"


async def test_async_degrade_silently_on_store_failure():
    """Async twin: if the store raises, the awaited fetch's value is
    still returned."""
    from viewer import _cache

    _reset_for_tests()
    original_cache = _cache._CACHE

    class _FailingDict:
        def get(self, *args, **kwargs):
            return original_cache.get(*args, **kwargs)

        def __setitem__(self, *args, **kwargs):
            raise RuntimeError("simulated store failure")

        def clear(self):
            original_cache.clear()

    _cache._CACHE = _FailingDict()
    try:

        async def fetch() -> str:
            await asyncio.sleep(0)
            return "async-still-here"

        assert await _acached("a3", 1.0, fetch) == "async-still-here"
    finally:
        _cache._CACHE = original_cache


async def test_sync_and_async_share_dict():
    """Sync and async entries land in the same module-level dict, so a
    fresh sync read after an async write hits the cache (and vice versa).
    One store, two entry points - the design intent of the twin."""
    _reset_for_tests()
    calls: list[int] = []

    async def afetch() -> str:
        await asyncio.sleep(0)
        calls.append(1)
        return f"v{calls[0]}"

    # Async write: ts = now, value = "v1"
    assert await _acached("shared", 1.0, afetch) == "v1"
    # Sync read within TTL: must NOT call afetch again, returns the
    # value the async call stored
    assert _cached("shared", 1.0, lambda: "WRONG") == "v1"
    assert len(calls) == 1


def test_reset_for_tests_clears_cache():
    """_reset_for_tests empties the module-level cache so a fresh test
    sees no leftovers from a prior one."""
    _reset_for_tests()
    assert _cached("k6", 1.0, lambda: "first") == "first"
    _reset_for_tests()
    # After reset, the next call MUST refetch
    calls = [0]

    def fetch() -> str:
        calls[0] += 1
        return f"v{calls[0]}"

    assert _cached("k6", 1.0, fetch) == "v1"
    assert calls[0] == 1


if __name__ == "__main__":
    init()

    sync_fns = [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v) and not k.startswith("test_async")
    ]
    async_fns = [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_async") and callable(v)
    ]

    for fn in sync_fns:
        fn()
    for fn in async_fns:
        asyncio.run(fn())

    print(f"{len(sync_fns) + len(async_fns)} tests passed")
