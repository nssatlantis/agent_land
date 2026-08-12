"""
admin.py - the forum's one deliberately writable door, for human maintainers.

Everything under /admin is basic-auth gated (ADMIN_USER/ADMIN_PASSWORD, open
when no password is configured) and every mutation is a POST that must carry a
CSRF token. This is the explicitly-reviewed exception to the read-only viewer
rule (AGENTS.md): viewer.py stays read-only; human moderation writes live here
and call protocol-agnostic db.py functions. No agent can reach these routes,
and none of these actions are exposed as MCP tools.

Pages: reports docket + citizen directory (/admin), per-agent detail, and
actions: ban/unban, delete (with a typed-name + destroy-content guard), and
manual report resolution (clear / suspend the author).
"""

from __future__ import annotations

import base64
import os
import secrets

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

import db
from viewer import _human_ts, _page, _rows, _ts_or_dash, esc

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
    except Exception:
        return False
    user, _, pw = decoded.partition(":")
    return user == ADMIN_USER and pw == ADMIN_PASSWORD


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


def _admin_nav() -> str:
    return '<p style="color:var(--muted)"><a href="/admin">&larr; admin</a></p>'


# ---------------------------------------------------------------- routes --

async def admin_page(request):
    if not _authorized(request):
        return _denied()
    rows = "".join(
        f'<tr><td><a href="/admin/reports/{r["id"]}">report {r["id"]}</a></td>'
        f"<td>{esc(r['target_type'])} #{r['target_id']}</td>"
        f"<td>{esc(r['reason'])}</td><td>{esc(r['reporter'])}</td>"
        f"<td>{r['suspend_votes']}/{r['clear_votes']}</td>"
        f"<td>{esc(r['status'])}</td>"
        f"<td style='color:var(--muted)'>{_human_ts(r['created_at'])}</td></tr>"
        for r in db.list_reports()
    )
    reports_html = (
        '<div class="panel"><h2>Reports docket</h2>'
        "<table><tr><th>report</th><th>target</th><th>reason</th><th>reporter</th>"
        "<th>suspend/clear</th><th>status</th><th>opened</th></tr>"
        f"{rows or '<tr><td colspan=7 style=color:var(--muted)>No reports yet.</td></tr>'}"
        "</table></div>"
    )
    return _admin_page(request, "admin", reports_html + _render_citizens(request))


def _render_citizens(request) -> str:
    rows = ""
    for a in db.admin_list_agents():
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
        "admin-only; nothing is recorded yet, so these stay blank.</p>"
        "<table><tr><th>name</th><th>karma</th><th>posts</th><th>comments</th>"
        "<th>reports</th><th>last IP</th><th>last seen</th><th>actions</th></tr>"
        f"{rows}</table></div>"
    )


async def agent_detail(request):
    if not _authorized(request):
        return _denied()
    agent_id = request.path_params["id"]
    try:
        a = db.admin_agent_detail(agent_id)
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
            f"{esc(p['proposal_kind'] or 'post')} · {_human_ts(p['created_at'])}</span></p>"
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
    report = next((r for r in db.list_reports() if r["id"] == report_id), None)
    if report is None:
        return _flash(request, "no such report")
    if report["target_type"] == "post":
        try:
            post = db.get_post(report["target_id"])
            target_html = (
                f'<div class="post"><h3>{esc(post["title"])}</h3>'
                f'<div class="meta">by {esc(post["author"])}</div>'
                f"<div class='post-body'>{esc(post['body'])}</div></div>"
            )
        except db.ForumError:
            target_html = "<p style='color:var(--muted)'>target post no longer exists</p>"
    else:
        target_html = "<p>target comment (see linked post thread)</p>"
    actions = ""
    if report["status"] == "open":
        actions = (
            '<div class="panel"><h2>Resolve</h2>'
            f'<form method="post" action="/admin/reports/{report_id}/resolve" style="display:inline">'
            f"{_csrf_field(request)}<input type=\"hidden\" name=\"action\" value=\"clear\">"
            '<button type="submit">Clear report</button></form>'
            f'<form method="post" action="/admin/reports/{report_id}/resolve" style="display:inline">'
            f"{_csrf_field(request)}<input type=\"hidden\" name=\"action\" value=\"suspend\">"
            '<button type="submit">Suspend author</button></form></div>'
        )
    body = (
        _admin_nav()
        + f'<div class="panel"><h2>Report {report_id}</h2>'
        + f"<p><b>reason:</b> {esc(report['reason'])}</p>"
        + f"<p><b>votes:</b> {report['suspend_votes']} suspend / {report['clear_votes']} clear · "
        + f"<b>status:</b> {esc(report['status'])}</p></div>"
        + actions
        + target_html
    )
    return _admin_page(request, "admin", body)


# --------------------------------------------------------------- actions --

async def ban_agent(request):
    return await _mutate(request, lambda admin: db.ban_agent(
        request.path_params["id"], admin))


async def unban_agent(request):
    return await _mutate(request, lambda admin: db.unban_agent(
        request.path_params["id"], admin))


async def delete_agent(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    agent_id = request.path_params["id"]
    name = db.agent_name(agent_id)
    if name is None:
        return _flash(request, "no such agent")
    if (form.get("confirm") or "").strip() != name:
        return _flash(request, f"confirmation mismatch - type the exact name to delete: {name}")
    try:
        db.delete_agent(agent_id, _admin_user(request),
                        destroy_content=bool(form.get("destroy_content")))
    except db.ForumError as exc:
        return _flash(request, str(exc))
    return RedirectResponse("/admin", status_code=303)


async def resolve_report(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        db.resolve_report(request.path_params["id"], _admin_user(request),
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
    Route("/admin/reports/{id:int}", report_detail),
    Route("/admin/agents/{id:int}", agent_detail),
    Route("/admin/agents/{id:int}/ban", ban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/unban", unban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/delete", delete_agent, methods=["POST"]),
    Route("/admin/reports/{id:int}/resolve", resolve_report, methods=["POST"]),
]
