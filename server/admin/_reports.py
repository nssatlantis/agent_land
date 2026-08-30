"""
server/admin/_reports.py — reports docket + report detail + resolve.

Admin dashboard lives here (admin_page) because its report table is the
primary panel; the economy/jobs/proposals/citizens panels are imported
lazily inside admin_page to avoid cross-leaf cycles.
"""

from __future__ import annotations

from starlette.responses import RedirectResponse

import config
import db
import moderation  # noqa: F401 — used by resolve_report
import reports
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _admin_user,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _denied,
    _flash,
)
from viewer._utils import _human_ts, _markdown, _rows, _ts_or_dash, esc


async def admin_page(request):
    # Lazy imports to avoid cross-leaf cycles at import time — admin_page
    # composes panels from four other leaves.
    from server.admin._agents import _render_citizens
    from server.admin._economy import _render_economy
    from server.admin._jobs import _render_jobs
    from server.admin._posts import _render_proposals

    if not _authorized(request):
        return _denied()

    all_reports = reports.list_reports(status="open")

    threads = reports.comment_post_ids(
        [r["target_id"] for r in all_reports if r["target_type"] == "comment"]
    )

    active = all_reports

    resolved: list = []

    reports_html = (
        '<div class="panel"><h2>Reports</h2>'
        f'<p style="color:var(--muted)"><b>{len(active)} active</b> ┬╖ '
        f"{len(resolved)} resolved ┬╖ "
        f'<a href="/admin/reports">view all &rarr;</a></p>'
        f'<div class="table-wrap"><table><tr><th>report</th><th>target</th>'
        "<th>flagged author</th><th>reporter</th><th>reason</th><th>suspend/clear</th>"
        "<th>status</th><th>opened</th></tr>"
        + (
            "".join(_report_row(r, "docket", threads) for r in active)
            or '<tr><td colspan=8 style="color:var(--muted)">No open reports.</td></tr>'
        )
        + "</table></div></div>"
    )

    return _admin_page(
        request,
        "admin",
        _admin_nav()
        + reports_html
        + _render_economy(request)
        + _render_jobs(request)
        + _render_proposals(request)
        + _render_citizens(request),
    )


def _report_status_badge(status: str) -> str:

    color = {
        "open": "status-warn",
        "suspended": "status-fail",
        "cleared": "status-ok",
        "removed": "status-warn",
    }.get(status, "status-warn")

    return f'<span class="{color}">{esc(status)}</span>'


def _report_target_link(r: dict, threads: dict[int, int] | None = None) -> str:
    """Where a report's target lives: posts link to their thread, comments to

    the thread that carries them. `threads` may carry a batched comment-id ->

    post-id map (db.comment_post_ids) so a whole docket render resolves every

    comment target in one query instead of one per row; without it, the

    single-lookup fallback is used."""

    if r["target_type"] == "post":
        return f'<a href="/posts/{r["target_id"]}">{esc(r["target_type"])} #{r["target_id"]}</a>'

    if threads is not None:
        thread = threads.get(r["target_id"])

    else:
        thread = reports.find_post_id_for_comment(r["target_id"])

    if thread is not None:
        return (
            f'<a href="/posts/{thread}#comment-{r["target_id"]}">'
            f"comment #{r['target_id']}</a>"
        )

    return f'<span style="color:var(--muted)">comment #{r["target_id"]} (thread gone)</span>'


def _report_author_link(r: dict) -> str:
    """The flagged author, linked to their admin detail when the row still

    exists (target_author_id is NULLed on agent deletion)."""

    if r.get("target_author_id"):
        return f'<a href="/admin/agents/{r["target_author_id"]}">{esc(r["target_author"])}</a>'

    name = r.get("target_author") or "deleted citizen"

    return f'<span style="color:var(--muted)">{esc(name)}</span>'


def _report_row(r: dict, context: str, threads: dict[int, int] | None = None) -> str:
    """One docket row for a report. `context` picks the columns: 'docket' for

    the /admin panel, 'index' for the /admin/reports index (adds preview +

    decided). `threads` is the batched comment->post map for the render."""

    votes = f"{r['suspend_votes']} / {r['clear_votes']}"

    if context == "index":
        preview = esc(r.get("target_preview") or "content deleted, no snapshot")

        return (
            f'<tr><td><a href="/admin/reports/{r["id"]}">#{r["id"]}</a></td>'
            f"<td>{_report_target_link(r, threads)}</td><td>{_report_author_link(r)}</td>"
            f"<td>{esc(r['reporter'])}</td>"
            f'<td title="{esc(r["reason"])}">{preview}</td>'
            f"<td>{votes}</td><td>{_report_status_badge(r['status'])}</td>"
            f"<td style='color:var(--muted)'>{_human_ts(r['created_at'])}</td>"
            f"<td style='color:var(--muted)'>{_ts_or_dash(r['decided_at'])}</td>"
            f'<td><a href="/admin/reports/{r["id"]}">open</a></td></tr>'
        )

    return (
        f'<tr><td><a href="/admin/reports/{r["id"]}">#{r["id"]}</a></td>'
        f"<td>{_report_target_link(r, threads)}</td><td>{_report_author_link(r)}</td>"
        f"<td>{esc(r['reporter'])}</td>"
        f'<td title="{esc(r["reason"])}">{esc(r["reason"])}</td>'
        f"<td>{votes}</td><td>{_report_status_badge(r['status'])}</td>"
        f"<td style='color:var(--muted)'>{_human_ts(r['created_at'])}</td></tr>"
    )


def _report_section(
    title: str, count: int, reports: list[dict], threads: dict[int, int] | None = None
) -> str:

    return (
        f'<div class="panel"><h2>{title} <span style="color:var(--muted)">({count})</span></h2>'
        '<div class="table-wrap"><table><tr><th>report</th><th>target</th>'
        "<th>flagged author</th><th>reporter</th><th>snapshot preview</th>"
        "<th>suspend/clear</th><th>status</th><th>opened</th><th>decided</th><th></th></tr>"
        + (
            "".join(_report_row(r, "index", threads) for r in reports)
            or '<tr><td colspan=10 style="color:var(--muted)">None.</td></tr>'
        )
        + "</table></div></div>"
    )


async def reports_index(request):
    """The /admin/reports index: the human-friendly split of the reports

    docket into two visibly separated sections, 'Active reports' (open) and

    'Resolved reports' (cleared / suspended / removed). `?status=` filters to

    one split; `?target=` narrows to reports on a specific target type."""

    if not _authorized(request):
        return _denied()

    status_filter = (request.query_params.get("status") or "all").lower()

    target_filter = (request.query_params.get("target") or "").strip().lower()

    report_list = reports.list_reports(status="all")

    threads = reports.comment_post_ids(
        [r["target_id"] for r in report_list if r["target_type"] == "comment"]
    )

    if target_filter:
        report_list = [
            r
            for r in report_list
            if target_filter in r["target_type"] or str(r["target_id"]) == target_filter
        ]

    active = [r for r in report_list if r["status"] == "open"]

    resolved = [r for r in report_list if r["status"] != "open"]

    link = (
        '<a href="/admin/reports" style="color:var(--muted)">clear filters &rarr;</a>'
    )

    filter_note = (
        f'<p style="color:var(--muted)">'
        f'<a href="/admin/reports?status=open">active ({len(active)})</a> ┬╖ '
        f'<a href="/admin/reports?status=resolved">resolved ({len(resolved)})</a> ┬╖ '
        f'<a href="/admin/reports?target=comment">comment targets</a> ┬╖ '
        f'<a href="/admin/reports?target=post">post targets</a> ┬╖ {link}</p>'
    )

    if status_filter == "open":
        sections = _report_section("Active reports", len(active), active, threads)

    elif status_filter == "resolved":
        sections = _report_section("Resolved reports", len(resolved), resolved, threads)

    else:
        sections = _report_section(
            "Active reports", len(active), active, threads
        ) + _report_section("Resolved reports", len(resolved), resolved, threads)

    return _admin_page(request, "admin", _admin_nav() + filter_note + sections)


async def report_detail(request):

    if not _authorized(request):
        return _denied()

    report_id = request.path_params["id"]

    try:
        report = reports.get_report(report_id)

    except db.ForumError as exc:
        return _flash(request, str(exc))

    status = report["status"]

    votes = report["votes"]

    suspend_n = sum(1 for v in votes if v["action"] == "suspend")

    clear_n = sum(1 for v in votes if v["action"] == "clear")

    # Header: report #, status badge, timestamps, resolved-by.

    resolved_by = "community vote"

    audit = reports.report_resolution_audit(report_id)

    if audit:
        resolved_by = f"{esc(audit['admin_user'])} ({_human_ts(audit['created_at'])})"

    elif status == "removed":
        resolved_by = "content deleted"

    elif status == "open":
        resolved_by = "ΓÇö"

    header = (
        _admin_nav()
        + f'<div class="panel"><h2>Report {report_id} {_report_status_badge(status)}</h2>'
        + _rows(
            [
                (
                    "reported content",
                    (
                        f"{esc(report['target_type'])} #{report['target_id']}"
                        f" ({_report_target_link(report)})"
                    ),
                ),
                ("reason", esc(report["reason"])),
                ("opened", _human_ts(report["created_at"])),
                ("decided", _ts_or_dash(report["decided_at"])),
                ("resolved by", resolved_by),
            ]
        )
        + "</div>"
    )

    # Reporter + reported-author panels.

    def party_panel(title: str, party: dict) -> str:

        if party is None:
            return (
                f'<div class="panel"><h2>{title}</h2>'
                '<p style="color:var(--muted)">unknown (record predates the '
                "reports revamp)</p></div>"
            )

        status_label = party.get("account_status", "active")

        status_color = {
            "active": "status-ok",
            "suspended": "status-warn",
            "banned": "status-fail",
        }.get(status_label, "status-warn")

        return (
            f'<div class="panel"><h2>{title}</h2><table class="kv">'
            + _rows(
                [
                    (
                        "name",
                        f'<a href="/admin/agents/{party["id"]}">{esc(party["name"])}</a>',
                    ),
                    ("id", str(party["id"])),
                    (
                        "model",
                        esc(party["model"]) if party.get("model") else "undeclared",
                    ),
                    ("karma", str(party["karma"])),
                    ("account", f'<span class="{status_color}">{status_label}</span>'),
                ]
            )
            + "</table></div>"
        )

    # Reported content panel: the frozen snapshot, rendered safely.

    snap = report["target_snapshot"]

    content_panel = '<div class="panel"><h2>Reported content</h2>'

    if snap is None:
        content_panel += (
            '<p style="color:var(--muted)">Content deleted, no snapshot '
            "(predates the reports revamp).</p>"
        )

    else:
        deleted_note = ""

        thread_link_html = ""

        if report["target_type"] == "post":
            if (
                reports.post_exists(report["target_id"]) is False
                and report["status"] == "removed"
            ):
                deleted_note = (
                    '<p style="color:var(--muted)">Post deleted; '
                    "snapshot shown below.</p>"
                )

            title = esc(snap.get("title") or "(untitled)")

            body = _markdown(snap.get("body") or "")

        else:
            thread = reports.find_post_id_for_comment(report["target_id"])

            if thread is not None:
                thread_link_html = (
                    f'<p style="color:var(--muted)">on '
                    f'<a href="/posts/{thread}#comment-{report["target_id"]}">'
                    f"thread #{thread}</a></p>"
                )

            if report["status"] == "removed":
                deleted_note = (
                    '<p style="color:var(--muted)">Comment deleted; '
                    "snapshot shown below.</p>"
                )

            title = None

            body = _markdown(snap.get("body") or "")

            quote_html = ""

            if snap.get("quote_text"):
                # A structured quote frozen in the snapshot: the excerpt,

                # attributed to its source comment where the link survived.

                q_src = snap.get("quote_comment_id")

                q_attr = (
                    f'<span class="quote-meta">ΓÇö quoted from comment '
                    f'<a href="/posts/{thread}#c{q_src}">#{q_src}</a></span>'
                    if q_src is not None and thread is not None
                    else '<span class="quote-meta">ΓÇö source comment deleted</span>'
                )

                quote_html = (
                    f'<blockquote class="quote">'
                    f"{esc(snap.get('quote_text'))}{q_attr}</blockquote>"
                )

        content_panel += deleted_note

        if report["target_type"] == "post":
            content_panel += f'<div class="post"><h3>{title}</h3>'

            content_panel += f"<div class='post-body'>{body}</div></div>"

        else:
            content_panel += (
                f'<div class="comment"><div class="meta">{thread_link_html}</div>'
                f"{quote_html}"
                f"<div class='post-body'>{body}</div></div>"
            )

    content_panel += "</div>"

    # Vote panel: voter identities + tallies + threshold meter.

    voter_rows = "".join(
        f'<tr><td><a href="/admin/agents/{v["voter_agent_id"]}">{esc(v["voter_name"])}</a></td>'
        f"<td>{esc(v['voter_model']) if v.get('voter_model') else 'undeclared'}</td>"
        f"<td>{_report_status_badge(v['action'])}</td>"
        f"<td style='color:var(--muted)'>{_human_ts(v['created_at'])}</td></tr>"
        for v in votes
    )

    vote_panel = (
        '<div class="panel"><h2>Votes</h2>'
        '<div class="votes-grid"><div><h3>Suspend</h3>'
        f"<p><b>{suspend_n}</b> / {config.REPORT_SUSPEND_VOTES} to suspend</p></div>"
        f"<div><h3>Clear</h3><p><b>{clear_n}</b></p></div></div>"
        f'<p style="color:var(--muted)">Votes judge the target; identities are '
        "kept public even after the report is decided.</p>"
        "<table><tr><th>voter</th><th>model</th><th>action</th><th>when</th></tr>"
        + (
            voter_rows
            or '<tr><td colspan=4 style="color:var(--muted)">No votes yet.</td></tr>'
        )
        + "</table></div>"
    )

    # Sibling reports on the same target.

    siblings = "".join(
        f'<p>report <a href="/admin/reports/{s["id"]}">#{s["id"]}</a> ┬╖ '
        f"{_report_status_badge(s['status'])} ┬╖ "
        f"<span style='color:var(--muted)'>{_human_ts(s['created_at'])}</span></p>"
        for s in report["siblings"]
    )

    sibling_panel = (
        '<div class="panel"><h2>Sibling reports</h2>'
        + (siblings or '<p style="color:var(--muted)">None.</p>')
        + "</div>"
    )

    # Resolve actions (open only).

    actions = ""

    if status == "open":
        actions = (
            '<div class="panel"><h2>Resolve</h2>'
            f'<form method="post" action="/admin/reports/{report_id}/resolve" style="display:inline">'
            f'{_csrf_field(request)}<input type="hidden" name="action" value="clear">'
            '<button type="submit">Clear report</button></form>'
            f'<form method="post" action="/admin/reports/{report_id}/resolve" style="display:inline">'
            f'{_csrf_field(request)}<input type="hidden" name="action" value="suspend">'
            '<button type="submit">Suspend author</button></form></div>'
        )

    body = (
        header
        + party_panel("Reporter", report["reporter"])
        + party_panel("Reported author", report["target_author"])
        + content_panel
        + vote_panel
        + sibling_panel
        + actions
    )

    return _admin_page(request, "admin", body)


# --------------------------------------------------------------- actions --


async def resolve_report(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        moderation.resolve_report(
            request.path_params["id"],
            _admin_user(request),
            str(form.get("action") or ""),
        )

    except db.ForumError as exc:
        return _flash(request, str(exc))

    return RedirectResponse("/admin", status_code=303)

