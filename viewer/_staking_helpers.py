"""
viewer/_staking_helpers.py - Staking fragment builders (stake panels, /staking rows, lock detail, summary

Staking fragment builders (stake panels, /staking rows, lock detail, summary
card). Split out of the former viewer/_helpers.py (which grew too large). Pure
HTML builders - no route handlers.
"""

from __future__ import annotations

import time
from typing import Any

import db
from db._staking import list_stake_locks
from viewer._utils import (
    _human_ts,
    esc,
)

_STAKE_SUMMARY_CACHE_SECONDS = 60.0
_stake_summary_cache: dict[str, Any] = {"ts": 0.0, "html": None}


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
    # single pass — was 4× list scans
    avail_karma = avail_cred = locked_karma = locked_cred = 0
    for b in stakes:
        if b["status"] != "active":
            continue
        cur = b.get("currency", "karma")
        rem = b["max_prs"] - b["paid_count"] - b["locked_count"]
        if cur == "karma":
            avail_karma += b["per_pr"] * rem
            locked_karma += b["per_pr"] * b["locked_count"]
        else:
            avail_cred += b["per_pr"] * rem
            locked_cred += b["per_pr"] * b["locked_count"]
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
    and paid amounts across all active stakes, split by currency. Cached 60s
    like _governance/_pulse to avoid per-request `list_all_stakes`."""
    now = time.monotonic()
    cached_ts = _stake_summary_cache["ts"]
    cached_html = _stake_summary_cache["html"]
    if (
        cached_html is not None
        and isinstance(cached_ts, (int, float))
        and now - float(cached_ts) < _STAKE_SUMMARY_CACHE_SECONDS
    ):
        return str(cached_html)
    stakes = db.list_all_stakes(status="active")
    if not stakes:
        _stake_summary_cache.update(ts=now, html="")
        return ""

    # single pass — was 6× scans via _sum/getattr_b
    ka = ca = kl = cl = kp = cp = 0
    for b in stakes:
        if b["status"] != "active":
            # still count paid for summary? paid is tracked regardless of active? keep active filter like before
            continue
        cur = b.get("currency", "karma")
        rem = b["max_prs"] - b["paid_count"] - b["locked_count"]
        if cur == "karma":
            ka += b["per_pr"] * rem
            kl += b["per_pr"] * b["locked_count"]
            kp += b["per_pr"] * b["paid_count"]
        else:
            ca += b["per_pr"] * rem
            cl += b["per_pr"] * b["locked_count"]
            cp += b["per_pr"] * b["paid_count"]
    if not (ka or ca or kl or cl or kp or cp):
        _stake_summary_cache.update(ts=now, html="")
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
    html = (
        '<div class="panel"><h2>Staking \xb7 '
        '<a href="/staking" style="color:var(--accent);font-weight:normal;font-size:14px">view all \u2192</a></h2>'
        '<p class="meta">'
        + str(len(stakes))
        + " active stakes \xb7 "
        + " \xb7 ".join(parts)
        + "</p>"
        "</div>"
    )
    _stake_summary_cache.update(ts=now, html=html)
    return html
