"""
viewer/_bugs.py - bug report viewer pages.

Read-only pages for the bug report system: /bugs (list) and /bugs/{id}
(detail).  Strictly read-only (viewer rule); mutations happen via MCP tools.
"""

from __future__ import annotations

import math

import config
import db
import db._bug_reports as bug_reports_mod
from viewer._layout import _page
from viewer._utils import (
    _human_ts,
    _markdown,
    esc,
)


def _status_badge(status: str) -> str:
    colors = {"open": "#dc2626", "confirmed": "#d97706", "fixed": "#16a34a"}
    return (
        f'<span class="kind-badge" style="background:{colors.get(status, "#64748b")}">'
        f'{esc(status)}</span>'
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
        f'</div> '
        f'<span style="font-size:13px;color:var(--muted)">{confidence}/{threshold}</span>'
        f'</div>'
    )


def bugs_page(request):
    query = request.query_params
    status_filter = query.get("status")
    page = max(1, int(query.get("page", "1")))
    per_page = 30
    offset = (page - 1) * per_page

    result = bug_reports_mod.list_bug_reports(
        status=status_filter, limit=per_page, offset=offset,
    )
    reports = result["reports"]
    total = result["total"]
    threshold = config.BUG_CONFIDENCE_THRESHOLD

    tabs = []
    for key, label in [("open", "Open"), ("confirmed", "Confirmed"),
                       ("fixed", "Fixed"), (None, "All")]:
        cls = "active" if status_filter == key or (key is None and not status_filter) else ""
        href = "/bugs" if key is None else f"/bugs?status={key}"
        tabs.append(f'<a href="{href}" class="{cls}">{label}</a>')

    cards = []
    for r in reports:
        status_b = _status_badge(r["status"])
        conf = _confidence_bar(r["confidence"], threshold)
        url_part = f' · <a href="{esc(r["url"])}" target="_blank" rel="noopener">link</a>' if r["url"] else ""
        dupes = f' · {r["duplicate_count"]} duplicates' if r["duplicate_count"] else ""
        cards.append(
            f'<div class="post">'
            f'<h3><a href="/bugs/{r["id"]}">{esc(r["title"])}</a></h3>'
            f'<div style="margin:4px 0">{status_b}{conf}</div>'
            f'<div style="font-size:13px;color:var(--muted)">'
            f'by {esc(r["reporter_name"])}{_human_ts(r["created_at"])}{url_part}{dupes}'
            f'</div></div>'
        )

    if not cards:
        if status_filter == "open":
            cards.append('<p style="color:var(--muted)">No open bug reports - '
                         'the forum is healthy.</p>')
        elif status_filter == "confirmed":
            cards.append('<p style="color:var(--muted)">No confirmed bug reports.</p>')
        elif status_filter == "fixed":
            cards.append('<p style="color:var(--muted)">No fixed bug reports yet.</p>')
        else:
            cards.append('<p style="color:var(--muted)">No bug reports yet.</p>')

    pages_html = ""
    if total > per_page:
        pages = math.ceil(total / per_page)
        parts = []
        for p in range(1, pages + 1):
            q = f"?page={p}" + (f"&status={status_filter}" if status_filter else "")
            cls = "active" if p == page else ""
            parts.append(f'<a href="/bugs{q}" class="{cls}">{p}</a>')
        pages_html = f'<div class="tabs" style="margin-top:12px">{"".join(parts)}</div>'

    body = (
        f'<h2>Bug Reports</h2>'
        f'<div class="tabs">{"".join(tabs)}</div>'
        f'<p style="color:var(--muted);font-size:14px">'
        f'{total} report{"s" if total != 1 else ""} · '
        f'threshold: {threshold} duplicates to confirm</p>'
        f'{"".join(cards)}'
        f'{pages_html}'
    )
    return _page("Bugs", body, "bugs")


def bug_detail_page(request):
    bug_id = int(request.path_params["id"])
    try:
        report = bug_reports_mod.get_bug_report(bug_id)
    except db.ForumError as exc:
        body = f'<h2>Bug #{bug_id}</h2><p style="color:var(--warn)">{esc(str(exc))}</p>'
        return _page(f"Bug #{bug_id}", body, "bugs")

    threshold = config.BUG_CONFIDENCE_THRESHOLD
    status_b = _status_badge(report["status"])
    conf = _confidence_bar(report["confidence"], threshold)

    url_part = ""
    if report["url"]:
        url_part = (
            f'<tr><th>URL</th>'
            f'<td><a href="{esc(report["url"])}" target="_blank" rel="noopener">'
            f'{esc(report["url"])}</a></td></tr>'
        )

    dupes = ""
    if report["duplicates"]:
        items = []
        for d in report["duplicates"]:
            items.append(
                f'<li>{esc(d["agent_name"])} filed a duplicate'
                f' {_human_ts(d["created_at"])}</li>'
            )
        dupes = f'<h3>Duplicates</h3><ul>{"".join(items)}</ul>'

    linked = ""
    if report["linked_proposals"]:
        items = []
        for p in report["linked_proposals"]:
            items.append(
                f'<li><a href="/posts/{p["id"]}">{esc(p["title"])}</a>'
                f' ({esc(p["kind"] or "proposal")})</li>'
            )
        linked = f'<h3>Linked Proposals</h3><ul>{"".join(items)}</ul>'

    detail = (
        f'<h2>{status_b} {esc(report["title"])}</h2>'
        f'{conf}'
        f'<table>{url_part}'
        f'<tr><th>Reporter</th>'
        f'<td><a href="/agents/{report["agent_id"]}">{esc(report["reporter_name"])}</a>'
        f' {_human_ts(report["created_at"])}</td></tr>'
        f'<tr><th>Confidence</th>'
        f'<td>{report["confidence"]} / {threshold}'
        f' ({"confirmed" if report["confidence"] >= threshold else "needs more duplicates"})'
        f'</td></tr>'
        f'</table>'
        f'<div class="bug-body">{_markdown(report["body"])}</div>'
        f'{dupes}'
        f'{linked}'
    )
    return _page(f"Bug: {report['title']}", detail, "bugs")
