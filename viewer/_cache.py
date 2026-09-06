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

Boundary (per #315):
- Bucket / TTL-slot caches stay bespoke (_pulse._panel_cache,
  _trend_cache): per-bucket keys would accrete the exact #915 leak class
  this helper prevents.
- Deadline + eviction caches stay bespoke (_api._recent_cache: ETag/304,
  bounded size, deadline-stored).
- functools.lru_cache memoizations (no TTL) are a separate concern.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

_CACHE: dict[Any, tuple[float, Any]] = {}


def _cached(key: Any, ttl: float, fetch: Callable[[], Any]) -> Any:
    """Fresh-read TTL-dict: returns the cached value if fresh
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


def _reset_for_tests() -> None:
    """Test helper: clear the module-level cache. Not for production use."""
    _CACHE.clear()
