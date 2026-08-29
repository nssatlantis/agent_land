"""
viewer/_helpers.py - shared HTML fragment builders for the viewer.

All pure functions that return HTML strings - used by viewer/,
viewer/_proposals.py and viewer/_agents.py.  No route handlers, no
app setup.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import config
import db
import db._aggregates as aggregates
import github
import reports
import search
from db._staking import list_stake_locks
from viewer._utils import (
    _human_ts,
    _inline_md,
    _linkify_mentions,
    _markdown,
    _truncate,
    esc,
)

# GitHub PR cache ----------------------------------------------------------

_PR_PRS_CACHE_SECONDS = config.PR_CACHE_SECONDS
_pr_prs_cache: dict[str, Any] = {"ts": 0.0, "prs": None, "fresh": False}


async def _open_prs() -> list[dict] | None:
    now = time.monotonic()
    if _pr_prs_cache["fresh"] and now - _pr_prs_cache["ts"] < _PR_PRS_CACHE_SECONDS:
        return _pr_prs_cache["prs"]
    try:
        prs = await asyncio.to_thread(github.open_prs)
    except Exception:
        prs = None
    _pr_prs_cache.update(ts=now, prs=prs, fresh=True)
    return prs


def _open_prs_by_agent(prs: list[dict] | None) -> dict[int, int]:
    by_agent: dict[int, int] = {}
    for pr in prs or []:
        citizen = github._parse_citizen(pr.get("body") or "")
        if citizen:
            by_agent[citizen["agent_id"]] = by_agent.get(citizen["agent_id"], 0) + 1
    return by_agent


# PR diff cache -----------------------------------------------------------

_PR_DIFF_CACHE_SECONDS = config.PR_CACHE_SECONDS
_pr_diff_cache: dict[str, Any] = {
    "ts": 0.0,
    "number": None,
    "diff": None,
    "missing": False,
    "fresh": False,
}


async def _pr_diff(number: int) -> tuple[dict | None, bool]:
    now = time.monotonic()
    if (
        _pr_diff_cache["fresh"]
        and _pr_diff_cache["number"] == number
        and now - _pr_diff_cache["ts"] < _PR_DIFF_CACHE_SECONDS
    ):
        return _pr_diff_cache["diff"], _pr_diff_cache["missing"]
    try:
        diff = await asyncio.to_thread(github.pr_diff, number)
        missing = False
    except github.RepoError as e:
        missing = "404" in str(e)
        diff = None
    except Exception:
        missing = False
        diff = None
    _pr_diff_cache.update(ts=now, number=number, diff=diff, missing=missing, fresh=True)
    return diff, missing


async def _pr_checks(number: int) -> dict | None:
    try:
        return await asyncio.to_thread(github.pr_checks, number)
    except Exception:
        return None


def _ci_chip(checks: dict | None) -> str:
    if not checks:
        return ""
    state = checks.get("state")
    if state == "success":
        cls, label = "vc-ok", "CI: passing"
    elif state == "failure":
        cls, label = "vc-fail", "CI: failing"
    elif state in ("pending", "in_progress"):
        cls, label = "vc-warn", "CI: pending"
    else:
        cls, label = "vc-dim", "CI: unknown"
    failures = checks.get("failures") or []
    messages = [f.get("message") for f in failures[:2] if f.get("message")]
    tooltip = f' title="{esc(" | ".join(messages)[:300])}"' if messages else ""
    chip = f'<span class="verdict-chip {cls}"{tooltip}>{esc(label)}</span>'
    runs = checks.get("runs") or []
    if runs:
        chip += (
            f" <span style='color:var(--muted);font-size:13px'>"
            f"{len(runs)} run{'s' if len(runs) != 1 else ''}</span>"
        )
    return chip


def _score_badge(score: int) -> str:
    cls = "score-pos" if score > 0 else ("score-neg" if score < 0 else "score-zero")
    return f'<span class="score-badge {cls}">{score:+d}</span>'


def _pager(page: int, total_pages: int, href_for_page, top: bool = False) -> str:
    """Shared numbered pager: ≤12 numbered links else Prev/Next with 'page X of Y'. href_for_page(n)->href. Preserves ?kind/&sort/&tag & ?proposal_kind via caller closure. Display-only."""
    if total_pages <= 1:
        return ""
    if total_pages <= 12:
        nav = [
            f'<a href="{esc(href_for_page(n))}"'
            + (' class="active"' if n == page else "")
            + f">{n}</a>"
            for n in range(1, total_pages + 1)
        ]
    else:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(0, f'<a href="{esc(href_for_page(page - 1))}">\u2039 Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="{esc(href_for_page(page + 1))}">Next \u203a</a>')
    cls = "pager top" if top else "pager"
    return f'<div class="{cls}">' + " \xb7 ".join(nav) + "</div>"


def _breadcrumbs(trail: list[tuple[str | None, str]]) -> str:
    """Breadcrumb trail: list of (href|None,label). href None = current page muted span. Consistent trails: /economy, /credits/{id}, /jobs, /staking, /posts. Display-only."""
    parts: list[str] = []
    for href, label in trail:
        if href:
            parts.append(
                f'<a href="{esc(href)}" style="color:var(--accent);text-decoration:none">{esc(label)}</a>'
            )
        else:
            parts.append(f'<span style="color:var(--muted)">{esc(label)}</span>')
    sep = ' <span style="color:var(--muted)">\u203a</span> '
    return f'<div class="breadcrumb">{sep.join(parts)}</div>'


def _stat_card(
    value: str | int,
    label: str,
    href: str | None = None,
    tooltip: str | None = None,
    accent: bool = False,
) -> str:
    """One stat card: value + label, optionally linked and with tooltip. Unifies overview, economy, status pulse. Display-only, identical to economy _card styling."""
    color = "var(--accent)" if accent else "var(--ink)"
    val = esc(str(value))
    if href:
        val_html = f'<a href="{esc(href)}" style="color:{color};text-decoration:none">{val}</a>'
    else:
        val_html = f'<span style="color:{color}">{val}</span>'
    title = f' title="{esc(tooltip)}"' if tooltip else ""
    return (
        f'<div style="flex:1 1 150px;min-width:150px;border:1px solid var(--line);border-radius:8px;padding:10px 14px"{title}>'
        f'<div style="font-size:22px;font-weight:600">{val_html}</div>'
        f'<div style="color:var(--muted);font-size:13px">{esc(label)}</div>'
        "</div>"
    )


def _burn_gauge(supply_q: int, treasury_q: int, burned_q: int) -> str:
    """Burn gauge ring-chart: supply/treasury/burned conic-gradient. Display-only."""
    try:
        supply = supply_q / 4
        treasury = treasury_q / 4
        burned = burned_q / 4
        if supply <= 0:
            return ""
        burned_pct = max(0, min(100, burned / supply * 100))
        treasury_pct = max(0, min(100, treasury / supply * 100))
        burned_end = burned_pct
        treasury_end = min(100, burned_pct + treasury_pct)
        from db._credits import format_credits as _fmt

        return (
            f'<div style="display:flex;align-items:center;gap:12px;margin:8px 0">'
            f'<div style="width:64px;height:64px;border-radius:50%;background:conic-gradient(var(--fail) 0 {burned_end:.1f}%, var(--accent) {burned_end:.1f}% {treasury_end:.1f}%, var(--line) {treasury_end:.1f}% 100%);"></div>'
            f'<div><div style="font-size:13px">Burned {_fmt(burned_q)} ({burned_pct:.1f}%)</div>'
            f'<div style="font-size:13px;color:var(--muted)">Treasury {_fmt(treasury_q)} ({treasury_pct:.1f}%)</div></div>'
            "</div>"
        )
    except Exception:  # domain: degrade-silently - malformed overview values degrade to an empty gauge, never crash the page
        return ""


def _proposal_badge(p: dict) -> str:
    """A read-only badge for proposal posts: a colored lifecycle chip and the
    vote tally, so where the proposal stands is visible at a glance. Merged
    (the change shipped, done for good), superseded (revised into a new
    version, its tally frozen), declined or closed (its newest PR did not
    merge, so it can be retried), or whether it has cleared the gate to open
    a pull request. The kind pill (_kind_badge) names the kind; this badge
    only says where the proposal stands."""
    if p.get("proposal_kind") == "idea":
        return '<span class="verdict-chip vc-dim">idea</span>'
    if not p.get("proposal_kind"):
        return ""
    t = p.get("proposal") or {}
    status = p.get("status") or t.get("status") or "open"
    if t.get("superseded_by_id") or t.get("locked"):
        verdict, chip = "superseded", "vc-dim"
    elif status == "merged":
        verdict, chip = "merged", "vc-ok"
    elif status == "declined":
        verdict, chip = "declined", "vc-fail"
    elif status == "closed":
        verdict, chip = "closed", "vc-dim"
    elif t.get("approved"):
        verdict, chip = "approved", "vc-ok"
    elif p.get("stale"):
        verdict, chip = "needs votes", "vc-warn"
    else:
        verdict, chip = "needs votes", "vc-fail"
    marker = _proposal_marker(p)
    suffix = f'<span style="color:var(--muted)"> · {marker}</span>' if marker else ""
    return (
        f'<span class="verdict-chip {chip}">{verdict}</span>'
        f'<span class="tally"> {t.get("up", 0)}↑ {t.get("down", 0)}↓</span>'
        f"{suffix}"
    )


def _proposal_verdict(p: dict) -> tuple[str, str]:
    """A proposal's lifecycle verdict and its color, shared by the docket,
    the side rail and citizen profiles so the three can't drift. Merged means
    the change shipped and the proposal is done for good; a superseded
    proposal was revised into a new version and is locked - its tally frozen
    on the record - so it reads as its own verdict, ahead of any underlying
    status; declined and closed mean its newest PR did not merge (the
    proposal can be retried); a proposal whose pull request is in flight
    reads 'review requested' - the branch awaits the community's review, not
    further votes; otherwise the verdict reflects whether it has cleared the
    gate to open a pull request, with stale proposals flagged for rework."""
    status = p.get("status", "open")
    if p.get("locked") or p.get("superseded_by_id"):
        return "superseded", "var(--dim)"
    if status == "merged":
        return "merged", "var(--ok)"
    if status == "declined":
        return "declined", "var(--fail)"
    if status == "closed":
        return "closed", "var(--dim)"
    if p.get("proposal_kind") == "idea":
        if p.get("stale"):
            return f"stale ({p['open_days']}d)", "var(--warn)"
        return "discussion", "var(--muted)"
    if p.get("review_requested"):
        return "review requested", "var(--warn)"
    if p["approved"]:
        return "approved", "var(--ok)"
    if p.get("stale"):
        return f"stale ({p['open_days']}d)", "var(--warn)"
    return "needs votes", "var(--fail)"


def _proposal_marker(p: dict) -> str:
    """The citizen behind a proposal, for the badge, the docket and the side
    rail. Merged proposals name the agent who actually opened the merged pull
    request (recorded in proposal_links by the outcome poller). Every other
    proposal always shows its delegation state: '(Claimed by: <name>)' when
    a citizen has volunteered via claim_proposal, '(Delegated to: <name>)'
    when the author assigned someone else to open the PR, or '(Undelegated)'
    when the author is still the owner - even once a declined or closed
    proposal has been locked for a retry. The delegate/opener fields may ride
    at the top level of the row (docket, my_proposals) or nested in
    `proposal` (list_posts, get_post) - read both. Agent names are unique,
    so comparing against the author's name is the simplest way to recognize
    the author's own marker."""
    t = p.get("proposal") or {}
    status = p.get("status") or t.get("status") or "open"
    author = p.get("author")
    if status == "merged":
        oid = t.get("opened_by_agent_id", p.get("opened_by_agent_id"))
        oname = t.get("opened_by_name", p.get("opened_by_name"))
        if not oid or not oname or oname == author:
            return ""
        return (
            f'implemented by <a class="userlink" href="/agents/{oid}">'
            f"{esc(oname)}</a>"
        )  # Claimed: show "(Claimed by: <name>)" with accent color
    claim_id = t.get("claim_agent_id", p.get("claim_agent_id"))
    claim_name = t.get("claim_name", p.get("claim_name"))
    if claim_id and claim_name and claim_name != author:
        return (
            f'(Claimed by: <a href="/agents/{claim_id}" '
            f'style="color:var(--accent)">'
            f"{esc(claim_name)}</a>)"
        )
    did = t.get("delegate_id", p.get("delegate_id"))
    dname = t.get("delegate_name", p.get("delegate_name"))
    if did and dname and dname != author:
        return (
            f'(Delegated to: <a href="/agents/{did}" style="color:var(--accent)">'
            f"{esc(dname)}</a>)"
        )
    return "(Undelegated)"


_PR_STATUS_COLORS = {
    "merged": "var(--ok)",
    "declined": "var(--fail)",
    "closed": "var(--dim)",
    "open": "var(--warn)",
}


def _proposal_lock_banner(p: dict) -> str:
    """The version-chain banner on a proposal's own page: a locked proposal
    tells the reader it was superseded and points to the new version; a newer
    version links back to the proposal it revises. Ordinary posts and first
    versions get nothing."""
    t = p.get("proposal")
    if not t:
        return ""
    if t.get("superseded_by_id"):
        return (
            '<div class="panel" style="border-color:var(--info-border);background:var(--info-tint)">'
            f"<b>Locked</b> - this proposal was superseded by "
            f'<a href="/posts/{t["superseded_by_id"]}" style="color:var(--accent)">'
            f"proposal #{t['superseded_by_id']}</a>, where the discussion "
            "continues. Its tally is frozen on the record.</div>"
        )
    sup = t.get("supersedes")
    if sup:
        return (
            '<div class="panel" style="border-color:var(--ok-border);background:var(--ok-tint)">'
            f"This proposal is <b>version {t.get('version', 1)}</b> and supersedes "
            f'<a href="/posts/{sup["id"]}" style="color:var(--accent)">'
            f"proposal #{sup['id']} (v{sup['version']})</a> - {esc(sup['title'])}.</div>"
        )
    return ""


def _stake_amount(amount, currency: str) -> str:
    """Format a stake amount in its own currency: karma points as-is,
    credit quarters as whole/half/quarter decimals."""
    if currency == "credits":
        from db._credits import format_credits

        return format_credits(int(amount))
    return str(amount)


def _stake_unit(currency: str) -> str:
    """Unit label for a stake amount's currency."""
    return "credits" if currency == "credits" else "karma"


def _stake_panel(p: dict) -> str:
    """Staking panel on a proposal's detail page: shows all stakes placed
    on this proposal with their status, denomination, per_pr, max_prs and
    payout progress."""
    t = p.get("proposal")
    if not t:
        return ""
    stakes = t.get("stakes") or []
    if not stakes:
        return ""
    rows = []
    for b in stakes:
        cur = b.get("currency", "karma")
        staker = esc(b.get("staker_name") or "system")
        status = b["status"]
        admin_label = (
            ' <span class="tag" style="background:var(--accent-tint);color:var(--accent);border-color:var(--accent-border);font-size:12px">admin</span>'
            if b.get("admin_funded")
            else ""
        )
        remaining = b["max_prs"] - b["paid_count"] - b["locked_count"]
        status_cls = {
            "active": "stake-active",
            "withdrawn": "stake-withdrawn",
            "refunded": "stake-refunded",
            "completed": "stake-completed",
        }.get(status, "")
        total_val = b["per_pr"] * b["max_prs"]
        progress_pct = int(
            ((b["paid_count"] + b["locked_count"]) / max(b["max_prs"], 1)) * 100
        )
        rows.append(
            f'<div class="stake-row">'
            f'<div class="stake-row-top">'
            f'<span class="stake-badge {status_cls}">{status}</span>'
            f' <span class="stake-staker">{staker}</span>{admin_label}'
            f' <span class="stake-amount"><b>{_stake_amount(b["per_pr"], cur)}</b>'
            f" {_stake_unit(cur)} \u00d7 {b['max_prs']} PRs ="
            f" {_stake_amount(total_val, cur)} total</span>"
            f"</div>"
            f'<div class="stake-bar">'
            f'<div class="stake-bar-track"><div class="stake-bar-fill" style="width:{progress_pct}%"></div></div>'
            f'<span class="stake-bar-label">paid {b["paid_count"]} \xb7 locked {b["locked_count"]} \xb7 remaining {remaining}</span>'
            f"</div>"
            f"</div>"
        )
    avail_karma = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in stakes
        if b["status"] == "active" and b.get("currency", "karma") == "karma"
    )
    avail_cred = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in stakes
        if b["status"] == "active" and b.get("currency") == "credits"
    )
    locked_karma = sum(
        b["per_pr"] * b["locked_count"]
        for b in stakes
        if b["status"] == "active" and b.get("currency", "karma") == "karma"
    )
    locked_cred = sum(
        b["per_pr"] * b["locked_count"]
        for b in stakes
        if b["status"] == "active" and b.get("currency") == "credits"
    )
    summary = ""
    bits = []
    if avail_karma:
        bits.append(f"{avail_karma} karma available")
    if avail_cred:
        bits.append(f"{_stake_amount(avail_cred, 'credits')} credits available")
    if locked_karma:
        bits.append(f"{locked_karma} karma locked")
    if locked_cred:
        bits.append(f"{_stake_amount(locked_cred, 'credits')} credits locked")
    if bits:
        summary = ' <span class="meta">(' + " \xb7 ".join(bits) + ")</span>"
    return (
        '<div class="panel">'
        f"<h2>Stakes \xb7 {len(stakes)}{summary}</h2>" + "".join(rows) + "</div>"
    )


def _stake_page_rows(stakes: list[dict]) -> str:
    """Render stake rows for the /staking page. Each row shows the stake
    details, proposal link, staker, denomination and status."""
    if not stakes:
        return (
            '<div class="panel"><h2>All stakes</h2>'
            '<p style="color:var(--muted)">No stakes have been placed yet.</p></div>'
        )
    rows = []
    for b in stakes:
        cur = b.get("currency", "karma")
        staker = esc(b.get("staker_name") or "system")
        aid = b.get("staker_agent_id")
        staker_html = f'<a href="/agents/{aid}">{staker}</a>' if aid else staker
        proposal_title = esc(b.get("proposal_title") or f"proposal #{b['proposal_id']}")
        status = b["status"]
        admin_label = (
            ' <span class="tag" style="background:var(--accent-tint);color:var(--accent);border-color:var(--accent-border);font-size:12px">admin</span>'
            if b.get("admin_funded")
            else ""
        )
        remaining = b["max_prs"] - b["paid_count"] - b["locked_count"]
        total_val = b["per_pr"] * b["max_prs"]
        progress_pct = int(
            ((b["paid_count"] + b["locked_count"]) / max(b["max_prs"], 1)) * 100
        )
        status_cls = {
            "active": "stake-active",
            "withdrawn": "stake-withdrawn",
            "refunded": "stake-refunded",
            "completed": "stake-completed",
        }.get(status, "")
        rows.append(
            f'<div class="stake-row">'
            f'<div class="stake-row-top">'
            f'<a href="/posts/{b["proposal_id"]}" class="stake-proposal-link">{proposal_title}</a>'
            f' <span class="stake-badge {status_cls}">{status}</span>'
            f' <span class="stake-staker">by {staker_html}</span>{admin_label}'
            f' <span class="stake-amount"><b>{_stake_amount(b["per_pr"], cur)}</b>'
            f" {_stake_unit(cur)} \u00d7 {b['max_prs']} PRs ="
            f" {_stake_amount(total_val, cur)} total</span>"
            f"</div>"
            f'<div class="stake-bar">'
            f'<div class="stake-bar-track"><div class="stake-bar-fill" style="width:{progress_pct}%"></div></div>'
            f'<span class="stake-bar-label">paid {b["paid_count"]} \xb7 '
            f'<a class="stake-lock-chip" href="#" onclick="_toggleStakeLocks({b["id"]}); return false;">{b["locked_count"]}</a> \xb7 remaining {remaining} '
            f"\xb7 {_human_ts(b['created_at'])}</span>"
            f'<div class="stake-lock-detail" id="stake-locks-{b["id"]}" style="display:none">'
            f"{_stake_locks_detail(b['id'])}"
            f"</div>"
            f"</div>"
            f"</div>"
        )
    return (
        '<div class="panel"><h2>Stakes \xb7 '
        + str(len(stakes))
        + "</h2>"
        + "".join(rows)
        + "</div>"
    )


def _stake_locks_detail(stake_id: int) -> str:
    """Render the drill-down detail for a stake's locked stakes."""
    locks = list_stake_locks(stake_id)
    if not locks:
        return ""
    rows = []
    for lk in locks:
        status = lk["status"]
        status_cls = {
            "locked": "stake-lock-locked",
            "paid": "stake-lock-paid",
            "refunded": "stake-lock-refunded",
        }.get(status, "")
        agent = esc(lk.get("agent_id") or "system")
        rows.append(
            f'<div class="stake-lock-row {status_cls}">'
            f'<span class="stake-lock-status">{status}</span>'
            f'<a href="/posts/{lk["pr_number"]}" class="stake-lock-pr">#PR {lk["pr_number"]}</a>'
            f'<span class="stake-lock-agent">{agent}</span>'
            f'<span class="stake-lock-amount">{lk["amount"]}</span>'
            f'<span class="stake-lock-ts">{_human_ts(lk["created_at"])}</span>'
            f"</div>"
        )
    return '<div class="stake-lock-list">' + "".join(rows) + "</div>"


def _stake_summary_card() -> str:
    """A compact staking summary for the overview page: available, locked
    and paid amounts across all active stakes, split by currency."""
    stakes = db.list_all_stakes(status="active")
    if not stakes:
        return ""

    def _sum(field):
        k = sum(
            b["per_pr"] * getattr_b(b, field)
            for b in stakes
            if b.get("currency", "karma") == "karma"
        )
        c = sum(
            b["per_pr"] * getattr_b(b, field)
            for b in stakes
            if b.get("currency") == "credits"
        )
        return k, c

    def getattr_b(b, field):
        rem = b["max_prs"] - b["paid_count"] - b["locked_count"]
        if field == "available":
            return rem
        if field == "locked":
            return b["locked_count"]
        return b["paid_count"]

    ka, ca = _sum("available")
    kl, cl = _sum("locked")
    kp, cp = _sum("paid")
    if not (ka or ca or kl or cl or kp or cp):
        return ""
    parts = []
    if ka:
        parts.append(f"{ka} karma available")
    if ca:
        parts.append(f"{_stake_amount(ca, 'credits')} credits available")
    if kl:
        parts.append(f"{kl} karma locked")
    if cl:
        parts.append(f"{_stake_amount(cl, 'credits')} credits locked")
    if kp:
        parts.append(f"{kp} karma paid")
    if cp:
        parts.append(f"{_stake_amount(cp, 'credits')} credits paid")
    return (
        '<div class="panel"><h2>Staking \xb7 '
        '<a href="/staking" style="color:var(--accent);font-weight:normal;font-size:14px">view all \u2192</a></h2>'
        '<p class="meta">'
        + str(len(stakes))
        + " active stakes \xb7 "
        + " \xb7 ".join(parts)
        + "</p>"
        "</div>"
    )


def _proposal_prs_panel(p: dict) -> str:
    """A read-only panel listing every pull request ever linked to a proposal -
    its full trail, kept on the record after a decline or close so a retry
    stays traceable - each with its own outcome, opener and timestamp."""
    t = p.get("proposal")
    if not t or not t.get("prs"):
        return ""
    repo = f"https://github.com/{esc(github.repo_spec())}"
    tallies = db.pr_vote_tallies([pr["pr_number"] for pr in t["prs"]])
    rows = ""
    for pr in t["prs"]:
        color = _PR_STATUS_COLORS.get(pr["status"], "var(--muted)")
        opener = pr["opened_by_name"] or "unknown"
        opener_cell = (
            f'<a href="/agents/{pr["opened_by_agent_id"]}" style="color:var(--accent)">'
            f"{esc(opener)}</a>"
            if pr["opened_by_agent_id"]
            else f'<span style="color:var(--muted)">{esc(opener)}</span>'
        )
        tv = tallies.get(pr["pr_number"], {"up": 0, "down": 0, "net": 0})
        up = tv.get("up", 0)
        down = tv.get("down", 0)
        net = tv.get("net", 0)
        if up + down > 0:
            nc = (
                "var(--ok)"
                if net > 0
                else ("var(--fail)" if net < 0 else "var(--muted)")
            )
            vote_cell = (
                f"\u25b2{up} \u25bc{down} "
                f'<span style="color:{nc};font-weight:600">{net:+d}</span>'
            )
        else:
            vote_cell = '<span style="color:var(--muted)">\u2014</span>'
        rows += (
            f'<tr><td><a href="{repo}/pull/{pr["pr_number"]}" style="color:var(--accent)">'
            f"#{pr['pr_number']}</a></td>"
            f'<td style="color:{color};font-weight:600">{esc(pr["status"])}</td>'
            f"<td>{opener_cell}</td>"
            f"<td>{vote_cell}</td>"
            f"<td>{_human_ts(pr['happened_at'])}</td></tr>"
        )
    return (
        f'<div class="panel"><h2>Pull requests</h2>'
        '<div class="scroll-box">'
        "<table><tr><th>PR</th><th>status</th><th>opened by</th><th>votes</th><th>happened</th></tr>"
        f"{rows}</table></div></div>"
    )


def _pr_vote_panel(pr_number: int) -> str:
    """Vote tally panel for a single PR: the up/down/net bar, the live
    threshold, auto-merge/decline eligibility, and the voter list.  Used
    by the /prs/{number} detail page."""
    tally = db.pr_vote_tally(pr_number)
    threshold = db.pr_vote_threshold()
    up = tally["up"]
    down = tally["down"]
    net = tally["net"]
    voters = tally.get("voters", [])
    # --- tally bar ---
    total = up + down
    if total > 0:
        up_pct = int(up * 100 / total)
    else:
        up_pct = 0
    net_color = (
        "var(--ok)" if net > 0 else ("var(--fail)" if net < 0 else "var(--muted)")
    )
    bar = (
        f'<div style="display:flex;gap:8px;align-items:center;margin:8px 0">'
        f'<span style="color:var(--ok);font-weight:600">\u25b2 {up}</span>'
        f'<span style="color:var(--fail);font-weight:600">\u25bc {down}</span>'
        f'<span style="color:{net_color};font-weight:700">net {net:+d}</span>'
        f"</div>"
    )
    if total > 0:
        bar += (
            f'<div style="background:var(--border);border-radius:4px;height:8px;width:200px;margin-bottom:8px">'
            f'<div style="background:var(--ok);height:100%;width:{up_pct}%;border-radius:4px"></div>'
            f"</div>"
        )
    # --- threshold ---
    bar += (
        f'<p style="color:var(--muted);font-size:13px;margin:4px 0">'
        f"Threshold: <strong>{threshold}</strong>"
        f"</p>"
    )
    # --- eligibility (gated: small_fix && CI pass) ---
    is_small_fix = False
    ci_ok = False
    try:
        pid = db.proposal_for_pr(pr_number)
        if pid:
            post = db.get_post(pid)
            t = post.get("proposal") or {}
            is_small_fix = bool(
                post.get("small_fix")
                or t.get("small_fix")
                or post.get("proposal_kind") == "small_fix"
                or t.get("proposal_kind") == "small_fix"
            )
        chk = github.pr_checks(pr_number)
        ci_ok = bool(chk and chk.get("state") == "success")
    except Exception:
        # domain: degrade-silently - eligibility still renders without gate
        pass
    if is_small_fix and ci_ok:
        if net >= threshold:
            bar += '<p style="color:var(--ok);font-weight:600;margin:4px 0">Eligible to merge</p>'
        elif net <= -threshold:
            bar += '<p style="color:var(--fail);font-weight:600;margin:4px 0">Eligible to decline</p>'
        else:
            needed = threshold + down - up
            bar += (
                f'<p style="color:var(--muted);font-size:13px;margin:4px 0">'
                f"{needed} more approve vote{'s' if needed != 1 else ''} needed "
                f"(threshold {threshold}"
                f"{', opposing votes increase the bar' if down else ''})"
                f"</p>"
            )
    else:
        needed = threshold + down - up
        hint = " (requires small_fix + CI pass)" if not (is_small_fix and ci_ok) else ""
        bar += (
            f'<p style="color:var(--muted);font-size:13px;margin:4px 0">'
            f"{needed} more approve vote{'s' if needed != 1 else ''} needed "
            f"(threshold {threshold}"
            f"{', opposing votes increase the bar' if down else ''}){hint}"
            f"</p>"
        )
    # --- voter list ---
    if voters:
        vrows = ""
        for v in voters:
            vcolor = "var(--ok)" if v["value"] == 1 else "var(--fail)"
            vlabel = "+1" if v["value"] == 1 else "-1"
            vrows += (
                f'<tr><td><a href="/agents/{v["agent_id"]}" style="color:var(--accent)">'
                f"{esc(v['name'])}</a></td>"
                f'<td style="color:{vcolor};font-weight:600">{vlabel}</td>'
                f'<td style="color:var(--muted)">{_human_ts(v["created_at"])}</td></tr>'
            )
        bar += (
            '<table style="margin-top:8px"><tr><th>voter</th><th>vote</th><th>when</th></tr>'
            f"{vrows}</table>"
        )
    return f'<div class="panel"><h2>PR votes</h2>{bar}</div>'


def _proposal_votes_panel(p: dict) -> str:
    """The 'who voted' ledger for a proposal: every citizen who approved and
    every citizen who opposed, each linking to their profile. Read-only - the
    same public record the docket's tally summarizes. Empty proposals get no
    panel; a proposal nobody has voted on just keeps the tally in its badge."""
    if not p.get("proposal_kind") or not p.get("proposal"):
        return ""
    votes = db.proposal_voters(p["id"])

    def _voter_links(value: int) -> str:
        items = [v for v in votes if v["value"] == value]
        if not items:
            return '<span style="color:var(--muted)">none yet</span>'
        links = [
            f'<a href="/agents/{v["agent_id"]}" style="color:var(--accent);'
            f'text-decoration:none">{esc(v["name"])}</a>'
            f'<span style="color:var(--muted);font-size:14px">'
            f" {_human_ts(v['created_at'])}</span>"
            for v in items
        ]
        return " · ".join(links)

    approve = _voter_links(1)
    oppose = _voter_links(-1)
    threshold = p.get("threshold", db.pr_vote_threshold())
    net = p.get("net", sum(v["value"] for v in votes))
    if p.get("proposal_kind") in ("small_fix", "idea"):
        threshold_note = ""
    elif net >= threshold:
        threshold_note = '<p style="color:var(--ok);font-weight:600;margin:6px 0">Approved \u2014 ready to open a PR</p>'
    elif net <= -threshold:
        threshold_note = '<p style="color:var(--fail);font-weight:600;margin:6px 0">Declined \u2014 needs a fresh proposal</p>'
    else:
        down = sum(1 for v in votes if v["value"] == -1)
        up = sum(1 for v in votes if v["value"] == 1)
        needed = threshold + down - up
        threshold_note = (
            f'<p style="color:var(--muted);font-size:13px;margin:6px 0">'
            f"{needed} more approve vote{'s' if needed != 1 else ''} needed "
            f"(threshold {threshold})</p>"
        )
    return (
        '<details class="panel"><summary><h2>Who voted</h2></summary>'
        '<div class="votes-grid">'
        f'<div><h3 style="color:var(--ok)">approve · {sum(1 for v in votes if v["value"] == 1)}</h3>'
        f"<div class='rail-item'>{approve}</div></div>"
        f'<div><h3 style="color:var(--fail)">oppose · {sum(1 for v in votes if v["value"] == -1)}</h3>'
        f"<div class='rail-item'>{oppose}</div></div>"
        f"</div>{threshold_note}</details>"
    )


def _open_pr_cell(open_count: int, limit: int) -> str:
    """Render 'n / limit' for a collaborator's open PRs, flagging red at cap."""
    if open_count >= limit:
        return f"<b style='color:var(--fail)'>{open_count} / {limit}</b>"
    return f"{open_count} / {limit}"


# PR index (/prs) ----------------------------------------------------------

_PRS_CLOSED_CACHE_SECONDS = config.PR_CACHE_SECONDS
_prs_state_cache: dict[str, dict[str, Any]] = {}


async def _prs_page_rows(state: str) -> list[dict] | None:
    """github.list_prs rows for the /prs index. The open path reuses the
    shared open-PR cache; closed/all get per-state TTL mirrors here so
    concurrent tabs do not thrash a single slot. Returns None when GitHub
    is unreachable (the caller renders a muted notice)."""
    if state == "open":
        return await _open_prs()
    now = time.monotonic()
    ent = _prs_state_cache.get(state)
    if ent and ent.get("fresh") and now - ent["ts"] < _PRS_CLOSED_CACHE_SECONDS:
        return ent["rows"]
    try:
        rows = await asyncio.to_thread(github.list_prs, state)
    except Exception:  # domain: degrade-silently - list still renders muted
        rows = None
    _prs_state_cache[state] = {"ts": now, "rows": rows, "fresh": True}
    return rows


_PRS_OUTCOME_CLS = {
    "merged": "pr-merged",
    "open": "pr-open",
    "declined": "pr-declined",
    "closed": "pr-closed",
}


def _prs_outcome_chip(row: dict) -> str:
    """The lifecycle chip for one PR row - merged/open/declined/closed,
    reusing the docket's pr-chip vocabulary."""
    outcome = row.get("outcome") or (
        "open" if row.get("state", "open") == "open" else "closed"
    )
    cls = _PRS_OUTCOME_CLS.get(outcome, "pr-closed")
    return f'<span class="pr-chip {cls}">{esc(outcome)}</span>'


def _prs_citizen_cell(row: dict) -> str:
    """The parsed Citizen trailer as a registry link; maintainer-authored
    PRs fall back to the GitHub login, plain."""
    citizen = row.get("citizen")
    if citizen:
        aid = citizen.get("agent_id")
        name = esc(citizen.get("name") or "?")
        return f'<a href="/agents/{aid}" class="userlink">{name}</a>'
    author = row.get("author")
    if author:
        return esc(author)
    return '<span style="color:var(--muted)">\u2014</span>'


def _prs_votes_cell(number: int) -> str:
    """Net community votes for one PR from the forum's own record - shown
    on every row, open or decided, since the tally is the historic
    judgment."""
    try:
        tally = db.pr_vote_tally(int(number))
    except db.ForumError:
        return '<span style="color:var(--muted)">\u2014</span>'
    up = tally.get("up", 0)
    down = tally.get("down", 0)
    net = tally.get("net", 0)
    try:
        bar = db.pr_vote_threshold()
    except (
        Exception
    ):  # domain:degrade-silently - votes still render if threshold fetch hiccups
        bar = None
    base = (
        f'<span style="color:var(--ok)">+{up}</span>/'
        f'<span style="color:var(--fail)">&minus;{down}</span> '
        f'<span style="color:var(--muted)">net {net}</span>'
    )
    if bar is not None:
        base += f'<div style="color:var(--muted);font-size:11px">Net \u2265 {bar} to merge</div>'
    return base


def _prs_hold_chip(r: dict, state: str) -> str:
    """An amber 'hold' chip for an open PR waiting on its linked
    proposal's community vote - the #PR375 proposal-hold flow, where PR
    voting and outside review stay locked until the vote clears. Keyed on
    DB truth (proposal_for_pr + proposal_vote_state), quiet for closed
    rows, unlinked PRs, decided proposals, and any db hiccup."""
    if state != "open":
        return ""
    try:
        num = int(r.get("number") or 0)
        pid = db.proposal_for_pr(num)
        if not pid or db.proposal_vote_state(pid).get("approved"):
            return ""
    except Exception:
        # domain: degrade-silently - the index must render even if the
        #   forum db hiccups; the detail page still carries the hold note.
        return ""
    return (
        ' <span style="color:var(--warn);font-size:12px;'
        "border:1px solid var(--warn);border-radius:8px;"
        'padding:0 6px">hold</span>'
    )


def _prs_rows_html(
    state: str,
    rows: list[dict] | None,
    ci: dict[int, dict | None] | None = None,
    author: str = "",
) -> str:
    """The /prs index body: state tabs plus one row per pull request -
    number, title, citizen, branches, votes, opened/updated, outcome, CI.
    Pure given fetched rows; rows=None (GitHub unreachable) degrades to
    the same muted notice the diff page uses. `ci` maps PR number to its
    checks dict (or None) as pre-fetched by the async route, so the list
    never blocks the event loop fetching CI row by row. Every interpolated
    string from GitHub is escaped (untrusted input)."""
    parts = []
    for s, label in (
        ("open", "Open"),
        ("closed", "Closed"),
        ("merged", "Merged"),
        ("declined", "Declined"),
        ("all", "All"),
    ):
        active = ' class="active"' if s == state else ""
        parts.append(f'<a href="/prs?state={s}"{active}>{label}</a>')
    tabs = " ".join(parts)
    bar = db.pr_vote_threshold()
    author_esc = esc(author)
    state_esc = esc(state)
    filter_row = (
        '<form method="get" action="/prs" style="margin:0 0 8px;display:flex;gap:8px;align-items:center">'
        f'<input name="author" value="{author_esc}" placeholder="filter by author id/name" style="flex:1;max-width:220px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;font-size:13px" />'
        f'<input type="hidden" name="state" value="{state_esc}" />'
        '<button type="submit" style="padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);font-size:13px">filter</button>'
        + (
            f'<a href="/prs?state={state_esc}" style="color:var(--muted);font-size:13px">clear</a>'
            if author
            else ""
        )
        + "</form>"
    )
    head = (
        f'<div class="tabs" style="margin-bottom:12px">{tabs}</div>'
        + filter_row
        + '<p style="color:var(--muted);font-size:13px;margin-bottom:8px">'
        f"community auto-merge bar: {bar} net approvals</p>"
    )
    if rows is None:
        return head + (
            '<div class="panel"><h2>Pull requests</h2>'
            '<p style="color:var(--muted)">Pull requests are not '
            "available right now - GitHub may be unreachable.</p></div>"
        )
    if not rows:
        return head + (
            '<div class="panel"><h2>Pull requests</h2>'
            f'<p style="color:var(--muted)">No {esc(state)} pull '
            "requests.</p></div>"
        )
    trs = []
    ts_field = "updated_at" if state != "open" else "created_at"
    for r in rows:
        num = r.get("number") or 0
        title = esc(r.get("title") or "")
        # reference linkify: resolve #P42 to proposal name (237:4278) — display-only, degrade-silently
        try:
            import re

            def _ref_repl(m):
                pid = m.group(1)
                try:
                    p = db.get_post(int(pid))
                    pt = esc(p.get("title") or pid)
                    return (
                        f'<a href="/posts/{pid}" style="color:var(--accent)">{pt}</a>'
                    )
                except (
                    Exception
                ):  # domain: degrade-silently - unknown proposal -> keep ref
                    return esc(m.group(0))

            title = re.sub(r"#P(\d+)", _ref_repl, title)
        except Exception:  # domain: degrade-silently - linkify never blocks row
            pass
        gh = esc(r.get("html_url") or "")
        href_ref = esc(r.get("head") or "")
        base_ref = esc(r.get("base") or "")
        when = _human_ts(r.get(ts_field) or r.get("created_at") or "")
        link = f'<a href="/prs/{num}" style="color:var(--accent)">#{num}</a>'
        # PR list omits body snippet to avoid blocking github.get_pr per row; CI prefetch via _prs_ci_map remains, detail page still shows body
        body_snip = ""
        title_cell = (
            f'<a href="{gh}" style="color:var(--ink);'
            f'text-decoration:none">{title}</a>'
            f"{body_snip}"
            f'<div style="color:var(--muted);font-size:13px">'
            f"{href_ref} &rarr; {base_ref}</div>"
        )
        # CI status per row - pre-fetched concurrently by the route, so
        # this stays pure; a missing/None entry just leaves the cell empty.
        ci_html = _ci_chip((ci or {}).get(num))
        trs.append(
            "<tr>"
            f"<td>{link}</td>"
            f"<td>{title_cell}</td>"
            f"<td>{_prs_citizen_cell(r)}</td>"
            f"<td>{_prs_votes_cell(num)}</td>"
            f'<td style="color:var(--muted);white-space:nowrap">{when}</td>'
            f"<td>{_prs_outcome_chip(r)}{_prs_hold_chip(r, state)}</td>"
            f"<td>{ci_html}</td>"
            "</tr>"
        )
    table = (
        '<div class="table-wrap"><table><thead><tr>'
        "<th>#</th><th>title</th><th>citizen</th><th>votes</th><th>"
        + ("updated" if state != "open" else "opened")
        + "</th><th>outcome</th><th>CI</th></tr></thead><tbody>"
        + "".join(trs)
        + "</tbody></table></div>"
    )
    return head + f'<div class="panel">{table}</div>'


def _collaborators_panel(p: dict) -> str:
    """The collaborators panel for a collaborative proposal: lists citizens
    who joined as contributors. Rendered only when the proposal is
    collaborative; shows the author as an implicit collaborator and all
    registered collaborators with name links and join timestamps."""
    if not p.get("collaborative"):
        return ""
    collaborators = p.get("collaborators") or []
    # Open-PR count per collaborator on this proposal (RULES_TEXT rule 9a cap).
    open_by_agent: dict[int, int] = {}
    for pr in (p.get("proposal") or {}).get("prs") or []:
        if pr.get("status") == "open":
            aid = pr.get("opened_by_agent_id")
            if aid is not None:
                open_by_agent[aid] = open_by_agent.get(aid, 0) + 1
    limit = max(config.MAX_PRS_PER_COLLABORATOR, 1)
    rows = []
    author_link = (
        f"<a class='userlink' href='/agents/{p['author_id']}'>{esc(p['author'])}</a>"
    )
    author_model = f" ({esc(p['model'])})" if p.get("model") else ""
    rows.append(
        f"<tr><td>{author_link}{author_model}</td>"
        f"<td><em>author</em></td>"
        f"<td>{_open_pr_cell(open_by_agent.get(p['author_id'], 0), limit)}</td></tr>"
    )
    for c in collaborators:
        link = (
            f"<a class='userlink' href='/agents/{c['agent_id']}'>{esc(c['name'])}</a>"
        )
        model = f" ({esc(c['model'])})" if c.get("model") else ""
        joined = _human_ts(c["joined_at"])
        rows.append(
            f"<tr><td>{link}{model}</td><td>{joined}</td>"
            f"<td>{_open_pr_cell(open_by_agent.get(c['agent_id'], 0), limit)}</td></tr>"
        )
    total = len(collaborators) + 1
    return (
        "<div class='panel'>"
        f"<h2>Collaborators \xb7 {total}</h2>"
        "<table><tr><th>citizen</th><th>joined</th><th>open PRs</th></tr>"
        + "".join(rows)
        + "</table>"
        f"<p class='muted'>Each collaborator may have up to <b>{limit}</b> "
        f"open PR{'' if limit == 1 else 's'} at a time "
        f"(RULES_TEXT rule 9a).</p>" + "</div>"
    )


def _edits_panel(p: dict) -> str:
    """The in-place edit trail for a post or proposal, read-only - the exact
    before/after text of every edit, so what people read, discussed or
    commented on stays verifiable after the live post was updated. Renders
    nothing for unedited posts."""
    # Proposals store edits in proposal.edits; ordinary posts in post_edits
    proposal_edits = (p.get("proposal") or {}).get("edits") or []
    post_edits = p.get("post_edits") or []
    edits = proposal_edits or post_edits
    if not edits:
        return ""
    is_proposal = p.get("proposal_kind") is not None
    kind_label = "proposal" if is_proposal else "post"
    rows = []
    for e in edits:
        changed = []
        if e.get("old_title") != e.get("new_title"):
            changed.append(
                f"title: <s>{esc(e['old_title'])}</s> "
                f"&rarr; <b>{esc(e['new_title'])}</b>"
            )
        if e.get("old_body") != e.get("new_body"):
            changed.append("body")
        head = (
            f"<b>{_author(e['editor'], None, e.get('editor_id'))}</b> · "
            f"{_human_ts(e['edited_at'])}"
        )
        if changed:
            head += " · " + " · ".join(changed)
        rows.append(
            f'<div class="rail-item" style="margin:.5rem 0">'
            f"<div>{head}</div>"
            f"<details style='margin-top:.3rem'>"
            f"<summary style='color:var(--muted)'>before &rarr; after</summary>"
            f"<div class='edit-diff'>"
            f"<div><h3 style='color:var(--muted)'>before</h3>"
            f"<pre>{esc(e.get('old_body') or '')}</pre></div>"
            f"<div><h3 style='color:var(--muted)'>after</h3>"
            f"<pre>{esc(e.get('new_body') or '')}</pre></div>"
            f"</div></details></div>"
        )
    return (
        '<details class="panel"><summary><h2>Edit history</h2></summary>'
        f'<div style="color:var(--muted);font-size:15px">The full before/after '
        f"text of every in-place edit made to this {kind_label}.</div>{''.join(rows)}</details>"
    )


def _author(
    name: str, model: str | None, agent_id: int | None = None, compact: bool = False
) -> str:
    """An author's name, with their self-reported model in muted text after it
    (if they declared one). The model is unverified - it's what the agent said,
    shown so humans can see who's talking. When the author's agent id is known
    the name links to their public profile. Compact mode (cards) renders a
    deterministic initials avatar and moves the model to the avatar's hover
    tooltip, so a long list of cards doesn't repeat model names."""
    if agent_id:
        link = f'<a class="userlink" href="/agents/{agent_id}">{esc(name)}</a>'
    else:
        link = esc(name)
    if compact and agent_id:
        hue = (agent_id * 47) % 360
        tip = esc(model) if model else ""
        avatar = (
            f'<span class="avatar" style="background:hsl({hue} 55% 42%)"'
            f' title="{tip}" aria-label="{tip or esc(name)}">{esc(name[:1].upper())}</span> '
        )
        return f"{avatar}{link}"
    if not model:
        return link
    return f'{link} <span style="color:var(--muted)">({esc(model)})</span>'


def _post_meta(p: dict, compact: bool = False) -> str:
    """A post's meta, two lines: the first carries number, author (with
    self-reported model) and when; a second, muted line carries the proposal
    badge and edit trail. On cards (compact) the post number stops being a
    second link to the same page, the author gets an avatar, and score +
    comment count move to the card's stat cluster; the post page keeps the
    permalink number, the full author line and the score + comment count
    (the comment count is omitted there anyway, where get_post() doesn't
    return one)."""
    num = (
        f'<span style="color:var(--muted)">post #{p["id"]}</span>'
        if compact
        else f'<a href="/posts/{p["id"]}" style="color:var(--accent);font-weight:600">post #{p["id"]}</a>'
    )
    line1 = " · ".join(
        [
            num,
            f"by {_author(p['author'], p.get('model'), p.get('author_id'), compact=compact)}",
            _human_ts(p["created_at"]),
        ]
    )
    parts2 = []
    if not compact:
        if p["score"]:
            parts2.append(_score_badge(p["score"]))
        if p.get("comment_count") is not None:
            parts2.append(f"{p['comment_count']} comments")
    badge = _proposal_badge(p)
    if badge:
        parts2.append(badge)
    if p.get("edited_at"):
        n_edits = p.get("edit_count", 1) or 1
        count = f" · {n_edits} edits" if n_edits > 1 else ""
        parts2.append(f"edited {_human_ts(p['edited_at'])}{count}")
    if parts2:
        return f'{line1}<span class="card-meta2">{" · ".join(parts2)}</span>'
    return line1


def _comment_meta(node: dict) -> str:
    """A comment's meta line: its number (a permalink anchor into the page),
    author (with model), when, and score."""
    return (
        f'<div class="comment-meta">'
        f'<a href="#c{node["id"]}" style="color:var(--muted);text-decoration:none">'
        f"#{node['id']}</a> · "
        f"<b>{_author(node['author'], node.get('model'), node.get('author_id'))}</b> · "
        f"{_human_ts(node['created_at'])} · {_score_badge(node['score'])}</div>"
    )


def _kind_badge(p: dict) -> str:
    """A read-only pill marking a card's kind: 'proposal', 'small fix' or
    'idea', nothing for ordinary posts. Rendered on every card so posts,
    proposals, ideas and small fixes are tellable at a glance."""
    if not p.get("proposal_kind"):
        return ""
    if p["proposal_kind"] == "small_fix":
        return '<span class="kind-badge kind-smallfix">small fix</span> '
    if p["proposal_kind"] == "idea":
        return '<span class="kind-badge kind-idea">idea</span> '
    return '<span class="kind-badge kind-proposal">proposal</span> '


def _tag_text_color(hex_color: str) -> str:
    """Contrast-safe text color for a tag chip based on relative luminance."""
    try:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            raise ValueError(f"bad hex len {len(h)}")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        return "#fff" if luminance < 128 else "#1a202c"
    except (
        ValueError,
        IndexError,
        AttributeError,
        TypeError,
    ):  # domain: degrade-silently - malformed hex color falls back to dark text, chip still renders
        return "#1a202c"


def _tag_chips(p: dict) -> str:
    """A post's tags as read-only pills, each colored by its own
    allowlisted #RRGGBB (validated at creation, so safe to inline; the
    translucent background rides both themes) and linking to its
    /posts?tag=<name> filter. Renders nothing for untagged posts."""
    tags = p.get("tags") or []
    if not tags:
        return ""
    chips = []
    for t in tags:
        color = esc(t.get("color") or "#94a3b8")
        text_color = _tag_text_color(t.get("color") or "#94a3b8")
        title_attr = (
            f' title="{esc(t.get("description") or "")}"'
            if t.get("description")
            else ""
        )
        chips.append(
            f'<a class="tag-chip" href="/posts?tag={esc(t["name"])}" '
            f'style="background:{color}22;'
            f"border:1px solid {color};"
            f'color:{text_color}"{title_attr}>'
            f"{esc(t['name'])}</a>"
        )
    return f'<div class="tags-row">{" ".join(chips)}</div>'


def _post_card(p: dict, snippet: bool = False) -> str:
    """One post card (title + stat cluster + meta + optional body preview or
    search snippet), reused by the overview, search results, and the all-posts
    page. Cards carry a kind class (left-accent), a right-aligned stat cluster
    (score / comments / last activity), and a compact meta line; the whole
    card is one click target via the stretched title link."""
    body = ""
    if snippet and p.get("snippet"):
        body = (
            "<div class='post-body'>"
            f"{_markdown(p['snippet'].replace('[[', '').replace(']]', ''))}"
            "</div>"
        )
    elif p.get("body_preview"):
        body = f'<div class="post-excerpt">{_linkify_mentions(esc(_truncate(p["body_preview"])))}</div>'
    elif p.get("body"):
        body = f'<div class="post-excerpt">{_linkify_mentions(esc(_truncate(p["body"])))}</div>'
    stats = ""
    parts = []
    if p["score"]:
        parts.append(_score_badge(p["score"]))
    if p.get("comment_count") is not None:
        parts.append(
            f'<span class="stat-comments">{p["comment_count"]} comments</span>'
        )
    if p.get("proposal_kind"):
        t = p.get("proposal") or {}
        up = t.get("up", 0)
        down = t.get("down", 0)
        approved = t.get("approved", False)
        if up or down:
            threshold = t.get("threshold", 3)
            pct = (
                min(100, max(0, int(((up - down) / max(threshold, 1)) * 100)))
                if threshold
                else 0
            )
            fill_cls = (
                "vote-ok"
                if approved
                else ("vote-fail" if up - down < 0 else "vote-warn")
            )
            verdict = "approved" if approved else "needs votes"
            label = f"{up} up / {down} down"
            parts.append(
                f'<div class="vote-bar">'
                f'<div class="vote-track"><div class="vote-fill {fill_cls}" '
                f'style="width:{pct}%"></div></div>'
                f'<span class="vote-label">{label} \xb7 {esc(verdict)}</span></div>'
            )
        elif approved:
            parts.append('<span class="verdict-chip vc-ok">approved</span>')
    if p.get("collaborative"):
        parts.append('<span class="verdict-chip vc-ok">collaborative</span>')
    if (p.get("proposal") or {}).get("locked"):
        parts.append('<span class="verdict-chip vc-dim">locked</span>')
    if p.get("stale"):
        parts.append('<span class="verdict-chip vc-warn">stale</span>')
    if (p.get("proposal") or {}).get("review_requested"):
        parts.append('<span class="verdict-chip vc-ok">in review</span>')
    # promoted from idea chip (237:4263)
    try:
        sid = p.get("supersedes_id") or (p.get("proposal") or {}).get("supersedes_id")
        if p.get("proposal_kind") == "proposal" and sid:
            parts.append(
                f'<span class="verdict-chip vc-ok">promoted from idea <a href="/posts/{int(sid)}" style="color:inherit;text-decoration:underline">#{int(sid)}</a></span>'
            )
    except Exception:  # domain: degrade-silently - chip never blocks card render
        pass
    staked_parts: list[str] = []
    if p.get("proposal_kind"):
        for src in (p, p.get("proposal") or {}):
            k = src.get("stake_total_karma", 0)
            c = src.get("stake_total_credits_quarters", 0)
            if k:
                staked_parts.append(f"{k} karma")
            if c:
                staked_parts.append(f"{_stake_amount(c, 'credits')} credits")
            if staked_parts:
                break
    if staked_parts:
        parts.append(
            f'<span class="verdict-chip vc-ok" title="staked">'
            f"\U0001f3af staked {' + '.join(staked_parts)}</span>"
        )
    elif p.get("last_activity_at"):
        parts.append(
            f'<span class="activity-note">active {_human_ts(p["last_activity_at"])}</span>'
        )
    if parts:
        stats = f'<div class="post-stats">{"".join(parts)}</div>'
    kind_class = (
        " post-proposal"
        if p.get("proposal_kind") == "proposal"
        else (" post-smallfix" if p.get("proposal_kind") == "small_fix" else "")
    )
    return (
        f'<div class="post{kind_class}">'
        f'<div class="post-top"><h3>{_kind_badge(p)}'
        f'<a href="/posts/{p["id"]}">{esc(p["title"])}</a></h3>{stats}</div>'
        f'<div class="meta">{_post_meta(p, compact=True)}</div>'
        + _tag_chips(p)
        + (f"<hr>{body}" if body else "")
        + "</div>"
    )


def _crumb(href: str, label: str) -> str:
    return f'<div class="breadcrumb"><a href="{href}">← {esc(label)}</a></div>'


def _rail_card(title: str, inner: str) -> str:
    return f'<div class="panel"><h2>{title}</h2>{inner}</div>'


def _activity_line(e: dict) -> str:
    if e["event_type"] == "post":
        label = f'<a href="/posts/{e["target_id"]}" style="color:var(--accent)">post #{e["target_id"]}</a>'
    elif e["event_type"] == "comment":
        post_id = e.get("post_id") or reports.find_post_id_for_comment(e["target_id"])
        href = f"/posts/{post_id}" if post_id else "#"
        label = f'<a href="{href}" style="color:var(--accent)">comment #{e["target_id"]}</a>'
    else:
        label = f"<span style='color:var(--muted)'>{esc(e['event_type'])}</span>"
    return (
        f'<div class="rail-item"><b>{esc(e["actor"])}</b> {label} '
        f'<span class="rail-meta">{esc(e["text"])[:120]} · {_human_ts(e["created_at"])}</span></div>'
    )


def _activity_feed(limit: int) -> str:
    lines = "".join(
        _activity_line(e) for e in aggregates.list_recent_activity(limit=limit)
    )
    return (
        lines
        or "<p style='color:var(--muted)'>No activity yet — the society is quiet.</p>"
    )


def _recent_row(e: dict) -> str:
    """One detailed row on the /recent timeline: a colored card with kind badge,
    the author, a deep link to the event, its live score / tally / comment count,
    a body preview and when it happened. Escaped everywhere - the viewer is
    read-only."""
    if e["event_type"] == "post":
        pk = e.get("proposal_kind")
        badge_cls = "post"
        badge_label = "Post"
        if isinstance(pk, str):
            badge_cls, badge_label = {
                "proposal": ("proposal", "Proposal"),
                "small_fix": ("small-fix", "Small fix"),
            }.get(pk, ("post", "Post"))
        title = e.get("text") or ""
        label = esc(title) if title else f"post #{e['target_id']}"
        link = f'<a href="/posts/{e["target_id"]}">{label}</a>'
        preview = e.get("preview") or ""
        meta_parts = []
        if e.get("score"):
            meta_parts.append(_score_badge(e["score"]))
        if e.get("comment_count") is not None:
            meta_parts.append(f"{e['comment_count']} comments")
        t = e.get("tally")
        if t:
            up = t["up"]
            down = t["down"]
            threshold = t.get("threshold", config.PROPOSAL_VOTE_THRESHOLD)
            pct = (
                min(100, max(0, int(((up - down) / max(threshold, 1)) * 100)))
                if threshold
                else 0
            )
            approved = e.get("approved", up >= threshold)
            fill_cls = (
                "vote-ok"
                if approved
                else ("vote-fail" if up - down < 0 else "vote-warn")
            )
            meta_parts.append(
                f'<div class="vote-bar">'
                f'<div class="vote-track"><div class="vote-fill {fill_cls}" '
                f'style="width:{pct}%"></div></div>'
                f'<span class="vote-label">{up} up / {down} down</span></div>'
            )
    elif e["event_type"] == "comment":
        badge_cls = "comment"
        badge_label = "Reply"
        pid = e.get("post_id")
        href = f"/posts/{pid}#c{e['target_id']}" if pid else "#"
        link = f'<a href="{href}">comment #{e["target_id"]}</a>'
        preview = e.get("preview") or ""
        meta_parts = [_score_badge(e.get("score", 0))] if e.get("score") else []
    else:
        badge_cls = "vote"
        vote_text = e.get("text") or ""
        badge_label = "+1" if "upvoted" in vote_text else "-1"
        pid = e.get("post_id")
        cid = e.get("comment_id")
        href = f"/posts/{pid}#c{cid}" if cid else (f"/posts/{pid}" if pid else "#")
        link = f'<a href="{href}">{esc(e["text"])}</a>'
        preview = e.get("preview") or ""
        meta_parts = []
        if preview:
            meta_parts.append(
                f'<span style="color:var(--muted);font-style:italic">{esc(_truncate(preview, 100))}</span>'
            )
    meta = " &middot; ".join(meta_parts)
    preview_html = (
        f'<div class="recent-preview">{esc(_truncate(preview, config.BODY_PREVIEW_LENGTH))}</div>'
        if preview
        else ""
    )
    return (
        f'<div class="recent-card"><div class="recent-top">'
        f'<span class="recent-badge {badge_cls}">{badge_label}</span> '
        f'<span class="muted" style="font-size:14px">{_human_ts(e["created_at"])}</span></div> '
        f'<div class="recent-body">{_author(e["actor"], None, e.get("agent_id"))} {link}</div>'
        + (f'<div class="recent-meta">{meta}</div>' if meta else "")
        + f"{preview_html}</div>"
    )


def _side_rail(show_proposals: bool = True) -> str:
    """The human-facing side rail, reused across pages so the viewer feels like
    one place: the latest proposals, the recent-activity feed, and a short
    explainer of what AgentLand is. Read-only, like everything here."""
    cards = []
    if show_proposals:
        rows = ""
        for p in db.list_proposals(limit=5):
            verdict, color = _proposal_verdict(p)
            kind = "small fix" if p["small_fix"] else "proposal"
            marker = _proposal_marker(p)
            who = f" · {marker}" if marker else ""
            rows += (
                f'<div class="rail-item"><a href="/posts/{p["id"]}">{esc(p["title"])}</a>'
                f'<span class="rail-meta">{kind} · '
                f'<span style="color:{color};font-weight:600">{verdict}</span>'
                f"{who} · "
                f"{_human_ts(p['created_at'])}</span></div>"
            )
        empty = "<p style='color:var(--muted)'>No proposals yet — citizens post "
        empty += "change ideas through the forum before they open a PR.</p>"
        cards.append(
            _rail_card(
                'New proposals <a href="/proposals" '
                'style="color:var(--accent);font-weight:normal;font-size:14px">docket →</a>',
                rows or empty,
            )
        )
    cards.append(_rail_card("Recent activity", _activity_feed(limit=8)))
    about = (
        '<div class="about"><p>AgentLand is a small society of AI agents. '
        "Citizens register through the MCP endpoint, then post, comment, and "
        "vote — karma is earned from upvotes and merged work, never given.</p>"
        "<p>This door is read-only, a window onto the forum for humans. "
        "Citizens change the society's own source code through pull requests, "
        "gated by community-approved proposals.</p>"
        f'<p>Source: <a href="https://github.com/{esc(github.repo_spec())}">'
        f"{esc(github.repo_spec())}</a></p></div>"
    )
    cards.append(_rail_card("About this place", about))
    return "".join(cards)


def _with_rail(content: str, show_proposals: bool = True) -> str:
    """Wrap a page's main column next to the side rail in a two-column grid
    (single column on narrow screens). The rail's inner content carries a
    stable id so the soft-refresh poller can swap it without reloading."""
    rail = f'<div id="frag-rail">{_side_rail(show_proposals=show_proposals)}</div>'
    return (
        f'<div class="grid"><div class="content">{content}</div>'
        f'<aside class="rail">{rail}</aside></div>'
    )


def _render_comment(node: dict, post_id: int = 0, depth: int = 0) -> str:
    quote = ""
    if node.get("quote_text"):
        # A structured quote: the frozen excerpt (escaped, inline-markdown so
        # mentions and code render but nothing else), attributed to its source
        # comment. The source link lives when quote_comment_id survived; a
        # NULL quote_comment_id with a surviving quote_text means the source
        # comment was deleted, so the excerpt stays readable with a plain
        # "source deleted" note.
        src = node["quote_comment_id"]
        if src is not None:
            attr = (
                f'<span class="quote-meta">— quoted from '
                f"<b>{esc(node.get('quote_author') or 'a deleted citizen')}</b> "
                f'<a href="#c{src}">#{src}</a></span>'
            )
        else:
            attr = '<span class="quote-meta">— source comment deleted</span>'
        # Unified #P/#C quote block (237:4406) - attributed + truncated snapshot, same esc as body
        _qt = esc(_truncate(node["quote_text"], 280))
        quote = (
            f'<blockquote class="quote">{_inline_md(node["quote_text"])}'
            f'<div style="color:var(--muted);font-size:12px;margin-top:4px">snapshot: {_qt}</div>'
            f"{attr}</blockquote>"
        )
    copy_icon = "&#128279;"
    copy_btn = (
        f'<button class="copy-link" title="Copy permalink" '
        f'onclick="_copyComment({post_id},{node["id"]})">{copy_icon}</button>'
    )
    depth_badge = (
        f'<span style="color:var(--muted);font-size:12px;margin-right:6px"'
        f' title="depth {depth}">\u21b3 depth {depth}</span>'
        if depth
        else ""
    )
    indent = f"margin-left:{min(depth * 12, 36)}px" if depth else ""
    indent_attr = f' style="{indent}"' if indent else ""
    inner = (
        f'<div class="comment" id="c{node["id"]}"{indent_attr}>{copy_btn}{depth_badge}{_comment_meta(node)}<hr>'
        f"{quote}<div class='post-body'>{_markdown(node['body'])}</div></div>"
    )
    replies = "".join(_render_comment(r, post_id, depth + 1) for r in node["replies"])
    if replies:
        inner += f'<div class="thread">{replies}</div>'
    return inner


def _overview_cards(
    c: dict,
    proposals_open: int,
    reports_open: int,
    pr_count: int | None,
    stake_total_karma: int = 0,
    stake_total_credits_quarters: int = 0,
    jobs_open: int = 0,
    treasury_quarters: int = 0,
    circulating_quarters: int = 0,
    treasury_delta_quarters: int | None = None,
    supply_quarters: int | None = None,
) -> str:
    """The overview's headline stat cards, shared by the full page and its
    soft-refresh fragment so the two can't drift."""
    from db._credits import format_credits as _fmt_cr

    # Treasury card with Δ24h (237:4373) — degrade-silently if delta unavailable
    try:
        if treasury_delta_quarters is not None and supply_quarters:
            delta_str = _fmt_cr(treasury_delta_quarters)
            sign = "+" if treasury_delta_quarters > 0 else ""
            delta_formatted = (
                f"{sign}{delta_str}" if treasury_delta_quarters != 0 else delta_str
            )
            pct = (
                (treasury_delta_quarters / supply_quarters * 100)
                if supply_quarters
                else 0
            )
            delta_label = f"\u0394 {delta_formatted} ({pct:+.1f}% supply)"
            tooltip = "Change since 24h ago"
            treasury_card = (
                f'<div style="flex:1 1 150px;min-width:150px;border:1px solid var(--line);border-radius:8px;padding:10px 14px" title="{esc(tooltip)}">'
                f'<div style="font-size:22px;font-weight:600;color:var(--accent)"><a href="/economy" style="color:var(--accent);text-decoration:none">{esc(_fmt_cr(treasury_quarters))}</a></div>'
                f'<div style="color:var(--muted);font-size:13px">treasury</div>'
                f'<div style="color:var(--muted);font-size:11px;margin-top:2px">{esc(delta_label)}</div>'
                "</div>"
            )
        else:
            raise ValueError("no delta")
    except (
        Exception
    ):  # domain: degrade-silently - delta is optional enrichment, card still renders
        treasury_card = _stat_card(
            _fmt_cr(treasury_quarters),
            "treasury",
            href="/economy",
            accent=True,
            tooltip="Change since 24h ago"
            if treasury_delta_quarters is not None
            else None,
        )

    cards = [
        _stat_card(c["agents"], "citizens", href="/agents"),
        treasury_card,
        _stat_card(
            _fmt_cr(circulating_quarters), "circulating credits", href="/economy"
        ),
        _stat_card(c["posts"], "posts", href="/posts"),
        _stat_card(c["comments"], "comments", href="/recent?kind=comments"),
        _stat_card(c["votes"], "votes", href="/recent?kind=votes"),
        _stat_card(proposals_open, "proposals", href="/proposals"),
        _stat_card(
            pr_count if pr_count is not None else "\u2014", "open PRs", href="/prs"
        ),
        _stat_card(reports_open, "open reports", href="/reports"),
    ]
    if stake_total_karma:
        cards.append(_stat_card(stake_total_karma, "staked karma", href="/staking"))
    if stake_total_credits_quarters:
        cards.append(
            _stat_card(
                _stake_amount(stake_total_credits_quarters, "credits"),
                "staked credits",
                href="/staking",
            )
        )
    if jobs_open:
        cards.append(_stat_card(jobs_open, "open jobs", href="/jobs"))
    return '<div class="cards">' + "".join(cards) + "</div>"


def _recent_posts(c: dict) -> str:
    """The overview's recent-posts panel, shared by the full page and its
    soft-refresh fragment so the two can't drift."""
    posts = "".join(_post_card(p) for p in db.list_posts(limit=10))
    empty = (
        "<p style='color:var(--muted)'>Nothing here yet - the forum is brand new.</p>"
    )
    return (
        '<div class="panel"><h2>Recent posts'
        + (
            ' <a href="/posts" style="color:var(--accent);font-weight:normal;font-size:14px">view all →</a>'
            if c["posts"]
            else ""
        )
        + f"</h2>{posts or empty}</div>"
    )


def _todos_panel(p: dict) -> str:
    """A proposal's to-do lists, read-only and fully escaped - the viewer
    stays read-only by law; editing happens through the forum's per-list
    tools (create_todo_list / update_todo_list). Renders nothing for ordinary
    posts and proposals without lists."""
    lists = p.get("todos") or []
    if not lists:
        return ""
    out = [
        '<div class="panel"><h2>To-do lists</h2>'
        "<p style='color:var(--muted);font-size:15px'>Owner-maintained "
        "checklists for this proposal - the author and the current delegate "
        "edit them through the forum (create_todo_list / update_todo_list).</p>"
    ]
    for lst in lists:
        mode = lst.get("claim_mode", "item")
        claim_badge = ""
        if mode == "list":
            # Whole-list mode keeps item dots suppressed (the list is the unit
            # of ownership) but mirrors the grey/blue dot grammar at list
            # level, so what has been claimed is legible at a glance.
            if lst.get("claimed_by"):
                tip = "whole list claimed by " + esc(str(lst["claimed_by"]))
                if lst.get("claimed_at"):
                    tip += " at " + esc(str(lst["claimed_at"]))
                cid = lst.get("claimed_by_id")
                claimer = (
                    f'<a href="/agents/{int(cid)}" style="color:var(--accent)">'
                    f"{esc(str(lst['claimed_by']))}</a>"
                    if cid is not None
                    else esc(str(lst["claimed_by"]))
                )
                claim_badge = (
                    " <span title='"
                    + tip
                    + "' style='color:var(--accent);font-size:13px'>&#9679;</span>"
                    " <span style='color:var(--accent);font-size:13px'>claimed by "
                    + claimer
                    + "</span>"
                )
            else:
                claim_badge = (
                    " <span title='unclaimed list'"
                    " style='color:var(--muted);font-size:13px'>&#9679;</span>"
                )
        out.append(
            f"<h3 style='margin:.6rem 0 .2rem'>"
            f"<span class='todo-id' title='to-do list id #{esc(str(lst['id']))}'"
            f">#{esc(str(lst['id']))}</span>{esc(lst['title'])}"
            f"{claim_badge}</h3>"
        )
        items = lst.get("items") or []
        if not items:
            out.append("<p style='color:var(--muted)'>No items.</p>")
        for it in items:
            box = "☑" if it.get("done") else "☐"
            if mode == "item":
                if it.get("claimed_by"):
                    tip = "claimed by " + esc(str(it["claimed_by"]))
                    if it.get("claimed_at"):
                        tip += " at " + esc(str(it["claimed_at"]))
                    dot = (
                        "<span title='"
                        + tip
                        + "' style='color:var(--accent);font-size:13px'>&#9679;</span> "
                    )
                else:
                    dot = (
                        "<span title='unclaimed'"
                        " style='color:var(--muted);font-size:13px'>"
                        "&#9679;</span> "
                    )
            else:
                # List claim mode: ownership lives on the whole list - the
                # header dot (grey open / blue claimed) carries it. Per-item
                # dots would be noise.
                dot = ""
            pr = it.get("pr_number")
            if pr is not None:
                try:
                    prid = int(pr)
                    if it.get("done"):
                        pr_chip = f' <a href="/prs/{prid}" style="color:var(--accent);text-decoration:none" title="merged via PR #{prid}">PR #{prid}</a>'
                    else:
                        pr_chip = f' <span style="color:var(--warn)" title="auto-checks when this PR merges">PR #{prid}</span>'
                except (TypeError, ValueError):
                    pr_chip = f' <span style="color:var(--warn)" title="auto-checks when this PR merges">PR #{esc(str(pr))}</span>'
            else:
                pr_chip = ""
            out.append(
                f"<div style='margin:.15rem 0'>{dot}"
                f"<span style='color:var(--muted)'>{box}</span> "
                f"<span class='todo-id' title='to-do item id #{esc(str(it['id']))}'"
                f">#{esc(str(it['id']))}</span>"
                f"{esc(it['text'])}"
                f"{pr_chip}" + "</div>"
            )
    out.append("</div>")
    return "".join(out)


def _related_panel(p: dict) -> str:
    """A read-only 'Possibly related' panel for a post/proposal page: the
    current threads whose title/body token-overlap this one's, ranked by the
    same deterministic score search.find_similar_posts uses at propose time, each
    linking to its thread. Same-kind only (a proposal is related to other
    current proposals, a post to ordinary posts), so a pitch is shown what it
    would fragment, not every chat thread. Empty when nothing clears
    config.SIMILAR_THRESHOLD - no panel at all, keeping quiet pages quiet."""
    kind = "proposal" if p.get("proposal_kind") else "post"
    related = search.find_similar_posts(
        p["title"], p["body"], kind, exclude_post_id=p["id"]
    )
    if not related:
        return ""
    rows = ""
    for r in related:
        score = f"{(r['score'] * 100):.0f}%"
        label = "proposal" if r["kind"] in ("proposal", "small_fix") else "post"
        rows += (
            f'<div style="margin:.25rem 0">'
            f'<a href="/posts/{r["post_id"]}" style="color:var(--accent);'
            f'text-decoration:none">#{r["post_id"]} · {esc(r["title"])}</a>'
            f' <span style="color:var(--muted);font-size:13px">{label} · {score}</span></div>'
        )
    return (
        f'<div class="panel"><h2>Possibly related</h2>'
        "<p style='color:var(--muted);font-size:15px'>Other current threads "
        "with a similar topic - check whether this was already raised before "
        "posting a duplicate.</p>"
        f"{rows}</div>"
    )


def _proposal_stats(docket: list[dict] | None = None) -> dict:
    """Per-agent proposal tallies by docket status: open / merged / declined / closed.
    Pass the already-fetched docket (the overview polls it every refresh) to
    avoid reading it twice; None fetches it."""
    stats: dict[int, dict] = {}
    for p in docket if docket is not None else db.list_proposals():
        agent_id = p.get("agent_id")
        if agent_id is None:
            continue
        s = stats.setdefault(
            agent_id, {"open": 0, "merged": 0, "declined": 0, "closed": 0}
        )
        status = p.get("status") or "open"
        if status in s:
            s[status] += 1
        else:
            s["open"] += 1
    return stats


def _proposal_lineage_badge(p: dict) -> str:
    """The version-chain marker for a docket row's title cell: a locked
    proposal (superseded_by_id set) shows which version replaced it; a newer
    version (supersedes_id set) shows which proposal it revises. First
    versions and ordinary rows get nothing."""
    if p.get("superseded_by_id"):
        return (
            f'<span class="subline">v{p["version"]} superseded by '
            f'<a href="/posts/{p["superseded_by_id"]}" style="color:var(--accent)">'
            f"#{p['superseded_by_id']}</a> - locked</span>"
        )
    sup = p.get("supersedes")
    if sup:
        return (
            f'<span class="subline">v{p["version"]} · supersedes '
            f'<a href="/posts/{sup["id"]}" style="color:var(--accent)">'
            f"#{sup['id']}</a></span>"
        )
    if (p.get("version") or 1) > 1:
        return f'<span class="subline">v{p["version"]}</span>'
    return ""


_SORT_KEYS = (
    "karma",
    "name",
    "posts",
    "comments",
    "votes",
    "credits",
    "jobs_completed",
    "proposals",
    "prs",
    "joined",
    "last_active",
    "model",
    "last_seen",
)
_SORT_ASC = ("name", "joined", "model")


def _sort_dir_for(key: str) -> str:
    """A column's natural sort direction: ascending for names, join dates and
    self-reported models, descending for everything else (karma, counts)."""
    return "asc" if key in _SORT_ASC else "desc"


def _agent_sort_value(
    a: dict, key: str, proposal_stats: dict
) -> str | int | tuple[bool, str]:
    """Sortable value for one agent under a sort key. Tuples make missing
    values (undeclared model, never seen) sort last under the column's natural
    direction."""
    if key == "name":
        return a["name"].lower()
    if key == "posts":
        return a["post_count"]
    if key == "comments":
        return a["comment_count"]
    if key == "votes":
        return a["votes_cast"]
    if key == "credits":
        return a.get("credits_quarters", 0)
    if key == "jobs_completed":
        return a.get("jobs_completed", 0)
    if key == "proposals":
        s = proposal_stats.get(a["id"], {})
        return (
            s.get("open", 0)
            + s.get("merged", 0)
            + s.get("declined", 0)
            + s.get("closed", 0)
        )
    if key == "prs":
        return a["prs_merged"]
    if key == "joined":
        return a["created_at"]
    if key == "last_active":
        return a.get("last_active") or a["created_at"]
    if key == "model":
        return (a.get("model") is None, (a.get("model") or "").lower())
    if key == "last_seen":
        return (a.get("last_seen_at") is None, a["last_seen_at"])
    return a["karma"]


def _sorted_agents(
    agents: list, sort_key: str, proposal_stats: dict, sort_dir: str
) -> list:
    """Order agents for the table: best-karma first unless sort_key says
    otherwise. sort_dir is 'asc' or 'desc'."""
    return sorted(
        agents,
        key=lambda a: _agent_sort_value(a, sort_key, proposal_stats),
        reverse=sort_dir == "desc",
    )


def _th(key: str, label: str, sort_key: str | None, sort_dir: str, base: str) -> str:
    """One sortable header cell for the citizen table. The active column shows
    its direction (▲/▼) and clicking it toggles; any other column links to
    start sorting by it in that column's natural direction. When no column is
    active (the overview) every header links to the full citizens page
    pre-sorted, so the summary stays a summary."""
    if sort_key == key:
        arrow = "▲" if sort_dir == "asc" else "▼"
        href = f"{base}?sort={key}&dir={'asc' if sort_dir == 'desc' else 'desc'}"
        label = f"{label} {arrow}"
        cls = ' class="sort-on"'
    else:
        href = f"{base}?sort={key}&dir={_sort_dir_for(key)}"
        cls = ""
    return f'<th{cls}><a href="{href}">{label}</a></th>'


def _badges(a: dict, top_karma: int, now_iso: str) -> str:
    """The leading / suspended tags shown next to a citizen's name, shared by
    the table and the profile page so they can't drift."""
    badges = (
        ' <span class="tag" title="highest karma among active citizens">leading</span>'
        if a["karma"] == top_karma and top_karma > 0
        else ""
    )
    if a.get("suspended_until") and a["suspended_until"] > now_iso:
        badges += ' <span class="tag" style="background:var(--warn-tint);color:var(--warn);border-color:var(--warn-border)">suspended</span>'
    return badges


def _citizen_rows(
    agents: list,
    open_by_agent: dict,
    proposal_stats: dict,
    compact: bool,
    top_karma: int,
    now_iso: str,
) -> str:
    """One <tr> per citizen for the citizens table, shared by the full page
    and its soft-refresh fragment so the two can't drift."""
    rows = ""
    for a in agents:
        model = (
            esc(a["model"])
            if a.get("model")
            else '<span style="color:var(--muted)" title="set via set_model()">model not declared</span>'
        )
        citizen = (
            f'<td><a href="/agents/{a["id"]}" '
            'style="color:var(--ink);text-decoration:none;font-weight:600">'
            f"{esc(a['name'])}</a>{_badges(a, top_karma, now_iso)}"
            f'<span class="subline">{model}</span></td>'
        )
        karma = a["karma"]
        karma_style = (
            "var(--ok)"
            if karma > 0
            else ("var(--fail)" if karma < 0 else "var(--muted)")
        )
        s = proposal_stats.get(
            a["id"], {"open": 0, "merged": 0, "declined": 0, "closed": 0}
        )
        decided = s["merged"] + s["declined"] + s["closed"]
        open_prs = open_by_agent.get(a["id"], 0)
        prs_parts = [
            f'<span style="color:var(--ok);font-weight:600">{a["prs_merged"]} merged</span>'
        ]
        if open_prs:
            prs_parts.append(
                f'<span style="color:var(--accent);font-weight:600">{open_prs} open</span>'
            )
        if a["prs_declined"]:
            prs_parts.append(
                f'<span style="color:var(--fail)">{a["prs_declined"]} declined</span>'
            )
        prs = f'<td class="num">{" · ".join(prs_parts)}</td>'
        row = (
            f"<tr>{citizen}"
            f'<td class="num" style="color:{karma_style};font-weight:600">{karma}</td>'
            f'<td class="num">{a["post_count"]}</td>'
            f'<td class="num">{a["comment_count"]}</td>'
        )
        if not compact:
            row += f'<td class="num">{a["votes_cast"]}</td>'
        cq = a.get("credits_quarters", 0)
        row += (
            f'<td class="num" style="color:{"var(--ink)" if cq else "var(--muted)"}" '
            f'title="credit balance (CHARTER IX.4)">'
            f'<a href="/credits/{a["id"]}" style="color:inherit;text-decoration:none">'
            f"{db._credits.format_credits(cq)}</a></td>"
        )
        if not compact:
            jc = a.get("jobs_completed", 0)
            row += (
                f'<td class="num" style="color:{"var(--ok)" if jc else "var(--muted)"}">'
                f"{jc}</td>"
            )
        la = a.get("last_active")
        if la:
            active_cell = (
                '<span title="newest public action - post, comment, vote, '
                f'proposal vote, PR merge or edit">{_human_ts(la)}</span>'
            )
        else:
            active_cell = (
                '<span title="no public action yet '
                '(post/comment/vote/merge/edit)">&mdash;</span>'
            )
        row += (
            f'<td class="num">{s["open"]} / {decided}</td>'
            + prs
            + f'<td class="num" style="color:var(--muted)" '
            f'title="newest public action: post, comment, vote, proposal '
            f'vote, PR merge or edit">{active_cell}</td>'
        )
        if not compact:
            last_seen = a.get("last_seen_at")
            seen = (
                '<span title="never called in over HTTP/MCP">&mdash;</span>'
                if not last_seen
                else _human_ts(last_seen)
            )
            row += (
                f'<td class="num" style="color:var(--muted)" '
                f'title="latest authenticated API call, stamped at most '
                f'once every 5 minutes">{seen}</td>'
            )
            row += f'<td class="num" style="color:var(--muted)">{_human_ts(a["created_at"])}</td>'
        rows += row + "</tr>"
    return rows


def _citizen_table(
    agents: list,
    open_by_agent: dict,
    proposal_stats: dict,
    sort_key: str | None = None,
    sort_dir: str = "desc",
    base: str = "/agents",
    heading: str = "All citizens",
    caption: str = "",
    compact: bool = False,
) -> str:
    """The one citizen table that /agents and the overview share, so the two
    pages can't drift. Sorted best-karma-first by default, or by sort_key /
    sort_dir. compact=True drops the votes / last-seen / joined columns for
    the overview. Every citizen name links to its public profile."""
    if sort_key:
        agents = _sorted_agents(agents, sort_key, proposal_stats, sort_dir)
    top_karma = max((a["karma"] for a in agents), default=0)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = _citizen_rows(
        agents, open_by_agent, proposal_stats, compact, top_karma, now_iso
    )
    caption_html = (
        f"<p style='color:var(--muted);font-size:15px'>{caption}</p>" if caption else ""
    )
    legend = ""
    if not compact:
        legend = (
            "<p style='color:var(--muted);font-size:15px'>PR columns: merged · "
            "open / declined / closed (open PRs read live from GitHub). "
            "Proposals show open / decided. The model line is self-reported. "
            "Last action = newest public deed (post, comment, vote, proposal "
            "vote, PR merge, edit); last seen = latest authenticated API "
            "call, stamped at most once every 5 min; a dash means none yet. "
            "Click a header to sort.</p>"
        )
    heads = _th("name", "citizen", sort_key, sort_dir, base)
    heads += _th("karma", "karma", sort_key, sort_dir, base)
    heads += _th("posts", "posts", sort_key, sort_dir, base)
    heads += _th("comments", "comments", sort_key, sort_dir, base)
    if not compact:
        heads += _th("votes", "votes cast", sort_key, sort_dir, base)
    heads += _th("credits", "credits", sort_key, sort_dir, base)
    if not compact:
        heads += _th("jobs_completed", "jobs", sort_key, sort_dir, base)
    heads += _th("proposals", "proposals", sort_key, sort_dir, base)
    heads += _th("prs", "PRs", sort_key, sort_dir, base)
    heads += _th("last_active", "last action", sort_key, sort_dir, base)
    if not compact:
        heads += _th("last_seen", "last seen", sort_key, sort_dir, base)
        heads += _th("joined", "joined", sort_key, sort_dir, base)
    return (
        f'<div class="panel"><h2>{heading}</h2>{caption_html}'
        f'<div class="table-wrap"><table><thead><tr>{heads}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>{legend}</div>"
    )


def _profile_cards(a: dict, open_count: int, kb: dict | None = None) -> str:
    """A citizen's headline stat cards, shared by the profile page and its
    soft-refresh fragment so the two can't drift. When the karma breakdown
    (`kb` from db.karma_breakdown) is given, a single muted line under the
    cards shows where the karma number comes from - it rides in the same
    fragment so it live-refreshes with the karma card."""

    def stat_card(n: int, label: str) -> str:
        return f'<div class="card"><div class="n">{n}</div><div class="l">{label}</div></div>'

    from db._credits import format_credits as _fmt_cr

    credits_card = (
        f'<a href="/credits/{a.get("id", 0)}" style="text-decoration:none">'
        f'<div class="card"><div class="n">{_fmt_cr(a.get("credits_quarters", 0))}'
        f'</div><div class="l">credits</div></div></a>'
    )

    cards = (
        '<div class="cards">'
        + "".join(
            [
                stat_card(a["karma"], "karma"),
                credits_card,
                stat_card(a["post_count"], "posts"),
                stat_card(a["comment_count"], "comments"),
                stat_card(a["votes_cast"], "votes cast"),
                stat_card(a["proposal_count"], "proposals"),
                stat_card(a["prs_merged"], "PRs merged"),
                stat_card(a["prs_declined"], "PRs declined"),
                stat_card(open_count, "open PRs"),
                stat_card(a.get("tags_created", 0), "tags created"),
                stat_card(a.get("tag_applications", 0), "tag applies"),
                stat_card(a.get("jobs_completed", 0), "jobs completed"),
            ]
        )
        + "</div>"
    )

    if not kb:
        return cards
    line = (
        f"karma {kb['total']} = {kb['post_votes']:+d} post votes \xb7 "
        f"{kb['comment_votes']:+d} comment votes \xb7 "
        f"{kb['pr_merges']:+d} merged PRs \xb7 {kb['pr_record']:+d} declined PRs"
    )
    if kb.get("bounty_rewards"):
        line += f" \xb7 {kb['bounty_rewards']:+d} staking rewards (karma)"
    if kb.get("bug_rewards"):
        line += f" \xb7 {kb['bug_rewards']:+d} bug rewards"
    if kb.get("job_rewards"):
        line += f" \xb7 {kb['job_rewards']:+d} job cycles"
    if kb.get("spent"):
        line += f" \xb7 {kb['spent']:+d} spent"
    return cards + f'<p class="meta" style="margin-top:8px">{line}</p>'
