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


def _render_markdown(text: str) -> str:
    return _markdown(text)


def _markdown(text: str) -> str:
    if not text.strip():
        return ""
    lines = text.splitlines()
    out_lines: list[str] = []
    in_ul = False
    in_ol = False
    in_pre = False
    pre_lines: list[str] = []

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out_lines.append("</ul>")
            in_ul = False

    def close_ol():
        nonlocal in_ol
        if in_ol:
            out_lines.append("</ol>")
            in_ol = False

    def open_ul():
        nonlocal in_ul
        close_ul()
        if not in_ul:
            out_lines.append("<ul>")
            in_ul = True

    def open_ol():
        nonlocal in_ol
        close_ol()
        if not in_ol:
            out_lines.append("<ol>")
            in_ol = True

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_pre:
                out_lines.append("".join(pre_lines))
                out_lines.append("</pre>")
                pre_lines = []
                in_pre = False
            else:
                close_ul()
                close_ol()
                out_lines.append("<pre><code>")
                in_pre = True
            continue
        if in_pre:
            pre_lines.append(line + "\n")
            continue
        if not stripped:
            close_ul()
            close_ol()
            continue
        if stripped.startswith("# "):
            close_ul()
            close_ol()
            out_lines.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            close_ul()
            close_ol()
            out_lines.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            close_ul()
            close_ol()
            out_lines.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("#### "):
            close_ul()
            close_ol()
            out_lines.append(f"<h4>{_inline(stripped[5:])}</h4>")
        elif stripped.startswith("> "):
            close_ul()
            close_ol()
            out_lines.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
        elif _ORDERED_LIST_RE.match(stripped):
            open_ol()
            m = _ORDERED_LIST_RE.match(stripped)
            assert m is not None
            item = stripped[m.end() :]
            out_lines.append(f"<li>{_inline(item)}</li>")
        elif stripped.startswith(("- ", "* ", "+ ")):
            open_ul()
            out_lines.append(f"<li>{_inline(stripped[2:])}</li>")
        elif _TABLE_ROW_RE.match(stripped):
            close_ul()
            close_ol()
            out_lines.append(_render_table_row(stripped))
        elif _TABLE_SEP_RE.match(stripped):
            out_lines.append("<hr>")
        else:
            close_ul()
            close_ol()
            out_lines.append(f"<p>{_inline(stripped)}</p>")

    if in_pre:
        out_lines.append("".join(pre_lines))
        out_lines.append("</code></pre>")
    close_ul()
    close_ol()
    return "\n".join(out_lines)


def _render_table_row(line: str) -> str:
    cells = [c.strip() for c in line.strip(" |\t").split("|")]
    if not cells:
        return ""
    return (
        '<table class="table table-sm table-hover "\n'
        'style="width:auto;">\n<tbody>\n<tr>\n'
        + "\n".join(f"<td>{_inline(c)}</td>" for c in cells)
        + "\n</tr>\n</tbody>\n</table>"
    )


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _BARE_URL_RE.sub(r'<a href="\1" rel="noopener">\1</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _DEL_RE.sub(r"<del>\1</del>", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    text = _WS_RE.sub(" ", text)
    return text


_BARE_URL_RE = re.compile(r"(\S+://\S+)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")
_DEL_RE = re.compile(r"~~(.+?)~~")
_CODE_RE = re.compile(r"`(.+?)`")


def format_datetime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return "—"
    return dt.strftime(fmt)


def format_age(dt: datetime | None, detail: bool = False) -> str:
    if dt is None:
        return "—"
    now = datetime.now(timezone.utc)
    aware = dt
    if aware.tzinfo is None:
        aware = aware.replace(tzinfo=timezone.utc)
    delta = now - aware
    total_seconds = delta.total_seconds()
    if abs(total_seconds) < 60:
        return "just now" if total_seconds >= 0 else "in the future"
    minutes = int(total_seconds / 60)
    if minutes < 60:
        return f"{minutes}m ago" if total_seconds >= 0 else f"in {minutes}m"
    hours = int(minutes / 60)
    if hours < 24:
        return f"{hours}h ago" if total_seconds >= 0 else f"in {hours}h"
    days = int(hours / 24)
    if days < 30:
        return f"{days}d ago" if total_seconds >= 0 else f"in {days}d"
    if detail:
        return dt.strftime("%Y-%m-%d %H:%M")
    months = int(days / 30)
    if months < 12:
        return f"{months}mo ago"
    years = int(months / 12)
    return f"{years}y ago"


def format_karma(karma: int) -> str:
    sign = "+" if karma >= 0 else ""
    return f"{sign}{karma}"


def format_credits(
    whole: int, half: int = 0, quarter: int = 0, *, suffix: str = ""
) -> str:
    parts = []
    if whole:
        parts.append(f"{whole}c")
    if half:
        parts.append(f"+{half}½c")
    if quarter:
        parts.append(f"+{quarter}¼c")
    if not parts:
        return "0c" + suffix
    return "".join(parts) + suffix


def pluralize(n: int, singular: str, plural: str | None = None) -> str:
    if plural is None:
        plural = singular + "s"
    return f"{n} {singular if n == 1 else plural}"


def abbrev(number: int) -> str:
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


# -----------------------------------------------------------------------------
# TTLCache - shared time-to-live cache for viewer modules
# -----------------------------------------------------------------------------
#
# Rationale: several viewer modules used a hand-rolled pattern of
#   _cache = {}  # dict[key] = (timestamp, value)
#   def _is_fresh(key, ttl=60):
#       ts, _ = _cache.get(key, (0, None))
#       return time.monotonic() - ts < ttl
# This is verbose, error-prone (easy to forget the _is_fresh guard), and
# the RLock to make it thread-safe has to be re-implemented per-site.
# TTLCache abstracts all of it: O(1) get/set keyed by an arbitrary hashable,
# bounded storage (stale entries are dropped on read so the dict
# never grows past the working set the page actually visits.
#
# Usage:
#   _foo_cache = TTLCache(ttl_seconds=60.0)
#   def foo(key):
#       return _foo_cache.get_or_compute(key, lambda: _expensive(key))
#
# `get_or_compute` holds the lock across `compute()` too, so a thundering
# herd of concurrent requests on a cold key collapses to a single compute
# (the others see the freshly-set value when they reacquire). This is the
# "no thundering herd" half of the inspection item 4921.

V = TypeVar("V")


class TTLCache(Generic[V]):
    """Bounded time-to-live cache: O(1) get/set keyed by an arbitrary hashable.

    Not thread-safe across PROCESSES (processes have their own dicts);
    thread-safe WITHIN a process via a single re-entrant lock, so a holder
    can recurse into a nested `get_or_compute` for the same instance
    without deadlocking. The class is intentionally tiny: every viewer
    cache uses the same get_or_compute / set / invalidate pattern.
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

        Drops a stale entry as a side-effect, so the dict never grows past
        the working set the page actually visits.
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

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def get_or_compute(self, key, compute: Callable[[], V]) -> V:
        """Return the cached value or run `compute()` under the lock.

        The lock is held across the compute so a thundering herd of N
        concurrent callers on a cold key collapses to a single compute;
        the next N-1 waiters see the freshly-set value when they
        reacquire and re-read. Hot reads (cache hit) only take the lock
        long enough for a dict get.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                ts, value = entry
                if self._now() - ts < self._ttl:
                    return value
            value = compute()
            self._data[key] = (self._now(), value)
            return value