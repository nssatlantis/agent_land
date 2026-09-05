"""viewer/_utils.py - shared HTML rendering helpers for the viewer and admin panels.

Pure formatting utilities with no domain logic. Both viewer/ and server/admin.py
import from here instead of server/admin.py reaching into viewer/ private names.
"""
from __future__ import annotations

import html
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Generic, TypeVar

import config

HOST = config.VIEWER_HOST
PORT = config.VIEWER_PORT

_WS_RE = re.compile(r"\s+")
_ORDERED_LIST_RE = re.compile(r"^\d+[.)] ")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*[-:]+\s*(\|\s*[-:]+\s*)*\|?\s*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def esc(text: object) -> str:
    return html.escape(str(text))


@lru_cache(maxsize=128)
def _parse_iso_cached(raw: str) -> datetime | None:
    """Cached ISO parse for _human_ts: strip Z/+00:00, fromisoformat, utc."""
    text = raw.rstrip("Z")
    if text.endswith("+00:00"):
        text = text[:-6]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:  # domain: degrade-silently - malformed timestamp renders raw
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _human_ts(value: str) -> str:
    """Relative timestamp for recent dates: '3 hours ago'.
    For dates older than 30 days returns an absolute date string instead.
    Returns ``''`` for null/empty strings.
    """
    if not value:
        return ""
    dt = _parse_iso_cached(value)
    if dt is None:
        return esc(value)
    now = datetime.now(timezone.utc)
    diff = now - dt
    if diff.total_seconds() < 0:
        return esc(_human_ts_absolute(value))
    if diff < timedelta(days=30):
        return _human_duration(diff) + " ago"
    return esc(_human_date(value))


def _human_ts_absolute(value: str) -> str:
    """Absolute UTC timestamp: YYYY-MM-DD HH:MM UTC."""
    if not value:
        return ""
    dt = _parse_iso_cached(value)
    if dt is None:
        return esc(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _human_date(value: str) -> str:
    """Date only: YYYY-MM-DD."""
    if not value:
        return ""
    dt = _parse_iso_cached(value)
    if dt is None:
        return esc(value)
    return dt.strftime("%Y-%m-%d")


def _human_duration(diff: timedelta) -> str:
    """Format a timedelta as the largest unit that is non-zero.
    Examples: 45s / 12m / 2h / 4d / 3w / 2mo / 1y.
    """
    total = diff.total_seconds()
    if abs(total) < 60:
        return f"{int(total)}s"
    minutes = int(total // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}h"
    days = int(hours // 24)
    if days < 7:
        return f"{days}d"
    weeks = int(days // 7)
    if weeks < 4:
        return f"{weeks}w"
    months = int(days // 30)
    if months < 12:
        return f"{months}mo"
    years = int(days // 365)
    return f"{years}y"


V = TypeVar("V")


class TTLCache(Generic[V]):
    """Bounded time-to-live cache: O(1) get/set keyed by an arbitrary hashable.

    Not thread-safe across PROCESSES (processes have their own dicts);
    thread-safe WITHIN a process via a single re-entrant lock, so a holder
    can recurse into a nested `get_or_compute` for the same instance
    without deadlocking.
    """

    __slots__ = ("_ttl", "_data", "_lock")

    def __init__(self, ttl_seconds: float):
        self._ttl = float(ttl_seconds)
        self._data: dict = {}
        self._lock = threading.RLock()

    def _now(self) -> float:
        return time.monotonic()

    def get(self, key) -> V | None:
        """Return the cached value for `key` if present and fresh, else None.
        Drops a stale entry as a side-effect.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, value = entry
            if self._now() - ts < self._ttl:
                return value
            del self._data[key]
            return None

    def set(self, key, value: V) -> None:
        with self._lock:
            self._data[key] = (self._now(), value)

    def invalidate(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def pop(self, key, default=...) -> V | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                if default is ...:
                    raise KeyError(key)
                return default
            ts, value = entry
            if self._now() - ts >= self._ttl:
                del self._data[key]
                if default is ...:
                    raise KeyError(key)
                return default
            del self._data[key]
            return value

    def get_or_compute(self, key, compute: Callable[[], V]) -> V:
        """Return the cached value or run ``compute()`` under the lock.
        ``compute`` must be a zero-argument callable.  The RLock allows a
        re-entrant call (nested get_or_compute for the same cache) to proceed
        without deadlock.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                ts, value = entry
                if self._now() - ts < self._ttl:
                    return value
                del self._data[key]
            value = compute()
            self._data[key] = (self._now(), value)
            return value
