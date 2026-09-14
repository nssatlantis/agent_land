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


def _confidence_bar(confidence: int, threshold: int) -> str:
    if threshold <= 0:
        return ""
    pct = min(100, int(confidence / threshold * 100))
    color = "#16a34a" if confidence >= threshold else "#d97706"
    return (
        f'<div style="margin:8px 0">'
        f'<div class="bug-conf-track">'
        f'<div style="background:{color};height:8px;border-radius:4px;width:{pct}%"></div>'
        f"</div> "
        f'<span style="font-size:13px;color:var(--muted)">{confidence}/{threshold}</span>'
        f"</div>"
    )


@lru_cache(maxsize=16)
def _timeline_cached(status: str, has_proposal: bool) -> str:
    """Cached timeline like _governance 60s - status+proposal determines 4 chips."""
    steps = [
        ("Reported", True),
        ("Confirmed", status in ("confirmed", "fixed")),
        ("Proposal", has_proposal),
        ("Fixed", status == "fixed"),
    ]
    bits: list[str] = []
    for i, (label, done) in enumerate(steps):
        color = "#16a34a" if done else "var(--muted)"
        weight = "600" if done else "400"
        bits.append(f'<span style="color:{color};font-weight:{weight}">{label}</span>')
        if i < len(steps) - 1:
            bits.append('<span style="color:var(--muted)"> → </span>')
    return '<div style="margin:10px 0;font-size:14px">' + "".join(bits) + "</div>"


def _bug_timeline(report: dict, threshold: int) -> str:
    """Lifecycle steps for a bug report: reported -> confirmed -> proposal
    -> fixed, with each completed step highlighted. Display-only. Cached per status."""
    # threshold unused for timeline, kept for call-site compat
    return _timeline_cached(
        report.get("status") or "open", bool(report.get("linked_proposals"))
    )


_BUG_STATUSES = ("open", "confirmed", "fixed", "closed")
_BUG_SORTS = ("newest", "confidence")
_BUG_SEVERITY_FILTERS = ("low", "medium", "high", "critical")


def bugs_page(request):
    query = request.query_params
    status_filter = query.get("status")
    if status_filter not in _BUG_STATUSES:
        status_filter = None
    raw_agent = query.get("agent_id")
    reporter_id = None
    if raw_agent:
        try:
            reporter_id = int(raw_agent)
        except ValueError:
            reporter_id = None
    # Search input is isolated from global search like reports_q: distinct
    # name/id and stopPropagation so typing here never bleeds upward.
    bugs_q = (query.get("bugs_q") or "").strip()[:80]
    sort = query.get("sort") or "newest"
    if sort not in _BUG_SORTS:
        sort = "newest"
    severity_filter = query.get("severity")
    if severity_filter not in _BUG_SEVERITY_FILTERS:
        severity_filter = None
    raw_page = query.get("page") or "1"
    try:
        page = max(1, int(raw_page))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - garbage page param means page 1
        page = 1
    per_page = 30

    def _link(label, **kw):
        params = {}
        if status_filter:
            params["status"] = status_filter
        if reporter_id is not None:
            params["agent_id"] = str(reporter_id)
        if bugs_q:
            params["bugs_q"] = bugs_q
        if sort != "newest":
            params["sort"] = sort
        if severity_filter:
            params["severity"] = severity_filter
        params.update({k: v for k, v in kw.items() if v is not None})
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        href = f"/bugs/{('#bugs')}" if False else ("/bugs" + (f"?{qs}" if qs else ""))
        active = all(params.get(k) == v for k, v in kw.items())
        style = (
            "font-weight:700;color:var(--accent)" if active and kw else ""
        )
        return f'<a href="{href}" style="{style}">{esc(label)}</a>'

    def _fetch(st, pg):
        rows = db.list_bug_reports(
            status=st,
            agent_id=reporter_id,
            q=bugs_q or None,
            severity=severity_filter,
            sort=sort,
            limit=per_page,
            offset=(pg - 1) * per_page,
        )
        return rows["reports"], rows["total"]
