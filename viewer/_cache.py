"""viewer/_cache.py - shared TTL-dict helper for single-entry fresh-read caches.

The (ts, value) shape used across the viewer's module-level caches
(_VERDICT_CACHE, _GOV_CACHE, _stake_summary_cache, the record-trio, etc.):

    def _cached(key, ttl, fetch):
        entry = _CACHE.get(key)
        if entry is not None and now - entry[0] < ttl:
            return entry[1]
        value = fetch()
        try:
            _CACHE[key] = (now, value)
        except Exception:  # domain: degrade-silently - cache never blocks render
            pass
        return value

Per-call TTL lets each panel pick its own knob (60s governance, 30s pulse,
5s record-trio) without a per-knob helper variant. The store is wrapped
in try/except so a cache write failure never blocks the render path.

The async twin `_acached` (below) shares the same (ts, value) shape and
the same degrade-silently store policy; it `await`s the fetch so async
caches (the record-trio readers in `viewer/_record_*`, which use
`asyncio.to_thread` to keep file/git reads off the loop) can migrate
without restructuring. The two entry points share one store: a sync
read after an async write hits the cache, and vice versa, by design
(same key, same TTL window, same degrade-silently contract).

Boundary (per #315):
- Bucket / TTL-slot caches stay bespoke (_pulse._panel_cache,
  _trend_cache): per-bucket keys would accrete the exact #915 leak class
  this helper prevents.
- Deadline + eviction caches stay bespoke (_api._recent_cache: ETag/304,
  bounded size, deadline-stored).
- functools.lru_cache memoizations (no TTL) are a separate concern.

Contract for callers: hand the helper a hashable key. The cache never
sees an unhashable key; the caller filters before entry.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_T = TypeVar("_T")

_CACHE: dict[Any, tuple[float, Any]] = {}


def _cached(key: Any, ttl: float, fetch: Callable[[], _T]) -> _T:
    """Fresh-read TTL-dict (sync): returns the cached value if fresh
    (now - ts < ttl), else computes it via fetch() and stores it.

    Per-call TTL; the cache write is wrapped in try/except so a store
    failure never blocks the render path. ASCII-only.
    """
    now = time.monotonic()
    entry = _CACHE.get(key)
    if entry is not None and now - entry[0] < ttl:
        return entry[1]
    value = fetch()
    try:
        _CACHE[key] = (now, value)
    except Exception:
        pass  # domain: degrade-silently - cache never blocks render
    return value


async def _acached(key: Any, ttl: float, fetch: Callable[[], Awaitable[_T]]) -> _T:
    """Fresh-read TTL-dict (async): same shape as `_cached` but `await`s
    the fetch. Use this in async route handlers / viewer panel builders
    where the underlying read is `await`-native (e.g. the record-trio
    readers in `viewer/_record_*`, which wrap file/git reads in
    `asyncio.to_thread` to keep the event loop free).

    Per-call TTL; the cache write is wrapped in try/except so a store
    failure never blocks the render path. Shares the same store as
    `_cached` so a sync read after an async write (and vice versa) hits
    the cache.
    """
    now = time.monotonic()
    entry = _CACHE.get(key)
    if entry is not None and now - entry[0] < ttl:
        return entry[1]
    value = await fetch()
    try:
        _CACHE[key] = (now, value)
    except Exception:
        pass  # domain: degrade-silently - cache never blocks render
    return value


def _reset_for_tests() -> None:
    """Test helper: clear the module-level cache. Not for production use."""
    _CACHE.clear()
