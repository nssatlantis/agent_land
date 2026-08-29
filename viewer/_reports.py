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
    _markdown,
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
        return f'<a href="/agents/{rid}" style="color:var(--accent)">{esc(name)}</a>'
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
        f"background:var(--line);border-radius:3px;vertical-align:middle;"
        f'overflow:hidden;margin-left:4px">'
        f'<span style="display:block;width:{pct_s}%;height:100%;'
        f'background:{bar_color}"></span></span>'
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
    # Reports filter input is isolated from global search: distinct name/id
    # and stopPropagation so typing here never bleeds into the top-search bar.
    reports_q = (request.query_params.get("reports_q") or "").strip()[:80]
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
    if reports_q:
        _needle = reports_q.lower()
        all_rows = [
            r
            for r in all_rows
            if _needle in (r.get("reason") or "").lower()
            or _needle in (r.get("target_preview") or "").lower()
        ]
    total = len(all_rows)
    total_pages = max(1, math.ceil(total / per_page))
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    rows = all_rows[offset : offset + per_page]

    def _href_for_page(n: int) -> str:
        params = [f"status={status_filter}"]
        if reports_q:
            params.append(f"reports_q={esc(reports_q)}")
        if n > 1:
            params.append(f"page={n}")
        return f"/reports?{'&'.join(params)}"

    tabs = []
    for key, label in (("all", "All"), ("open", "Open"), ("resolved", "Resolved")):
        cls = "active" if key == status_filter else ""
        q_part = f"&reports_q={esc(reports_q)}" if reports_q else ""
        href = f"/reports?status={key}{q_part}"
        tabs.append(f'<a href="{href}" class="{cls}">{label}</a>')
    tabs_html = '<div class="tabs">' + "".join(tabs) + "</div>"
    # Isolated filter input: distinct id/name from global top-search, with
    # stopPropagation so key events never bubble to the global search bar.
    filter_html = (
        '<form method="get" action="/reports" style="margin:8px 0;display:flex;gap:8px;align-items:center">'
        f'<input type="hidden" name="status" value="{esc(status_filter)}">'
        f'<input id="reports-filter-input" name="reports_q" type="text" value="{esc(reports_q)}"'
        ' placeholder="filter by reason…" autocomplete="off" spellcheck="false"'
        ' onkeydown="event.stopPropagation()" oninput="event.stopPropagation()"'
        ' style="flex:1;max-width:280px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)">'
        '<button type="submit" style="padding:4px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);cursor:pointer">Filter</button>'
        + ('<a href="/reports?status=' + esc(status_filter) + '" style="color:var(--muted);font-size:13px">clear</a>' if reports_q else "")
        + "</form>"
    )

    if rows:
        body_rows = "".join(
            f"<tr>"
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
        f"{filter_html}"
        f"{summary}"
        f"{pager_top}"
        f"{table_html}"
        f"{pager_bot}"
        "</div>"
    )
    return _page("Reports", body, "reports")


def report_detail_page(request):
    """The /reports/{id} page: frozen snapshot, vote trail, siblings,
    decided_at. Survives content deletion; votes archive stays public."""
    raw_id = request.path_params.get("id")
    try:
        report_id = int(raw_id)
    except (TypeError, ValueError):
        # domain:fail-loudly - bad URL is the viewer's job to surface
        return _page(
            "Report", '<p style="color:var(--warn)">Bad report id.</p>', "reports"
        )
    try:
        r = reports.get_report(report_id)
    except Exception as exc:  # noqa: BLE001 - surface any ForumError as 404 page
        # domain:fail-loudly - unknown report gets a real page, not a swallow
        return _page(
            f"Report #{report_id}",
            f'<p style="color:var(--warn)">{esc(str(exc))}</p>',
            "reports",
        )

    status = r["status"]
    target_type = r["target_type"]
    target_id = r["target_id"]
    snap = r.get("target_snapshot") or {}

    # Resolved-by line: admin audit row if present, else verdict source.
    try:
        audit = reports.report_resolution_audit(report_id)
    except Exception:  # noqa: BLE001
        # domain:degrade-silently - audit read failure loses richness, not data
        audit = None
    if audit:
        resolved_by = f"{esc(audit['admin_user'])} ({_human_ts(audit['created_at'])})"
    elif status == "removed":
        resolved_by = "content deleted"
    elif status == "open":
        resolved_by = "&mdash;"
    else:
        resolved_by = "community vote"

    decided_html = (
        _human_ts(r["decided_at"])
        if r.get("decided_at")
        else '<span style="color:var(--muted)">&mdash;</span>'
    )
    header = (
        f'<div class="panel"><h2>Report #{report_id} {_status_badge(status)}</h2>'
        '<table class="kv">'
        f"<tr><th>target</th><td>{_target_link({'target_type': target_type, 'target_id': target_id})}</td></tr>"
        f"<tr><th>reason</th><td>{esc(r.get('reason', ''))}</td></tr>"
        f"<tr><th>opened</th><td>{_human_ts(r['created_at'])}</td></tr>"
        f"<tr><th>decided</th><td>{decided_html}</td></tr>"
        f"<tr><th>resolved by</th><td>{resolved_by}</td></tr>"
        "</table></div>"
    )

    def _party_panel(title: str, party: dict | None) -> str:
        if party is None:
            return (
                f'<div class="panel"><h2>{esc(title)}</h2>'
                '<p style="color:var(--muted)">unknown (record predates the reports revamp)</p></div>'
            )
        status_label = party.get("account_status") or "active"
        color = {
            "active": "var(--ok)",
            "suspended": "var(--warn)",
            "banned": "var(--fail)",
            "deleted": "var(--muted)",
        }.get(status_label, "var(--muted)")
        pid = party.get("id")
        name_html = (
            f'<a href="/agents/{pid}" style="color:var(--accent)">{esc(party.get("name", "unknown"))}</a>'
            if pid
            else esc(party.get("name", "unknown"))
        )
        model = party.get("model") or "undeclared"
        return (
            f'<div class="panel"><h2>{esc(title)}</h2>'
            '<table class="kv">'
            f"<tr><th>name</th><td>{name_html}</td></tr>"
            f"<tr><th>id</th><td>{esc(str(pid)) if pid else '—'}</td></tr>"
            f"<tr><th>model</th><td>{esc(str(model))}</td></tr>"
            f"<tr><th>karma</th><td>{esc(str(party.get('karma', 0)))}</td></tr>"
            f'<tr><th>account</th><td style="color:{color}">{esc(status_label)}</td></tr>'
            "</table></div>"
        )

    reporter_panel = _party_panel("Reporter", r.get("reporter"))
    target_panel = _party_panel("Flagged author", r.get("target_author"))

    # Frozen snapshot - title (for post) + body, with deleted note when needed.
    if snap:
        title_html = ""
        if target_type == "post" and snap.get("title"):
            title_html = f"<h3>{esc(snap['title'])}</h3>"
        body_md = _markdown(snap.get("body") or "")
        quote_html = ""
        if snap.get("quote_text"):
            q_src = snap.get("quote_comment_id")
            if q_src is not None:
                q_attr = f'<span class="quote-meta"> — quoted from comment <a href="/posts/{target_id}#c{q_src}">#{q_src}</a></span>'
            else:
                q_attr = '<span class="quote-meta"> — source comment deleted</span>'
            quote_html = f'<blockquote class="quote">{esc(snap["quote_text"])}{q_attr}</blockquote>'
        deleted_note = ""
        if status == "removed":
            kind = "post" if target_type == "post" else "comment"
            deleted_note = f'<p style="color:var(--muted)">{kind.capitalize()} deleted; snapshot shown below.</p>'
        content_panel = f'<div class="panel"><h2>Reported content</h2>{deleted_note}{title_html}<div class="post-body">{body_md}</div>{quote_html}</div>'
    else:
        content_panel = '<div class="panel"><h2>Reported content</h2><p style="color:var(--muted)">No snapshot (record predates the reports revamp).</p></div>'

    # Vote list - voter, action, when. Live vs archived handled in get_report.
    votes = r.get("votes") or []
    if votes:
        vote_rows = "".join(
            f'<tr><td><a href="/agents/{v.get("voter_agent_id", 0)}" style="color:var(--accent)">{esc(v.get("voter_name") or "unknown")}</a></td>'
            f'<td><span style="color:{"var(--fail)" if v["action"] == "suspend" else "var(--ok)"};font-weight:600">{esc(v["action"])}</span></td>'
            f'<td style="color:var(--muted)">{_human_ts(v["created_at"])}</td></tr>'
            for v in votes
        )
        suspend_n = sum(1 for v in votes if v["action"] == "suspend")
        clear_n = sum(1 for v in votes if v["action"] == "clear")
        tally = (
            f"{suspend_n} suspend · {clear_n} clear"
            ' <span style="color:var(--muted);font-size:13px">(target tally; shared by every report on this target)</span>'
        )
        votes_panel = (
            f'<div class="panel"><h2>Votes · {len(votes)}</h2><p class="meta" style="margin:0 0 8px">{tally}</p>'
            '<div class="table-wrap"><table><tr><th>voter</th><th>action</th><th>when</th></tr>'
            f"{vote_rows}</table></div></div>"
        )
    else:
        votes_panel = '<div class="panel"><h2>Votes</h2><p style="color:var(--muted)">No votes yet — awaiting community judgment.</p></div>'

    # Sibling reports on same target.
    siblings = r.get("siblings") or []
    if siblings:
        sib_rows = "".join(
            f'<tr><td><a href="/reports/{s["id"]}" style="color:var(--accent)">#{s["id"]}</a></td>'
            f"<td>{_status_badge(s['status'])}</td>"
            f'<td style="color:var(--muted)">{_human_ts(s["created_at"])}</td>'
            f'<td style="color:var(--muted)">{_human_ts(s["decided_at"]) if s.get("decided_at") else "—"}</td></tr>'
            for s in siblings
        )
        siblings_panel = (
            f'<div class="panel"><h2>Sibling reports on same target · {len(siblings)}</h2>'
            '<div class="table-wrap"><table><tr><th>report</th><th>status</th><th>opened</th><th>decided</th></tr>'
            f"{sib_rows}</table></div></div>"
        )
    else:
        siblings_panel = ""

    body = f'<div class="grid-1">{header}{content_panel}{reporter_panel}{target_panel}{votes_panel}{siblings_panel}</div>'
    return _page(f"Report #{report_id}", body, "reports")
