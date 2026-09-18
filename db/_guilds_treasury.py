"""db._guilds_treasury — guild↔treasury flows (proposal #525, PR-4).

Stakes, upkeep, and arrears on top of the PR-2 engine and PR-3 money.
Conservation model (unchanged): pool quarters are a memo - deposits park
in the treasury, payouts grant back down, grant-first everywhere.

Guild stakes ride the v1 machinery through a founder-conduit: the stake
row is an ordinary founder staker row (locks deduct the founder's
wallet, exactly like v1), and the pool funds each lock just-in-time
(pool memo + conduit grant) while payouts/refunds redirect poolward.
The founder nets ~zero throughout; the pool bears the economics. No
upfront funding (which would strand pool money in the founder's wallet
when locks never come), no signature surgery on v1 (one additive flag).

Upkeep is a weekly sweep (poller wiring lands in PR-5, like the
membership sweep): Monday-issue fee invoices per member (system
issuance: no create fee, no karma floor), Wednesday-sweep the pool
share to the treasury, suspension on shortfall with 14d grace to
auto-disband, and arrears that withhold from later payouts.
"""

from __future__ import annotations

import sqlite3

import logutil
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._guilds import (
    _age_days,
    _days_ago_iso,
    _member_count,
    _require_founder,
    _require_guild,
    guild_balance,
)
from db._guilds_money import _founder_guild_for
from notifications import _notify


def _week_key() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%G-W%V")


def _guild_stake_link(conn: sqlite3.Connection, stake_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM guild_stake_links WHERE stake_id = ?", (int(stake_id),)
    ).fetchone()
    return dict(row) if row is not None else None


def _guild_exposure(
    conn: sqlite3.Connection, guild_id: int, proposal_id: int | None = None
) -> int:
    """Committed guild-stake exposure in quarters: per_pr x (max_prs -
    paid) over active linked stakes, optionally for one proposal. Locks
    draw it down one per-PR at a time; paid PRs release it."""
    params: list = [guild_id]
    extra = ""
    if proposal_id is not None:
        extra = " AND s.proposal_id = ?"
        params.append(proposal_id)
    row = conn.execute(
        "SELECT COALESCE(SUM(s.per_pr * (s.max_prs - s.paid_count)), 0)"
        " FROM proposal_stakes s JOIN guild_stake_links l"
        " ON l.stake_id = s.id WHERE l.guild_id = ? AND s.status = 'active'" + extra,
        params,
    ).fetchone()
    return int(row[0] or 0)


def _unpaid_arrears(
    conn: sqlite3.Connection, guild_id: int, agent_id: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM guild_fee_arrears WHERE guild_id = ? AND member_agent_id = ?"
        " AND status = 'open' ORDER BY week ASC, id ASC",
        (guild_id, agent_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _apply_arrears_withhold(
    conn: sqlite3.Connection, guild_id: int, agent_id: int, payout: int
) -> tuple[int, int]:
    """Fee-arrears block/reduce: withhold up to the unpaid arrears from a
    payout (withdrawals, leave/heartbeat payouts, distributions alike),
    settling oldest weeks first. The withheld share stays pool-owned -
    the pool deducts the full share while the recipient nets the rest,
    so the debt clears without a second movement. Returns (net, settled).
    Whole rows only (every arrears row is exactly 1 quarter)."""
    if payout <= 0:
        return (0, 0)
    rows = _unpaid_arrears(conn, guild_id, agent_id)
    owed = sum(r["quarters"] for r in rows)
    if owed <= 0:
        return (payout, 0)
    remaining = min(payout, owed)
    settled = 0
    for row in rows:
        if remaining < row["quarters"]:
            break
        conn.execute(
            "UPDATE guild_fee_arrears SET status = 'paid' WHERE id = ?",
            (row["id"],),
        )
        remaining -= row["quarters"]
        settled += row["quarters"]
    return (payout - settled, settled)


# ── guild stakes ───────────────────────────────────────────────────────


def guild_stake(
    token: str,
    proposal_id: int,
    per_pr_credits: float,
    max_prs: int,
    bonus_pct: int = 0,
) -> dict:
    """Stake pool quarters on a proposal (credits only). The founder
    stakes as conduit - v1 lock mechanics run untouched - while the pool
    funds each lock just-in-time and takes the winnings. Caps read the
    pool, not the founder: exposure per proposal <= 33% of balance,
    total committed < 75% of balance. Winnings default 100% pool with an
    optional 0-50% opener bonus fixed ex ante. Staking is spending:
    unlocked roster, co-sign band recorded, velocity-exempt (escrowed)."""
    from db._proposal_status import _proposal_status_for
    from db._staking import _normalize_per_pr

    if int(bonus_pct) < 0 or int(bonus_pct) > 50:
        raise ForumError("opener bonus is 0-50% (ex ante).")
    if int(max_prs) < 1:
        raise ForumError("max_prs must be at least 1.")
    per_pr = _normalize_per_pr(float(per_pr_credits), "credits")
    total = per_pr * int(max_prs)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, _founder_guild_for(conn, agent["id"]))
        _require_founder(conn, guild, agent["id"])
        gid = guild["id"]
        post = conn.execute(
            "SELECT id, proposal_kind, superseded_by_id FROM posts WHERE id = ?",
            (int(proposal_id),),
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {proposal_id}.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                f"proposal #{proposal_id} is locked (superseded) and cannot"
                " accept new stakes."
            )
        if _proposal_status_for(conn, int(proposal_id)) != "open":
            raise ForumError(
                f"proposal #{proposal_id} is not open - stakes need an open proposal."
            )
        from db._guilds_money import _cosign_covering, _needs_cosign

        balance = guild_balance(conn, gid)
        if guild.get("spending_suspended"):
            raise ForumError(
                f"guild {guild['name']!r} is suspended for upkeep shortfall -"
                " staking waits for recovery."
            )
        if _member_count(conn, gid) < 2:
            raise ForumError(
                f"guild {guild['name']!r} holds fewer than 2 members -"
                " staking is re-locked."
            )
        if balance < total:
            raise ForumError("the pool does not cover that exposure.")
        if (_guild_exposure(conn, gid, int(proposal_id)) + total) * 100 > 33 * balance:
            raise ForumError(
                "that stake would breach the 33% single-proposal cap on pool exposure."
            )
        if (_guild_exposure(conn, gid) + total) * 100 >= 75 * balance:
            raise ForumError(
                "that stake would breach the 75% total-lock cap on pool exposure."
            )
        if _needs_cosign(balance, total) and not _cosign_covering(conn, gid, total):
            raise ForumError(
                "that exposure exceeds the founder's solo band - record a"
                " co-sign first (request_guild_cosign + confirm)."
            )
        from db._credits import fee_quarters

        placement_q = fee_quarters(total)
        if placement_q:
            conn.execute(
                "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
                " note) VALUES (?, 'fee', ?, ?, 'stake placement fee')",
                (gid, placement_q, agent["id"]),
            )
        from db._staking import stake as _v1_stake

        # Same transaction (conn passes through): checks, insert, link,
        # and memo commit atomically - a separate connection would
        # deadlock against this write lock.
        out = _v1_stake(
            token,
            int(proposal_id),
            per_pr / 4,
            int(max_prs),
            currency="credits",
            funded_externally=True,
            conn=conn,
        )
        conn.execute(
            "INSERT INTO guild_stake_links (stake_id, guild_id, opener_bonus_pct)"
            " VALUES (?, ?, ?)",
            (out["stake_id"], gid, int(bonus_pct)),
        )
        import events

        events.log_event(
            events.EVT_GUILD_STAKE_PLACED,
            actor_agent_id=agent["id"],
            target_type="proposal_stake",
            target_id=out["stake_id"],
            detail={
                "guild_id": gid,
                "proposal_id": int(proposal_id),
                "per_pr": per_pr,
                "max_prs": int(max_prs),
                "bonus_pct": int(bonus_pct),
            },
            conn=conn,
        )
        return {
            "stake_id": out["stake_id"],
            "guild_id": gid,
            "per_pr": per_pr,
            "max_prs": int(max_prs),
            "bonus_pct": int(bonus_pct),
        }


def fund_guild_stake_lock(
    conn: sqlite3.Connection, link: dict, per_pr: int, staker_id: int
) -> int | None:
    """Pool-fund one imminent lock: grant then memo, in that grant-first
    order. Returns the memo row id, or None (skip this lock this pass,
    retry on the next) when the pool cannot cover or the treasury cannot
    fund - the transient-dip precedent from admin-funded stakes, never
    an abandon. The caller bumps its running balance tracker past this
    call, and reverses by memo id when the lock INSERT hits its dupe
    guard (same undo discipline as the v1 debit paths)."""
    from db._credits import grant

    if guild_balance(conn, link["guild_id"]) < per_pr:
        return None
    ok = grant(
        staker_id,
        per_pr,
        "guild_stake_conduit",
        target_type="proposal_stake",
        target_id=link["stake_id"],
        conn=conn,
    )
    if not ok:
        return None
    cur = conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
        " note) VALUES (?, 'stake_lock', ?, ?, 'stake lock funding')",
        (link["guild_id"], per_pr, staker_id),
    )
    return int(cur.lastrowid or 0)


def settle_guild_stake_payout(
    conn: sqlite3.Connection,
    link: dict,
    opener_id: int,
    amount: int,
    pr_number: int,
) -> None:
    """Split merged-PR winnings: the opener's ex-ante bonus via the same
    always-settling principal return v1 uses, the pool's share as a memo
    PLUS a matching treasury mint. The mint is load-bearing, not double
    counting: the conduit lock burned real quarters (v1 spend with no
    destination), so without it the pool memo would be a claim without
    backing and later payouts would hit an unfunded treasury. Total mint
    volume equals v1's (bonus to opener + rest to treasury == full payout
    to opener). Zero bonus pays the pool whole."""
    from db._credits import _insert_entry, return_principal

    bonus = amount * int(link["opener_bonus_pct"]) // 100
    if bonus > 0:
        return_principal(
            opener_id,
            bonus,
            "stake_paid",
            target_type="proposal_stake",
            target_id=link["stake_id"],
            conn=conn,
        )
    rest = amount - bonus
    if rest > 0:
        _insert_entry(
            conn,
            None,
            "treasury",
            rest,
            "guild_stake_winnings",
            "proposal_stake",
            link["stake_id"],
        )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'stake', ?, ?)",
            (
                link["guild_id"],
                rest,
                f"stake winnings (PR #{pr_number}, bonus {bonus}q to opener)",
            ),
        )


def settle_guild_stake_self(conn: sqlite3.Connection, link: dict, amount: int) -> None:
    """Founder opened a PR on their own guild-backed stake: v1 would
    refund the conduit, enriching the founder with pool money. Redirect
    whole to the pool instead (the founder nets zero across fund, lock,
    and return - the conduit invariant), minting the burned lock back to
    the treasury so the memo stays backed."""
    from db._credits import _insert_entry

    _insert_entry(
        conn,
        None,
        "treasury",
        amount,
        "guild_stake_winnings",
        "proposal_stake",
        link["stake_id"],
    )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
        " VALUES (?, 'stake', ?, 'self-stake return to pool')",
        (link["guild_id"], amount),
    )


def settle_guild_stake_refund(
    conn: sqlite3.Connection, link: dict, amount: int
) -> None:
    """Declined-PR lock refund: the v1 founder refund is skipped (it
    would enrich the conduit with pool money) and the pool takes a memo
    instead - plus the matching treasury mint, or the burned lock would
    leave the memo unbacked (same conservation as the payout above)."""
    from db._credits import _insert_entry

    _insert_entry(
        conn,
        None,
        "treasury",
        amount,
        "guild_stake_refund",
        "proposal_stake",
        link["stake_id"],
    )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
        " VALUES (?, 'stake', ?, 'stake lock refund to pool')",
        (link["guild_id"], amount),
    )


# ── upkeep (weekly fee invoices + sweep) ─────────────────────────────────


def _open_fee_invoice(
    conn: sqlite3.Connection, guild_id: int, member_id: int
) -> dict | None:
    """The open (pending/accepted) fee invoice for one member, if any."""
    row = conn.execute(
        "SELECT i.* FROM invoices i JOIN guild_fee_invoices l"
        " ON l.invoice_id = i.id WHERE l.guild_id = ? AND l.member_agent_id = ?"
        " AND i.status IN ('pending', 'accepted') ORDER BY i.id DESC LIMIT 1",
        (guild_id, member_id),
    ).fetchone()
    return dict(row) if row is not None else None


def sweep_guild_upkeep() -> dict:
    """Weekly upkeep sweep (poller wiring lands in PR-5, like the
    membership sweep): issue this week's 1-quarter fee arrears per
    member (one invoice per member covering all open arrears, at most
    one open each), then sweep pool shares older than 48h to the
    treasury, suspend on shortfall (self-healing on recovery), and
    disband past the 14d grace. Idempotent per week; every member ping
    is the mandated fee-invoice nudge, nothing else."""
    import events

    report: dict = {
        "week": _week_key(),
        "issued": 0,
        "swept": {},
        "suspended": [],
        "recovered": [],
        "disbanded": [],
        "skipped": [],
    }
    week = _week_key()
    with _conn(immediate=True) as conn:
        guilds = conn.execute("SELECT * FROM guilds WHERE status = 'active'").fetchall()
        for grow in guilds:
            guild = dict(grow)
            gid = guild["id"]
            try:
                members = conn.execute(
                    "SELECT agent_id FROM guild_members WHERE guild_id = ? ORDER BY id",
                    (gid,),
                ).fetchall()
                issued_here = 0
                for mrow in members:
                    aid = mrow[0]
                    has_week = conn.execute(
                        "SELECT 1 FROM guild_fee_arrears WHERE guild_id = ?"
                        " AND member_agent_id = ? AND week = ? LIMIT 1",
                        (gid, aid, week),
                    ).fetchone()
                    if has_week is None:
                        try:
                            conn.execute(
                                "INSERT INTO guild_fee_arrears (guild_id, member_agent_id,"
                                " week, quarters, status) VALUES (?, ?, ?, 1, 'open')",
                                (gid, aid, week),
                            )
                        except sqlite3.IntegrityError:
                            # domain: degrade-silently - a concurrent sweep won
                            # the week row for this member; the invoice branch
                            # below still bills the combined open arrears.
                            pass
                    if _open_fee_invoice(conn, gid, aid) is None:
                        owing = conn.execute(
                            "SELECT COALESCE(SUM(quarters), 0) FROM guild_fee_arrears"
                            " WHERE guild_id = ? AND member_agent_id = ?"
                            " AND status = 'open'",
                            (gid, aid),
                        ).fetchone()[0]
                        if owing and owing > 0:
                            cur = conn.execute(
                                "INSERT INTO invoices (payer_agent_id, created_by_agent_id,"
                                " amount_quarters, remaining_quarters, reason, status,"
                                " due_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                                (
                                    aid,
                                    guild["founder_agent_id"],
                                    owing,
                                    owing,
                                    f"guild {guild['name']!r} upkeep week {week}",
                                    _days_ago_iso(-7),
                                ),
                            )
                            inv_id = int(cur.lastrowid or 0)
                            conn.execute(
                                "INSERT INTO guild_fee_invoices (invoice_id, guild_id,"
                                " member_agent_id, week) VALUES (?, ?, ?, ?)",
                                (inv_id, gid, aid, week),
                            )
                            _notify(
                                conn,
                                aid,
                                "economy",
                                "invoice",
                                inv_id,
                                f"guild {guild['name']!r} upkeep fee due ({owing}q"
                                f" for week {week}) - accept and pay it.",
                            )
                            issued_here += 1
                            report["issued"] += 1
                if issued_here:
                    events.log_event(
                        events.EVT_GUILD_UPKEEP_ISSUED,
                        actor_agent_id=guild["founder_agent_id"],
                        target_type="guild",
                        target_id=gid,
                        detail={"week": week, "invoices": issued_here},
                        conn=conn,
                    )
                due = min(5, len(members))
                if guild.get("last_upkeep_week") == week:
                    continue
                old_enough = conn.execute(
                    "SELECT 1 FROM guild_fee_invoices l JOIN invoices i"
                    " ON i.id = l.invoice_id WHERE l.guild_id = ?"
                    " AND i.created_at <= ? LIMIT 1",
                    (gid, _days_ago_iso(2)),
                ).fetchone()
                if old_enough is None:
                    continue
                pool = guild_balance(conn, gid)
                if due > 0 and pool >= due:
                    conn.execute(
                        "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
                        " VALUES (?, 'fee', ?, 'weekly upkeep sweep to Treasury')",
                        (gid, due),
                    )
                    conn.execute(
                        "UPDATE guilds SET last_upkeep_week = ? WHERE id = ?",
                        (week, gid),
                    )
                    if guild.get("spending_suspended"):
                        # A delinquent freeze is owned by the debt path (PR-7):
                        # upkeep recovery must not clear it early.
                        if guild.get("suspend_reason") != "delinquent":
                            conn.execute(
                                "UPDATE guilds SET spending_suspended = 0, suspended_at = NULL"
                                " WHERE id = ?",
                                (gid,),
                            )
                            report["recovered"].append(gid)
                    report["swept"][gid] = due
                else:
                    if not guild.get("spending_suspended"):
                        conn.execute(
                            "UPDATE guilds SET spending_suspended = 1, suspended_at = ?"
                            " WHERE id = ?",
                            (_now_iso(), gid),
                        )
                        report["suspended"].append(gid)
                    elif _age_days(guild.get("suspended_at")) > 14:
                        # The sole raising call in this sweep: an unfunded
                        # disband must skip this guild (retry next tick), never
                        # roll back every other guild's issuance and sweeps.
                        try:
                            from db._guilds import _disband_distribute

                            _disband_distribute(
                                conn, gid, "upkeep grace lapsed (14d suspended)"
                            )
                        except ForumError:
                            report["skipped"].append(gid)
                            logutil.log(
                                "guild_upkeep_failed",
                                guild_id=gid,
                                why="grace-disband-unfunded",
                            )
                            continue
                        report["disbanded"].append(gid)
            except Exception as exc:
                # domain: never-lose-data - one poisoned guild logs and
                # retries next tick instead of rolling back its neighbours
                # (the membership sweep isolates per entry the same way).
                report["skipped"].append(gid)
                logutil.log(
                    "guild_upkeep_failed",
                    guild_id=gid,
                    error=str(exc),
                )
                continue
        worked = bool(
            report["issued"]
            or report["swept"]
            or report["suspended"]
            or report["recovered"]
            or report["disbanded"]
        )
        if worked:
            events.log_event(
                events.EVT_GUILD_UPKEEP_SWEPT,
                actor_agent_id=None,
                target_type=None,
                target_id=None,
                detail={k: v for k, v in report.items()},
                conn=conn,
            )
    return report


def settle_guild_fee_payment(
    conn: sqlite3.Connection, link: dict, payer_id: int, pay_q: int
) -> None:
    """Settle one upkeep payment poolward: member wallet parks in the
    treasury, the pool takes a deposit memo, arrears settle oldest-first.
    Shared by pay_invoice's guild branch (the single payment path - no
    separate tool needed). No pool fee on dues; the pool receives full."""
    from db._credits import spend

    spend(
        payer_id,
        pay_q,
        "guild_upkeep_fee",
        dest_treasury=True,
        target_type="invoice",
        target_id=link["invoice_id"],
        conn=conn,
    )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
        " note) VALUES (?, 'deposit', ?, ?, 'upkeep fee payment')",
        (link["guild_id"], pay_q, payer_id),
    )
    _settle_arrears(conn, link["guild_id"], payer_id, pay_q)
    import events

    events.log_event(
        events.EVT_GUILD_INVOICE_PAID,
        actor_agent_id=payer_id,
        target_type="invoice",
        target_id=link["invoice_id"],
        detail={"guild_id": link["guild_id"], "quarters": pay_q},
        conn=conn,
    )


def _void_open_arrears(conn: sqlite3.Connection, guild_id: int) -> int:
    """Void member arrears no payout will ever settle. Called on disband
    paths only: live guilds keep dormant rows (a rejoining debtor still
    owes - the withhold fires on their next payout). Returns rows voided."""
    cur = conn.execute(
        "UPDATE guild_fee_arrears SET status = 'void' WHERE guild_id = ?"
        " AND status = 'open'",
        (guild_id,),
    )
    return cur.rowcount or 0


def _settle_arrears(
    conn: sqlite3.Connection, guild_id: int, agent_id: int, paid_q: int
) -> int:
    """Settle open arrears oldest-first from a payment. Returns the
    unapplied leftover (overpayments stay on the invoice remaining)."""
    rows = conn.execute(
        "SELECT id, quarters FROM guild_fee_arrears WHERE guild_id = ?"
        " AND member_agent_id = ? AND status = 'open' ORDER BY week ASC, id ASC",
        (guild_id, agent_id),
    ).fetchall()
    leftover = paid_q
    for row in rows:
        if leftover < row["quarters"]:
            break
        conn.execute(
            "UPDATE guild_fee_arrears SET status = 'paid' WHERE id = ?", (row["id"],)
        )
        leftover -= row["quarters"]
    return leftover
