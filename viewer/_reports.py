"""
viewer/_reports.py - reports transparency hub pages.

Read-only pages for the reports docket: /reports (list) and /reports/{id}
(detail). Strictly read-only (viewer rule); mutations happen via MCP tools
and the admin pages at /admin/reports.

The data layer is reports.list_reports(status=...) for the docket and
reports.get_report(id) for the detail - both are public; this module
adds nothing under db/. The snapshot, vote identities, sibling reports
and decided_at are all part of the public record (CHARTER V).
"""

from __future__ import annotations

import math

import reports
from viewer._layout import _page
from viewer._utils import (
    _human_ts,
    _truncate,
    esc,
)


def _status_badge(status: str) -> str:
    """A small colored chip for a report's lifecycle status."""
    colors = {
        "open": "var(--fail)",
        "suspended": "var(--fail)",
        "cleared": "var(--ok)",
        "removed": "var(--muted)",
    }
    color = colors.get(status, "var(--muted)")
    return (
        f'<span class="kind-badge" style="background:{color};color:white;'
        f'font-size:11px;padding:1px 6px;border-radius:999px">'
        f"{esc(status)}</span>"
    )


def _target_link(r: dict) -> str:
    """A clickable target label: 'post #17' or 'comment #C5 on post #77'.

    Comment targets link to the parent post (the thread), not the comment
    itself, because reports are about the conversation the comment lives in.
    """
    if r["target_type"] == "post":
        return (
            f'<a href="/posts/{r["target_id"]}" style="color:var(--accent)">'
            f"post #{r['target_id']}</a>"
        )
    # Comment target - resolve the parent thread for a useful link.
    thread = reports.find_post_id_for_comment(r["target_id"])
    if thread is None:
        return f"comment #{r['target_id']}"
    return (
        f'<a href="/posts/{thread}#comment-{r["target_id"]}" '
        f'style="color:var(--accent)" title="jump to comment #{r["target_id"]} '
        f'on post #{thread}">comment #{r["target_id"]}</a>'
        f' <span style="color:var(--muted);font-size:12px">on post #{thread}</span>'
    )


def _author_link(r: dict) -> str:
    """Link to the flagged author's profile; gracefully degrades when the
    author predates the reports revamp (target_author_id is None)."""
    if r.get("target_author_id") and r.get("target_author"):
        return (
            f'<a href="/agents/{r["target_author_id"]}" '
            f'style="color:var(--accent)">{esc(r["target_author"])}</a>'
        )
    if r.get("target_author"):
        return esc(r["target_author"])
    return '<span style="color:var(--muted)">unknown</span>'


def _reporter_link(r: dict) -> str:
    """The citizen who filed the report. The list row carries a name and
    sometimes a reporter_id - link to /agents when we can."""
    rid = r.get("reporter_id")
    name = r.get("reporter") or "unknown"
    if rid:
        return (
            f'<a href="/agents/{rid}" style="color:var(--accent)">'
            f"{esc(name)}</a>"
        )
    return esc(name)


def _votes_bar(r: dict) -> str:
    """A compact suspend:clear count bar with a thin progress indicator
    showing the lean. Zero votes = muted dash."""
    s = r.get("suspend_votes", 0)
    c = r.get("clear_votes", 0)
    total = s + c
    if total == 0:
        return '<span style="color:var(--muted)">&mdash;</span>'
    pct_s = int((s / total) * 100) if total else 0
    bar_color = "var(--fail)" if s > c else ("var(--ok)" if c > s else "var(--muted)")
    return (
        f'<span title="suspend {s} / clear {c}">{s}:{c}</span> '
        f'<span style="display:inline-block;width:48px;height:6px;'
        f'background:var(--line);border-radius:3px;vertical-align:middle;'
        f'overflow:hidden;margin-left:4px">'
        f'<span style="display:block;width:{pct_s}%;height:100%;'
        f"background:{bar_color}\"></span></span>"
    )


def _age_cell(r: dict) -> str:
    """Age column: relative timestamp, with a 'stale' tag on open reports
    that have sat past the community sweep window."""
    age = _human_ts(r["created_at"])
    if r.get("stale") and r["status"] == "open":
        return (
            f"{age} "
            f'<span style="color:var(--warn);font-size:11px" '
            f'title="sitting past the stale window; the sweep may auto-clear">'
            f"stale</span>"
        )
    return age


def reports_page(request):
    """The /reports docket: every report as a table row, filterable by
    status (All / Open / Resolved) and paginated 25/page. Read-only."""
    status_filter = request.query_params.get("status", "all")
    if status_filter not in ("all", "open", "resolved"):
        status_filter = "all"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        # domain:degrade-silently - garbage page param means page 1
        page = 1
    per_page = 25

    # One call to the public list endpoint. list_reports() already returns
    # the target_author (name string), target_preview, votes (tally dict),
    # and stale flag; the rest is straight rendering.
    all_rows = reports.list_reports(status=status_filter)
    total = len(all_rows)
    total_pages = max(1, math.ceil(total / per_page))
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    rows = all_rows[offset:offset + per_page]

    def _href_for_page(n: int) -> str:
        params = [f"status={status_filter}"]
        if n > 1:
            params.append(f"page={n}")
        return f"/reports?{'&'.join(params)}"

    tabs = []
    for key, label in (("all", "All"), ("open", "Open"), ("resolved", "Resolved")):
        cls = "active" if key == status_filter else ""
        href = f"/reports?status={key}"
        tabs.append(f'<a href="{href}" class="{cls}">{label}</a>')
    tabs_html = '<div class="tabs">' + "".join(tabs) + "</div>"

    if rows:
        body_rows = "".join(
            f'<tr>'
            f'<td><a href="/reports/{r["id"]}" '
            f'style="color:var(--accent)">#{r["id"]}</a></td>'
            f"<td>{_target_link(r)}</td>"
            f"<td>{_author_link(r)}</td>"
            f'<td title="{esc(r.get("reason", ""))}">'
            f"{esc(_truncate(r.get('reason', ''), 60))}</td>"
            f"<td>{_reporter_link(r)}</td>"
            f"<td>{_votes_bar(r)}</td>"
            f"<td>{_status_badge(r['status'])}</td>"
            f"<td style='color:var(--muted)'>{_age_cell(r)}</td>"
            f'<td style="color:var(--muted)">'
            f"{_human_ts(r['decided_at']) if r.get('decided_at') else '—'}"
            f"</td>"
            f"</tr>"
            for r in rows
        )
        table_html = (
            '<div class="table-wrap"><table>'
            "<tr><th>report</th><th>target</th><th>flagged author</th>"
            "<th>reason</th><th>reporter</th><th>suspend:clear</th>"
            "<th>status</th><th>age</th><th>decided</th></tr>"
            f"{body_rows}"
            "</table></div>"
        )
    else:
        empty = {
            "all": "No reports filed yet - the docket is empty.",
            "open": "No open reports. The community has nothing pending.",
            "resolved": "No resolved reports. A clean docket, for now.",
        }.get(status_filter, "No reports.")
        table_html = f'<p style="color:var(--muted)">{empty}</p>'

    summary = (
        f'<p class="meta" style="margin:0 0 8px">'
        f"Page {page} of {total_pages} · {total} report"
        f"{'s' if total != 1 else ''}"
        f"</p>"
    )
    from viewer._helpers import _pager
    pager_top = _pager(page, total_pages, _href_for_page, top=True)
    pager_bot = _pager(page, total_pages, _href_for_page)
    body = (
        '<div class="panel"><h2>Reports</h2>'
        "<p style='color:var(--muted);font-size:15px'>"
        "Community transparency: every report and how it was judged. "
        "The frozen content snapshot survives deletion; resolved reports "
        "archive their votes so the verdict stays public. Use the tabs to "
        "see what's currently being judged vs what's been decided."
        "</p>"
        f"{tabs_html}"
        f"{summary}"
        f"{pager_top}"
        f"{table_html}"
        f"{pager_bot}"
        "</div>"
    )
    return _page("Reports", body, "reports")
