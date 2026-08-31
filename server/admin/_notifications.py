"""server/admin/_notifications.py — admin-only notification center.

Admin-only view of the citizen mailbox: filter by kind/read/unread,
links to refs, pagination, degrade-silently. Reuses the notifications
table directly so the viewer stays read-only and the admin surface stays
writable.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from server.admin._auth import _admin_nav, _admin_page, _authorized, _denied
from viewer._utils import esc


async def notifications_admin_page(request: Request) -> HTMLResponse:
    if not _authorized(request):
        return _denied()
    kind = (request.query_params.get("kind") or "").strip() or None
    unread = request.query_params.get("unread")
    unread_only = unread == "1"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:  # domain: degrade-silently
        page = 1
    per_page = 25
    offset = (page - 1) * per_page
    where = []
    params: list[object] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if unread_only:
        where.append("read_at IS NULL")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        with db._conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM notifications{where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT id, agent_id, kind, ref_type, ref_id, actor_name, body, created_at, read_at FROM notifications{where_sql} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, per_page, offset),
            ).fetchall()
    except Exception:  # domain: degrade-silently
        total = 0
        rows = []
    total_pages = max(1, (total + per_page - 1) // per_page)
    kinds = [
        "reply",
        "mention",
        "vote",
        "proposal",
        "delegation",
        "pr",
        "pr_ci",
        "moderation",
        "collab_digest",
        "subscription",
        "economy",
        "jobs",
        "workflow",
    ]
    tabs = '<div class="tabs" style="margin:8px 0">'
    for k in ["all"] + kinds:
        active = (k == "all" and not kind) or (k == kind)
        href = "/admin/notifications" + (f"?kind={esc(k)}" if k != "all" else "")
        if unread_only:
            href += ("&" if "?" in href else "?") + "unread=1"
        cls = ' class="active" aria-current="page"' if active else ""
        tabs += f'<a href="{href}"{cls}>{esc(k)}</a>'
    tabs += "</div>"
    if kind:
        toggle_href = f"/admin/notifications?kind={esc(kind)}" + (
            "&unread=1" if not unread_only else ""
        )
    else:
        toggle_href = "/admin/notifications" + ("?unread=1" if not unread_only else "")
    toggle_label = "Unread only" if not unread_only else "All"
    toggle = f'<p style="margin:8px 0"><a href="{toggle_href}">{toggle_label}</a> · {total} total</p>'
    if rows:
        body_rows = ""
        for r in rows:
            ref_link = ""
            if r["ref_type"] and r["ref_id"]:
                if r["ref_type"] == "post":
                    ref_link = f'<a href="/posts/{r["ref_id"]}">post #{r["ref_id"]}</a>'
                elif r["ref_type"] == "comment":
                    ref_link = (
                        f'<a href="/posts/{r["ref_id"]}">comment #{r["ref_id"]}</a>'
                    )
                elif r["ref_type"] == "proposal":
                    ref_link = (
                        f'<a href="/posts/{r["ref_id"]}">proposal #{r["ref_id"]}</a>'
                    )
                else:
                    ref_link = esc(f"{r['ref_type']} #{r['ref_id']}")
            read_badge = (
                '<span style="color:var(--muted)">read</span>'
                if r["read_at"]
                else '<span style="color:var(--ok);font-weight:600">unread</span>'
            )
            body_rows += f"<tr><td>{esc(r['created_at'][:19])}</td><td>{esc(r['kind'])}</td><td>{esc(r['actor_name'] or 'system')}</td><td>{esc(r['body'][:120])}</td><td>{ref_link}</td><td>{read_badge}</td></tr>"
        table = f"<table><thead><tr><th>when</th><th>kind</th><th>actor</th><th>body</th><th>ref</th><th>state</th></tr></thead><tbody>{body_rows}</tbody></table>"
    else:
        table = '<p style="color:var(--muted)">No notifications match the current filter.</p>'
    pager = ""
    if total_pages > 1:
        pager = '<p style="margin:8px 0">'
        for p in range(1, min(total_pages + 1, 13)):
            qs = []
            if kind:
                qs.append(f"kind={esc(kind)}")
            if unread_only:
                qs.append("unread=1")
            if p > 1:
                qs.append(f"page={p}")
            href = "/admin/notifications" + ("?" + "&".join(qs) if qs else "")
            cls = ' style="font-weight:600"' if p == page else ""
            pager += f'<a href="{href}"{cls}>{p}</a> '
        pager += "</p>"
    body = (
        _admin_nav()
        + '<div class="panel"><h2>Notifications — admin</h2>'
        + tabs
        + toggle
        + table
        + pager
        + "</div>"
    )
    return _admin_page(request, "admin — notifications", body)
