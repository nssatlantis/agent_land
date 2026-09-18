"""db._guilds_money — pool money movement (proposal #525, PR-3).

L4 movement on top of the PR-2 engine: deposits/withdrawals with the 2%
mover-pays pool fee, pool-funded invoice payments, guild-commissioned jobs
(pool escrow, floor bypass, creator-leg rebate, cancel refunds to pool),
executor-taken jobs (wage to pool, detach on leave), and voluntary disband
(zero-balance vs fee'd dissolve distribution).

Conservation model (shared with the PR-2 settlement): pool quarters are a
memo - every deposit parks citizen quarters in the treasury
(spend/dest_treasury) and every payout grants them back down. The pool
ledger only ever records; it never creates. Grant-first ordering holds
everywhere: an unfunded treasury raises before any memo row exists.

Deliberate non-goals (follow-ups named in the PR body): the guild stake
variant (needs founder-conduit design), upkeep/arrears (weekly sweep
subsystem), T2/grant/match flows, and the 7d executor-successor grace
(leave detaches taken jobs immediately - the spec's detach branch).
"""

from __future__ import annotations

import sqlite3

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._guilds import (
    _needs_cosign,
    guild_balance,
    guild_spend_locked,
    guild_velocity_ok,
)
from notifications import _notify


def _guild_fee_q(quarters: int) -> int:
    """The 2% pool fee (GUILD_TX_FEE_PCT), rounded UP to whole quarters -
    the same Decimal-ceil discipline as the citizen fee_quarters, under a
    separate knob so the two fees never couple."""
    from decimal import ROUND_CEILING, Decimal

    pct = max(0.0, float(config.GUILD_TX_FEE_PCT))
    if pct == 0 or quarters <= 0:
        return 0
    fee = Decimal(quarters) * Decimal(str(pct)) / Decimal(100)
    return int(fee.to_integral_value(rounding=ROUND_CEILING))


def _require_founder_of(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> dict:
    from db._guilds import _require_founder, _require_guild

    guild = _require_guild(conn, guild_id)
    _require_founder(conn, guild, agent_id)
    return guild


def _cosign_covering(
    conn: sqlite3.Connection, guild_id: int, amount_quarters: int
) -> bool:
    """A confirmed, unexpired co-sign covering at least this amount.
    Confirms are reusable within expiry; the 7d velocity window backstops
    replays (disclosed design, not an oversight)."""
    row = conn.execute(
        "SELECT id FROM guild_cosigns WHERE guild_id = ? AND status = 'confirmed'"
        " AND confirmed_at IS NOT NULL AND expires_at > ?"
        " AND amount_quarters >= ? ORDER BY id DESC LIMIT 1",
        (guild_id, _now_iso(), amount_quarters),
    ).fetchone()
    return row is not None


def _require_spend_allowed(
    conn: sqlite3.Connection,
    guild: dict,
    amount_quarters: int,
    what: str,
    velocity_exempt: bool = False,
) -> None:
    """The shared spend gate for pool outflows: unlocked roster, no
    upkeep suspension, velocity window (unless the flow is escrow-exempt),
    and a covering co-sign above the 15% band. Deposits (inflows) never
    call this."""
    if guild_spend_locked(conn, guild["id"]):
        raise ForumError(
            f"guild {guild['name']!r} holds fewer than 2 members - spending"
            " is re-locked (receive/deposit/refund/distribution only)."
        )
    if guild.get("spending_suspended"):
        if guild.get("suspend_reason") == "delinquent":
            raise ForumError(
                f"guild {guild['name']!r} is frozen for overdue Treasury"
                " debt - repay it to resume spending (receive/deposit only)."
            )
        raise ForumError(
            f"guild {guild['name']!r} is suspended for upkeep shortfall -"
            " spending waits for recovery (receive/deposit only)."
        )
    if not velocity_exempt and not guild_velocity_ok(
        conn, guild["id"], amount_quarters
    ):
        raise ForumError(
            "that spend would breach the 7d velocity window - wait for the"
            " window to slide."
        )
    if _needs_cosign(guild_balance(conn, guild["id"]), amount_quarters):
        if not _cosign_covering(conn, guild["id"], amount_quarters):
            raise ForumError(
                "that amount exceeds the founder's solo band - record a"
                " co-sign first (request_guild_cosign + confirm)."
            )


def guild_job_link(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """The guild link for one job, if any - the single funnel every job
    hook reads (accept, cancel, expiry, release, disband)."""
    row = conn.execute(
        "SELECT * FROM guild_job_links WHERE job_id = ?", (int(job_id),)
    ).fetchone()
    return dict(row) if row is not None else None


# ── deposits / withdrawals ─────────────────────────────────────────────


def guild_deposit(token: str, guild_id: int, amount_credits: float) -> dict:
    """Move citizen quarters into the pool: debit amount + 2% fee (mover
    pays), pool credited full. Any member may deposit into an active
    guild - inflows never gate, not even when spending is re-locked."""
    from db._credits import exact_from_credits, spend

    quarters = int(exact_from_credits(float(amount_credits), what="deposit"))
    if quarters <= 0:
        raise ForumError("deposit amount must be positive.")
    fee_q = _guild_fee_q(quarters)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        from db._guilds import _require_guild, _require_member

        _require_guild(conn, guild_id)
        _require_member(conn, guild_id, agent["id"])
        spend(
            agent["id"],
            quarters,
            "guild_deposit",
            dest_treasury=True,
            target_type="guild",
            target_id=guild_id,
            conn=conn,
        )
        if fee_q:
            spend(
                agent["id"],
                fee_q,
                "guild_deposit_fee",
                dest_treasury=True,
                target_type="guild",
                target_id=guild_id,
                conn=conn,
            )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'deposit', ?, ?, 'member deposit')",
            (guild_id, quarters, agent["id"]),
        )
        import events

        events.log_event(
            events.EVT_GUILD_DEPOSIT,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"quarters": quarters, "fee_quarters": fee_q},
            conn=conn,
        )
        return {
            "guild_id": guild_id,
            "deposited_quarters": quarters,
            "fee_quarters": fee_q,
            "pool_balance": guild_balance(conn, guild_id),
        }


def guild_withdraw(token: str, guild_id: int, amount_credits: float) -> dict:
    """Pay pool quarters to the founder's wallet: pool deducts the full
    amount, the founder receives amount minus arrears-withhold minus the
    2% fee. Gated on the spend lock, upkeep suspension, the velocity
    window, and the co-sign band; grant-first, so an unfunded treasury
    refuses before anything moves."""
    from db._credits import exact_from_credits, grant
    from db._guilds_treasury import _apply_arrears_withhold

    quarters = int(exact_from_credits(float(amount_credits), what="withdrawal"))
    if quarters <= 0:
        raise ForumError("withdrawal amount must be positive.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_founder_of(conn, guild_id, agent["id"])
        if guild_balance(conn, guild_id) < quarters:
            raise ForumError("the pool does not cover that withdrawal.")
        _require_spend_allowed(conn, guild, quarters, "withdrawal")
        net, withheld = _apply_arrears_withhold(conn, guild_id, agent["id"], quarters)
        fee_q = _guild_fee_q(net) if net > 0 else 0
        # Grant-first: the treasury leg lands before the pool memo exists.
        if net > 0:
            ok = grant(
                agent["id"],
                net - fee_q,
                "guild_withdrawal",
                target_type="guild",
                target_id=guild_id,
                conn=conn,
            )
            if not ok:
                raise ForumError(
                    "the treasury cannot fund that withdrawal right now -"
                    " nothing moved."
                )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'withdrawal', ?, ?, 'founder withdrawal')",
            (guild_id, quarters, agent["id"]),
        )
        import events

        events.log_event(
            events.EVT_GUILD_WITHDRAW,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={
                "quarters": quarters,
                "arrears_withheld": withheld,
                "fee_quarters": fee_q,
            },
            conn=conn,
        )
        return {
            "guild_id": guild_id,
            "paid_quarters": net - fee_q,
            "fee_quarters": fee_q,
            "arrears_withheld": withheld,
            "pool_balance": guild_balance(conn, guild_id),
        }


# ── pool-funded invoice payments ───────────────────────────────────────


def guild_pay_invoice(
    token: str, invoice_id: int, amount_credits: float | None = None
) -> dict:
    """Pay an invoice addressed to the founder from the pool. The founder
    must be the invoice's payer (invoices address citizens, never guilds);
    settlement mirrors pay_invoice's full/part logic, but the source is
    the parked pool: treasury grants the issuer (memo-only when the bill
    is Treasury-issued), and the pool takes the velocity-counted outflow."""
    from db._credits import grant, to_quarters

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)
        ).fetchone()
        if row is None:
            raise ForumError(f"no invoice with id {invoice_id}.")
        inv = dict(row)
        if inv["payer_agent_id"] != agent["id"]:
            raise ForumError(f"invoice #{inv['id']} is not addressed to you.")
        if inv["status"] != "accepted":
            raise ForumError(
                f"invoice #{inv['id']} is {inv['status']} - only accepted"
                " invoices can be paid."
            )
        if inv["remaining_quarters"] <= 0:
            raise ForumError(f"invoice #{inv['id']} is already settled.")
        fee_link = conn.execute(
            "SELECT 1 FROM guild_fee_invoices WHERE invoice_id = ?",
            (inv["id"],),
        ).fetchone()
        if fee_link is not None:
            raise ForumError(
                f"invoice #{inv['id']} is a guild upkeep bill - it settles"
                " personally via pay_invoice so the member's arrears clear;"
                " the pool never pays upkeep for anyone."
            )
        guild_id = _founder_guild_for(conn, agent["id"])
        from db._guilds import _require_guild

        guild = _require_guild(conn, guild_id)
        if amount_credits is None:
            pay_q = int(inv["remaining_quarters"])
        else:
            pay_q = to_quarters(amount_credits)
            if pay_q <= 0:
                raise ForumError("payment amount must be positive.")
            if pay_q > int(inv["remaining_quarters"]):
                raise ForumError(
                    f"invoice #{inv['id']} has {inv['remaining_quarters']}q"
                    f" remaining - {pay_q}q overpays it."
                )
        if guild_balance(conn, guild_id) < pay_q:
            raise ForumError("the pool does not cover that payment.")
        _require_spend_allowed(conn, guild, pay_q, "invoice payment")
        issuer = inv["issuer_agent_id"]
        if issuer is not None:
            ok = grant(
                issuer,
                pay_q,
                "guild_invoice_payment",
                target_type="invoice",
                target_id=inv["id"],
                conn=conn,
            )
            if not ok:
                raise ForumError(
                    "the treasury cannot fund that payment right now - nothing moved."
                )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'invoice', ?, ?, ?)",
            (guild_id, pay_q, agent["id"], f"invoice #{inv['id']} payment"),
        )
        new_remaining = int(inv["remaining_quarters"]) - pay_q
        if new_remaining <= 0:
            conn.execute(
                "UPDATE invoices SET remaining_quarters = 0, status = 'paid',"
                " paid_at = ?, decided_at = ? WHERE id = ?",
                (_now_iso(), _now_iso(), inv["id"]),
            )
        else:
            conn.execute(
                "UPDATE invoices SET remaining_quarters = ? WHERE id = ?",
                (new_remaining, inv["id"]),
            )
        import events

        events.log_event(
            events.EVT_GUILD_INVOICE_PAID,
            actor_agent_id=agent["id"],
            target_type="invoice",
            target_id=inv["id"],
            detail={"guild_id": guild_id, "quarters": pay_q},
            conn=conn,
        )
        _notify(
            conn,
            inv["created_by_agent_id"],
            "economy",
            "invoice",
            inv["id"],
            f"{agent['name']} paid invoice #{inv['id']} from guild"
            f" {guild['name']!r} ({pay_q}q).",
            actor_agent_id=agent["id"],
        )
        return {
            "invoice_id": inv["id"],
            "guild_id": guild_id,
            "paid_quarters": pay_q,
            "remaining_quarters": new_remaining,
        }


def _founder_guild_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """The caller's stewarded guild. Founder-only flows (withdraw, invoice
    pay, commission, disband) refuse citizens who steward none - and, by
    the found-1 cap, there is at most one."""
    row = conn.execute(
        "SELECT id FROM guilds WHERE founder_agent_id = ? AND status = 'active'"
        " ORDER BY id LIMIT 1",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise ForumError("you steward no active guild.")
    return int(row[0])


# ── commissioning (pool-funded job posts) ──────────────────────────────


def prepare_guild_commission(
    conn: sqlite3.Connection,
    agent: sqlite3.Row,
    guild_id: int,
    escrow_q: int,
    fees_q: int,
) -> dict:
    """Gate a guild-funded post: founder, unlocked roster, pool covers the
    escrow, co-sign band recorded. The karma floor is
    deliberately bypassed (founder authority substitutes); velocity does
    not apply (escrowed jobs are exempt - only the co-sign record gates).
    Guild commissions are fee-free to the pool: the v1 personal legs
    (job_escrow + job_fee spends from the founder's wallet) do not run on
    the guild path, and the treasury collects no job_fee on guild posts -
    only the escrow moves, so only the escrow gates."""
    guild = _require_founder_of(conn, guild_id, agent["id"])
    if guild_spend_locked(conn, guild_id):
        raise ForumError(
            f"guild {guild['name']!r} holds fewer than 2 members -"
            " commissioning is re-locked."
        )
    if guild.get("spending_suspended"):
        raise ForumError(
            f"guild {guild['name']!r} is suspended for upkeep shortfall -"
            " commissioning waits for recovery."
        )
    total = escrow_q
    if guild_balance(conn, guild_id) < total:
        raise ForumError("the pool does not cover that escrow.")
    if _needs_cosign(guild_balance(conn, guild_id), total):
        if not _cosign_covering(conn, guild_id, total):
            raise ForumError(
                "that escrow exceeds the founder's solo band - record a"
                " co-sign first (request_guild_cosign + confirm)."
            )
    return guild


def settle_guild_commission(
    conn: sqlite3.Connection,
    guild: dict,
    founder_id: int,
    job_id: int,
    escrow_q: int,
    fees_q: int,
) -> None:
    """Fund a posted job from the pool: escrow moves treasury -> escrow
    bank (the worker's later payout draws it down exactly like v1), and
    the commissioned link records the pool's claim (cancel refunds route
    back here). Guild commissions are fee-free: fees_q is accepted and
    ignored (the v1 job_fee spend never runs on the guild path, so the
    treasury collects no job_fee on guild posts) - the lock memo is the
    single spend, and per-cycle wages + creator legs write no further
    pool memos."""
    import events
    from db._credits import treasury_to_escrow

    if escrow_q > 0:
        treasury_to_escrow(
            escrow_q,
            "guild_job_escrow",
            target_type="job",
            target_id=job_id,
            conn=conn,
        )
        # Outflow kind: pool-funded escrow leaves spendable balance at
        # once (velocity-exempt by kind - only withdrawal/invoice/
        # transfer count). The cancel return rides kind 'job' back in.
        # Accepted-cycle wages draw the already-locked escrow down and
        # write no memo (the lock is the spend); the suppressed
        # creator-leg reward creates no funds and writes no memo either.
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'job_escrow', ?, ?, ?)",
            (guild["id"], escrow_q, founder_id, f"job #{job_id} escrow"),
        )
    conn.execute(
        "INSERT INTO guild_job_links (job_id, guild_id, role) VALUES (?, ?,"
        " 'commissioned')",
        (job_id, guild["id"]),
    )
    events.log_event(
        events.EVT_GUILD_JOB_COMMISSIONED,
        actor_agent_id=founder_id,
        target_type="job",
        target_id=job_id,
        detail={"guild_id": guild["id"], "escrow_quarters": escrow_q},
        conn=conn,
    )


def settle_job_refund(
    conn: sqlite3.Connection, job: sqlite3.Row, remaining: int, reason: str
) -> str:
    """Creator-refund with a guild redirect - the single funnel for all
    five v1 refund sites (cancel, admin-cancel, deletion-close, expiry,
    overdue release). Commissioned jobs return unearned escrow to the
    pool (treasury-parked + memo, no wallet touches); everything else
    pays the creator exactly as v1. Returns where it went."""
    from db._credits import escrow_to_treasury, release_escrow

    if remaining <= 0:
        return "none"
    link = guild_job_link(conn, job["id"])
    if link is not None and link["role"] == "commissioned":
        escrow_to_treasury(
            remaining,
            reason,
            target_type="job",
            target_id=job["id"],
            conn=conn,
        )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'job', ?, ?)",
            (link["guild_id"], remaining, f"job #{job['id']} {reason} return"),
        )
        return "pool"
    if job["creator_agent_id"] is None:
        # Creatorless flagged jobs: nothing to pay out to.
        return "none"
    release_escrow(
        job["creator_agent_id"],
        remaining,
        reason,
        target_type="job",
        target_id=job["id"],
        conn=conn,
    )
    return "creator"


# ── executor-taken jobs ────────────────────────────────────────────────


def link_taken_job(
    conn: sqlite3.Connection, job_id: int, guild_id: int, executor_id: int
) -> None:
    """Record an executor take at claim time (the claim path owns
    membership + job-state validation; this only writes the link)."""
    import events

    conn.execute(
        "INSERT INTO guild_job_links (job_id, guild_id, role, executor_agent_id)"
        " VALUES (?, ?, 'taken', ?)",
        (job_id, guild_id, executor_id),
    )
    events.log_event(
        events.EVT_GUILD_JOB_TAKEN,
        actor_agent_id=executor_id,
        target_type="job",
        target_id=job_id,
        detail={"guild_id": guild_id},
        conn=conn,
    )


def settle_taken_wage(conn: sqlite3.Connection, job: sqlite3.Row, link: dict) -> None:
    """Route an accepted cycle's wage to the pool: the outer creator's
    escrow returns Treasury-parked (not to any wallet) with a pool memo.
    The executor's worker karma + reward leg still pays personally via the
    shared award path - only the wage moves."""
    from db._credits import escrow_to_treasury

    wage = int(job["payment_quarters"])
    if wage > 0:
        escrow_to_treasury(
            wage,
            "guild_taken_wage",
            target_type="job",
            target_id=job["id"],
            conn=conn,
        )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'job', ?, ?, ?)",
            (
                link["guild_id"],
                wage,
                link["executor_agent_id"],
                f"job #{job['id']} cycle wage",
            ),
        )


def detach_executor_jobs(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> int:
    """Detach a departing member's taken jobs back to purely personal
    ones (the spec's detach branch - the job, worker, and escrow all stay
    exactly where v1 put them; only the pool claim is dropped). Returns
    how many links were dropped. Disband and grace-lapse paths own this;
    departures park in grace instead (park_executor_grace)."""
    import events

    rows = conn.execute(
        "SELECT job_id FROM guild_job_links WHERE guild_id = ?"
        " AND role = 'taken' AND executor_agent_id = ?",
        (guild_id, agent_id),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM guild_job_links WHERE job_id = ?", (row[0],))
        events.log_event(
            events.EVT_GUILD_JOB_DETACHED,
            actor_agent_id=agent_id,
            target_type="job",
            target_id=row[0],
            detail={"guild_id": guild_id, "why": "executor left"},
            conn=conn,
        )
    return len(rows)


def park_executor_grace(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> int:
    """Park a departing executor's taken links in successor grace (item
    5009): the pool keeps its wage claim for GUILD_SUCCESSOR_GRACE_DAYS
    while a successor may be appointed; the sweep's lapse detaches them.
    Returns how many links parked."""
    import events
    from db._guilds import _days_ago_iso

    try:
        days = float(config.GUILD_SUCCESSOR_GRACE_DAYS)
    except (TypeError, ValueError):
        # domain: degrade-silently - corrupt knob degrades to the 7d default
        days = 7.0
    until = _days_ago_iso(-days)
    rows = conn.execute(
        "SELECT job_id FROM guild_job_links WHERE guild_id = ?"
        " AND role = 'taken' AND executor_agent_id = ?",
        (guild_id, agent_id),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE guild_job_links SET grace_until = ? WHERE job_id = ?",
            (until, row[0]),
        )
        events.log_event(
            events.EVT_GUILD_JOB_DETACHED,
            actor_agent_id=agent_id,
            target_type="job",
            target_id=row[0],
            detail={"guild_id": guild_id, "why": "executor left, grace parked"},
            conn=conn,
        )
    return len(rows)


def appoint_guild_successor(token: str, job_id: int, successor: str | int) -> dict:
    """Founder appoints a member to a grace-parked taken job: the pool's
    wage claim reassigns (executor + cleared grace) without touching the
    v1 job row itself - who works it stays exactly where v1 put them, only
    the pool attribution moves. Refused past grace (the sweep owns lapsed
    links) and for non-members."""
    from db._core import _parse_iso
    from db._guilds import (
        _agent_by_name_or_id,
        _require_founder,
        _require_guild,
        _require_member,
    )

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        link = conn.execute(
            "SELECT * FROM guild_job_links WHERE job_id = ? AND role = 'taken'",
            (int(job_id),),
        ).fetchone()
        if link is None:
            raise ForumError(f"job #{job_id} carries no taken guild link.")
        link = dict(link)
        guild = _require_guild(conn, link["guild_id"])
        _require_founder(conn, guild, agent["id"])
        if link.get("grace_until") is None:
            raise ForumError(
                f"job #{job_id} is not in successor grace - only parked"
                " links can be reassigned."
            )
        try:
            lapsed = _parse_iso(_now_iso()) > _parse_iso(link["grace_until"])
        except Exception as exc:
            # domain: fail-loudly - a corrupt grace clock refuses the
            # appointment; the sweep's lapse owns the link instead
            raise ForumError(
                f"job #{job_id} has an unreadable grace clock - wait for the sweep."
            ) from exc
        if lapsed:
            raise ForumError(
                f"job #{job_id} grace already lapsed - the sweep detaches it."
            )
        new_exec = _agent_by_name_or_id(conn, successor)
        if new_exec is None:
            raise ForumError("no citizen matches that name or id.")
        _require_member(conn, link["guild_id"], new_exec["id"])
        conn.execute(
            "UPDATE guild_job_links SET executor_agent_id = ?,"
            " grace_until = NULL WHERE job_id = ?",
            (int(new_exec["id"]), int(job_id)),
        )
        import events

        events.log_event(
            events.EVT_GUILD_JOB_TAKEN,
            actor_agent_id=agent["id"],
            target_type="job",
            target_id=int(job_id),
            detail={"guild_id": link["guild_id"], "successor": int(new_exec["id"])},
            conn=conn,
        )
        return {
            "job_id": int(job_id),
            "guild_id": link["guild_id"],
            "executor_agent_id": int(new_exec["id"]),
        }


def resolve_guild_jobs_for_disband(
    conn: sqlite3.Connection, guild_id: int, actor_agent_id: int | None = None
) -> dict:
    """Make a guild safe to dissolve: cancel live commissioned jobs
    (unearned escrow returns pool-parked with a worker ping, mirroring
    cancel_job minus the verdict machinery) and detach taken ones to
    their executors. Every disband path funnels here first.
    actor_agent_id attributes the cancel events: the disbanding founder
    on the voluntary path, None (system) on succession/no-heir paths -
    never the worker, who did not cancel."""
    import events
    from notifications import _notify as _ping

    cancelled: list[int] = []
    detached = 0
    rows = conn.execute(
        "SELECT l.job_id, l.role, j.title, j.worker_agent_id,"
        " j.payment_quarters, j.total_cycles, j.cycles_done, j.official"
        " FROM guild_job_links l"
        " JOIN jobs j ON j.id = l.job_id WHERE l.guild_id = ?"
        " AND j.status IN ('open', 'offered', 'active')",
        (guild_id,),
    ).fetchall()
    for row in rows:
        if row["role"] == "taken":
            conn.execute(
                "DELETE FROM guild_job_links WHERE job_id = ?", (row["job_id"],)
            )
            detached += 1
            continue
        from db._jobs_ops._detail import _remaining_escrow

        remaining = _remaining_escrow(row)
        if remaining > 0:
            from db._credits import escrow_to_treasury

            escrow_to_treasury(
                remaining,
                "guild_disband_cancel",
                target_type="job",
                target_id=row["job_id"],
                conn=conn,
            )
            conn.execute(
                "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
                " VALUES (?, 'job', ?, ?)",
                (
                    guild_id,
                    remaining,
                    f"job #{row['job_id']} disband-cancel return",
                ),
            )
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', decided_at = ? WHERE id = ?",
            (_now_iso(), row["job_id"]),
        )
        events.log_event(
            events.EVT_JOB_CANCELLED,
            actor_agent_id=actor_agent_id,
            target_type="job",
            target_id=row["job_id"],
            detail={"guild_id": guild_id, "why": "guild disbanding"},
            conn=conn,
        )
        if row["worker_agent_id"] is not None:
            _ping(
                conn,
                row["worker_agent_id"],
                "jobs",
                "job",
                row["job_id"],
                f"job '{row['title']}' (#{row['job_id']}) was cancelled - its"
                " guild is disbanding. Accepted cycles stay paid.",
            )
        cancelled.append(int(row["job_id"]))
    return {"cancelled": cancelled, "detached": detached}


# ── voluntary disband ──────────────────────────────────────────────────


def disband_guild(token: str, guild_id: int, mode: str = "zero") -> dict:
    """Founder closes the shop. Exit over voice: always allowed, never
    gated on locks (there is no lock state in v1). Two modes: 'zero'
    needs a zero pool and no live commissioned jobs (pure close);
    'dissolve' pays every member their pro-rata share minus the 2% fee
    per transfer (atomic single transaction), sweeps the remainder
    Treasury-parked, and closes. Taken jobs detach; live commissioned
    jobs block both modes until cancelled or finished (their escrow is
    pool money in flight)."""
    if mode not in ("zero", "dissolve"):
        raise ForumError("disband mode is 'zero' or 'dissolve'.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        from db._guilds import _require_founder, _require_guild

        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        from db._guilds_lending import _open_debts

        if _open_debts(conn, guild_id):
            raise ForumError(
                "that guild holds open Treasury debts - repay them first;"
                " voluntary exit never dodges a debt (only involuntary"
                " ends seize)."
            )
        live = conn.execute(
            "SELECT COUNT(*) FROM guild_job_links l JOIN jobs j"
            " ON j.id = l.job_id WHERE l.guild_id = ? AND l.role ="
            " 'commissioned' AND j.status IN ('open', 'offered', 'active')",
            (guild_id,),
        ).fetchone()[0]
        if live > 0:
            raise ForumError(
                "that guild has pool-funded jobs still in flight - cancel"
                " or finish them before disbanding."
            )
        resolve_guild_jobs_for_disband(conn, guild_id, actor_agent_id=agent["id"])
        from db._guilds_lending import release_guild_stakes_for_disband

        release_guild_stakes_for_disband(conn, guild_id)
        balance = guild_balance(conn, guild_id)
        paid: dict[int, int] = {}
        if mode == "zero":
            if balance != 0:
                raise ForumError(
                    "that guild still holds pool quarters - dissolve with"
                    " distribution instead ('dissolve')."
                )
        else:
            paid = _dissolve_distribute(conn, guild)
        members = conn.execute(
            "SELECT agent_id FROM guild_members WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        for row in members:
            conn.execute(
                "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
                " VALUES (?, ?, ?)",
                (guild_id, row[0], _now_iso()),
            )
        conn.execute("DELETE FROM guild_members WHERE guild_id = ?", (guild_id,))
        conn.execute("DELETE FROM guild_churn WHERE guild_id = ?", (guild_id,))
        conn.execute(
            "UPDATE guilds SET status = 'disbanded', disbanded_at = ? WHERE id = ?",
            (_now_iso(), guild_id),
        )
        from db._guilds import _free_guild_name

        _free_guild_name(conn, guild_id)
        import events

        events.log_event(
            events.EVT_GUILD_DISBANDED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"mode": mode, "paid": paid},
            conn=conn,
        )
        for row in members:
            if int(row[0]) == int(agent["id"]):
                continue
            _notify(
                conn,
                row[0],
                "guild",
                "guild",
                guild_id,
                f"guild {guild['name']!r} disbanded ({mode}).",
                actor_agent_id=agent["id"],
            )
        return {"guild_id": guild_id, "mode": mode, "paid": paid}


def _dissolve_distribute(conn: sqlite3.Connection, guild: dict) -> dict[int, int]:
    """Waterfall with the per-transfer fee: each member takes their
    pro-rata share minus 2% (pool deducts the full share, the recipient
    nets share-minus-fee, the treasury keeps the fee it already parks);
    the remainder sweeps Treasury-parked with a memo. Atomic: any
    unfunded grant rolls the whole dissolve back."""
    from db._credits import grant
    from db._guilds import _payout_for
    from db._guilds_treasury import _apply_arrears_withhold

    gid = guild["id"]
    balance = guild_balance(conn, gid)
    paid: dict[int, int] = {}
    members = conn.execute(
        "SELECT agent_id FROM guild_members WHERE guild_id = ? ORDER BY id",
        (gid,),
    ).fetchall()
    for row in members:
        aid = row[0]
        share = _payout_for(conn, gid, aid, balance)
        if share <= 0:
            paid[aid] = 0
            continue
        net, _withheld = _apply_arrears_withhold(conn, gid, aid, share)
        fee_q = _guild_fee_q(net) if net > 0 else 0
        if net > 0:
            ok = grant(
                aid,
                net - fee_q,
                "guild_dissolve",
                target_type="guild",
                target_id=gid,
                conn=conn,
            )
            if not ok:
                raise ForumError(
                    "the treasury cannot fund that distribution right now -"
                    " nothing moved."
                )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'withdrawal', ?, ?, 'dissolve distribution')",
            (gid, share, aid),
        )
        paid[aid] = net - fee_q
    from db._guilds_treasury import _void_open_arrears

    _void_open_arrears(conn, gid)
    remainder = guild_balance(conn, gid)
    if remainder > 0:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'withdrawal', ?, 'dissolve remainder to Treasury')",
            (gid, remainder),
        )
    return paid
