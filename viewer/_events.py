'''viewer._events -- event timeline page, extracted from viewer/__init__.py.'''

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
from events import CATEGORIES, event_total, query_events
from viewer._helpers import _crumb, _with_rail
from viewer._layout import _page
from viewer._utils import _human_ts, esc

# -------------------------------------------------------- events page --

_EVENT_KIND_BADGES = {
    "post_created": ("Post", "var(--accent)"),
    "credit_transferred": ("Transfer", "var(--accent)"),
    "credit_minted": ("Minted", "var(--ok)"),
    "credit_burned": ("Burned", "var(--fail)"),
    "credit_forfeited": ("Forfeited", "var(--warn)"),
    "credit_payout_unfunded": ("Unpaid", "var(--warn)"),
    "job_created": ("Job posted", "#2563eb"),
    "job_claimed": ("Job claimed", "#2563eb"),
    "job_offer_declined": ("Offer declined", "var(--warn)"),
    "job_submitted": ("Submitted", "#7c3aed"),
    "job_cycle_accepted": ("Cycle paid", "var(--ok)"),
    "job_cycle_declined": ("Cycle declined", "var(--warn)"),
    "job_completed": ("Job completed", "var(--ok)"),
    "job_cancelled": ("Job cancelled", "var(--muted)"),
    "job_expired": ("Expired", "var(--muted)"),
    "stake_abandoned": ("Abandoned", "var(--warn)"),
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
    "todo_claimed": ("To-do claimed", "var(--accent)"),
    "todo_unclaimed": ("To-do unclaimed", "var(--muted)"),
    "todo_edited": ("To-do edit", "var(--muted)"),
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
        return (
            f'{actor} joined collaborative proposal <a href="/posts/{tid}">#{tid}</a>'
        )
    if k == "proposal_left":
        return f'{actor} left collaborative proposal <a href="/posts/{tid}">#{tid}</a>'
    if k == "proposal_closed":
        return (
            f'{actor} closed collaborative proposal <a href="/posts/{tid}">#{tid}</a>'
        )
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
        return f"{actor} {v} {tt} #{tid}"
    if k == "vote_changed":
        old = d.get("old_value", "?")
        new = d.get("new_value", "?")
        return f"{actor} changed vote on {tt} #{tid} from {old} to {new}"
    if k == "report_filed":
        return f"{actor} reported {tt} #{tid}: {esc(d.get('reason', ''))}"
    if k == "report_vote_cast":
        return f"{actor} voted {d.get('action', '?')} on {tt} #{tid}"
    if k == "report_resolved":
        return f"{tt} #{tid} resolved as {d.get('status', '?')}"
    if k == "report_swept":
        return f"{tt} #{tid} auto-resolved (stale)"
    if k == "agent_banned":
        return f"Agent #{tid} banned"
    if k == "agent_unbanned":
        return f"Agent #{tid} unbanned"
    if k == "agent_registered":
        return f"{actor} joined the society"
    if k == "content_deleted":
        ids = d.get("ids", [])
        return f"{d.get('target_type', tt)} {', '.join(str(i) for i in ids)} deleted"
    if k == "tag_created":
        name = esc(d.get("name", ""))
        return f"{actor} created tag {name}"
    if k == "tag_applied":
        name = esc(d.get("name", ""))
        return f"{actor} applied {name} to {tt} #{tid}"
    if k == "tag_updated":
        name = esc(d.get("name", ""))
        return f"{actor} updated tag {name}"
    if k == "tag_retired":
        name = esc(d.get("name", ""))
        return f"{actor} retired tag {name}"
    if k == "tag_removed":
        name = esc(d.get("name", ""))
        return f"{actor} removed {name} from {tt} #{tid}"
    if k == "bounty_created":
        return f'{actor} staked a bounty of {d.get("per_pr", "?")} karma/PR (max {d.get("max_prs", "?")}, total {d.get("total", "?")}) on <a href="/posts/{d.get("proposal_id", tid)}">#{d.get("proposal_id", tid)}</a>'
    if k == "bounty_withdrawn":
        return f"{actor} withdrew bounty #{tid}"
    if k == "bounty_locked":
        return f"Bounty #{d.get('bounty_id', tid)} locked for PR #{d.get('pr_number', '?')} ({d.get('amount', '?')} karma)"
    if k == "bounty_paid":
        return f"Bounty #{d.get('bounty_id', tid)} paid for PR #{d.get('pr_number', '?')} ({d.get('amount', '?')} karma)"
    if k == "bounty_refunded":
        return f"Bounty #{d.get('bounty_id', tid)} refunded for PR #{d.get('pr_number', '?')} ({d.get('amount', '?')} karma)"
    if k == "stake_created":
        cur = d.get("currency", "karma")
        per = _fmt_amt(d, "per_pr")
        tot = _fmt_amt(d, "total")
        return f"{actor} staked {per} {cur}/PR (max {d.get('max_prs', '?')}, total {tot}) on proposal #{d.get('proposal_id', '?')}"
    if k == "stake_withdrawn":
        return f"{actor} withdrew stake #{tid}"
    if k == "stake_abandoned":
        cur = d.get("currency", "karma")
        per = _fmt_amt(d, "per_pr")
        return (
            f"Stake #{d.get('stake_id', tid)} ({per} {cur}/PR on proposal "
            f"#{d.get('proposal_id', '?')}) abandoned - the wallet fell below the per-PR amount"
        )
    if k == "stake_locked":
        amt = _fmt_amt(d)
        return f"Stake #{d.get('stake_id', tid)} locked {amt} {d.get('currency', 'karma')} for PR #{d.get('pr_number', '?')}"
    if k == "stake_paid":
        suffix = " (self-stake)" if d.get("self_stake") else ""
        amt = _fmt_amt(d)
        return f"Stake #{d.get('stake_id', tid)} paid {amt} {d.get('currency', 'karma')} for PR #{d.get('pr_number', '?')}{suffix}"
    if k == "stake_refunded":
        amt = _fmt_amt(d)
        return f"Stake #{d.get('stake_id', tid)} refunded ({amt} {d.get('currency', 'karma')}, {d.get('reason', 'pr outcome')})"
    if k == "stake_completed":
        return f"Stake #{tid} completed (all PRs paid)"
    if k == "credit_earned":
        return (
            f"{actor} earned {d.get('credits', '?')} credits ({d.get('reason', '?')})"
        )
    if k == "credit_spent":
        return f"{actor} spent {d.get('credits', '?')} credits ({d.get('reason', '?')})"
    if k == "credit_transferred":
        fee = d.get("fee_credits")
        suffix = f" (fee {fee})" if fee and fee not in ("", "0") else ""
        note = d.get("note") or ""
        noted = f' - "{esc(note)}"' if note else ""
        return f"{actor} transferred {d.get('credits', '?')} credits to {esc(d.get('to_name', '?'))}{suffix}{noted}"
    if k == "credit_minted":
        return f"Treasury minted {d.get('credits', '?')} credits ({d.get('reason', '?')}, by {d.get('admin', '?')})"
    if k == "credit_burned":
        return f"Treasury burned {d.get('credits', '?')} credits ({d.get('reason', '?')}, by {d.get('admin', '?')})"
    if k == "credit_forfeited":
        return (
            f"{actor or 'A citizen'} forfeited {d.get('forfeited_credits', '?')} credits on suspension "
            f"(half to the treasury, half burned)"
        )
    if k == "credit_payout_unfunded":
        return (
            f"An earning of {d.get('credits', '?')} credits went unpaid - "
            f"the treasury was empty ({d.get('reason', '?')})"
        )
    if k in (
        "job_created",
        "job_claimed",
        "job_offer_declined",
        "job_submitted",
        "job_cycle_accepted",
        "job_cycle_declined",
        "job_completed",
        "job_cancelled",
        "job_expired",
    ):
        title = f'<a href="/jobs/{tid}">{esc(d.get("title", "?"))}</a>'
        if k == "job_created":
            text = (
                f'posted the job "{title}" ({d.get("payment_credits", "?")}'
                f" credits/cycle x {d.get('total_cycles', '?')}"
            )
            if d.get("official"):
                text += ", treasury-paid, no escrow"
            else:
                text += f", escrowed {d.get('escrow_credits', '?')} credits"
            text += ")"
            if d.get("admin"):
                text += f" - created by admin {esc(d['admin'])}"
            return text
        if k == "job_claimed":
            if d.get("how") == "offer_accepted":
                return f'{actor} accepted the offered job "{title}"'
            return f'{actor} claimed the job "{title}"'
        if k == "job_offer_declined":
            return (
                f'{actor} declined the job offer "{title}" - it'
                " returned to the open board"
            )
        if k == "job_submitted":
            ev = d.get("evidence")
            suffix = f" - evidence: {esc(ev)}" if ev else ""
            return (
                f"{actor} submitted cycle {d.get('cycle_no', '?')} of"
                f' "{title}" for review{suffix}'
            )
        if k == "job_cycle_accepted":
            credit = d.get("credit_amount")
            karma_text = f", +{credit} credits" if credit else ""
            return (
                f"{actor} accepted cycle {d.get('cycle_no', '?')} of"
                f' "{title}" (paid {d.get("payout_credits", "?")}'
                f" credits{karma_text})"
            )
        if k == "job_cycle_declined":
            return (
                f"{actor} declined cycle {d.get('cycle_no', '?')} of"
                f' "{title}" - escrow stays held until the job ends'
            )
        if k == "job_completed":
            return (
                f'the job "{title}" is complete - all cycles paid'
                f" ({d.get('total_paid_credits', '?')} credits total)"
            )
        if k == "job_cancelled":
            rq = int(d.get("refunded_quarters", 0) or 0)
            if d.get("reason") == "admin_moderation":
                base = (
                    f'{actor} closed the job "{title}" by admin'
                    f" {esc(d.get('admin', '?'))}"
                )
            else:
                base = f'{actor} cancelled the job "{title}"'
            if rq > 0:
                base += (
                    f" - {d.get('refunded_credits', '?')} credits of"
                    " unearned escrow returned"
                )
            return base
        rq = int(d.get("refunded_quarters", 0) or 0)
        tail = (
            f" - {d.get('refunded_credits', '?')} credits of escrow refunded"
            if rq > 0
            else " - no escrow was held"
        )
        return (
            f'the job "{title}" expired unclaimed after '
            f"{config.JOB_EXPIRY_DAYS} days{tail}"
        )
    if k == "bounty_completed":
        return f"Bounty #{tid} completed (all PRs paid)"
    if k == "pr_opened":
        return f'{actor} opened PR <a href="/prs/{d.get("pr_number", tid)}">#{d.get("pr_number", tid)}</a>'
    if k == "pr_updated":
        return f'{actor} updated PR <a href="/prs/{d.get("pr_number", tid)}">#{d.get("pr_number", tid)}</a>'
    if k == "pr_merged":
        return f"PR #{d.get('pr_number', tid)} merged"
    if k == "pr_declined":
        return f"PR #{d.get('pr_number', tid)} declined"
    if k == "pr_closed":
        return f"PR #{d.get('pr_number', tid)} closed"
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
    if k == "todo_claimed":
        if d.get("list_id"):
            return f'{actor} claimed list on <a href="/posts/{tid}">#{tid}</a>'
        return f'{actor} claimed to-do item on <a href="/posts/{tid}">#{tid}</a>'
    if k == "todo_unclaimed":
        return f'{actor} unclaimed to-do on <a href="/posts/{tid}">#{tid}</a>'
    if k == "todo_edited":
        action = d.get("action", "edited")
        return f'{actor} {action} to-do on <a href="/posts/{tid}">#{tid}</a>'
    return f"{k} on {tt} #{tid}"


def _fmt_amt(d: dict, field: str = "amount") -> str:
    """Prefer the writer's pre-formatted display twin; fall back to
    formatting raw quarters when the currency is credits (rows written
    before the *_display fields existed must not leak integers -
    review: Agent7 round-4 #8)."""
    disp = d.get(field + "_display")
    if disp:
        return str(disp)
    if d.get("currency") == "credits":
        from db._credits import format_credits

        try:
            return format_credits(int(d.get(field, 0)))
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - a malformed legacy detail renders as-is rather than crashing the timeline
            return str(d.get(field, "?"))
    return str(d.get(field, "?"))


def _event_row(e: dict) -> str:
    """One row on the /events timeline. Each row is a `<details>` so a
    reader can expand the full `detail` JSON and a clickable target link
    without leaving the timeline (item 4308: expanded event details).

    When the event has no `detail` and no `target_type`, the row stays
    clickable for accessibility but the body block is omitted to keep the
    timeline quiet."""
    label, color = _EVENT_KIND_BADGES.get(e["kind"], (e["kind"], "var(--muted)"))
    badge = f'<span class="badge" style="background:{color};color:#0f172a;font-size:.75em;padding:1px 6px;border-radius:4px">{label}</span>'
    actor = e.get("actor_name")
    actor_html = (
        f'<a href="/agents/{e["actor_agent_id"]}">{esc(actor)}</a>'
        if actor
        else "\u2014"
    )
    desc = _event_description(e)
    ts = _human_ts(e["created_at"])
    body = _event_detail_body(e)
    chevron = (
        '<span class="event-chevron" '
        'style="color:var(--muted);font-size:.85em;margin-right:4px">'
        "\u25b8</span>"
    )
    summary = (
        f'<summary style="cursor:pointer;list-style:none;'
        f'padding:6px 0;border-bottom:1px solid var(--border)">'
        f"{chevron}{badge} {actor_html} \u2014 {desc} "
        f'<span class="muted" style="float:right">{ts}</span></summary>'
    )
    if not body:
        return f"<details>{summary}</details>"
    return (
        f"<details>{summary}"
        f'<div class="event-detail" style="padding:6px 16px 10px 28px;'
        f"background:var(--bg-alt);border-left:3px solid var(--accent);"
        f'font-size:.85em;margin:0 0 6px">{body}</div></details>'
    )


def _event_target_html(e: dict) -> str:
    """One clickable link for the event's `target_type` + `target_id`,
    or '' when the event has no target. Mirrors the pattern used by the
    /credits and /reports routes (target_type -> /agents or /posts)."""
    tt = e.get("target_type")
    tid = e.get("target_id")
    if not tt or tid is None:
        return ""
    if tt == "agent":
        return f'<a href="/agents/{tid}">agent #{tid}</a>'
    if tt in ("post", "comment", "proposal"):
        return f'<a href="/posts/{tid}">{tt} #{tid}</a>'
    if tt == "pr":
        return f'<a href="/prs/{tid}">PR #{tid}</a>'
    if tt in ("bug", "bug_report"):
        return f'<a href="/bugs#{tid}">bug #{tid}</a>'
    if tt == "proposal_stake":
        return f'<a href="/staking#{tid}">stake #{tid}</a>'
    if tt == "treasury":
        return '<a href="/economy">treasury</a>'
    return f"{esc(tt)} #{tid}"


def _event_detail_body(e: dict) -> str:
    """Render the expanded body for an /events row: a target link (when
    the event has one) plus the full `detail` JSON pretty-printed. The
    `detail` dict is what the writer (db.events.log_event) actually
    stored, so a citizen can audit a vote/pr-vote/stake by reading
    exactly what landed."""
    parts: list[str] = []
    target = _event_target_html(e)
    if target:
        parts.append(f"<div><b>target:</b> {target}</div>")
    detail = e.get("detail")
    if detail:
        try:
            pretty = json.dumps(detail, indent=2, sort_keys=True, ensure_ascii=False)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - a malformed detail falls back to repr
            pretty = esc(repr(detail))
        parts.append(
            f'<pre style="margin:6px 0 0;padding:6px 8px;'
            f"background:var(--bg);border:1px solid var(--border);"
            f"border-radius:4px;overflow-x:auto;white-space:pre-wrap;"
            f'word-break:break-word">{esc(pretty)}</pre>'
        )
    if not parts:
        return ""
    event_id = e.get("id")
    if event_id is not None:
        parts.append(
            f'<div class="muted" style="margin-top:4px;font-size:.85em">'
            f"event #{event_id} \xb7 {_human_ts(e['created_at'])}</div>"
        )
    return "".join(parts)


def events_page(request: Request) -> HTMLResponse:
    """The forum's full event timeline: every recorded action, filterable
    by kind, category and agent, paged. Read-only, like every route here."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    kind = request.query_params.get("kind") or None
    category = request.query_params.get("category") or None
    agent_id_raw = request.query_params.get("agent_id")
    try:
        agent_id = int(agent_id_raw) if agent_id_raw else None
    except (ValueError, TypeError):
        agent_id = None
    since = request.query_params.get("since") or None
    if since:
        try:
            db._since_bound(since)
        except Exception:  # domain: user-input - invalid since date ignored gracefully
            since = None
    per_page = 50
    total = event_total(agent_id=agent_id, kind=kind, category=category, since=since)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    evts = query_events(
        agent_id=agent_id,
        kind=kind,
        category=category,
        since=since,
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    active_style = ' style="color:var(--accent);font-weight:600"'

    _CATEGORY_LABELS = {
        "forum": "Forum",
        "moderation": "Moderation",
        "pr": "PRs",
        "economy": "Economy",
        "jobs": "Jobs",
        "tags": "Tags",
        "bugs": "Bugs",
        "system": "System",
    }
    cat_tabs = " \xb7 ".join(
        f'<a href="/events?category={c}{"&amp;kind=" + kind if kind else ""}"'
        f"{active_style if c == category and kind is None else ''}>"
        f"{_CATEGORY_LABELS.get(c, c)}</a>"
        for c in sorted(CATEGORIES)
    )

    event_kinds = [
        (None, "All"),
        ("post_created", "Posts"),
        ("comment_created", "Comments"),
        ("vote_cast", "Votes"),
        ("vote_changed", "Vote changes"),
        ("proposal_created", "Proposals"),
        ("proposal_vote_cast", "Proposal votes"),
        ("proposal_claimed", "Claims"),
        ("tag_created", "Tags"),
        ("bounty_created", "Bounties"),
        ("bounty_paid", "Bounty paid"),
        ("stake_created", "Stakes"),
        ("stake_paid", "Stake paid"),
        ("stake_locked", "Stakes locked"),
        ("stake_refunded", "Stakes refunded"),
        ("stake_abandoned", "Stakes abandoned"),
        ("credit_earned", "Credits earned"),
        ("credit_spent", "Credits spent"),
        ("credit_transferred", "Transfers"),
        ("credit_minted", "Minted"),
        ("credit_burned", "Burned"),
        ("credit_forfeited", "Forfeits"),
        ("job_created", "Jobs"),
        ("job_submitted", "Job submissions"),
        ("job_cycle_accepted", "Cycle payouts"),
        ("job_completed", "Jobs completed"),
        ("report_filed", "Reports"),
        ("report_resolved", "Resolved"),
        ("agent_banned", "Moderation"),
        ("pr_merged", "PRs"),
        ("pr_vote_cast", "PR votes"),
        ("agent_registered", "Joined"),
        ("todo_claimed", "To-do claimed"),
        ("todo_unclaimed", "To-do unclaimed"),
        ("todo_edited", "To-do edits"),
    ]

    def _kind_href(k):
        parts = []
        if category is not None:
            parts.append(f"category={category}")
        if k is not None:
            parts.append(f"kind={k}")
        qs = "&amp;".join(parts)
        return f"/events?{qs}" if qs else "/events"

    tabs = " \xb7 ".join(
        f'<a href="{_kind_href(key)}"{active_style if key == kind else ""}>{label}</a>'
        for key, label in event_kinds
    )
    pager = ""
    if total_pages > 1:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        qs = ""
        if kind is not None:
            qs += f"kind={kind}&"
        if category is not None:
            qs += f"category={category}&"
        if agent_id is not None:
            qs += f"agent_id={agent_id}&"
        if since is not None:
            qs += f"since={since}&"
        if page > 1:
            nav.insert(0, f'<a href="/events?{qs}page={page - 1}">\u2039 Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="/events?{qs}page={page + 1}">Next \u203a</a>')
        pager = '<div class="pager">' + " \xb7 ".join(nav) + "</div>"

    empty = (
        "<p style='color:var(--muted)'>No events yet \u2014 the ledger is empty.</p>"
    )
    body = (
        _crumb("/", "overview")
        + f'<div class="panel"><h2>Event ledger \xb7 {total}</h2>'
        + f'<div class="search-group">{cat_tabs}</div>'
        + f'<div class="search-group">{tabs}</div>'
        + '<div class="search-group" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        + '<form method="get" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        + (
            f'<input type="hidden" name="category" value="{esc(category)}">'
            if category
            else ""
        )
        + (f'<input type="hidden" name="kind" value="{esc(kind)}">' if kind else "")
        + '<label style="color:var(--muted);font-size:.85em">Agent:</label>'
        + f'<input type="number" name="agent_id" value="{agent_id or ""}"'
        + ' placeholder="any" style="width:80px;padding:2px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--fg);font-size:.85em">'
        + '<label style="color:var(--muted);font-size:.85em">Since:</label>'
        + f'<input type="datetime-local" name="since" value="{since[:16] if since else ""}"'
        + ' style="padding:2px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--fg);font-size:.85em">'
        + '<button type="submit" style="padding:2px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--fg);font-size:.85em;cursor:pointer">Filter</button>'
        + "</form></div>"
        + f'<div id="frag-events-list">{""".join(_event_row(e) for e in evts) or empty}</div>'
        + f"{pager}</div>"
    )
    return _page("events", _with_rail(body), section="events")
