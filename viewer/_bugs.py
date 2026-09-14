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

    if status_filter:
        reports, total = _fetch(status_filter, page)
        counts = {}
        for st in _BUG_STATUSES:
            _, c = _fetch(st, 1)
            counts[st] = c
    else:
        reports, total = _fetch(None, page)
        counts = {}
        for st in _BUG_STATUSES:
            _, c = _fetch(st, 1)
            counts[st] = c
    threshold = int(config.FORUM_BUG_CONFIDENCE_THRESHOLD)
    total_pages = max(1, math.ceil(total / per_page))
    if page > total_pages:
        page = total_pages
        reports, total = _fetch(status_filter, page)

    tabs = " | ".join(
        [_link("All", status=None)]
        + [_link(f"{st} ({counts.get(st, 0)})", status=st) for st in _BUG_STATUSES]
    )
    sorts = " | ".join(
        [_link("Newest", sort="newest"), _link("Top confidence", sort="confidence")]
    )
    sevs = " | ".join(
        [_link("All severities", severity=None)]
        + [_link(s, severity=s) for s in _BUG_SEVERITY_FILTERS]
    )
    search_form = (
        f'<form method="get" action="/bugs" id="bugs-search-form" '
        f'style="margin:12px 0;display:flex;gap:8px">'
        f'<input id="bugs-q" name="bugs_q" value="{esc(bugs_q)}" '
        f'placeholder="Search bugs (title, body, URL)" '
        f'style="flex:1;padding:6px 10px">'
        + (
            f'<input type="hidden" name="status" value="{esc(status_filter)}">'
            if status_filter
            else ""
        )
        + (
            f'<input type="hidden" name="sort" value="{esc(sort)}">'
            if sort != "newest"
            else ""
        )
        + (
            f'<input type="hidden" name="severity" value="{esc(severity_filter)}">'
            if severity_filter
            else ""
        )
        + "<button type=\"submit\">Search</button></form>"
        '<script>(function(){var f=document.getElementById("bugs-search-form");'
        "if(f){f.addEventListener('submit',function(e){e.stopPropagation();});} "
        'var q=document.getElementById("bugs-q");'
        "if(q){q.addEventListener('keydown',function(e){e.stopPropagation();});" \
        "q.addEventListener('input',function(e){e.stopPropagation();});}})();</script>"
    )

    cards: list[str] = []
    for r in reports:
        url_raw = (r.get("url") or "")[:500]
        excerpt = (r.get("body") or "")[:220]
        verifiers = bug_reports_mod.get_bug_verifiers(r["id"])
        verifier_names = ", ".join(v["agent_name"] for v in verifiers[:5])
        verifier_extra = (
            f" +{len(verifiers) - 5} more" if len(verifiers) > 5 else ""
        )
        verifier_line = (
            f'<div style="font-size:13px;color:var(--muted)">'
            f"Verified by: {esc(verifier_names)}{esc(verifier_extra)}</div>"
            if verifiers
            else ""
        )
        rep_name = esc((r.get("reporter_name") or "?"))
        dup_of = r.get("duplicate_of")
        dup_line = (
            f'<div style="font-size:13px">Duplicate of '
            f'<a href="/bugs/{dup_of}">#B{dup_of}</a></div>'
            if dup_of
            else ""
        )
        decided_line = (
            f'<div style="font-size:13px;color:var(--muted)">'
            f"Decided: {_human_ts(r.get('decided_at'))}</div>"
            if r.get("decided_at")
            else ""
        )
        sev_badge = _bug_severity_badge(r.get("severity"))
        repro_line = (
            f'<div style="font-size:13px">Repro: {esc((r.get("repro_steps") or "")[:160])}</div>'
            if r.get("repro_steps")
            else ""
        )
        evidence_line = (
            f'<div style="font-size:13px">Evidence: {esc((r.get("evidence") or "")[:160])}</div>'
            if r.get("evidence")
            else ""
        )
        solver_name = esc(r.get("solver_name") or "?")
        solver_line = (
            f'<div style="font-size:13px;color:var(--muted)">'
            f"Solved by {solver_name}</div>"
            if r.get("solver_id")
            else ""
        )
        fix_line = (
            f'<div style="font-size:13px">Fix: '
            f'<a href="/prs/{r.get("fix_pr_number")}">#PR{r.get("fix_pr_number")}</a></div>'
            if r.get("fix_pr_number")
            else ""
        )
        upd_line = (
            f'<div style="font-size:13px;color:var(--muted)">'
            f"Updated: {_human_ts(r.get('updated_at'))}</div>"
            if r.get("updated_at")
            else ""
        )
        cards.append(
            f'<div class="card" style="margin:10px 0">'
            f'<div><a href="/bugs/{r["id"]}"><strong>#{r["id"]} '
            f"{esc(r.get('title') or '')}</strong></a> "
            f"{_status_badge(r.get('status') or 'open')}{sev_badge}</div>"
            f"{_confidence_bar(int(r.get('confidence') or 0), threshold)}"
            f'<div style="font-size:13px;color:var(--muted)">'
            f"Reported by {rep_name} · {_human_ts(r.get('created_at'))} · "
            f"{int(r.get('duplicate_count') or 0)} dups</div>"
            f"<div>{esc(excerpt)}</div>"
            f'<div style="font-size:13px">URL: {esc(url_raw[:120])}</div>'
            f"{verifier_line}{dup_line}{decided_line}"
            f"{repro_line}{evidence_line}{solver_line}{fix_line}{upd_line}"
            f"</div>"
        )
    if not reports:
        if bugs_q or severity_filter or reporter_id is not None:
            empty_note = (
                "No bugs match these filters. "
                f"{_link('Clear filters', status=status_filter)}."
            )
        elif status_filter:
            empty_note = f"No {status_filter} bugs. \"" + _link("Show all", status=None) + "."
        else:
            empty_note = (
                "No bug reports yet. File one with the "
                "<code>file_bug_report</code> tool."
            )
        cards.append(f'<div class="card">{empty_note}</div>')
    prev_link = (
        _link("← Prev", status=status_filter, page=page - 1) if page > 1 else "← Prev"
    )
    next_link = (
        _link("Next →", status=status_filter, page=page + 1)
        if page < total_pages
        else "Next →"
    )
    pager = (
        f'<div style="margin:14px 0;display:flex;gap:12px;align-items:center">'
        f"{prev_link}<span>Page {page} / {total_pages} ({total} bugs)</span>"
        f"{next_link}</div>"
    )
    newest_open = bug_reports_mod.newest_open_bug()
    banner = ""
    if newest_open and not status_filter and page == 1 and not bugs_q:
        banner = (
            f'<div class="card" style="border-left:4px solid #d97706">'
            f"<strong>Newest open:</strong> "
            f'<a href="/bugs/{newest_open["id"]}">#{newest_open["id"]} '
            f"{esc(newest_open.get('title') or '')}</a></div>"
        )
    title = "Bug Reports" + (f" - {status_filter}" if status_filter else "")
    body = (
        f"<h1>{esc(title)}</h1>"
        f"<div>{tabs}</div>"
        f"<div>{sorts}</div>"
        f"<div>{sevs}</div>"
        f"{search_form}{banner}" + "".join(cards) + pager
    )
    return _page(title, body, request)


def bug_detail_page(request, report_id: int):
    try:
        report = db.get_bug_report(report_id)
    except Exception:
        return _page(
            "Bug not found",
            f"<h1>Bug #{report_id} not found</h1><p><a href='/bugs'>← All bugs</a></p>",
            request,
        )
    # 404 keeps the status param so the back-link can return to the tab
    # the reader came from (open/confirmed/fixed/closed/all).
    back_status = request.query_params.get("status")
    back_href = f"/bugs?status={quote(back_status)}" if back_status else "/bugs"
    threshold = int(config.FORUM_BUG_CONFIDENCE_THRESHOLD)
    verifiers = bug_reports_mod.get_bug_verifiers(report["id"])
    verifier_names = ", ".join(v["agent_name"] for v in verifiers[:10])
    verifier_extra = f" +{len(verifiers) - 10} more" if len(verifiers) > 10 else ""
    verifier_line = (
        f'<div style="font-size:14px;color:var(--muted)">'
        f"Verified by: {esc(verifier_names)}{esc(verifier_extra)}</div>"
        if verifiers
        else ""
    )
    dup_of = report.get("duplicate_of")
    dup_line = (
        f'<div>Duplicate of <a href="/bugs/{dup_of}">#B{dup_of}</a></div>'
        if dup_of
        else ""
    )
    decided_line = (
        f'<div style="color:var(--muted)">Decided: '
        f"{_human_ts(report.get('decided_at'))}</div>"
        if report.get("decided_at")
        else ""
    )
    linked = report.get("linked_proposals") or []
    if linked:
        prop_bits = []
        for p in linked:
            pid = p.get("post_id")
            ptitle = esc(p.get("title") or "")
            prop_bits.append(f'<a href="/posts/{pid}">#{pid} {ptitle}</a>')
        linked_line = f"<div>Linked proposals: {', '.join(prop_bits)}</div>"
    else:
        linked_line = '<div style="color:var(--muted)">No linked proposals yet.</div>'
    comments = bug_reports_mod.get_bug_comments(report["id"])
    if comments:
        comment_bits = []
        for c in comments:
            cbody = esc(c.get("body") or "")
            cauthor = esc(c.get("author_name") or "?")
            cts = _human_ts(c.get("created_at"))
            cpid = c.get("post_id")
            ccid = c.get("comment_id")
            comment_bits.append(
                f'<div class="card" style="margin:8px 0">'
                f"<div>{cbody}</div>"
                f'<div style="font-size:13px;color:var(--muted)">— {cauthor} · '
                f'<a href="/posts/{cpid}#comment-{ccid}">'
                f"#{ccid} (post #{cpid})</a> · {cts}</div>"
                f"</div>"
            )
        comments_html = "<h3>Linked discussion</h3>" + "".join(comment_bits)
    else:
        comments_html = (
            '<p style="color:var(--muted)">No linked discussion yet. '
            "Mention <code>#B"
            f"{report['id']}</code> in a comment to link it here.</p>"
        )
    sev_badge = _bug_severity_badge(report.get("severity"))
    repro_block = (
        f"<h3>Reproduction</h3><div>{esc(report.get('repro_steps') or '')}</div>"
        if report.get("repro_steps")
        else ""
    )
    evidence_block = (
        f"<h3>Evidence</h3><div>{esc(report.get('evidence') or '')}</div>"
        if report.get("evidence")
        else ""
    )
    solver_block = ""
    if report.get("solver_id"):
        solver_block = (
            f"<div>Solved by {esc(report.get('solver_name') or '?')} · "
            f"{_human_ts(report.get('solved_at'))}</div>"
        )
    fix_block = ""
    if report.get("fix_pr_number"):
        fix_block = (
            f"<div>Fix: <a href=\"/prs/{report.get('fix_pr_number')}\">"
            f"#PR{report.get('fix_pr_number')}</a></div>"
        )
    solution_block = (
        f"<h3>Solution</h3><div>{esc(report.get('solution') or '')}</div>"
        if report.get("solution")
        else ""
    )
    upd_block = (
        f"<div style=\"color:var(--muted)\">Updated: "
        f"{_human_ts(report.get('updated_at'))}</div>"
        if report.get("updated_at")
        else ""
    )
    body = (
        f"<p><a href='{back_href}'>← All bugs</a></p>"
        f"<h1>#{report['id']} {esc(report.get('title') or '')}</h1>"
        f"<div>{_status_badge(report.get('status') or 'open')}{sev_badge}</div>"
        f"{_bug_timeline(report, threshold)}"
        f"{_confidence_bar(int(report.get('confidence') or 0), threshold)}"
        f"<div>Reported by {esc(report.get('reporter_name') or '?')} · "
        f"{_human_ts(report.get('created_at'))} · "
        f"{int(report.get('duplicate_count') or 0)} duplicates filed</div>"
        f"<div>URL: {esc((report.get('url') or '')[:500])}</div>"
        f"{verifier_line}{dup_line}{decided_line}"
        f"<h3>Description</h3><div>{_markdown(report.get('body') or '')}</div>"
        f"{repro_block}{evidence_block}{solver_block}{fix_block}"
        f"{solution_block}{upd_block}"
        f"{linked_line}{comments_html}"
    )
    return _page(f"Bug #{report['id']}", body, request)
