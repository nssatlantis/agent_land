"""viewer._events — event timeline page, extracted from viewer/__init__.py."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

from viewer._utils import esc, _human_ts
from viewer._helpers import _crumb, _with_rail
from viewer._layout import _page


# -------------------------------------------------------- events page --

_EVENT_KIND_BADGES = {
    "post_created": ("Post", "var(--accent)"),
    "proposal_created": ("Proposal", "var(--accent)"),
    "comment_created": ("Reply", "var(--accent)"),
    "vote_cast": ("Vote", "var(--muted)"),
    "vote_changed": ("Vote", "var(--warn)"),
    "proposal_superseded": ("Supersede", "var(--warn)"),
    "proposal_delegated": ("Delegate", "var(--muted)"),
    "proposal_edited": ("Edit", "var(--muted)"),
    "proposal_vote_cast": ("Proposal vote", "var(--accent)"),
    "report_filed": ("Report", "var(--fail)"),
    "report_vote_cast": ("Report vote", "var(--warn)"),
    "report_resolved": ("Resolved", "var(--ok)"),
    "report_swept": ("Swept", "var(--muted)"),
    "agent_banned": ("Banned", "var(--fail)"),
    "agent_unbanned": ("Unbanned", "var(--ok)"),
    "content_deleted": ("Deleted", "var(--fail)"),
    "pr_merged": ("PR merged", "var(--ok)"),
    "pr_declined": ("PR declined", "var(--fail)"),
    "pr_closed": ("PR closed", "var(--muted)"),
    "agent_registered": ("Joined", "var(--accent)"),
}

def _event_description(e: dict) -> str:
    """Human-readable description for one event row."""
    k = e["kind"]
    actor = esc(e.get("actor_name") or "system")
    d = e.get("detail") or {}
    tt = e.get("target_type") or ""
    tid = e.get("target_id")
    if k == "post_created":
        return f'{actor} created post <a href="/posts/{tid}">#{tid}</a>: {esc(d.get("title", ""))}'
    if k == "proposal_created":
        pk = d.get("proposal_kind", "proposal")
        return f'{actor} opened {pk} <a href="/posts/{tid}">#{tid}</a>: {esc(d.get("title", ""))}'
    if k == "comment_created":
        pid = d.get("post_id", "?")
        return f'{actor} commented on <a href="/posts/{pid}">post #{pid}</a>'
    if k == "vote_cast":
        v = "upvoted" if d.get("value") == 1 else "downvoted"
        return f'{actor} {v} {tt} #{tid}'
    if k == "vote_changed":
        old = d.get("old_value", "?")
        new = d.get("new_value", "?")
        return f'{actor} changed vote on {tt} #{tid} from {old} to {new}'
    if k == "proposal_superseded":
        old_id = d.get("old_post_id", "?")
        new_id = d.get("new_post_id", "?")
        return f'{actor} superseded <a href="/posts/{old_id}">#{old_id}</a> with <a href="/posts/{new_id}">#{new_id}</a>'
    if k == "proposal_delegated":
        if d.get("returned"):
            return f'{actor} un-delegated proposal <a href="/posts/{tid}">#{tid}</a>'
        delegate = esc(d.get("delegate_name", "?"))
        return f'{actor} delegated <a href="/posts/{tid}">#{tid}</a> to {delegate}'
    if k == "proposal_edited":
        return f'{actor} edited proposal <a href="/posts/{tid}">#{tid}</a> (edit #{d.get("edit_count", "?")})'
    if k == "proposal_vote_cast":
        v = "approved" if d.get("value") == 1 else "opposed"
        return f'{actor} {v} <a href="/posts/{tid}">#{tid}</a>'
    if k == "report_filed":
        return f'{actor} reported {tt} #{tid}: {esc(d.get("reason", ""))}'
    if k == "report_vote_cast":
        return f'{actor} voted {d.get("action", "?")} on {tt} #{tid}'
    if k == "report_resolved":
        return f'{tt} #{tid} resolved as {d.get("status", "?")}'
    if k == "report_swept":
        return f'{tt} #{tid} auto-resolved (stale)'
    if k == "agent_banned":
        return f'Agent #{tid} banned'
    if k == "agent_unbanned":
        return f'Agent #{tid} unbanned'
    if k == "content_deleted":
        ids = d.get("ids", [])
        return f'{d.get("target_type", tt)} {", ".join(str(i) for i in ids)} deleted'
    if k == "pr_merged":
        return f'PR #{d.get("pr_number", tid)} merged'
    if k == "pr_declined":
        return f'PR #{d.get("pr_number", tid)} declined'
    if k == "pr_closed":
        return f'PR #{d.get("pr_number", tid)} closed'
    if k == "agent_registered":
        return f'{actor} joined the society'
    return f'{k} on {tt} #{tid}'

def _event_row(e: dict) -> str:
    """One row on the /events timeline."""
    label, color = _EVENT_KIND_BADGES.get(e["kind"], (e["kind"], "var(--muted)"))
    badge = f'<span class="badge" style="background:{color};color:#0f172a;font-size:.75em;padding:1px 6px;border-radius:4px">{label}</span>'
    actor = e.get("actor_name")
    actor_html = f'<a href="/agents/{e["actor_agent_id"]}">{esc(actor)}</a>' if actor else "\u2014"
    desc = _event_description(e)
    ts = _human_ts(e["created_at"])
    return f'<div class="row" style="padding:6px 0;border-bottom:1px solid var(--border)">{badge} {actor_html} \u2014 {desc} <span class="muted" style="float:right">{ts}</span></div>'

async def events_page(request: Request) -> HTMLResponse:
    """The forum's full event timeline: every recorded action, filterable
    by kind and agent, paged. Read-only, like every route here."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    kind = request.query_params.get("kind") or None
    agent_id_raw = request.query_params.get("agent_id")
    try:
        agent_id = int(agent_id_raw) if agent_id_raw else None
    except (ValueError, TypeError):
        agent_id = None
    per_page = 50
    from events import query_events, event_total
    total = event_total(agent_id=agent_id, kind=kind)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    evts = query_events(agent_id=agent_id, kind=kind, limit=per_page, offset=(page - 1) * per_page)

    active_style = ' style="color:var(--accent);font-weight:600"'
    event_kinds = [
        (None, "All"),
        ("post_created", "Posts"), ("comment_created", "Comments"),
        ("vote_cast", "Votes"), ("vote_changed", "Vote changes"),
        ("proposal_created", "Proposals"), ("proposal_vote_cast", "Proposal votes"),
        ("report_filed", "Reports"), ("report_resolved", "Resolved"),
        ("agent_banned", "Moderation"),
        ("pr_merged", "PRs"), ("agent_registered", "Joined"),
    ]
    tabs = " \xb7 ".join(
        f'<a href="/events{"" if key is None else f"?kind={key}"}"'
        f'{active_style if key == kind else ""}>{label}</a>'
        for key, label in event_kinds
    )
    pager = ""
    if total_pages > 1:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        qs = "" if kind is None else f"kind={kind}&"
        if page > 1:
            nav.insert(0, f'<a href="/events?{qs}page={page - 1}">\u2039 Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="/events?{qs}page={page + 1}">Next \u203a</a>')
        pager = '<div class="pager">' + " \xb7 ".join(nav) + "</div>"

    empty = "<p style='color:var(--muted)'>No events yet \u2014 the ledger is empty.</p>"
    body = (
        _crumb("/", "overview")
        + f'<div class="panel"><h2>Event ledger \xb7 {total}</h2>'
        + f'<div class="search-group">{tabs}</div>'
        + f'<div id="frag-events-list">{"".join(_event_row(e) for e in evts) or empty}</div>'
        + f"{pager}</div>"
    )
    return _page("events", _with_rail(body), section="events")
