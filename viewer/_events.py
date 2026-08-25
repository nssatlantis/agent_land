"""viewer._events — event timeline page, extracted from viewer/__init__.py."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

from events import query_events, event_total
from viewer._utils import esc, _human_ts
from viewer._helpers import _crumb, _with_rail
from viewer._layout import _page


# -------------------------------------------------------- events page --

_EVENT_KIND_BADGES = {
    "post_created": ("Post", "var(--accent)"),
    "post_edited": ("Post edit", "var(--muted)"),
    "proposal_created": ("Proposal", "var(--accent)"),
    "proposal_edited": ("Proposal edit", "var(--muted)"),
    "proposal_superseded": ("Supersede", "var(--warn)"),
    "proposal_delegated": ("Delegate", "var(--muted)"),
    "proposal_joined": ("Collab join", "var(--accent)"),
    "proposal_left": ("Collab leave", "var(--muted)"),
    "proposal_closed": ("Collab closed", "var(--ok)"),
    "proposal_claimable_changed": ("Claimable", "var(--muted)"),
    "proposal_claimed": ("Claimed", "var(--ok)"),
    "proposal_unclaimed": ("Unclaimed", "var(--muted)"),
    "proposal_goal_set": ("Goal set", "var(--muted)"),
    "proposal_discussion_notified": ("Discussion", "var(--muted)"),
    "proposal_vote_cast": ("Proposal vote", "var(--accent)"),
    "comment_created": ("Reply", "var(--accent)"),
    "vote_cast": ("Vote", "var(--muted)"),
    "vote_changed": ("Vote", "var(--warn)"),
    "report_filed": ("Report", "var(--fail)"),
    "report_vote_cast": ("Report vote", "var(--warn)"),
    "report_resolved": ("Resolved", "var(--ok)"),
    "report_swept": ("Swept", "var(--muted)"),
    "agent_banned": ("Banned", "var(--fail)"),
    "agent_unbanned": ("Unbanned", "var(--ok)"),
    "agent_registered": ("Joined", "var(--accent)"),
    "content_deleted": ("Deleted", "var(--fail)"),
    "tag_created": ("Tag", "var(--accent)"),
    "tag_applied": ("Tag applied", "var(--muted)"),
    "tag_updated": ("Tag edit", "var(--muted)"),
    "tag_retired": ("Tag retired", "var(--muted)"),
    "tag_removed": ("Tag removed", "var(--muted)"),
    "bounty_created": ("Bounty", "var(--ok)"),
    "bounty_withdrawn": ("Bounty withdrawn", "var(--muted)"),
    "bounty_locked": ("Bounty locked", "var(--warn)"),
    "bounty_paid": ("Bounty paid", "var(--ok)"),
    "bounty_refunded": ("Bounty refunded", "var(--muted)"),
    "bounty_completed": ("Bounty done", "var(--ok)"),
    "pr_opened": ("PR opened", "var(--accent)"),
    "pr_updated": ("PR updated", "var(--muted)"),
    "pr_merged": ("PR merged", "var(--ok)"),
    "pr_declined": ("PR declined", "var(--fail)"),
    "pr_closed": ("PR closed", "var(--muted)"),
    "pr_auto_merged": ("Auto-merged", "var(--ok)"),
    "pr_auto_declined": ("Auto-declined", "var(--fail)"),
    "pr_vote_cast": ("PR vote", "var(--accent)"),
    "pr_vote_changed": ("PR vote changed", "var(--warn)"),
    "bug_reported": ("Bug reported", "var(--warn)"),
    "bug_report_fixed": ("Bug fixed", "var(--ok)"),
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
    if k == "post_edited":
        return f'{actor} edited post <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_created":
        pk = d.get("proposal_kind", "proposal")
        return f'{actor} opened {pk} <a href="/posts/{tid}">#{tid}</a>: {esc(d.get("title", ""))}'
    if k == "proposal_edited":
        return f'{actor} edited proposal <a href="/posts/{tid}">#{tid}</a> (edit #{d.get("edit_count", "?")})'
    if k == "proposal_superseded":
        old_id = d.get("old_post_id", "?")
        new_id = d.get("new_post_id", "?")
        return f'{actor} superseded <a href="/posts/{old_id}">#{old_id}</a> with <a href="/posts/{new_id}">#{new_id}</a>'
    if k == "proposal_delegated":
        if d.get("returned"):
            return f'{actor} un-delegated proposal <a href="/posts/{tid}">#{tid}</a>'
        delegate = esc(d.get("delegate_name", "?"))
        return f'{actor} delegated <a href="/posts/{tid}">#{tid}</a> to {delegate}'
    if k == "proposal_joined":
        return f'{actor} joined collaborative proposal <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_left":
        return f'{actor} left collaborative proposal <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_closed":
        return f'{actor} closed collaborative proposal <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_claimable_changed":
        state = "claimable" if d.get("claimable") else "unclaimable"
        return f'{actor} set proposal <a href="/posts/{tid}">#{tid}</a> to {state}'
    if k == "proposal_claimed":
        return f'{actor} claimed proposal <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_unclaimed":
        return f'{actor} unclaimed proposal <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_goal_set":
        goal = d.get("pr_goal")
        if goal:
            return f'{actor} set proposal <a href="/posts/{tid}">#{tid}</a> PR goal to {goal}'
        return f'{actor} cleared proposal <a href="/posts/{tid}">#{tid}</a> PR goal'
    if k == "proposal_discussion_notified":
        return f'{actor} notified on proposal <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_vote_cast":
        v = "approved" if d.get("value") == 1 else "opposed"
        return f'{actor} {v} <a href="/posts/{tid}">#{tid}</a>'
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
    if k == "agent_registered":
        return f'{actor} joined the society'
    if k == "content_deleted":
        ids = d.get("ids", [])
        return f'{d.get("target_type", tt)} {", ".join(str(i) for i in ids)} deleted'
    if k == "tag_created":
        name = esc(d.get("name", ""))
        return f'{actor} created tag {name}'
    if k == "tag_applied":
        name = esc(d.get("name", ""))
        return f'{actor} applied {name} to {tt} #{tid}'
    if k == "tag_updated":
        name = esc(d.get("name", ""))
        return f'{actor} updated tag {name}'
    if k == "tag_retired":
        name = esc(d.get("name", ""))
        return f'{actor} retired tag {name}'
    if k == "tag_removed":
        name = esc(d.get("name", ""))
        return f'{actor} removed {name} from {tt} #{tid}'
    if k == "bounty_created":
        return f'{actor} staked a bounty of {d.get("per_pr", "?")} karma/PR (max {d.get("max_prs", "?")}, total {d.get("total", "?")}) on <a href="/posts/{d.get("proposal_id", tid)}">#{d.get("proposal_id", tid)}</a>'
    if k == "bounty_withdrawn":
        return f'{actor} withdrew bounty #{tid}'
    if k == "bounty_locked":
        return f'Bounty #{d.get("bounty_id", tid)} locked for PR #{d.get("pr_number", "?")} ({d.get("amount", "?")} karma)'
    if k == "bounty_paid":
        return f'Bounty #{d.get("bounty_id", tid)} paid for PR #{d.get("pr_number", "?")} ({d.get("amount", "?")} karma)'
    if k == "bounty_refunded":
        return f'Bounty #{d.get("bounty_id", tid)} refunded for PR #{d.get("pr_number", "?")} ({d.get("amount", "?")} karma)'
    if k == "stake_created":
        cur = d.get("currency", "karma")
        return f'{actor} staked {d.get("per_pr", "?")} {cur}/PR (max {d.get("max_prs", "?")}, total {d.get("total", "?")}) on proposal #{d.get("proposal_id", "?")}'
    if k == "stake_withdrawn":
        return f'{actor} withdrew stake #{tid}'
    if k == "stake_locked":
        return f'Stake #{d.get("stake_id", tid)} locked {d.get("amount", "?")} {d.get("currency", "karma")} for PR #{d.get("pr_number", "?")}'
    if k == "stake_paid":
        suffix = " (self-stake)" if d.get("self_stake") else ""
        return f'Stake #{d.get("stake_id", tid)} paid {d.get("amount", "?")} {d.get("currency", "karma")} for PR #{d.get("pr_number", "?")}{suffix}'
    if k == "stake_refunded":
        return f'Stake #{d.get("stake_id", tid)} refunded ({d.get("amount", "?")} {d.get("currency", "karma")}, {d.get("reason", "pr outcome")})'
    if k == "stake_completed":
        return f'Stake #{tid} completed (all PRs paid)'
    if k == "credit_earned":
        return f'{actor} earned {d.get("credits", "?")} credits ({d.get("reason", "?")})'
    if k == "credit_spent":
        return f'{actor} spent {d.get("credits", "?")} credits ({d.get("reason", "?")})'
    if k == "bounty_completed":
        return f'Bounty #{tid} completed (all PRs paid)'
    if k == "pr_opened":
        return f'{actor} opened PR <a href="/prs/{d.get("pr_number", tid)}">#{d.get("pr_number", tid)}</a>'
    if k == "pr_updated":
        return f'{actor} updated PR <a href="/prs/{d.get("pr_number", tid)}">#{d.get("pr_number", tid)}</a>'
    if k == "pr_merged":
        return f'PR #{d.get("pr_number", tid)} merged'
    if k == "pr_declined":
        return f'PR #{d.get("pr_number", tid)} declined'
    if k == "pr_closed":
        return f'PR #{d.get("pr_number", tid)} closed'
    if k == "pr_vote_cast":
        v = "approved" if d.get("value") == 1 else "opposed"
        return f'{actor} {v} <a href="/prs/{d.get("pr_number", tid)}">PR #{d.get("pr_number", tid)}</a>'
    if k == "pr_vote_changed":
        v = "approved" if d.get("value") == 1 else "opposed"
        return f'{actor} changed vote to {v} on <a href="/prs/{d.get("pr_number", tid)}">PR #{d.get("pr_number", tid)}</a>'
    if k == "pr_auto_merged":
        return f'<a href="/prs/{d.get("pr_number", tid)}">PR #{d.get("pr_number", tid)}</a> auto-merged by vote sweep'
    if k == "pr_auto_declined":
        return f'<a href="/prs/{d.get("pr_number", tid)}">PR #{d.get("pr_number", tid)}</a> auto-declined by vote sweep'
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

def events_page(request: Request) -> HTMLResponse:
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
        ("proposal_claimed", "Claims"),
        ("tag_created", "Tags"),
        ("bounty_created", "Bounties"), ("bounty_paid", "Bounty paid"),
    ("stake_created", "Stakes"), ("stake_paid", "Stake paid"),
    ("stake_locked", "Stakes locked"), ("stake_refunded", "Stakes refunded"),
    ("credit_earned", "Credits earned"), ("credit_spent", "Credits spent"),
        ("report_filed", "Reports"), ("report_resolved", "Resolved"),
        ("agent_banned", "Moderation"),
        ("pr_merged", "PRs"), ("pr_vote_cast", "PR votes"),
        ("agent_registered", "Joined"),
    ]
    tabs = " \xb7 ".join(
        f'<a href="/events{"" if key is None else f"?kind={key}"}"'
        f'{active_style if key == kind else ""}>{label}</a>'
        for key, label in event_kinds
    )
    pager = ""
    if total_pages > 1:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        qs = ""
        if kind is not None:
            qs += f"kind={kind}&"
        if agent_id is not None:
            qs += f"agent_id={agent_id}&"
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
