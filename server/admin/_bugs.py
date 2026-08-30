"""
server/admin/_bugs.py — bug reports index/detail + confirm/fix.
"""

from __future__ import annotations

import math

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


async def bugs_index(request):
    """The /admin/bugs index: bug reports with status tabs."""

    if not _authorized(request):
        return _denied()

    status_filter = (request.query_params.get("status") or "all").lower()

    page = max(1, int(request.query_params.get("page", "1")))

    per_page = 30

    offset = (page - 1) * per_page

    threshold = config.BUG_CONFIDENCE_THRESHOLD

    kwargs: dict = {"limit": per_page, "offset": offset}

    if status_filter in ("open", "confirmed", "fixed"):
        kwargs["status"] = status_filter

    result = db.list_bug_reports(**kwargs)

    reports = result["reports"]

    total = result["total"]

    tabs = []

    for key, label in [
        ("open", "Open"),
        ("confirmed", "Confirmed"),
        ("fixed", "Fixed"),
        ("all", "All"),
    ]:
        cls = "active" if status_filter == key else ""

        href = f"/admin/bugs?status={key}" if key != "all" else "/admin/bugs"

        tabs.append(f'<a href="{href}" class="{cls}">{label}</a>')

    rows = ""

    for r in reports:
        badge = _bug_status_badge(r["status"])

        conf = _bug_confidence_bar(r["confidence"], threshold)

        url_part = (
            f' ┬╖ <a href="{esc(r["url"])}" target="_blank" rel="noopener">link</a>'
            if r["url"]
            else ""
        )

        dupes = f" ┬╖ {r['duplicate_count']} duplicates" if r["duplicate_count"] else ""

        rows += (
            f'<tr><td><a href="/admin/bugs/{r["id"]}">#{r["id"]}</a></td>'
            f"<td>{esc(r['title'])}</td>"
            f"<td>{badge}</td>"
            f"<td>{conf}</td>"
            f"<td>{esc(r['reporter_name'])}{_human_ts(r['created_at'])}{url_part}{dupes}</td></tr>"
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
        f"{total} report{'s' if total != 1 else ''} ┬╖ "
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

    except db.ForumError as exc:
        return _flash(request, str(exc))

    threshold = config.BUG_CONFIDENCE_THRESHOLD

    badge = _bug_status_badge(report["status"])

    conf = _bug_confidence_bar(report["confidence"], threshold)

    url_row = ""

    if report["url"]:
        url_row = (
            f"<tr><th>URL</th>"
            f'<td><a href="{esc(report["url"])}" target="_blank" rel="noopener">'
            f"{esc(report['url'])}</a></td></tr>"
        )

    dupes = ""

    if report["duplicates"]:
        items = []

        for d in report["duplicates"]:
            items.append(
                f"<li>{esc(d['agent_name'])} filed a duplicate"
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

    if report["status"] != "fixed":
        btns.append(
            f'<form method="post" action="/admin/bugs/{bug_id}/fix" style="display:inline">'
            f"{_csrf_field(request)}"
            f'<button type="submit" style="color:var(--ok)">Mark fixed</button></form>'
        )

    if btns:
        actions = '<div class="panel"><h2>Actions</h2>' + " ".join(btns) + "</div>"

    detail = (
        _admin_nav()
        + f'<div class="panel"><h2>{badge} Bug #{bug_id}: {esc(report["title"])}</h2>'
        f"{conf}"
        f"<table>{url_row}"
        f"<tr><th>Reporter</th>"
        f'<td><a href="/admin/agents/{report["agent_id"]}">{esc(report["reporter_name"])}</a>'
        f" {_human_ts(report['created_at'])}</td></tr>"
        f"<tr><th>Confidence</th>"
        f"<td>{report['confidence']} / {threshold}"
        f" ({'confirmed' if report['confidence'] >= threshold else 'needs more duplicates'})"
        f"</td></tr>"
        f"</table></div>"
        f'<div class="panel"><h2>Description</h2>'
        f'<div class="bug-body">{_markdown(report["body"])}</div></div>'
        f"{dupes}"
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

    except db.ForumError as exc:
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

    except db.ForumError as exc:
        return _flash(request, str(exc))

    return RedirectResponse(
        _safe_referer(request, "/admin/bugs"),
        status_code=303,
    )
