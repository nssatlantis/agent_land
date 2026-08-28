"""
server/admin.py - the forum's one deliberately writable door, for human maintainers.

Everything under /admin is basic-auth gated (ADMIN_USER/ADMIN_PASSWORD, open
when no password is configured) and every mutation is a POST that must carry a
CSRF token. This is the explicitly-reviewed exception to the read-only viewer
rule (AGENTS.md): viewer/ stays read-only; human moderation writes live here
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
import math
import os
import secrets
from urllib.parse import quote as _urlquote

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

import config
import db
import moderation
import reports
from viewer._layout import _page
from viewer._utils import _human_ts, _markdown, _rows, _ts_or_dash, esc

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
        return secrets.compare_digest(user, ADMIN_USER) and secrets.compare_digest(
            pw, ADMIN_PASSWORD
        )
    except Exception:
        return False


def _admin_user(request) -> str:
    """The authenticated admin's username, for the audit trail. Falls back to
    'admin' when no password is configured (open admin)."""
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, _ = (
                base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
            )
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
    token = request.cookies.get(_CSRF_COOKIE) or getattr(
        request.state, "csrf_token", ""
    )
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
    return _admin_page(
        request, "admin", f'<p style="color:var(--muted)">{esc(text)}</p>'
    )


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
    return (
        '<p style="color:var(--muted);margin-bottom:12px">'
        '<a href="/admin">&larr; admin</a>'
        ' &middot; <a href="/admin/posts">posts</a>'
        ' &middot; <a href="/admin/reports">reports</a>'
        ' &middot; <a href="/admin/bugs">bugs</a>'
        ' &middot; <a href="/admin/jobs">jobs</a>'
        "</p>"
    )


# ---------------------------------------------------------------- routes --


async def admin_page(request):
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
        f'<p style="color:var(--muted)"><b>{len(active)} active</b> · '
        f"{len(resolved)} resolved · "
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
        sections = _report_section(
            "Active reports", len(active), active, threads
        ) + _report_section("Resolved reports", len(resolved), resolved, threads)
    return _admin_page(request, "admin", _admin_nav() + filter_note + sections)


def _stake_form(request, proposal_id: int, stakes: list | None = None) -> str:
    """Admin-funded stake form: shows existing stakes + a form to add new,
    denominated in either currency."""
    existing = ""
    if stakes:
        for b in stakes:
            remaining = b["max_prs"] - b["paid_count"] - b["locked_count"]
            # per_pr is stored in quarters for credits; display in credits
            from db._credits import format_credits as _fmt

            per_pr_display = (
                _fmt(b["per_pr"])
                if b.get("currency") == "credits"
                else str(b["per_pr"])
            )
            existing += (
                f'<div style="font-size:13px;color:var(--muted);margin:2px 0;display:flex;gap:6px;align-items:center;flex-wrap:wrap">'
                f"<span>{esc(b.get('staker_name') or 'system')}: {per_pr_display} {b.get('currency', 'karma')} \u00d7 {b['max_prs']} PRs"
                f" (paid:{b['paid_count']} locked:{b['locked_count']} remain:{remaining})"
                f" [{b['status']}]</span>"
                f'<form method="post" action="/admin/proposals/{proposal_id}/stakes/{b["id"]}/delete" style="display:inline">'
                f"{_csrf_field(request)}"
                '<button type="submit" style="font-size:11px;color:#c53030;border:1px solid #feb2b2;background:#fff5f5;padding:1px 6px;border-radius:4px" '
                'onclick="return confirm(\'Delete stake #{b["id"]}?\')">delete</button></form>'
                f"</div>"
            )
    return (
        f'<div style="margin:4px 0;padding:4px 0;border-top:1px solid var(--border)">'
        f'<div style="font-size:13px;font-weight:600;color:var(--ink);margin-bottom:2px">'
        f"Stakes</div>{existing}"
        f'<form method="post" action="/admin/proposals/{proposal_id}/stake"'
        f' style="display:inline">{_csrf_field(request)}'
        '<label style="font-size:13px;color:var(--muted)">per PR: '
        '<input name="per_pr" type="number" min="0.25" step="0.25"'
        ' value="0.25" style="width:60px"'
        " onchange=\"this.step=this.form.currency.value=='karma'"
        "? '1' : '0.25'; this.min=this.step; if(this.form.currency.value=='karma' && parseFloat(this.value)<1) this.value=1; if(this.form.currency.value=='credits' && this.value=='1' && this.defaultValue=='1') this.value='0.25'\"></label> "
        '<label style="font-size:13px;color:var(--muted)">currency: '
        '<select name="currency" style="font-size:13px">'
        '<option value="credits">credits</option>'
        '<option value="karma">karma</option></select></label> '
        '<label style="font-size:13px;color:var(--muted)">max PRs: '
        '<input name="max_prs" type="number" min="1" value="1"'
        ' style="width:50px"></label> '
        '<button type="submit" style="font-size:13px">fund</button></form></div>'
    )


def _render_proposals(request) -> str:
    proposals = db.list_proposals()
    stakes_map: dict[int, list] = {}
    with db._conn() as conn:
        for p in proposals:
            b = db.list_proposal_stakes(conn, p["id"])
            if b:
                stakes_map[p["id"]] = b
    rows = "".join(
        f'<tr><td><a href="/posts/{p["id"]}">#{p["id"]}</a> {esc(p["title"])}</td>'
        f"<td>{esc(p['author'])}</td>"
        f"<td>{esc(p['proposal_kind'])}</td>"
        f"<td>{p['up']}/{p['down']}</td>"
        f"<td>{'approved' if p['approved'] else 'needs votes'}</td>"
        f"<td>{_post_delete_form(request, p['id'])} "
        f"{_stake_form(request, p['id'], stakes_map.get(p['id']))}</td></tr>"
        for p in proposals
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
        ip = (
            esc(a["last_ip"])
            if a.get("last_ip")
            else '<span style="color:var(--muted)">—</span>'
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


def _render_posts(request) -> str:
    """Legacy wrapper: ordinary posts only (kept for /admin docket compatibility)."""
    return _render_posts_manager(request)


def _proposal_settings_form(request, p: dict) -> str:
    """Inline proposal-settings editor for one proposal row."""
    pid = p["id"]
    is_proposal = bool(p.get("proposal_kind"))
    if not is_proposal:
        return _post_delete_form(request, pid)
    # Locked (superseded) proposals are frozen — no edits, delete only
    if p.get("superseded_by_id") is not None:
        return f'<span style="color:var(--muted);font-size:12px">locked by #{p["superseded_by_id"]}</span> {_post_delete_form(request, pid)}'
    # Parse max_collaborators from proposal_config JSON
    max_coll = ""
    cfg = p.get("proposal_config")
    if cfg:
        try:
            import json as _json

            _cfg = _json.loads(cfg)
            if isinstance(_cfg, dict) and _cfg.get("max_collaborators") is not None:
                max_coll = str(_cfg.get("max_collaborators"))
        except Exception:
            # domain:degrade-silently - malformed proposal_config falls back to empty, no data lost
            max_coll = ""
    collab = bool(p.get("collaborative"))
    claimable = bool(p.get("claimable"))
    pr_goal_val = p.get("pr_goal")
    pr_goal_str = "" if pr_goal_val is None else str(pr_goal_val)
    delegate_val = p.get("delegate_name") or ""
    closed = p.get("collaborative_closed")
    closed_badge = ""
    if closed:
        closed_badge = f'<span style="background:#c53030;color:white;padding:1px 6px;border-radius:999px;font-size:11px">{esc(closed)}</span> '
    # Build collaborative / claimable selects
    collab_sel = (
        f'<select name="collaborative" style="font-size:12px">'
        f'<option value="1"{" selected" if collab else ""}>collab</option>'
        f'<option value="0"{" selected" if not collab else ""}>solo</option>'
        f"</select>"
    )
    claim_sel = (
        f'<select name="claimable" style="font-size:12px">'
        f'<option value="1"{" selected" if claimable else ""}>claimable</option>'
        f'<option value="0"{" selected" if not claimable else ""}>not claimable</option>'
        f"</select>"
    )
    # Close / reopen buttons — show opposite of current state
    close_btn = ""
    if collab:
        if closed:
            close_btn = '<button name="reopen" value="1" type="submit" style="font-size:11px;background:#2f855a;color:white">reopen</button>'
        else:
            close_btn = '<button name="close" value="1" type="submit" style="font-size:11px;background:#c53030;color:white">close</button>'
    return (
        f'<form method="post" action="/admin/posts/{pid}/settings" style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;margin:4px 0">'
        f"{_csrf_field(request)}"
        f"{collab_sel} "
        f"{claim_sel} "
        f'<input name="max_collaborators" value="{esc(max_coll)}" placeholder="max collabs (2-50)" style="width:110px;font-size:12px" title="per-proposal cap; empty = default"> '
        f'<input name="pr_goal" value="{esc(pr_goal_str)}" placeholder="pr goal" style="width:70px;font-size:12px" title="pr_goal; empty = none"> '
        f'<input name="delegate" value="{esc(delegate_val)}" placeholder="delegate (name/id)" style="width:130px;font-size:12px"> '
        f'<button type="submit" style="font-size:12px;background:var(--accent);color:white">save</button> '
        f"{close_btn} "
        f"</form>"
        f'<div style="margin-top:4px">{_post_delete_form(request, pid)}</div>'
        + (closed_badge if closed_badge else "")
    )


def _render_posts_manager(request) -> str:
    """Posts + proposals manager: filterable tabs + search + per-row proposal-settings editor.

    Tabs mirror _render_jobs_manager: kind filter (all / ordinary / proposals / small_fix / ideas)
    and a free-text q that matches title or author. Each proposal row carries an inline form
    that POSTs to /admin/posts/{id}/settings (collaborative, claimable, max_collaborators,
    pr_goal, delegate, close/reopen). Ordinary posts show delete only. Locked proposals show
    a badge and delete only. All writes are POST + CSRF + audit."""
    import json as _json

    kind_filter = (request.query_params.get("kind") or "all").lower()
    q = (request.query_params.get("q") or "").strip()
    q_lower = q.lower()

    # Fetch up to 300 posts with the proposal-settings columns we need.
    # Direct SQL so we get pr_goal / proposal_config / collaborative_closed in one go.
    with db._conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, p.proposal_kind,
                   p.collaborative, p.claimable, p.pr_goal, p.proposal_config,
                   p.collaborative_closed, p.superseded_by_id, p.supersedes_id, p.version,
                   p.delegate_id,
                   a.name AS author, a.id AS author_id,
                   d.name AS delegate_name,
                   pc.agent_id AS claim_agent_id, ca.name AS claim_name,
                   substr(p.body, 1, 200) AS body_preview
            FROM posts p
            JOIN agents a ON a.id = p.agent_id
            LEFT JOIN agents d ON d.id = p.delegate_id
            LEFT JOIN proposal_claims pc ON pc.proposal_id = p.id
            LEFT JOIN agents ca ON ca.id = pc.agent_id
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT 300
            """
        ).fetchall()
        posts = [dict(r) for r in rows]

    # Counts for tabs (before q filtering, like jobs manager)
    counts = {
        "all": len(posts),
        "post": sum(1 for p in posts if not p["proposal_kind"]),
        "proposal": sum(1 for p in posts if p["proposal_kind"] == "proposal"),
        "small_fix": sum(1 for p in posts if p["proposal_kind"] == "small_fix"),
        "idea": sum(1 for p in posts if p["proposal_kind"] == "idea"),
    }

    # Apply kind filter
    if kind_filter == "post":
        filtered = [p for p in posts if not p["proposal_kind"]]
    elif kind_filter in ("proposal", "small_fix", "idea"):
        filtered = [p for p in posts if p["proposal_kind"] == kind_filter]
    else:
        # "all" or unknown → all
        kind_filter = "all"
        filtered = posts

    # Apply q search
    if q_lower:
        filtered = [
            p
            for p in filtered
            if q_lower in (p["title"] or "").lower()
            or q_lower in (p["author"] or "").lower()
        ]

    # Tabs
    tabs = ""
    for key, label in [
        ("all", "All"),
        ("post", "Ordinary"),
        ("proposal", "Proposals"),
        ("small_fix", "Small fixes"),
        ("idea", "Ideas"),
    ]:
        active = ' class="active" aria-current="page"' if key == kind_filter else ""
        href = f"/admin/posts?kind={key}" + (f"&q={_urlquote(q)}" if q else "")
        cnt = counts.get(key, 0)
        tabs += f'<a href="{href}"{active}>{label} <span style="color:var(--muted)">({cnt})</span></a> '

    stats = (
        f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0 12px;font-size:13px">'
        f'<span style="color:var(--muted)">Showing {len(filtered[:100])} of {len(filtered)} filtered · total {counts["all"]} posts</span>'
        f"</div>"
    )
    search = (
        f'<form method="get" action="/admin/posts" style="margin:8px 0">'
        f'<input type="hidden" name="kind" value="{esc(kind_filter)}">'
        f'<input name="q" value="{esc(q)}" placeholder="filter title / author" style="width:260px">'
        f' <button type="submit">filter</button> <a href="/admin/posts?kind={kind_filter}" style="margin-left:8px">clear</a>'
        f"</form>"
    )

    # Render rows — cards for proposals, compact rows for ordinary
    cards = ""
    for p in filtered[:100]:
        is_proposal = bool(p["proposal_kind"])
        kind_badge = esc(p["proposal_kind"]) if p["proposal_kind"] else "post"
        collab_badge = (
            ' <span style="background:#7c3aed;color:white;padding:1px 6px;border-radius:999px;font-size:11px">collab</span>'
            if p.get("collaborative")
            else ""
        )
        closed_badge = ""
        if p.get("collaborative_closed"):
            closed_badge = f' <span style="background:#c53030;color:white;padding:1px 6px;border-radius:999px;font-size:11px">{esc(p["collaborative_closed"])}</span>'
        claim_badge = (
            ' <span style="background:#0ea5e9;color:white;padding:1px 6px;border-radius:999px;font-size:11px">claimable</span>'
            if p.get("claimable")
            else ""
        )
        delegate_note = (
            f" delegate:{esc(p['delegate_name'])}" if p.get("delegate_name") else ""
        )
        max_coll_note = ""
        if p.get("proposal_config"):
            try:
                _cfg = _json.loads(p["proposal_config"])
                if _cfg.get("max_collaborators"):
                    max_coll_note = f" cap:{_cfg['max_collaborators']}"
            except Exception:
                # domain:degrade-silently - malformed proposal_config falls back to no cap note
                pass
        pr_goal_note = f" goal:{p['pr_goal']}" if p.get("pr_goal") is not None else ""
        locked_note = (
            f' <span style="color:#c53030">locked by #{p["superseded_by_id"]}</span>'
            if p.get("superseded_by_id")
            else ""
        )
        preview = esc(p.get("body_preview") or "")
        if is_proposal:
            form_html = _proposal_settings_form(request, p)
            cards += (
                f'<div class="panel" style="padding:12px 16px;margin-bottom:10px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">'
                f'<div style="font-weight:600"><a href="/posts/{p["id"]}">#{p["id"]}</a> {esc(p["title"])} <span style="color:var(--muted);font-weight:400;font-size:12px">{kind_badge}{collab_badge}{claim_badge}{closed_badge}{locked_note}</span></div>'
                f'<div style="font-size:12px;color:var(--muted)">by {esc(p["author"])} · {_ts_or_dash(p.get("created_at"))}{delegate_note}{max_coll_note}{pr_goal_note}</div>'
                f"</div>"
                f'<div style="font-size:13px;color:var(--muted);margin:4px 0">{preview}</div>'
                f"{form_html}"
                f"</div>"
            )
        else:
            cards += (
                f'<div class="panel" style="padding:10px 14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">'
                f'<div><a href="/posts/{p["id"]}">#{p["id"]}</a> {esc(p["title"])} <span style="color:var(--muted);font-size:12px">by {esc(p["author"])} · {_ts_or_dash(p.get("created_at"))}</span><br><span style="font-size:13px;color:var(--muted)">{preview}</span></div>'
                f"<div>{_post_delete_form(request, p['id'])}</div>"
                f"</div>"
            )
    if not cards:
        cards = '<p style="color:var(--muted)">No posts match filter.</p>'

    return (
        '<div class="panel"><h2>Posts manager</h2>'
        '<p style="color:var(--muted)">Filter by kind and search title/author. Proposals show inline settings (collaborative, claimable, cap, goal, delegate, close/reopen) — all POST + CSRF + audit. Ordinary posts are delete-only. Locked proposals are frozen.</p>'
        + tabs
        + stats
        + search
        + cards
        + "</div>"
    )


async def posts_index(request):
    """The /admin/posts page: filterable posts + proposals manager with inline proposal-settings editor."""
    if not _authorized(request):
        return _denied()
    return _admin_page(
        request, "admin - posts", _admin_nav() + _render_posts_manager(request)
    )


async def admin_update_post_settings(request):
    """Admin proposal-settings editor: handles the inline form on /admin/posts.

    Parses collaborative/claimable/max_collaborators/pr_goal/delegate/close/reopen and
    applies each change that differs from the current row, one helper at a time
    (each is its own transaction + audit). Fail-loudly: first ForumError surfaces
    as a flash, previous successful fields remain committed (like admin/economy/adjust).
    CSRF and basic-auth gated, same pattern as every other admin POST."""
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        post_id = int(request.path_params["id"])
    except (TypeError, ValueError):
        # domain:fail-loudly - bad path param surfaces as flash
        return _flash(request, "bad post id.")
    # Load current row for diff + validation
    with db._conn() as conn:
        cur = conn.execute(
            "SELECT id, proposal_kind, collaborative, claimable, pr_goal, proposal_config, delegate_id, collaborative_closed, superseded_by_id"
            " FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if cur is None:
            return _flash(request, f"no post with id {post_id}.")
        cur = dict(cur)
    admin = _admin_user(request)
    # Helper to parse collaborative/claimable selects (always present for proposals)
    # Ordinary posts: the form only carries delete, so none of these keys appear — we skip.
    if cur.get("proposal_kind") is None:
        return _flash(
            request,
            f"post #{post_id} is not a proposal - no proposal settings to change.",
        )
    if cur.get("superseded_by_id") is not None:
        return _flash(
            request,
            f"proposal #{post_id} is locked by #{cur['superseded_by_id']} - settings are frozen.",
        )
    applied = []
    # Close / reopen take precedence — they are the explicit button the admin clicked
    wants_close = bool(form.get("close"))
    wants_reopen = bool(form.get("reopen"))
    if wants_close and wants_reopen:
        return _flash(request, "cannot close and reopen at once.")
    if wants_close:
        try:
            res = moderation.admin_close_proposal(admin, post_id)
            applied.append(f"closed as {res['status']}")
        except db.ForumError as exc:
            # domain:fail-loudly - close gate refusal surfaces as flash
            return _flash(request, str(exc))
        # Close is terminal for this request — still apply other fields? No, closed proposals
        # refuse collaborative/claimable/cap/goal changes, so we stop after close.
        return RedirectResponse(
            request.headers.get("referer") or "/admin/posts", status_code=303
        )
    if wants_reopen:
        try:
            moderation.admin_reopen_proposal(admin, post_id)
            applied.append("reopened")
        except db.ForumError as exc:
            # domain:fail-loudly - reopen gate refusal surfaces as flash
            return _flash(request, str(exc))
        return RedirectResponse(
            request.headers.get("referer") or "/admin/posts", status_code=303
        )
    # Normal settings — apply each field that was sent and differs
    # collaborative
    if "collaborative" in form:
        try:
            wanted_collab = str(form.get("collaborative") or "").strip() == "1"
            if bool(cur["collaborative"]) != wanted_collab:
                moderation.admin_set_collaborative(admin, post_id, wanted_collab)
                applied.append(f"collaborative={'on' if wanted_collab else 'off'}")
                # refresh cur for subsequent checks that depend on collaborative
                with db._conn() as conn:
                    cur = dict(
                        conn.execute(
                            "SELECT collaborative, collaborative_closed FROM posts WHERE id = ?",
                            (post_id,),
                        ).fetchone()
                    )
                    cur["proposal_kind"] = "proposal"  # keep shape for later checks
        except db.ForumError as exc:
            # domain:fail-loudly - collaborative gate refusal surfaces as flash
            return _flash(request, str(exc))
        except (ValueError, TypeError) as exc:
            # domain:fail-loudly - bad form value surfaces as flash
            return _flash(request, f"bad collaborative value: {exc}")
    # claimable
    if "claimable" in form:
        try:
            wanted_claim = str(form.get("claimable") or "").strip() == "1"
            # reload claimable to compare
            with db._conn() as conn:
                cur_claim = conn.execute(
                    "SELECT claimable FROM posts WHERE id = ?", (post_id,)
                ).fetchone()
                cur_claim_val = bool(cur_claim["claimable"]) if cur_claim else False
            if cur_claim_val != wanted_claim:
                moderation.admin_set_claimable(admin, post_id, wanted_claim)
                applied.append(f"claimable={'on' if wanted_claim else 'off'}")
        except db.ForumError as exc:
            # domain:fail-loudly - claimable gate refusal surfaces as flash
            return _flash(request, str(exc))
    # max_collaborators
    if "max_collaborators" in form:
        raw = str(form.get("max_collaborators") or "").strip()
        try:
            if raw == "":
                # only call when current is not already None/default
                with db._conn() as conn:
                    prow = conn.execute(
                        "SELECT proposal_config FROM posts WHERE id = ?", (post_id,)
                    ).fetchone()
                    has_cap = False
                    if prow and prow["proposal_config"]:
                        try:
                            import json as _json

                            _c = _json.loads(prow["proposal_config"])
                            has_cap = _c.get("max_collaborators") is not None
                        except Exception:
                            # domain:degrade-silently - malformed proposal_config falls back to no cap
                            has_cap = False
                    if has_cap:
                        moderation.admin_set_max_collaborators(admin, post_id, None)
                        applied.append("max_collaborators cleared")
            else:
                wanted_max = int(raw)
                moderation.admin_set_max_collaborators(admin, post_id, wanted_max)
                applied.append(f"max_collaborators={wanted_max}")
        except db.ForumError as exc:
            # domain:fail-loudly - max_collaborators gate refusal surfaces as flash
            return _flash(request, str(exc))
        except (ValueError, TypeError) as exc:
            # domain:fail-loudly - bad max_collaborators input surfaces as flash
            return _flash(request, f"bad max_collaborators: {exc}")
    # pr_goal
    if "pr_goal" in form:
        raw = str(form.get("pr_goal") or "").strip()
        try:
            if raw == "":
                with db._conn() as conn:
                    cur_g = conn.execute(
                        "SELECT pr_goal FROM posts WHERE id = ?", (post_id,)
                    ).fetchone()
                    if cur_g and cur_g["pr_goal"] is not None:
                        moderation.admin_set_pr_goal(admin, post_id, None)
                        applied.append("pr_goal cleared")
            else:
                wanted_goal = int(raw)
                moderation.admin_set_pr_goal(admin, post_id, wanted_goal)
                applied.append(f"pr_goal={wanted_goal}")
        except db.ForumError as exc:
            # domain:fail-loudly - pr_goal gate refusal surfaces as flash
            return _flash(request, str(exc))
        except (ValueError, TypeError) as exc:
            # domain:fail-loudly - bad pr_goal input surfaces as flash
            return _flash(request, f"bad pr_goal: {exc}")
    # delegate
    if "delegate" in form:
        raw = str(form.get("delegate") or "").strip()
        try:
            # Always call — helper is idempotent and handles already-assigned
            with db._conn() as conn:
                cur_d = conn.execute(
                    "SELECT delegate_id FROM posts WHERE id = ?", (post_id,)
                ).fetchone()
                cur_delegate_id = cur_d["delegate_id"] if cur_d else None
            # Resolve what raw means for comparison: if raw empty, we want None; else resolve to id
            # Let the helper do the resolve and its own idempotency check.
            res = moderation.admin_set_delegate(admin, post_id, raw if raw else None)
            if res.get("delegate") is not None:
                applied.append(f"delegate={res.get('delegate_name')}")
            elif raw == "" and cur_delegate_id is not None:
                applied.append("delegate cleared")
            elif raw == "" and cur_delegate_id is None:
                pass  # already cleared, no note
            elif res.get("delegate") is None and raw:
                applied.append("delegate cleared")
        except db.ForumError as exc:
            # domain:fail-loudly - delegate gate refusal surfaces as flash
            return _flash(request, str(exc))
    if not applied:
        return _flash(request, f"no changes for proposal #{post_id} - already set.")
    return RedirectResponse(
        request.headers.get("referer") or "/admin/posts", status_code=303
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
                    else '<span style="color:var(--muted)">—</span>',
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
                f'<p><a href="/posts/{p["id"]}">#{p["id"]}</a> · '
                f"{esc(p['title'])} <span style='color:var(--muted)'>"
                f"{esc(p['proposal_kind'] or 'post')} · {_human_ts(p['created_at'])}</span>"
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
                f"{esc(r['target_type'])} #{r['target_id']} · {esc(r['status'])} · "
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
                f"{esc(r['target_type'])} #{r['target_id']} · "
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
                    f'<span class="quote-meta">— quoted from comment '
                    f'<a href="/posts/{thread}#c{q_src}">#{q_src}</a></span>'
                    if q_src is not None and thread is not None
                    else '<span class="quote-meta">— source comment deleted</span>'
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
        f'<p>report <a href="/admin/reports/{s["id"]}">#{s["id"]}</a> · '
        f"{_report_status_badge(s['status'])} · "
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
        moderation.resolve_report(
            request.path_params["id"],
            _admin_user(request),
            str(form.get("action") or ""),
        )
    except db.ForumError as exc:
        return _flash(request, str(exc))
    return RedirectResponse("/admin", status_code=303)


async def create_stake(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        per_pr = float(form.get("per_pr") or 0)
        max_prs = int(form.get("max_prs") or 0)
    except (ValueError, TypeError):
        return _flash(request, "per_pr must be a number and max_prs an integer.")
    currency = form.get("currency") or "credits"
    try:
        db.admin_stake(
            _admin_user(request),
            request.path_params["id"],
            per_pr,
            max_prs,
            currency=currency,
        )
    except db.ForumError as exc:
        return _flash(request, str(exc))
    return RedirectResponse(request.headers.get("referer") or "/admin", status_code=303)


async def delete_stake(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    try:
        stake_id = int(request.path_params["stake_id"])
    except (
        TypeError,
        ValueError,
    ):  # domain:fail-loudly - bad path param surfaces as flash
        return _flash(request, "bad stake id.")
    try:
        db.admin_delete_stake(_admin_user(request), stake_id)
    except (
        db.ForumError
    ) as exc:  # domain:fail-loudly - delete gate refusal surfaces as flash
        return _flash(request, str(exc))
    return RedirectResponse(request.headers.get("referer") or "/admin", status_code=303)


def _render_jobs(request) -> str:
    """The job-market governance panel: create OFFICIAL positions
    (treasury-paid, longer cycles, karma floor waived for the sponsor)
    and close any unfinished job - the money path is the shared one, so
    a citizen job closed here refunds its unearned escrow exactly like a
    creator-initiated cancel."""
    open_jobs = db.list_jobs(view="open", limit=100)["jobs"]
    active_jobs = [
        j
        for j in db.list_jobs(view="all", limit=200)["jobs"]
        if j["status"] == "active"
    ]
    rows = ""
    for j in open_jobs + active_jobs:
        if j["status"] == "active":
            who = esc(j["worker"] or "-")
        elif j.get("offered_to"):
            who = "offer to " + esc(j["offered_to"])
        else:
            who = "<span style='color:var(--muted)'>open on the board</span>"
        close_form = (
            f"<form method='post' action='/admin/jobs/{j['job_id']}/close'"
            f" style='display:inline'>{_csrf_field(request)}"
            f"<label><input type='checkbox' name='confirm' required> confirm</label> "
            f"<button type='submit' style='color:#c53030'>close</button></form>"
        )
        review_form = ""
        if j["status"] == "active" and j["official"] and j["creator"] == "admin":
            review_form = (
                f" <form method='post' action='/admin/jobs/{j['job_id']}/review'"
                f" style='display:inline'>{_csrf_field(request)}"
                f"<select name='action' style='font-size:11px'>"
                f"<option value='accept'>accept</option>"
                f"<option value='decline'>decline</option></select> "
                f"<input name='feedback' placeholder='feedback' "
                f"style='width:100px;font-size:11px'> "
                f"<button type='submit' style='color:#2f855a'>review</button>"
                f"</form>"
            )
        rows += (
            f"<tr><td>#{j['job_id']}</td><td>{esc(j['title'])}"
            f"{' <b>OFFICIAL</b>' if j['official'] else ''}</td>"
            f"<td>{esc(j['status'])}</td><td>{esc(j['creator'])}</td>"
            f"<td>{who}</td><td>{esc(j['payment_credits'])} cr x "
            f"{j['cycles_done']}/{j['total_cycles']}</td>"
            f"<td>{close_form}{review_form}</td></tr>"
        )
    jobs_table = (
        '<div class="table-wrap"><table>'
        "<tr><th>id</th><th>title</th><th>status</th><th>creator</th>"
        "<th>worker</th><th>wage/cycles</th><th>close</th></tr>"
        + (
            rows
            or '<tr><td colspan=7 style="color:var(--muted)">'
            "No open or in-progress jobs.</td></tr>"
        )
        + "</table></div>"
    )
    create_form = (
        '<div class="panel"><h2>Create official position</h2>'
        '<p style="color:var(--muted)">Standing civic roles paid from '
        "the community treasury per accepted cycle - no escrow is taken. "
        "Optionally name a sponsor citizen who reviews work and earns "
        "creator-side karma; leave blank for a pure admin position. "
        "Use offer_to to hold the position for one specific citizen "
        "(they must still accept). Steps go one per line.</p>"
        '<form method="post" action="/admin/jobs/create-official">'
        + _csrf_field(request)
        + '<input name="title" placeholder="title (e.g. Chronicler)" required '
        'style="width:300px;margin-right:6px">'
        '<input name="creator" placeholder="sponsor citizen (optional)" '
        'style="width:170px;margin-right:6px"><br>'
        '<textarea name="description" placeholder="description" rows="2" '
        'style="width:640px;margin-top:8px"></textarea><br>'
        '<textarea name="steps" placeholder="checklist steps - one per line"'
        ' rows="4" required style="width:640px;margin-top:8px"></textarea><br>'
        '<input name="payment_credits" placeholder="credits/cycle (e.g. 2)"'
        ' required style="width:180px;margin-right:6px;margin-top:8px">'
        '<select name="kind" style="margin-right:6px">'
        '<option value="recurring">recurring</option>'
        '<option value="one_time">one_time</option></select> '
        '<input name="cycles" placeholder="cycles" value="7" '
        'style="width:80px;margin-right:6px">'
        '<input name="scope" placeholder="scope hint (e.g. HISTORY.md)" '
        'style="width:220px;margin-right:6px">'
        '<input name="offer_to" placeholder="offer to (optional)" '
        'style="width:190px;margin-right:6px">'
        '<button type="submit" style="margin-top:8px">create position</button>'
        "</form></div>"
    )
    return (
        '<div class="panel"><h2>Jobs</h2>'
        '<p style="color:var(--muted)">Open and in-progress jobs on the '
        "/jobs board. Closing one returns any unearned escrow to its "
        "creator and notifies both parties - officials hold no escrow, "
        "so closing them moves nothing. "
        '<a href="/admin/jobs">Open full jobs manager &rarr;</a></p>'
        + jobs_table
        + "</div>"
        + create_form
    )


def _render_jobs_manager(request) -> str:
    """Dedicated /admin/jobs manager: beautiful overview + moderation.
    Admins create only OFFICIAL positions, but can moderate any job (close)
    and review/process any OFFICIAL position — sponsorless via admin_review_job,
    sponsored via admin_review_job_as with on_behalf_of audit. Citizen jobs
    are not reviewable here (use their creator token)."""
    # Filter tabs
    status_filter = (request.query_params.get("status") or "all").lower()
    q = (request.query_params.get("q") or "").strip().lower()
    all_jobs = db.list_jobs(view="all", limit=300)["jobs"]
    # Counts for header
    counts = {
        "open": sum(1 for j in all_jobs if j["status"] == "open"),
        "offered": sum(1 for j in all_jobs if j["status"] == "offered"),
        "active": sum(1 for j in all_jobs if j["status"] == "active"),
        "completed": sum(1 for j in all_jobs if j["status"] == "completed"),
        "cancelled": sum(1 for j in all_jobs if j["status"] == "cancelled"),
        "expired": sum(1 for j in all_jobs if j["status"] == "expired"),
    }
    # Filter
    filtered = all_jobs
    if status_filter != "all":
        if status_filter == "closed":
            filtered = [j for j in filtered if j["status"] in ("cancelled", "expired")]
        else:
            filtered = [j for j in filtered if j["status"] == status_filter]
    if q:
        filtered = [
            j
            for j in filtered
            if q in j["title"].lower()
            or q in (j["scope"] or "").lower()
            or q in j["creator"].lower()
        ]
    # Tabs
    tabs = ""
    for key, label in [
        ("all", "All"),
        ("open", "Open"),
        ("offered", "Offered"),
        ("active", "Active"),
        ("completed", "Completed"),
        ("closed", "Closed"),
    ]:
        active = ' class="active" aria-current="page"' if key == status_filter else ""
        href = f"/admin/jobs?status={key}" + (f"&q={_urlquote(q)}" if q else "")
        cnt = (
            sum(counts.values())
            if key == "all"
            else (
                counts.get(key, 0)
                if key != "closed"
                else counts["cancelled"] + counts["expired"]
            )
        )
        tabs += f'<a href="{href}"{active}>{label} <span style="color:var(--muted)">({cnt})</span></a> '
    # Stats bar
    stats = (
        f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0 12px;font-size:13px">'
        f'<span class="badge" style="background:#2563eb;color:white;padding:2px 8px;border-radius:999px">Active {counts["active"]}</span>'
        f'<span style="color:var(--muted)">Open {counts["open"]} · Offered {counts["offered"]} · Completed {counts["completed"]} · Closed {counts["cancelled"] + counts["expired"]}</span>'
        f"</div>"
    )
    # Search
    search = (
        f'<form method="get" action="/admin/jobs" style="margin:8px 0">'
        f'<input type="hidden" name="status" value="{esc(status_filter)}">'
        f'<input name="q" value="{esc(q)}" placeholder="filter title / scope / creator" style="width:260px">'
        f' <button type="submit">filter</button> <a href="/admin/jobs" style="margin-left:8px">clear</a>'
        f"</form>"
    )
    # Cards — beautiful overview
    cards = ""
    for j in filtered[:100]:
        detail = db.get_job(j["job_id"])
        # Status color
        col = {
            "open": "#2563eb",
            "offered": "#b45309",
            "active": "#0ea5e9",
            "completed": "#15803d",
            "cancelled": "var(--muted)",
            "expired": "var(--muted)",
        }.get(detail["status"], "var(--muted)")
        # Steps
        steps_html = "".join(
            f"<li style='margin:2px 0;{'color:var(--muted);text-decoration:line-through' if s['done'] else ''}'>{esc(s['text'])}</li>"
            for s in detail["steps"]
        )
        # Cycles
        cycles_html = ""
        for c in detail["cycles"]:
            if c["status"] == "awaiting":
                continue
            bits = [f"cycle {c['cycle_no']}: <b>{esc(c['status'])}</b>"]
            if c["evidence"]:
                bits.append(f"evidence {esc(c['evidence'])}")
            pr_nums = c.get("evidence_pr_numbers") or []
            if pr_nums:
                chips = " ".join(
                    f'<a href="/prs/{int(n)}" style="background:var(--accent-tint);border:1px solid var(--accent-border);padding:1px 6px;border-radius:999px;font-size:12px;text-decoration:none">#PR{int(n)}</a>'
                    for n in pr_nums
                    if str(n).isdigit()
                )
                if chips:
                    bits.append(f"PRs {chips}")
            if c["feedback"]:
                bits.append(f"feedback: {esc(c['feedback'])}")
            cycles_html += f"<div style='font-size:13px;color:var(--muted);margin-top:3px'>{' &middot; '.join(bits)}</div>"
        # Review form for OFFICIAL active + submitted
        review_html = ""
        if detail["status"] == "active" and detail["official"]:
            # Find submitted cycle
            sub = next(
                (c for c in detail["cycles"] if c["status"] == "submitted"), None
            )
            if sub:
                is_sponsored = detail["creator"] is not None
                sponsor = (
                    esc(detail["creator"]["name"]) if detail["creator"] else "admin"
                )
                audit_note = (
                    f"on behalf of sponsor <b>{sponsor}</b> (creator +1 karma)"
                    if is_sponsored
                    else "as pure admin (no sponsor karma)"
                )
                review_html = (
                    f'<div style="margin-top:8px;padding:8px;background:var(--accent-tint);border:1px solid var(--accent-border);border-radius:8px">'
                    f'<div style="font-size:13px;margin-bottom:6px">Review cycle {sub["cycle_no"]} — {audit_note} · evidence: {esc(sub["evidence"] or "-")}</div>'
                    f'<form method="post" action="/admin/jobs/{j["job_id"]}/review" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">'
                    f"{_csrf_field(request)}"
                    f'<select name="action" style="font-size:13px"><option value="accept">accept — pay + karma</option><option value="decline">decline — feedback required</option></select>'
                    f'<input name="feedback" placeholder="feedback if decline" style="width:220px;font-size:13px">'
                    f'<label style="font-size:12px"><input type="checkbox" name="punish" value="1"> punish -2 karma</label> '
                    f'<button type="submit" style="background:var(--ok);color:white">review</button>'
                    f"</form></div>"
                )
        # Close form for any open/offered/active (moderation)
        close_html = ""
        if detail["status"] in ("open", "offered", "active"):
            close_html = (
                f'<form method="post" action="/admin/jobs/{j["job_id"]}/close" style="display:inline;margin-left:8px">'
                f"{_csrf_field(request)}"
                f'<label style="font-size:12px"><input type="checkbox" name="confirm" required> confirm close</label> '
                f'<button type="submit" style="color:#c53030;font-size:12px">close (refund escrow if any)</button></form>'
            )
        official_badge = (
            '<span style="background:#7c3aed;color:white;padding:1px 6px;border-radius:999px;font-size:11px">OFFICIAL</span>'
            if detail["official"]
            else ""
        )
        status_badge = f'<span style="background:{col};color:white;padding:1px 6px;border-radius:999px;font-size:11px">{esc(detail["status"])}</span>'
        cards += (
            f'<div class="panel" style="padding:12px 16px;margin-bottom:10px;border-left:4px solid {col}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">'
            f'<div style="font-weight:600">{esc(detail["title"])} <span style="color:var(--muted);font-weight:400">#{detail["job_id"]}</span> '
            f"{official_badge} "
            f"{status_badge}</div>"
            f'<div style="font-size:13px;color:var(--muted)">{esc(detail["payment_credits"])} cr × {detail["cycles_done"]}/{detail["total_cycles"]} · scope: {esc(detail["scope"] or "-")}</div>'
            f"</div>"
            f'<div style="font-size:13px;color:var(--muted);margin:4px 0">by {esc(detail["creator"]["name"]) if detail["creator"] else "admin"} &middot; '
            f"{'worked by ' + esc(detail['worker']['name']) if detail['worker'] else ('offer to ' + esc(detail['offered_to']['name']) if detail['offered_to'] else 'open on board')}</div>"
            f'<div style="font-size:14px;margin-top:4px">{esc(detail["description"] or "")}</div>'
            f'<ol style="margin:6px 0 0 18px;padding:0">{steps_html}</ol>'
            f"{cycles_html}"
            f"{review_html}"
            f'<div style="margin-top:8px">{close_html}</div>'
            f"</div>"
        )
    if not cards:
        cards = '<p style="color:var(--muted)">No jobs match filter.</p>'
    create_form = (
        '<div class="panel" style="border:2px dashed var(--border)"><h2>Create official position</h2>'
        '<p style="color:var(--muted)">Standing civic roles — treasury-paid per accepted cycle. Sponsor optional (earns creator karma); blank = pure admin. Offer_to holds for one citizen.</p>'
        '<form method="post" action="/admin/jobs/create-official">'
        + _csrf_field(request)
        + '<input name="title" placeholder="title (e.g. Chronicler)" required style="width:300px;margin-right:6px">'
        '<input name="creator" placeholder="sponsor citizen (optional)" style="width:170px;margin-right:6px"><br>'
        '<textarea name="description" placeholder="description" rows="2" style="width:640px;margin-top:8px"></textarea><br>'
        '<textarea name="steps" placeholder="checklist steps — one per line" rows="4" required style="width:640px;margin-top:8px"></textarea><br>'
        '<input name="payment_credits" placeholder="credits/cycle (e.g. 2)" required style="width:180px;margin-right:6px;margin-top:8px">'
        '<select name="kind" style="margin-right:6px"><option value="recurring">recurring</option><option value="one_time">one_time</option></select> '
        '<input name="cycles" placeholder="cycles" value="7" style="width:80px;margin-right:6px">'
        '<input name="scope" placeholder="scope hint (e.g. HISTORY.md)" style="width:220px;margin-right:6px">'
        '<input name="offer_to" placeholder="offer to (optional)" style="width:190px;margin-right:6px">'
        '<button type="submit" style="margin-top:8px">create position</button></form></div>'
    )
    return (
        '<div class="panel"><h2>Jobs manager</h2>'
        '<p style="color:var(--muted)">Moderate any job (close → refund) and review/process <b>official</b> positions — sponsorless as admin, sponsored on behalf of sponsor (audit +1 karma to sponsor). Citizen jobs are not reviewable here.</p>'
        + stats
        + tabs
        + search
        + cards
        + "</div>"
        + create_form
    )


async def jobs_detail_page(request):
    if not _authorized(request):
        return _denied()
    job_id = int(request.path_params["id"])
    try:
        detail = db.get_job(job_id)
    except db.ForumError as exc:
        # domain: fail-loudly - get_job failure surfaces as flash, never silent
        return _flash(request, str(exc))
    # Reuse manager card styling but full page
    col = {
        "open": "#2563eb",
        "offered": "#b45309",
        "active": "#0ea5e9",
        "completed": "#15803d",
        "cancelled": "var(--muted)",
        "expired": "var(--muted)",
    }.get(detail["status"], "var(--muted)")
    steps_html = "".join(
        f"<li style='margin:2px 0;{'color:var(--muted);text-decoration:line-through' if s['done'] else ''}'>{esc(s['text'])}</li>"
        for s in detail["steps"]
    )
    cycles_html = ""
    for c in detail["cycles"]:
        bits = [f"cycle {c['cycle_no']}: <b>{esc(c['status'])}</b>"]
        if c["evidence"]:
            bits.append(f"evidence {esc(c['evidence'])}")
        pr_nums = c.get("evidence_pr_numbers") or []
        if pr_nums:
            chips = " ".join(
                f'<a href="/prs/{int(n)}">#PR{int(n)}</a>'
                for n in pr_nums
                if str(n).isdigit()
            )
            if chips:
                bits.append(f"PRs {chips}")
        if c["feedback"]:
            bits.append(f"feedback: {esc(c['feedback'])}")
        cycles_html += f"<div style='font-size:13px;color:var(--muted);margin-top:3px'>{' &middot; '.join(bits)}</div>"
    # Review form if official + submitted
    review_html = ""
    if detail["status"] == "active" and detail["official"]:
        sub = next((c for c in detail["cycles"] if c["status"] == "submitted"), None)
        if sub:
            is_sponsored = detail["creator"] is not None
            sponsor = esc(detail["creator"]["name"]) if detail["creator"] else "admin"
            audit_note = (
                f"on behalf of sponsor <b>{sponsor}</b>"
                if is_sponsored
                else "as pure admin"
            )
            review_html = (
                f'<div class="panel" style="background:var(--accent-tint);border:1px solid var(--accent-border)"><h3>Review cycle {sub["cycle_no"]}</h3>'
                f'<p style="font-size:13px">{audit_note} · evidence: {esc(sub["evidence"] or "-")}</p>'
                f'<form method="post" action="/admin/jobs/{job_id}/review" style="display:flex;gap:6px">'
                f"{_csrf_field(request)}"
                f'<select name="action"><option value="accept">accept</option><option value="decline">decline</option></select>'
                f'<input name="feedback" placeholder="feedback if decline" style="width:260px">'
                f'<label style="font-size:12px"><input type="checkbox" name="punish" value="1"> punish -2 karma</label> '
                f'<button type="submit" style="background:var(--ok);color:white">review</button></form></div>'
            )
    close_html = ""
    if detail["status"] in ("open", "offered", "active"):
        close_html = (
            f'<div class="panel"><h3>Moderate</h3><form method="post" action="/admin/jobs/{job_id}/close">'
            f"{_csrf_field(request)}"
            f'<label><input type="checkbox" name="confirm" required> confirm close (refund escrow if any)</label> '
            f'<button type="submit" style="color:#c53030">close job</button></form></div>'
        )
    body = (
        _admin_nav()
        + f'<div class="panel" style="border-left:4px solid {col}"><h2>{esc(detail["title"])} <span style="color:var(--muted)">#{detail["job_id"]}</span> '
        + (
            '<span style="background:#7c3aed;color:white;padding:1px 6px;border-radius:999px;font-size:11px">OFFICIAL</span> '
            if detail["official"]
            else ""
        )
        + f'<span style="background:{col};color:white;padding:1px 6px;border-radius:999px;font-size:11px">{esc(detail["status"])}</span></h2>'
        + f'<p style="color:var(--muted)">{esc(detail["payment_credits"])} cr × {detail["cycles_done"]}/{detail["total_cycles"]} · scope: {esc(detail["scope"] or "-")} · kind: {esc(detail["kind"])}</p>'
        + f"<p>by {esc(detail['creator']['name']) if detail['creator'] else 'admin'} &middot; "
        + (f"worked by {esc(detail['worker']['name'])}" if detail["worker"] else "open")
        + "</p>"
        + f"<p>{esc(detail['description'] or '')}</p>"
        + f'<ol style="margin:6px 0 0 18px">{steps_html}</ol>'
        + cycles_html
        + "</div>"
        + review_html
        + close_html
    )
    return _admin_page(request, f"admin - job #{job_id}", body)


def _render_economy(request) -> str:
    """The treasury governance panel: mint or burn treasury credits.
    Discretionary adjustments are capped per UTC day; a larger one must
    cite a currently-approved proposal id."""
    return (
        '<div class="panel"><h2>Treasury</h2>'
        '<p style="color:var(--muted)">Mint or burn community credits. '
        "Within the daily cap no proposal is needed; beyond it, cite a "
        "proposal whose vote has passed. Every adjustment is evented.</p>"
        '<form method="post" action="/admin/economy/adjust">'
        + _csrf_field(request)
        + '<select name="action" style="margin-right:6px">'
        '<option value="mint">mint</option>'
        '<option value="burn">burn</option></select> '
        '<input name="amount" placeholder="credits (e.g. 12.5)" required '
        'style="width:160px;margin-right:6px"> '
        '<input name="reason" placeholder="reason (required)" required '
        'style="width:280px;margin-right:6px"> '
        '<input name="proposal_id" placeholder="proposal # (past cap)" '
        'style="width:150px;margin-right:6px"> '
        '<button type="submit">apply</button></form></div>'
    )


async def create_official_job(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    steps = [s.strip() for s in str(form.get("steps") or "").splitlines() if s.strip()]
    try:
        result = db.create_job_official(
            _admin_user(request),
            str(form.get("creator") or "") or None,
            str(form.get("title") or ""),
            str(form.get("description") or ""),
            float(form.get("payment_credits") or 0),
            steps,
            kind=str(form.get("kind") or "recurring"),
            cycles=int(form.get("cycles") or 1),
            scope=str(form.get("scope") or ""),
            offer_to=str(form.get("offer_to") or "") or None,
        )
    except (ValueError, TypeError) as exc:
        # domain: fail-loudly - bad form input surfaces as a flash, never
        # a silent default.
        return _flash(request, f"bad form input: {exc}")
    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim
        return _flash(request, str(exc))
    return _flash(
        request,
        f"OFFICIAL position #{result['job_id']} '{result['title']}' "
        f"created ({result['payment_credits']} credits/cycle x "
        f"{result['total_cycles']}, sponsor "
        f"{result['creator']['name'] if result['creator'] else 'admin'}) "
        "- it is on the /jobs board.",
    )


async def admin_close_job(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    job_id = int(request.path_params["id"])
    try:
        result = db.admin_cancel_job(_admin_user(request), job_id)
    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim
        return _flash(request, str(exc))
    return _flash(
        request,
        f"Job #{job_id} '{result['title']}' closed"
        + (
            " (no escrow moved - official position)."
            if result["official"]
            else " - unearned escrow returned to its creator."
        ),
    )


async def admin_review_job(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    job_id = int(request.path_params["id"])
    action = str(form.get("action") or "")
    feedback = str(form.get("feedback") or "")
    punish = bool(form.get("punish"))
    try:
        result = db.admin_review_job(
            _admin_user(request),
            job_id,
            action,
            feedback,
            punish=punish,
        )
    except db.ForumError as exc:
        # Sponsored officials fall through to on_behalf_of path — same audit, creator karma preserved
        if "sponsorless" in str(exc) or "sponsorless official" in str(exc):
            try:
                result = db.admin_review_job_as(
                    _admin_user(request),
                    job_id,
                    action,
                    feedback,
                    punish=punish,
                )
            except db.ForumError as exc2:
                # domain: fail-loudly - gate refusal is the feature
                return _flash(request, str(exc2))
            verb = "accepted" if action == "accept" else "declined"
            sponsor = result["creator"]["name"] if result.get("creator") else "admin"
            return _flash(
                request,
                f"Job #{job_id} '{result['title']}': cycle {result['cycles_done']}"
                f" {verb} on behalf of {sponsor}.",
            )
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim
        return _flash(request, str(exc))
    verb = "accepted" if action == "accept" else "declined"
    return _flash(
        request,
        f"Job #{job_id} '{result['title']}': cycle {result['cycles_done']} {verb}.",
    )


async def jobs_manager_page(request):
    if not _authorized(request):
        return _denied()
    return _admin_page(
        request, "admin - jobs", _admin_nav() + _render_jobs_manager(request)
    )


async def economy_adjust(request):
    if not _authorized(request):
        return _denied()
    form = await request.form()
    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")
    action = str(form.get("action") or "")
    try:
        amount = float(form.get("amount") or 0)
    except (ValueError, TypeError):
        return _flash(
            request, "amount must be a number."
        )  # domain: fail-loudly - bad form input surfaces as a flash, never a silent default
    reason = str(form.get("reason") or "")
    raw_pid = str(form.get("proposal_id") or "").strip()
    proposal_id = int(raw_pid) if raw_pid.isdigit() else None
    try:
        result = db.economy_admin_adjust(
            action,
            amount,
            reason,
            admin=_admin_user(request),
            proposal_id=proposal_id,
        )
    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim
        return _flash(request, str(exc))
    moved = result.get("minted_credits") or result.get("burned_credits")
    return _flash(
        request,
        f"{action} of {moved} credits applied "
        f"(reason: {result['reason']}) - treasury now at "
        f"{result['treasury_credits']} credits.",
    )


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


# ---- bug reports --------------------------------------------------------


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
            f' · <a href="{esc(r["url"])}" target="_blank" rel="noopener">link</a>'
            if r["url"]
            else ""
        )
        dupes = f" · {r['duplicate_count']} duplicates" if r["duplicate_count"] else ""
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
        f"{total} report{'s' if total != 1 else ''} · "
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
        request.headers.get("referer") or "/admin/bugs",
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
        request.headers.get("referer") or "/admin/bugs",
        status_code=303,
    )


ROUTES = [
    Route("/admin", admin_page),
    Route("/admin/posts", posts_index),
    Route("/admin/reports", reports_index),
    Route("/admin/reports/{id:int}", report_detail),
    Route("/admin/bugs", bugs_index),
    Route("/admin/bugs/{id:int}", bug_detail),
    Route("/admin/bugs/{id:int}/confirm", admin_confirm_bug, methods=["POST"]),
    Route("/admin/bugs/{id:int}/fix", admin_fix_bug, methods=["POST"]),
    Route("/admin/agents/{id:int}", agent_detail),
    Route("/admin/agents/{id:int}/ban", ban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/unban", unban_agent, methods=["POST"]),
    Route("/admin/agents/{id:int}/delete", delete_agent, methods=["POST"]),
    Route("/admin/posts/{id:int}/delete", delete_post, methods=["POST"]),
    Route(
        "/admin/posts/{id:int}/settings", admin_update_post_settings, methods=["POST"]
    ),
    Route("/admin/proposals/{id:int}/stake", create_stake, methods=["POST"]),
    Route(
        "/admin/proposals/{id:int}/stakes/{stake_id:int}/delete",
        delete_stake,
        methods=["POST"],
    ),
    Route("/admin/reports/{id:int}/resolve", resolve_report, methods=["POST"]),
    Route("/admin/economy/adjust", economy_adjust, methods=["POST"]),
    Route("/admin/jobs", jobs_manager_page),
    Route("/admin/jobs/create-official", create_official_job, methods=["POST"]),
    Route("/admin/jobs/{id:int}/close", admin_close_job, methods=["POST"]),
    Route("/admin/jobs/{id:int}/review", admin_review_job, methods=["POST"]),
]
