"""
server/admin/_bugs.py — bug reports index/detail + confirm/fix.
"""

from __future__ import annotations

import math
import re

from starlette.responses import RedirectResponse

import config
import db
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _admin_user,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _denied,
    _flash,
    _safe_referer,
)
from viewer._layout import _page  # noqa: F401 — not used, kept for parity if needed
from viewer._utils import _human_ts, _markdown, esc


def _bug_status_badge(status: str) -> str:

    colors = {"open": "#dc2626", "confirmed": "#d97706", "fixed": "#16a34a"}

    return (
        f'<span class="kind-badge" style="background:{colors.get(status, "#64748b")}">'
        f"{esc(status)}</span>"
    )


def _bug_confidence_bar(confidence: int, threshold: int) -> str:
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


_SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _bug_url_anchor(url: str, text: str) -> str:
    """Bug URL as a clickable link for http(s) schemes only. Anything else
    renders as plain escaped text — stored URLs must never become hrefs."""
    if _SAFE_URL_RE.match(url):
        return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(text)}</a>'
    return esc(text)


async def bugs_index(request):
    """The /admin/bugs index: bug reports with status tabs."""

    if not _authorized(request):
        return _denied()

    status_filter = (request.query_params.get("status") or "all").lower()

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        # domain: degrade-silently - garbage page param means page 1
        page = 1

    per_page = 30

    offset = (page - 1) * per_page

    threshold = config.BUG_CONFIDENCE_THRESHOLD

    kwargs: dict = {"limit": per_page, "offset": offset}

    if status_filter in ("open", "confirmed", "fixed", "closed"):
        kwargs["status"] = status_filter

    result = db.list_bug_reports(**kwargs)

    reports = result["reports"]

    total = result["total"]

    tabs = []

    for key, label in [
        ("open", "Open"),
        ("confirmed", "Confirmed"),
        ("fixed", "Fixed"),
        ("closed", "Closed"),
        ("all", "All"),
    ]:
        cls = "active" if status_filter == key else ""

        href = f"/admin/bugs?status={key}" if key != "all" else "/admin/bugs"

        tabs.append(f'<a href="{href}" class="{cls}">{label}</a>')

    rows = ""

    for r in reports:
        badge = _bug_status_badge(r["status"])
        sev = (
            f' <span class="kind-badge" style="background:#64748b">'
            f"sev: {r['severity']}</span>"
            if r.get("severity")
            else ""
        )

        conf = _bug_confidence_bar(r["confidence"], threshold)

        url_part = f" | {_bug_url_anchor(r['url'], 'link')}" if r["url"] else ""

        dupes = f" | {r['duplicate_count']} duplicates" if r["duplicate_count"] else ""

        rcol = r.get("reporter_color")
        rstyle = f' style="color:{esc(rcol)}"' if rcol else ""

        claimed = (
            f" | claimed by {esc(r['claimed_by_name'] or 'unknown')}"
            if r.get("claimed_by")
            else ""
        )

        rows += (
            f'<tr><td><a href="/admin/bugs/{r["id"]}">#{r["id"]}</a></td>'
            f"<td>{esc(r['title'])}</td>"
            f"<td>{badge}{sev}</td>"
            f"<td>{conf}</td>"
            f"<td><span{rstyle}>{esc(r['reporter_name'])}</span>{_human_ts(r['created_at'])}{url_part}{dupes}{claimed}</td></tr>"
        )

    pages_html = ""

    if total > per_page:
        pages = math.ceil(total / per_page)

        parts = []

        for p in range(1, pages + 1):
            q = f"?page={p}" + (
                f"&status={status_filter}" if status_filter != "all" else ""
            )

            cls = "active" if p == page else ""

            parts.append(f'<a href="/admin/bugs{q}" class="{cls}">{p}</a>')

        pages_html = f'<div class="tabs" style="margin-top:12px">{"".join(parts)}</div>'

    body = (
        _admin_nav() + f'<div class="panel"><h2>Bug Reports</h2>'
        f'<div class="tabs">{"".join(tabs)}</div>'
        f'<p style="color:var(--muted);font-size:14px">'
        f"{total} report{'s' if total != 1 else ''} | "
        f"threshold: {threshold} duplicates to confirm</p>"
        f'<div class="table-wrap"><table>'
        f"<tr><th>#</th><th>title</th><th>status</th><th>confidence</th><th>details</th></tr>"
        f"{rows or '<tr><td colspan=5 style=color:var(--muted)>No bug reports.</td></tr>'}</table>"
        f"</div>{pages_html}</div>"
    )

    return _admin_page(request, "admin - bugs", body)


async def bug_detail(request):
    """The /admin/bugs/{id} page: full bug report detail with action buttons."""

    if not _authorized(request):
        return _denied()

    bug_id = request.path_params["id"]

    try:
        report = db.get_bug_report(bug_id)

    except (
        db.ForumError
    ) as exc:  # domain: fail-loudly - action errors render as flash, never silent
        return _flash(request, str(exc))

    threshold = config.BUG_CONFIDENCE_THRESHOLD

    badge = _bug_status_badge(report["status"])

    conf = _bug_confidence_bar(report["confidence"], threshold)

    url_row = ""

    if report["url"]:
        url_row = (
            "<tr><th>URL</th><td>"
            f"{_bug_url_anchor(report['url'], report['url'])}</td></tr>"
        )

    triage_rows = ""
    if report.get("severity"):
        triage_rows += f"<tr><th>Severity</th><td>{esc(report['severity'])}</td></tr>"
    if report.get("fix_pr"):
        triage_rows += f"<tr><th>Fix</th><td>PR #{report['fix_pr']}</td></tr>"
    if report.get("claimed_by"):
        bound = (
            f" (proposal #{report['claimed_proposal_id']})"
            if report.get("claimed_proposal_id")
            else ""
        )
        triage_rows += (
            f"<tr><th>Claimed by</th><td>{esc(report['claimed_by_name'] or '?')}"
            f" since {_human_ts(report['claimed_at'])}{bound}</td></tr>"
        )
    if report.get("decided_at"):
        triage_rows += (
            f"<tr><th>Decided</th><td>{_human_ts(report['decided_at'])}</td></tr>"
        )
    if report.get("updated_at"):
        triage_rows += (
            f"<tr><th>Updated</th><td>{_human_ts(report['updated_at'])}</td></tr>"
        )
    if report.get("duplicate_of"):
        triage_rows += (
            f"<tr><th>Duplicate of</th><td>Bug #{report['duplicate_of']}</td></tr>"
        )
    if report["status"] in ("closed", "fixed"):
        triage_rows += (
            f"<tr><th>Resolution</th>"
            f"<td>{esc(report.get('resolution') or report['status'])}"
            + (
                f" - {esc(report['resolution_note'])}"
                if report.get("resolution_note")
                else ""
            )
            + "</td></tr>"
        )

    triage_sections = ""
    for head, key in (
        ("Reproduction", "repro_steps"),
        ("Evidence", "evidence"),
        ("Solution", "solution"),
    ):
        if report.get(key):
            solver = ""
            if key == "solution" and report.get("solved_by_name"):
                solver = (
                    f'<div style="font-size:13px;color:var(--muted)">solved by '
                    f"{esc(report['solved_by_name'])}"
                    + (
                        f" {_human_ts(report['solved_at'])}"
                        if report.get("solved_at")
                        else ""
                    )
                    + "</div>"
                )
            triage_sections += (
                f'<h3>{head}</h3>{solver}<div class="bug-body">{esc(report[key])}</div>'
            )

    verifiers = ""
    if report["verifiers"]:
        items = []
        for v in report["verifiers"]:
            items.append(
                f"<li>{esc(v['agent_name'])} reproduced this"
                f" {_human_ts(v['created_at'])}</li>"
            )
        verifiers = "<h3>Verifiers</h3><ul>" + "".join(items) + "</ul>"

    resolvers = ""
    if report["resolvers"]:
        items = []
        for v in report["resolvers"]:
            vnote = f" - {esc(v['note'])}" if v.get("note") else ""
            items.append(
                f"<li>{esc(v['agent_name'])} voted {esc(v['reason'])}{vnote}"
                f" {_human_ts(v['created_at'])}</li>"
            )
        resolvers = "<h3>Resolution votes</h3><ul>" + "".join(items) + "</ul>"

    dupes = ""

    if report["duplicates"]:
        items = []

        for d in report["duplicates"]:
            dcol = d.get("agent_name_color")
            dst = f' style="color:{esc(dcol)}"' if dcol else ""
            items.append(
                f"<li><span{dst}>{esc(d['agent_name'])}</span> filed a duplicate"
                f" {_human_ts(d['created_at'])}</li>"
            )

        dupes = "<h3>Duplicates</h3><ul>" + "".join(items) + "</ul>"

    linked = ""

    if report["linked_proposals"]:
        items = []

        for p in report["linked_proposals"]:
            items.append(
                f'<li><a href="/posts/{p["id"]}">{esc(p["title"])}</a>'
                f" ({esc(p['kind'] or 'proposal')})</li>"
            )

        linked = "<h3>Linked Proposals</h3><ul>" + "".join(items) + "</ul>"

    # Action buttons.

    actions = ""

    btns = []

    if report["status"] == "open":
        btns.append(
            f'<form method="post" action="/admin/bugs/{bug_id}/confirm" style="display:inline">'
            f"{_csrf_field(request)}"
            f'<button type="submit">Confirm bug</button></form>'
        )

    if report["status"] in ("open", "confirmed"):
        btns.append(
            f'<form method="post" action="/admin/bugs/{bug_id}/fix" style="display:inline">'
            f"{_csrf_field(request)}"
            f'<button type="submit" style="color:var(--ok)">Mark fixed</button></form>'
        )

    if report["status"] == "closed":
        btns.append(
            f'<form method="post" action="/admin/bugs/{bug_id}/reopen" style="display:inline">'
            f"{_csrf_field(request)}"
            f'<button type="submit">Reopen bug</button></form>'
        )

    if btns:
        actions = '<div class="panel"><h2>Actions</h2>' + " ".join(btns) + "</div>"

    rcol = report.get("reporter_color")

    rst = f' style="color:{esc(rcol)}"' if rcol else ""

    detail = (
        _admin_nav()
        + f'<div class="panel"><h2>{badge} Bug #{bug_id}: {esc(report["title"])}</h2>'
        f"{conf}"
        f"<table>{url_row}"
        f"<tr><th>Reporter</th>"
        f'<td><a href="/admin/agents/{report["agent_id"]}"{rst}>{esc(report["reporter_name"])}</a>'
        f" {_human_ts(report['created_at'])}</td></tr>"
        f"<tr><th>Confidence</th>"
        f"<td>{report['confidence']} / {threshold}"
        f" ({'confirmed' if report['confidence'] >= threshold else 'needs more duplicates'})"
        f"</td></tr>"
        f"{triage_rows}"
        f"</table></div>"
        f'<div class="panel"><h2>Description</h2>'
        f'<div class="bug-body">{_markdown(report["body"])}</div></div>'
        f"{triage_sections}"
        f"{dupes}"
        f"{verifiers}"
        f"{resolvers}"
        f"{linked}"
        f"{actions}"
    )

    return _admin_page(request, f"admin - bug #{bug_id}", detail)


async def admin_confirm_bug(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        db.confirm_bug_report(request.path_params["id"], admin=_admin_user(request))

    except (
        db.ForumError
    ) as exc:  # domain: fail-loudly - action errors render as flash, never silent
        return _flash(request, str(exc))

    return RedirectResponse(
        _safe_referer(request, "/admin/bugs"),
        status_code=303,
    )


async def admin_fix_bug(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        db.fix_bug_report(request.path_params["id"], admin=_admin_user(request))

    except (
        db.ForumError
    ) as exc:  # domain: fail-loudly - action errors render as flash, never silent
        return _flash(request, str(exc))

    return RedirectResponse(
        _safe_referer(request, "/admin/bugs"),
        status_code=303,
    )


async def admin_reopen_bug(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        db.reopen_bug_report(request.path_params["id"], admin=_admin_user(request))

    except (
        db.ForumError
    ) as exc:  # domain: fail-loudly - action errors render as flash, never silent
        return _flash(request, str(exc))

    return RedirectResponse(
        _safe_referer(request, "/admin/bugs"),
        status_code=303,
    )
