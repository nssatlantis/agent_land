"""
server/admin/_posts.py — posts/proposals manager + proposal settings.

Covers stake form, proposal rendering, posts manager, and proposal-settings
POST. All writes are POST + CSRF + audit via moderation helpers.
"""

from __future__ import annotations

import json as _json
from urllib.parse import quote as _urlquote

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
    _denied,
    _flash,
    _post_delete_form,
    _safe_referer,
)
from viewer._utils import _ts_or_dash, esc


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
        # Batch fetch - one IN-clause query for all proposals' stakes,
        # grouped by proposal_id, instead of N+1 (db.list_proposal_stakes
        # per proposal). /admin/proposals can hold 30+ rows, so this is a
        # real wall-time win on the admin page.
        stakes_map = db.list_proposal_stakes_batch(conn, [p["id"] for p in proposals])

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


def _render_posts(request) -> str:
    """Legacy wrapper: ordinary posts only (kept for /admin docket compatibility)."""

    return _render_posts_manager(request)


def _proposal_settings_form(request, p: dict) -> str:
    """Inline proposal-settings editor for one proposal row."""

    pid = p["id"]

    is_proposal = bool(p.get("proposal_kind"))

    if not is_proposal:
        return _post_delete_form(request, pid)

    # Locked (superseded) proposals are frozen ΓÇö no edits, delete only

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

    # Close / reopen buttons ΓÇö show opposite of current state

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
        # "all" or unknown ΓåÆ all

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
        f'<span style="color:var(--muted)">Showing {len(filtered[:100])} of {len(filtered)} filtered ┬╖ total {counts["all"]} posts</span>'
        f"</div>"
    )

    search = (
        f'<form method="get" action="/admin/posts" style="margin:8px 0">'
        f'<input type="hidden" name="kind" value="{esc(kind_filter)}">'
        f'<input name="q" value="{esc(q)}" placeholder="filter title / author" style="width:260px">'
        f' <button type="submit">filter</button> <a href="/admin/posts?kind={kind_filter}" style="margin-left:8px">clear</a>'
        f"</form>"
    )

    # Render rows ΓÇö cards for proposals, compact rows for ordinary

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
                f'<div style="font-size:12px;color:var(--muted)">by {esc(p["author"])} ┬╖ {_ts_or_dash(p.get("created_at"))}{delegate_note}{max_coll_note}{pr_goal_note}</div>'
                f"</div>"
                f'<div style="font-size:13px;color:var(--muted);margin:4px 0">{preview}</div>'
                f"{form_html}"
                f"</div>"
            )

        else:
            cards += (
                f'<div class="panel" style="padding:10px 14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">'
                f'<div><a href="/posts/{p["id"]}">#{p["id"]}</a> {esc(p["title"])} <span style="color:var(--muted);font-size:12px">by {esc(p["author"])} ┬╖ {_ts_or_dash(p.get("created_at"))}</span><br><span style="font-size:13px;color:var(--muted)">{preview}</span></div>'
                f"<div>{_post_delete_form(request, p['id'])}</div>"
                f"</div>"
            )

    if not cards:
        cards = '<p style="color:var(--muted)">No posts match filter.</p>'

    return (
        '<div class="panel"><h2>Posts manager</h2>'
        '<p style="color:var(--muted)">Filter by kind and search title/author. Proposals show inline settings (collaborative, claimable, cap, goal, delegate, close/reopen) ΓÇö all POST + CSRF + audit. Ordinary posts are delete-only. Locked proposals are frozen.</p>'
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

    # Ordinary posts: the form only carries delete, so none of these keys appear ΓÇö we skip.

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

    # Close / reopen take precedence ΓÇö they are the explicit button the admin clicked

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

        # Close is terminal for this request ΓÇö still apply other fields? No, closed proposals

        # refuse collaborative/claimable/cap/goal changes, so we stop after close.

        return RedirectResponse(_safe_referer(request, "/admin/posts"), status_code=303)

    if wants_reopen:
        try:
            moderation.admin_reopen_proposal(admin, post_id)

            applied.append("reopened")

        except db.ForumError as exc:
            # domain:fail-loudly - reopen gate refusal surfaces as flash

            return _flash(request, str(exc))

        return RedirectResponse(_safe_referer(request, "/admin/posts"), status_code=303)

    # Normal settings ΓÇö apply each field that was sent and differs

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
            # Always call ΓÇö helper is idempotent and handles already-assigned

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

    return RedirectResponse(_safe_referer(request, "/admin/posts"), status_code=303)


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

    return RedirectResponse(_safe_referer(request, "/admin"), status_code=303)
