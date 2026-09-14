"""
viewer/_bugs.py - bug report viewer pages.

Read-only pages for the bug report system: /bugs (list) and /bugs/{id}
(detail).  Strictly read-only (viewer rule); mutations happen via MCP tools.
"""

from __future__ import annotations

import math
from functools import lru_cache
from urllib.parse import quote

import config
import db
import db._bug_reports as bug_reports_mod
from viewer._layout import _page
from viewer._utils import (
    _human_ts,
    _markdown,
    esc,
)

_STATUS_COLORS = {
    "open": "#dc2626",
    "confirmed": "#d97706",
    "fixed": "#16a34a",
    "closed": "#64748b",
}


@lru_cache(maxsize=16)
def _status_badge_cached(status: str) -> str:
    return (
        f'<span class="kind-badge" style="background:{_STATUS_COLORS.get(status, "#64748b")}">'
        f"{esc(status)}</span>"
    )


def _status_badge(status: str) -> str:
    # cached badge dict like _governance 60s - display-only, no DB
    return _status_badge_cached(status)


_SEVERITY_COLORS = {
    "low": "#16a34a",
    "medium": "#d97706",
    "high": "#dc2626",
    "critical": "#991b1b",
}


def _bug_severity_badge(severity: str | None) -> str:
    """Stored triage severity chip. Empty when the report is untriaged -
    confidence is shown separately, never disguised as severity."""
    if not severity:
        return ""
    color = _SEVERITY_COLORS.get(severity, "#64748b")
    return (
        f'<span class="bug-sev" style="background:{color}">sev: {esc(severity)}</span>'
    )
