"""viewer/_feed_helpers.py - shared HTML fragment builders for the viewer.

Pure formatting utilities with no domain logic. Both viewer/ and server/admin.py
import from here instead of server/admin.py reaching into viewer/ private names.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from viewer._utils import (
    TTLCache,
    _human_ts,
    _human_date,
    esc,
    _record_files_list,
)

if TYPE_CHECKING:
    from datetime import timedelta

_COMMENT_PAGE = 20
_COMMENT_THREAD_PAGE = 10
_COMMENTS_CACHE_SECONDS = 60
_comments_cache: TTLCache[list[dict]] = TTLCache(
    ttl_seconds=_COMMENTS_CACHE_SECONDS
)


def _format_credits(value: int) -> str:
    """Format a credit amount for display."""
    if value >= 100:
        return str(value)
    if value == int(value):
        return str(int(value))
    return f"{int(value * 4) / 4:.2f}".rstrip("0").rstrip(".")
