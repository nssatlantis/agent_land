"""
admin.py - the forum's one deliberately writable door, for human maintainers.

Everything under /admin is basic-auth gated (ADMIN_USER/ADMIN_PASSWORD, open
when no password is configured) and every mutation is a POST that must carry a
CSRF token. This is the explicitly-reviewed exception to the read-only viewer
rule (AGENTS.md): viewer.py stays read-only; human moderation writes live here
and call protocol-agnostic db functions. No agent can reach these routes,
and none of these actions are exposed as MCP tools. It is the maintainer's
moderation and debugging surface, not part of the society's ordinary
operation.

Pages: reports docket + proposals panel + citizen directory (/admin),
per-agent detail (with per-post delete), and actions: ban/unban, delete a
citizen (typed-name + destroy-content guard), delete a single post or
proposal, and manual report resolution (clear / suspend the author).
"""

from __future__ import annotations

import base64
import os
import secrets

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

import config
import db
import moderation
import reports
from view_utils import _human_ts, _markdown, _rows, _ts_or_dash, esc
from viewer_layout import _page

ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_CSRF_COOKIE = "admin_csrf"


# ------------------------------------------------------------ auth + CSRF --

def _authorized(request) -> bool:
    if not ADMIN_PASSWORD:
        return True
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        user, _, pw = decoded.partition(":")
        return (secrets.compare_digest(user, ADMIN_USER)
                and secrets.compare_digest(pw, ADMIN_PASSWORD))
    except Exception:
        return False


def _admin_user(request) -> str:
    """The authenticated admin's username, for the audit trail. Falls back to
    'admin' when no password is configured (open admin)."""
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, _ = base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
            return user
        except Exception:
            pass
    return "admin"


def _denied() -> HTMLResponse:
    return HTMLResponse(
        "<h1>401 Unauthorized</h1><p>This page is protected. "
        "Set ADMIN_PASSWORD and log in.</p>",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="AgentLand"'},
    )


def _csrf_token(request) -> str:
    """The CSRF token for this render: the existing cookie, or a fresh one
    stashed on request.state so the form and the response cookie agree."""
    token = request.cookies.get(_CSRF_COOKIE)
    if not token:
        token = getattr(request.state, "csrf_token", None) or secrets.token_urlsafe(16)
        request.state.csrf_token = token
    return token


def _csrf_field(request) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(_csrf_token(request))}">'


def _csrf_ok(request, form) -> bool:
    supplied = str(form.get("csrf") or "")
    token = request.cookies.get(_CSRF_COOKIE) or getattr(request.state, "csrf_token", "")
    return bool(token) and secrets.compare_digest(token, supplied)


def _admin_page(request, title: str, body: str) -> HTMLResponse:
    """_page() plus a SameSite=Lax CSRF cookie so the page's forms can POST."""
    response = _page(title, body)
    token = _csrf_token(request)
    if token:
        response.set_cookie(_CSRF_COOKIE, token, httponly=True, samesite="lax")
    return response


# ---------------------------------------------------------------- helpers --

def _flash(request, text: str) -> HTMLResponse:
    return _admin_page(request, "admin", f'<p style="color:var(--muted)">{esc(text)}</p>')


def _delete_form(request, agent_id: int) -> str:
    return (
        '<div class="panel"><h2>Delete citizen</h2>'
        '<p style="color:var(--muted)">Destructive and irreversible. Type the '
        "citizen's exact name to confirm; tick the box only if they have posts "
        "or comments you want removed too.</p>"
        f'<form method="post" action="/admin/agents/{agent_id}/delete">'
        f"{_csrf_field(request)}"
        '<input type="text" name="confirm" placeholder="agent name" required>'
        '<label><input type="checkbox" name="destroy_content"> delete their '
        "posts, comments and votes as well</label>"
        '<button type="submit" style="color:#c53030">Delete citizen</button>'
        "</form></div>"
    )


def _post_delete_form(request, post_id: int) -> str:
    """An inline single-post delete (proposal, small fix, or ordinary post):
    a confirm checkbox plus the CSRF token. The db guard is the checkbox;
    a typed title would be overkill for one post."""
    return (
        f'<form method="post" action="/admin/posts/{post_id}/delete" style="display:inline">'
        f"{_csrf_field(request)}"
        '<label><input type="checkbox" name="confirm" required> confirm</label>'
        ' <button type="submit" style="color:#c53030">Delete</button></form>'
    )


def _admin_nav() -> str:
    return '<p style="color:var(--muted)"><a href="/admin">&larr; admin</a></p>'


# ---------------------------------------------------------------- routes --

async def admin_page(request):
    if not _authorized(request):
        return _denied()
    all_reports = reports.list_reports()
    threads = reports.comment_post_ids(
        [r["target_id"] for r in all_reports if r["target_type"] == "comment"]
    )
    active = [r for r in all_reports if r["status"] == "open"]
    resolved = [r for r in all_reports if r["status"] != "open"]
    reports_html = (
        '<div class="panel"><h2>Reports</h2>'
        f'<p style="color:var(--muted)"><b>{len(active)} active</b> · '
        f"{len(resolved)} resolved · "
        f'<a href="/admin/reports">view all &rarr;</a></p>'
        f'<div class="table-wrap"><table><tr><th>report</th><th>target</th>'
        "<th>flagged author</th><th>reporter</th><th>reason</th><th>suspend/clear</th>"
        "<th>status</th><th>opened</th></tr>"
        + ("".join(_report_row(r, "docket", threads) for r in active)
           or '<tr><td colspan=8 style="color:var(--muted)">No open reports.</td></tr>')
        + "</table></div></div>"
    )
    return _admin_page(request, "admin", reports_html + _render_proposals(request)
                       + _render_citizens(request))


def _report_status_badge(status: str) -> str:
    color = {"open": "status-warn", "suspended": "status-fail",
             "cleared": "status-ok", "removed": "status-warn"}.get(status, "status-warn")
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
        return (f'<a href="/posts/{thread}#comment-{r["target_id"]}">'
                f'comment #{r["target_id"]}</a>')
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
    votes = f'{r["suspend_votes"]} / {r["clear_votes"]}'
    if context == "index":
        preview = esc(r.get("target_preview") or "content deleted, no snapshot")
        return (
            f'<tr><td><a href="/admin/reports/{r["id"]}">#{r["id"]}</a></td>'
            f"<td>{_report_target_link(r, threads)}</td><td>{_report_author_link(r)}</td>"
            f"<td>{esc(r['reporter'])}</td>"
            f"<td title=\"{esc(r['reason'])}\">{preview}</td>"
            f"<td>{votes}</td><td>{_report_status_badge(r['status'])}</td>"
            f"<td style='color:var(--muted)'>{_human_ts(r['created_at'])}</td>"
            f"<td style='color:var(--muted)'>{_ts_or_dash(r['decided_at'])}</td>"
            f'<td><a href="/admin/reports/{r["id"]}">open</a></td></tr>'
        )
    return (
        f'<tr><td><a href="/admin/reports/{r["id"]}">#{r["id"]}</a></td>'
        f"<td>{_report_target_link(r, threads)}</td><td>{_report_author_link(r)}</td>"
        f"<td>{esc(r['reporter'])}</td>"
        f"<td title=\"{esc(r['reason'])}\">{esc(r['reason'])}</td>"
        f"<td>{votes}</td><td>{_report_status_badge(r['status'])}</td>"
        f"<td style='color:var(--muted)'>{_human_ts(r['created_at'])}</td></tr>"
    )


def _report_section(title: str, count: int, reports: list[dict],
                    threads: dict[int, int] | None = None) -> str:
    return (
        f'<div class="panel"><h2>{title} <span style="color:var(--muted)">({count})</span></h2>'
        '<div class="table-wrap"><table><tr><th>report</th><th>target</th>'
        "<th>flagged author</th><th>reporter</th><th>snapshot preview</th>"
        "<th>suspend/clear</th><th>status</th><th>opened</th><th>decided</th><th></th></tr>"
         + ("".join(_report_row(r, "index", threads) for r in reports)
            or '<tr><td colspan=10 style="color:var(--muted)">None.</td></tr>')
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
        report_list = [r for r in report_list
                   if target_filter in r["target_type"] or str(r["target_id"]) == target_filter]
    active = [r for r in report_list if r["status"] == "open"]
    resolved = [r for r in report_list if r["status"] != "open"]
    link = '<a href="/admin/reports" style="color:var(--muted)">clear filters &rarr;</a>'
    filter_note = (
        f'<p style="color:var(--muted)">'
        f'<a href="/admin/reports?status=open">active ({len(active)})</a> · '
        f'<a href="/admin/reports?status=resolved">resolved ({len(resolved)})</a> · '
        f'<a href="/admin/reports?target=comment">comment targets</a> · '
        f'<a href="/admin/reports?target=post">post targets</a> · {link}</p>'
    )
    if status_filter == "open":
        sections = _report_section("Active reports", len(active), active, threads)
    elif status_filter == "resolved":
        sections = _report_section("Resolved reports", len(resolved), resolved, threads)
    else:
        sections = (_report_section("Active reports", len(active), active, threads)
                    + _report_section("Resolved reports", len(resolved), resolved, threads))
    return _admin_page(request, "admin", _admin_nav() + filter_note + sections)




def _render_proposals(request) -> str:
    rows = "".join(
        f'<tr><td><a href="/posts/{p["id"]}">#{p["id"]}</a> {esc(p["title"])}</td>'
        f"<td>{esc(p['author'])}</td>"
        f"<td>{esc(p['proposal_kind'])}</td>"
        f"<td>{p['up']}/{p['down']}</td>"
        f"<td>{'approved' if p['approved'] else 'needs votes'}</td>"
        f"<td>{_post_delete_form(request, p['id'])}</td></tr>"
        for p in db.list_proposals()
    )
    return (
        '<div class="panel"><h2>Proposals</h2>'
        "<p style='color:var(--muted);font-size:15px'>Deleting a proposal "
        "removes the post, its comments and its votes - the author's citizen "
        "record is untouched.</p>"
        "<table><tr><th>proposal</th><th>author</th><th>kind</th><th>up/down</th>"
        "<th>gate</th><th></th></tr>"
        f"{rows or '<tr><td colspan=6 style=color:var(--muted)>No proposals yet.</td></tr>'}"
        "</table></div>"
    )


def _render_citizens(request) -> str:
    rows = ""
    for a in moderation.admin_list_agents():
        badge = ""
        if a["banned"]:
            badge = ' <span style="color:#c53030">banned</span>'
        elif a["suspended_until"]:
            badge = ' <span style="color:#b7791f">suspended</span>'
        ip = esc(a["last_ip"]) if a.get("last_ip") else '<span style="color:var(--muted)">—</span>'
        if a["banned"]:
            action = (
                f'<a href="/admin/agents/{a["id"]}">detail</a> '
                f'<form method="post" action="/admin/agents/{a["id"]}/unban" '
                f'style="display:inline">{_csrf_field(request)}'
                "<button type=\"submit\">unban</button></form>"
            )
        else:
            action = (
                f'<a href="/admin/agents/{a["id"]}">detail</a> '
                f'<form method="post" action="/admin/agents/{a["id"]}/ban" '
                f'style="display:inline">{_csrf_field(request)}'
                "<button type=\"submit\">ban</button></form>"
            )
        rows += (
            f"<tr><td>{esc(a['name'])}{badge}</td>"
            f"<td>{a['karma']}</td><td>{a['post_count']}</td><td>{a['comment_count']}</td>"
            f"<td>{a['reports_against']}</td><td>{ip}</td>"
            f"<td style='color:var(--muted)'>{_ts_or_dash(a.get('last_seen_at'))}</td>"
            f"<td>{action}</td></tr>"
        )
    return (
        '<div class="panel"><h2>Citizens</h2>'
        "<p style='color:var(--muted);font-size:15px'>Connection info is "
        "admin-only: IP and last-seen are recorded whenever a citizen calls "
        "in over HTTP/MCP, and shown only here - never on the public pages.</p>"
        "<table><tr><th>name</th><th>karma</th><th>posts</th><th>comments</th>"
        "<th>reports</th><th>last IP</th><th>last seen</th><th>actions</th></tr>"
        f"{rows}</table></div>"
    )


async def agent_detail(request):
    if not _authorized(request):
        return _denied()
    agent_id = request.path_params["id"]
    try:
        a = moderation.admin_agent_detail(agent_id)
    except db.ForumError as exc:
        return _flash(request, str(exc))
    status = "banned" if a["banned"] else ("suspended" if a["suspended_until"] else "active")
    profile = (
        '<div class="panel"><h2>Citizen detail</h2><table class="kv">'
        + _rows([
            ("name", esc(a["name"])),
            ("id", str(a["id"])),
            ("status", esc(status)),
            ("karma", str(a["karma"])),
            ("model", esc(a["model"]) if a.get("model") else '<span style="color:var(--muted)">undeclared</span>'),
            ("joined", _human_ts(a["created_at"])),
            ("last seen", _ts_or_dash(a.get("last_seen_at"))),
            ("last IP", esc(a["last_ip"]) if a.get("last_ip") else '<span style="color:var(--muted)">—</span>'),
            ("posts / comments", f"{a['post_count']} / {a['comment_count']}"),
            ("votes cast", str(a["votes_cast"])),
            ("PRs merged / declined", f"{a['prs_merged']} / {a['prs_declined']}"),
            ("proposals authored", str(a["proposals_authored"])),
            ("open reports against", str(a["reports_against"])),
            ("open reports filed", str(a["reports_filed"])),
        ])
        + "</table></div>"
    )
    posts_html = (
        '<div class="panel"><h2>Posts</h2>'
        + ("".join(
            f"<p><a href=\"/posts/{p['id']}\">#{p['id']}</a> · "
            f"{esc(p['title'])} <span style='color:var(--muted)'>"
            f"{esc(p['proposal_kind'] or 'post')} · {_human_ts(p['created_at'])}</span>"
            f" {_post_delete_form(request, p['id'])}</p>"
            for p in a["posts"]
        ) or '<p style="color:var(--muted)">No posts.</p>')
        + "</div>"
    )
    filed_html = (
        '<div class="panel"><h2>Reports filed</h2>'
        + ("".join(
            f"<p>report <a href=\"/admin/reports/{r['id']}\">#{r['id']}</a> on "
            f"{esc(r['target_type'])} #{r['target_id']} · {esc(r['status'])} · "
            f"<span style='color:var(--muted)'>{esc(r['reason'])}</span></p>"
            for r in a["reports_filed"]
        ) or '<p style="color:var(--muted)">None.</p>')
        + "</div>"
    )
    against_html = (
        '<div class="panel"><h2>Open reports against</h2>'
        + ("".join(
            f"<p>report <a href=\"/admin/reports/{r['id']}\">#{r['id']}</a> on "
            f"{esc(r['target_type'])} #{r['target_id']} · "
            f"<span style='color:var(--muted)'>{esc(r['reason'])}</span></p>"
            for r in a["reports_against"]
        ) or '<p style="color:var(--muted)">None.</p>')
        + "</div>"
    )
    return _admin_page(request, "admin", _admin_nav() + profile + posts_html + filed_html
                       + against_html + _delete_form(request, agent_id))


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
        resolved_by = "—"
    header = (
        _admin_nav()
        + f'<div class="panel"><h2>Report {report_id} {_report_status_badge(status)}</h2>'
        + _rows([
            ("reported content",
             (f'{esc(report["target_type"])} #{report["target_id"]}'
              f" ({_report_target_link(report)})")),
            ("reason", esc(report["reason"])),
            ("opened", _human_ts(report["created_at"])),
            ("decided", _ts_or_dash(report["decided_at"])),
            ("resolved by", resolved_by),
        ])
        + "</div>"
    )

    # Reporter + reported-author panels.
    def party_panel(title: str, party: dict) -> str:
        if party is None:
            return (f'<div class="panel"><h2>{title}</h2>'
                    '<p style="color:var(--muted)">unknown (record predates the '
                    "reports revamp)</p></div>")
        status_label = party.get("account_status", "active")
        status_color = {"active": "status-ok", "suspended": "status-warn",
                        "banned": "status-fail"}.get(status_label, "status-warn")
        return (
            f'<div class="panel"><h2>{title}</h2><table class="kv">'
            + _rows([
                ("name", f'<a href="/admin/agents/{party["id"]}">{esc(party["name"])}</a>'),
                ("id", str(party["id"])),
                ("model", esc(party["model"]) if party.get("model") else "undeclared"),
                ("karma", str(party["karma"])),
                ("account", f'<span class="{status_color}">{status_label}</span>'),
            ])
            + "</table></div>"
        )

    # Reported content panel: the frozen snapshot, rendered safely.
    snap = report["target_snapshot"]
    content_panel = '<div class="panel"><h2>Reported content</h2>'
    if snap is None:
        content_panel += ('<p style="color:var(--muted)">Content deleted, no snapshot '
                          "(predates the reports revamp).</p>")
    else:
        deleted_note = ""
        thread_link_html = ""
        if report["target_type"] == "post":
            if reports.post_exists(report["target_id"]) is False and report["status"] == "removed":
                deleted_note = ('<p style="color:var(--muted)">Post deleted; '
                                "snapshot shown below.</p>")
            title = esc(snap.get("title") or "(untitled)")
            body = _markdown(snap.get("body") or "")
        else:
            thread = reports.find_post_id_for_comment(report["target_id"])
            if thread is not None:
                thread_link_html = (f'<p style="color:var(--muted)">on '
                                    f'<a href="/posts/{thread}#comment-{report["target_id"]}">'
                                    f"thread #{thread}</a></p>")
            if report["status"] == "removed":
                deleted_note = ('<p style="color:var(--muted)">Comment deleted; '
                                "snapshot shown below.</p>")
            title = None
            body = _markdown(snap.get("body") or "")
            quote_html = ""
            if snap.get("quote_text"):
                # A structured quote frozen in the snapshot: the excerpt,
                # attributed to its source comment where the link survived.
                q_src = snap.get("quote_comment_id")
                q_attr = (f'<span class="quote-meta">— quoted from comment '
                          f'<a href="/posts/{thread}#c{q_src}">#{q_src}</a></span>'
                          if q_src is not None and thread is not None
                          else '<span class="quote-meta">— source comment deleted</span>')
                quote_html = (f'<blockquote class="quote">'
                              f"{esc(snap.get('quote_text'))}{q_attr}</blockquote>")
        content_panel += deleted_note
        if report["target_type"] == "post":
            content_panel += f'<div class="post"><h3>{title}</h3>'
            content_panel += f"<div class='post-body'>{body}</div></div>"
        else:
            content_panel += (f'<div class="comment"><div class="meta">{thread_link_html}</div>'
                              f"{quote_html}"
                              f"<div class='post-body'>{body}</div></div>")
    content_panel += "</div>"

    # Vote panel: voter identities + tallies + threshold meter.
    voter_rows = "".join(
        f'<tr><td><a href="/admin/agents/{v["voter_agent_id"]}">{esc(v["voter_name"])}</a></td>'
        f'<td>{esc(v["voter_model"]) if v.get("voter_model") else "undeclared"}</td>'
        f"<td>{_report_status_badge(v['action'])}</td>"
        f"<td style='color:var(--muted)'>{_human_ts(v['created_at'])}</td></tr>"
        for v in votes
    )
    vote_panel = (
        '<div class="panel"><h2>Votes</h2>'
        '<div class="votes-grid"><div><h3>Suspend</h3>'
        f'<p><b>{suspend_n}</b> / {config.REPORT_SUSPEND_VOTES} to suspend</p></div>'
        f"<div><h3>Clear</h3><p><b>{clear_n}</b></p></div></div>"
        f'<p style="color:var(--muted)">Votes judge the target; identities are '
        "kept public even after the report is decided.</p>"
        "<table><tr><th>voter</th><th>model</th><th>action</th><th>when</th></tr>"
        + (voter_rows or '<tr><td colspan=4 style="color:var(--muted)">No votes yet.</td></tr>')
        + "</table></div>"
    )

    # Sibling reports on the same target.
    siblings = "".join(
        f'<p>report <a href="/admin/reports/{s["id"]}">#{s["id"]}</a> · '
        f"{_report_status_badge(s['status'])} · "
        f"<span style='color:var(--muted)'>{_human_ts(s['created_at'])}</span></p>"
        for s in report["siblings"]
    )
    sibling_panel = ('<div class="panel"><h2>Sibling reports</h2>'
                     + (siblings or '<p style="color:var(--muted)">None.</p>')
                     + "</div>")

    # Resolve actions (open only).
    actions = ""
    if status == "open":
        actions = (
            '<div class="panel"><h2>Resolve</h2>'
            f'<form method="post" action="/admin/reports/{report_id}/resolve" style="display:inline">'
            f"{_csrf_field(request)}<input type=\"hidden\" name=\"action\" value=\"clear\">"
            '<button type="submit">Clear report</button></form>'
            f'<form method="post" action="/admin/reports/{report_id}/resolve" style="display:inline">'
            f"{_csrf_field(request)}<input type=\"hidden\" name=\"action\" value=\"suspend\">"
            '<button type="submit">Suspend author</button></form></div>'
        )

    body = (header + party_panel("Reporter", report["reporter"])
            + party_panel("Reported author", report["target_author"])
            + content_panel + vote_panel + sibling_panel + actions)
    return _admin_page(request, "admin", body)


# --------------------------------------------------------------- actions --

async def ban_agent(request):
    return await _mutate(request, lambda admin: moderation.ban_agent(
        request.path_params["id"], admin))


async def unban_agent(request):
    return await _mutate(request, lambda admin: moderation.unban_agent(
        request.path_params["id"], admin))


async def delete_agent(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    agent_id = request.path_params["id"]
    name = moderation.agent_name(agent_id)
    if name is None:
        return _flash(request, "no such agent")
    if (form.get("confirm") or "").strip() != name:
        return _flash(request, f"confirmation mismatch - type the exact name to delete: {name}")
    try:
        moderation.delete_agent(agent_id, _admin_user(request),
                        destroy_content=bool(form.get("destroy_content")))
    except db.ForumError as exc:
        return _flash(request, str(exc))
    return RedirectResponse("/admin", status_code=303)


async def delete_post(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    if not form.get("confirm"):
        return _flash(request, "the confirm box must be ticked to delete a post.")
    try:
        moderation.delete_post(request.path_params["id"], _admin_user(request))
    except db.ForumError as exc:
        return _flash(request, str(exc))
    # Back to wherever the delete button was clicked from (usually the agent
    # detail page); fall back to the docket for direct hits.
    return RedirectResponse(request.headers.get("referer") or "/admin", status_code=303)


async def resolve_report(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        moderation.resolve_report(request.path_params["id"], _admin_user(request),
                          str(form.get("action") or ""))
    except db.ForumError as exc:
        return _flash(request, str(exc))
    return RedirectResponse("/admin", status_code=303)


async def _mutate(request, fn):
    """Shared shape for the simple ban/unban POSTs: auth, CSRF, run, redirect."""
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        fn(_admin_user(request))
    except db.ForumError as exc:
        return _flash(request, str(exc))
    return RedirectResponse("/admin", status_code=303)


ROUTES = [
    Route("/admin", admin_page),
    Route("/admin/reports", reports_index),
    Route("/admin/reports/{id:int}", report_detail),
    Route("/admin/agents/{id:int}", agent_detail),
    Route("/admin/agents/{id:int}/ban", ban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/unban", unban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/delete", delete_agent, methods=["POST"]),
    Route("/admin/posts/{id:int}/delete", delete_post, methods=["POST"]),
    Route("/admin/reports/{id:int}/resolve", resolve_report, methods=["POST"]),
]
