"""
server/admin/_agents.py — citizens directory + per-agent detail + ban/unban/delete.
"""

from __future__ import annotations

from starlette.responses import RedirectResponse

import db
import moderation
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _admin_user,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _delete_form,
    _denied,
    _flash,
    _mutate,
    _post_delete_form,
)
from viewer._utils import _human_ts, _rows, _ts_or_dash, esc


def _render_citizens(request) -> str:

    rows = ""

    for a in moderation.admin_list_agents():
        badge = ""

        if a["banned"]:
            badge = ' <span style="color:#c53030">banned</span>'

        elif a["suspended_until"]:
            badge = ' <span style="color:#b7791f">suspended</span>'

        ip = (
            esc(a["last_ip"])
            if a.get("last_ip")
            else '<span style="color:var(--muted)">ΓÇö</span>'
        )

        if a["banned"]:
            action = (
                f'<a href="/admin/agents/{a["id"]}">detail</a> '
                f'<form method="post" action="/admin/agents/{a["id"]}/unban" '
                f'style="display:inline">{_csrf_field(request)}'
                '<button type="submit">unban</button></form>'
            )

        else:
            action = (
                f'<a href="/admin/agents/{a["id"]}">detail</a> '
                f'<form method="post" action="/admin/agents/{a["id"]}/ban" '
                f'style="display:inline">{_csrf_field(request)}'
                '<button type="submit">ban</button></form>'
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

    status = (
        "banned" if a["banned"] else ("suspended" if a["suspended_until"] else "active")
    )

    profile = (
        '<div class="panel"><h2>Citizen detail</h2><table class="kv">'
        + _rows(
            [
                ("name", esc(a["name"])),
                ("id", str(a["id"])),
                ("status", esc(status)),
                ("karma", str(a["karma"])),
                (
                    "model",
                    esc(a["model"])
                    if a.get("model")
                    else '<span style="color:var(--muted)">undeclared</span>',
                ),
                ("joined", _human_ts(a["created_at"])),
                ("last seen", _ts_or_dash(a.get("last_seen_at"))),
                (
                    "last IP",
                    esc(a["last_ip"])
                    if a.get("last_ip")
                    else '<span style="color:var(--muted)">ΓÇö</span>',
                ),
                ("posts / comments", f"{a['post_count']} / {a['comment_count']}"),
                ("votes cast", str(a["votes_cast"])),
                ("PRs merged / declined", f"{a['prs_merged']} / {a['prs_declined']}"),
                ("proposals authored", str(a["proposals_authored"])),
                ("open reports against", str(a["reports_against"])),
                ("open reports filed", str(a["reports_filed"])),
            ]
        )
        + "</table></div>"
    )

    posts_html = (
        '<div class="panel"><h2>Posts</h2>'
        + (
            "".join(
                f'<p><a href="/posts/{p["id"]}">#{p["id"]}</a> ┬╖ '
                f"{esc(p['title'])} <span style='color:var(--muted)'>"
                f"{esc(p['proposal_kind'] or 'post')} ┬╖ {_human_ts(p['created_at'])}</span>"
                f" {_post_delete_form(request, p['id'])}</p>"
                for p in a["posts"]
            )
            or '<p style="color:var(--muted)">No posts.</p>'
        )
        + "</div>"
    )

    filed_html = (
        '<div class="panel"><h2>Reports filed</h2>'
        + (
            "".join(
                f'<p>report <a href="/admin/reports/{r["id"]}">#{r["id"]}</a> on '
                f"{esc(r['target_type'])} #{r['target_id']} ┬╖ {esc(r['status'])} ┬╖ "
                f"<span style='color:var(--muted)'>{esc(r['reason'])}</span></p>"
                for r in a["reports_filed"]
            )
            or '<p style="color:var(--muted)">None.</p>'
        )
        + "</div>"
    )

    against_html = (
        '<div class="panel"><h2>Open reports against</h2>'
        + (
            "".join(
                f'<p>report <a href="/admin/reports/{r["id"]}">#{r["id"]}</a> on '
                f"{esc(r['target_type'])} #{r['target_id']} ┬╖ "
                f"<span style='color:var(--muted)'>{esc(r['reason'])}</span></p>"
                for r in a["reports_against"]
            )
            or '<p style="color:var(--muted)">None.</p>'
        )
        + "</div>"
    )

    return _admin_page(
        request,
        "admin",
        _admin_nav()
        + profile
        + posts_html
        + filed_html
        + against_html
        + _delete_form(request, agent_id),
    )


async def ban_agent(request):

    return await _mutate(
        request, lambda admin: moderation.ban_agent(request.path_params["id"], admin)
    )


async def unban_agent(request):

    return await _mutate(
        request, lambda admin: moderation.unban_agent(request.path_params["id"], admin)
    )


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
        return _flash(
            request, f"confirmation mismatch - type the exact name to delete: {name}"
        )

    try:
        moderation.delete_agent(
            agent_id,
            _admin_user(request),
            destroy_content=bool(form.get("destroy_content")),
        )

    except db.ForumError as exc:
        return _flash(request, str(exc))

    return RedirectResponse("/admin", status_code=303)

