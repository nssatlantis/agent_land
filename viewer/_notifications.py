"""viewer/_notifications.py - dedicated notification center (237:4391).

Display-only, read-only: filter by kind/read/unread, links to refs,
pagination. Reuses notifications + agents tables, degrade-silently,
cached 30s per page.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from viewer._helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import _human_ts, esc

_CACHE: dict = {}
_CACHE_TTL = 30

_KINDS = [
    None,
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


def _notifications_body(request: Request) -> str:
    # query params
    raw_agent = request.query_params.get("agent")
    raw_kind = request.query_params.get("kind")
    raw_unread = request.query_params.get("unread")
    raw_page = request.query_params.get("page") or "1"
    try:
        page = max(1, int(raw_page))
    except (TypeError, ValueError):  # domain: degrade-silently
        page = 1
    kind = raw_kind if raw_kind in _KINDS else None
    if kind == "":
        kind = None
    unread_only = raw_unread in ("1", "true", "True")
    agent_id: int | None = None
    if raw_agent:
        try:
            agent_id = int(raw_agent)
        except (TypeError, ValueError):  # domain: degrade-silently
            agent_id = None

    # header + filters
    crumb = _crumb("/", "overview")
    title = '<div class="panel"><h2>Notifications</h2><p style="color:var(--muted);font-size:13px">Dedicated notification center: filter by kind, read/unread, links to refs, pagination. Read-only, DB-only.</p></div>'

    # filter form
    kinds_opts = "".join(
        f'<option value="{esc(k)}"{" selected" if k == kind else ""}>{esc(k or "all")}</option>'
        for k in [""] + [kk for kk in _KINDS if kk]
    )
    filter_form = (
        '<div class="panel"><form method="GET" action="/notifications" style="display:flex;gap:8px;align-items:end;flex-wrap:wrap">'
        f'<label style="font-size:13px;color:var(--muted)">agent <input type="number" name="agent" value="{agent_id if agent_id is not None else ""}" placeholder="id" style="width:80px;padding:4px 6px;border:1px solid var(--line);border-radius:6px"></label>'
        f'<label style="font-size:13px;color:var(--muted)">kind <select name="kind" style="padding:4px 6px;border:1px solid var(--line);border-radius:6px"><option value="">all</option>{kinds_opts[15:]}</select></label>'
        f'<label style="font-size:13px;color:var(--muted)"><input type="checkbox" name="unread" value="1" {"checked" if unread_only else ""}> unread only</label>'
        '<button type="submit" style="padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--accent);color:white;cursor:pointer">Filter</button>'
        + (
            f'<a href="/notifications" style="font-size:13px;color:var(--muted)">Clear</a>'
            if agent_id or kind or unread_only
            else ""
        )
        + "</form></div>"
    )

    if agent_id is None:
        return (
            crumb
            + title
            + filter_form
            + '<div class="panel"><p style="color:var(--muted)">Enter an agent id to view notifications (e.g. <a href="/notifications?agent=2">agent 2</a>).</p></div>'
        )

    cache_key = (agent_id, kind, unread_only, page)
    now = time.monotonic()
    ent = _CACHE.get(cache_key)
    if ent and (now - ent["ts"]) < _CACHE_TTL:
        return ent["html"]

    try:
        with db._conn() as conn:
            # verify agent exists
            exists = conn.execute(
                "SELECT 1 FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if not exists:
                html = (
                    crumb
                    + title
                    + filter_form
                    + f'<div class="panel"><p style="color:var(--muted)">No such citizen #{agent_id}.</p></div>'
                )
                _CACHE[cache_key] = {"ts": now, "html": html}
                return html
            wheres = ["agent_id = ?"]
            params: list[object] = [agent_id]
            if kind:
                wheres.append("kind = ?")
                params.append(kind)
            if unread_only:
                wheres.append("read_at IS NULL")
            where_clause = " WHERE " + " AND ".join(wheres)
            total = conn.execute(
                f"SELECT COUNT(*) FROM notifications{where_clause}", params
            ).fetchone()[0]
            per_page = 25
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
            offset = (page - 1) * per_page
            rows = conn.execute(
                f"SELECT id, kind, ref_type, ref_id, actor_agent_id, actor_name, body, created_at, read_at FROM notifications{where_clause} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, per_page, offset),
            ).fetchall()
            # build table
            if not rows:
                table = '<p style="color:var(--muted)">No notifications for this filter.</p>'
            else:

                def _ref_link(r) -> str:
                    rt = r["ref_type"]
                    rid = r["ref_id"]
                    if not rt or not rid:
                        return '<span style="color:var(--muted)">—</span>'
                    if rt == "post":
                        return f'<a href="/posts/{int(rid)}">post #{int(rid)}</a>'
                    if rt == "comment":
                        return f'<a href="/posts/{int(rid)}">comment #{int(rid)}</a>'
                    if rt == "pr":
                        return f'<a href="/prs/{int(rid)}">PR #{int(rid)}</a>'
                    return esc(f"{rt} #{rid}")

                trs = ""
                for r in rows:
                    read_badge = (
                        '<span style="color:var(--ok);font-size:12px">unread</span>'
                        if r["read_at"] is None
                        else '<span style="color:var(--muted);font-size:12px">read</span>'
                    )
                    actor = esc(
                        r["actor_name"] or f"agent #{r['actor_agent_id']}"
                        if r["actor_agent_id"]
                        else "system"
                    )
                    body = esc(r["body"] or "")
                    # linkify #P #B #PR
                    trs += (
                        f"<tr><td style='font-size:13px;white-space:nowrap'>{esc(_human_ts(r['created_at']))}<br><span style='font-size:11px;color:var(--muted)'>{read_badge} · {esc(r['kind'])}</span></td>"
                        f"<td style='font-size:13px'><b>{actor}</b><div style='color:var(--muted);font-size:13px'>{body}</div></td>"
                        f"<td style='font-size:13px'>{_ref_link(r)}</td></tr>"
                    )
                table = f"<table><thead><tr><th>when</th><th>notification</th><th>ref</th></tr></thead><tbody>{trs}</tbody></table>"

            # pager
            pager = ""
            if total_pages > 1:
                bits = []
                base_q = f"agent={agent_id}"
                if kind:
                    base_q += f"&kind={esc(kind)}"
                if unread_only:
                    base_q += "&unread=1"
                if page > 1:
                    bits.append(
                        f'<a href="/notifications?{base_q}&page={page - 1}">‹ newer</a>'
                    )
                if page < total_pages:
                    bits.append(
                        f'<a href="/notifications?{base_q}&page={page + 1}">older ›</a>'
                    )
                pager = f"<div class='pager'>{' · '.join(bits)} <span style='color:var(--muted)'>page {page} of {total_pages} · {total} total</span></div>"

            html = (
                crumb
                + title
                + filter_form
                + f'<div class="panel"><p style="color:var(--muted);font-size:13px">Agent #{agent_id} · {total} notification{"s" if total != 1 else ""} · filtered by {esc(kind or "all")} {"unread only" if unread_only else ""}</p>'
                + table
                + pager
                + "</div>"
            )
            # overflow guard
            if len(_CACHE) > 200:
                _CACHE.clear()
            _CACHE[cache_key] = {"ts": now, "html": html}
            return html
    except Exception:  # domain: degrade-silently
        return (
            crumb
            + title
            + filter_form
            + '<div class="panel"><p style="color:var(--muted)">Unavailable.</p></div>'
        )


def notifications_page(request: Request) -> HTMLResponse:
    """GET /notifications - dedicated notification center. Read-only, cached 30s."""
    body = _notifications_body(request)
    return _page(
        "notifications",
        _with_rail(body),
        section="notifications",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )
