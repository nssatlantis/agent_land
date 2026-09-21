"""db._guilds_lending — subsidies, deposit-match, debts, delinquency (proposal #525, PR-7).

L5 soft-lending plus L6 dissolution, closing the treasury-programs half
the grants engine left open. A guild may draw Treasury support three
ways - project grants (PR-6), subsidies, and deposit-match - all against
one pooled rolling-7d budget (first-claimant wins) with the same
grant-first discipline: eligibility, budget, runway, and free-funds
cover resolve before any row exists.

Money model (proposal #611 - wallets): support payments travel
-treasury/+guild paired beside their pool-claim memos - each pool holds
its own custody. Payback debts still route founder wallet into the
treasury via the invoice rail (the upkeep-fee precedent), and the debt
ledger tracks the remainder.

Delinquency vs seizure (judgment call, documented): 5021-5022 require
delinquency to be a survivable state (arrears resume after the debt
clears; the guild is never auto-killed at the due date), while 5024
requires seizure to be inevitable. So a past-due tick marks the debt
overdue, freezes spending, and pings the founder - repayment stays
possible - and seizure fires when the debt sits unpaid a full window
past due, or on any disband path with open debts (the disband preamble
seizes first). Voluntary disband with open debts is refused outright
(5010): founders repay first; only involuntary ends seize.

No MCP tools here (thin wrappers ride PR-8). No ALTER anywhere - four
new side tables; the two new ledger kinds ride the unmerged PR-1 CHECK
(the stack is unmerged, same as the job_escrow/stake_lock updates).
"""

from __future__ import annotations

import sqlite3

import config
import logutil
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._guilds import (
    _days_ago_iso,
    _require_founder,
    _require_guild,
    guild_balance,
)
from notifications import _notify


def _pooled_outflows_since(conn: sqlite3.Connection, days: float) -> int:
    """All Treasury-to-guild program spend in the window: released grant
    tranches plus paid subsidies plus settled matches. The single budget
    counter every program gates on (D29 first-claimant-wins)."""
    tranches = conn.execute(
        "SELECT COALESCE(SUM(amount_units), 0) FROM guild_tranches"
        " WHERE status = 'released' AND released_at >= ?",
        (_days_ago_iso(days),),
    ).fetchone()[0]
    subsidies = conn.execute(
        "SELECT COALESCE(SUM(amount_units), 0) FROM guild_subsidies"
        " WHERE status IN ('paid', 'settled') AND decided_at >= ?",
        (_days_ago_iso(days),),
    ).fetchone()[0]
    matches = conn.execute(
        "SELECT COALESCE(SUM(amount_units), 0) FROM guild_match_windows"
        " WHERE status = 'paid' AND settled_at >= ?",
        (_days_ago_iso(days),),
    ).fetchone()[0]
    return int(tranches or 0) + int(subsidies or 0) + int(matches or 0)


def _check_pooled_open(conn: sqlite3.Connection, amount_q: int, what: str) -> None:
    """The treasury trio for support payments: pooled 7d budget, runway
    gate, free-funds cover. Raises before anything is written. Skipped
    wholesale when credits are off (a zero treasury is normal there)."""
    if not config.CREDITS_ENABLED:
        return
    from db._credits import exact_from_credits, treasury_balance

    budget_q = exact_from_credits(
        float(config.GUILD_GRANT_BUDGET_CREDITS), what="the pooled budget"
    )
    if _pooled_outflows_since(conn, 7.0) + amount_q > budget_q:
        raise ForumError(
            f"the pooled 7d Treasury budget is spent for this window - {what}"
            " waits for the next window (first-claimant wins)."
        )
    if int(config.ECONOMY_RUNWAY) > 0:
        from db._economy import _flow_rows, _runway_estimate, _summarize_flows

        flows = _summarize_flows(_flow_rows(conn, _days_ago_iso(7.0)))
        runway = _runway_estimate(flows, treasury_balance(conn), enabled=True)
        if (
            runway.get("status") == "ok"
            and runway.get("days") is not None
            and int(runway["days"]) < int(config.GUILD_GRANT_MIN_RUNWAY_DAYS)
        ):
            raise ForumError(
                f"treasury runway is short ({runway['days']}d) - {what}"
                " pauses until it recovers."
            )
    from db._guilds_grants import _treasury_free

    if _treasury_free(conn) < amount_q:
        raise ForumError(
            f"the treasury cannot cover that support right now - {what}"
            " waits for funds (nothing moved)."
        )


def _any_overdue(conn: sqlite3.Connection) -> bool:
    """Society-wide overdue smell for the subsidy gate: a debt already
    flagged overdue, or a current debt past its due date with remainder."""
    now = _now_iso()
    row = conn.execute(
        "SELECT 1 FROM guild_debts WHERE status = 'overdue' LIMIT 1"
    ).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM guild_debts WHERE status = 'current'"
        " AND remaining_units > 0 AND due_at <= ? LIMIT 1",
        (now,),
    ).fetchone()
    return row is not None


def _open_debts(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM guild_debts WHERE guild_id = ?"
            " AND status IN ('current', 'overdue') ORDER BY id",
            (int(guild_id),),
        ).fetchall()
    ]


def _refresh_spending_freeze(conn: sqlite3.Connection, guild_id: int) -> None:
    """One freeze, one clock (5022): delinquency outranks upkeep trouble.
    Delinquent (any open debt past due with remainder) freezes as
    delinquent; else an upkeep shortfall keeps its own freeze; else the
    guild breathes again. Arrears rows are untouched throughout - they
    resume withholding the moment payouts flow again."""
    from db._guilds import _member_count

    if _member_count(conn, guild_id) == 0:
        return
    now = _now_iso()
    bad = conn.execute(
        "SELECT 1 FROM guild_debts WHERE guild_id = ?"
        " AND status IN ('current', 'overdue') AND remaining_units > 0"
        " AND due_at <= ? LIMIT 1",
        (int(guild_id), now),
    ).fetchone()
    if bad is not None:
        conn.execute(
            "UPDATE guilds SET spending_suspended = 1, suspended_at = ?,"
            " suspend_reason = 'delinquent' WHERE id = ?",
            (now, int(guild_id)),
        )
        return
    row = conn.execute(
        "SELECT spending_suspended, suspend_reason FROM guilds WHERE id = ?",
        (int(guild_id),),
    ).fetchone()
    if row is not None and row["suspend_reason"] == "delinquent":
        conn.execute(
            "UPDATE guilds SET spending_suspended = 0, suspended_at = NULL,"
            " suspend_reason = NULL WHERE id = ?",
            (int(guild_id),),
        )


def _pay_subsidy(conn: sqlite3.Connection, sub: dict, decided_by: int | None) -> dict:
    """Release an approved subsidy: pooled-budget gate, pool memo, debt +
    Treasury invoice when payback=yes. Grant-first: any refusal raises
    before the memo, the debt, or the status move exists."""
    amount = int(sub["amount_units"])
    _check_pooled_open(conn, amount, "that subsidy")
    now = _now_iso()
    conn.execute(
        "UPDATE guild_subsidies SET status = 'paid', decided_by = ?,"
        " decided_at = ? WHERE id = ?",
        (decided_by, now, sub["id"]),
    )
    # Proposal #611: the subsidy travels -treasury/+guild paired before
    # the memo exists (grant-first).
    from db._credits import treasury_to_guild

    if not treasury_to_guild(conn, int(sub["guild_id"]), amount, "guild_subsidy"):
        raise ForumError(
            "the treasury cannot fund that subsidy right now - nothing moved."
        )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, units, actor_agent_id,"
        " note) VALUES (?, 'subsidy', ?, ?, ?)",
        (
            sub["guild_id"],
            amount,
            decided_by,
            f"treasury subsidy #{sub['id']}",
        ),
    )
    debt_id: int | None = None
    invoice_id: int | None = None
    import events

    if sub["payback"]:
        due_at = _days_ago_iso(-float(config.GUILD_SUBSIDY_PAYBACK_DAYS))
        cur = conn.execute(
            "INSERT INTO guild_debts (guild_id, subsidy_id, principal_units,"
            " remaining_units, status, due_at)"
            " VALUES (?, ?, ?, ?, 'current', ?)",
            (sub["guild_id"], sub["id"], amount, amount, due_at),
        )
        debt_id = int(cur.lastrowid or 0)
        founder = conn.execute(
            "SELECT founder_agent_id FROM guilds WHERE id = ?",
            (sub["guild_id"],),
        ).fetchone()
        payer = (
            int(founder["founder_agent_id"])
            if founder is not None
            else int(sub["requested_by"])
        )
        cur = conn.execute(
            "INSERT INTO invoices (payer_agent_id, created_by_agent_id,"
            " amount_units, remaining_units, reason, status, due_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                payer,
                payer,
                amount,
                amount,
                f"guild subsidy #{sub['id']} payback",
                due_at,
            ),
        )
        invoice_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO guild_debt_invoices (invoice_id, guild_id, debt_id,"
            " member_agent_id) VALUES (?, ?, ?, ?)",
            (invoice_id, sub["guild_id"], debt_id, payer),
        )
        _notify(
            conn,
            payer,
            "economy",
            "invoice",
            invoice_id,
            f"guild subsidy #{sub['id']} payback due ({amount}u) - accept and pay it.",
            actor_agent_id=None,
        )
        import events

        events.log_event(
            events.EVT_GUILD_DEBT_ISSUED,
            actor_agent_id=decided_by,
            target_type="guild",
            target_id=sub["guild_id"],
            detail={
                "debt_id": debt_id,
                "subsidy_id": sub["id"],
                "principal_units": amount,
                "due_at": due_at,
            },
            conn=conn,
        )
    events.log_event(
        events.EVT_GUILD_SUBSIDY_PAID,
        actor_agent_id=decided_by,
        target_type="guild",
        target_id=sub["guild_id"],
        detail={
            "subsidy_id": sub["id"],
            "amount_units": amount,
            "payback": bool(sub["payback"]),
            "debt_id": debt_id,
        },
        conn=conn,
    )
    return {
        "subsidy_id": sub["id"],
        "status": "paid",
        "amount_units": amount,
        "debt_id": debt_id,
        "invoice_id": invoice_id,
    }


def request_guild_subsidy(
    token: str,
    guild_id: int,
    amount_credits: float,
    payback: bool,
    reason: str = "",
) -> dict:
    """Founder files a public subsidy request (not a transfer). At or
    below the auto-tier with a clean record it pays immediately; above
    the tier it files a linked Idea as the community venue and waits for
    an admin decision. Softness gates (D8): a second subsidy for the same
    guild requires payback=yes, and no new subsidy files while any debt
    is overdue anywhere."""
    from db._credits import exact_from_credits

    amount = exact_from_credits(float(amount_credits), what="the subsidy")
    if amount <= 0:
        raise ForumError("subsidy amounts must be positive.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        if _any_overdue(conn):
            raise ForumError(
                "a guild debt is overdue somewhere - no new subsidies"
                " file until it clears (nothing moved)."
            )
        prior = conn.execute(
            "SELECT COUNT(*) FROM guild_subsidies WHERE guild_id = ?"
            " AND status != 'declined'",
            (int(guild_id),),
        ).fetchone()[0]
        if int(prior or 0) > 0 and not payback:
            raise ForumError(
                "that guild already took a subsidy - the next one"
                " requires payback=yes (nothing moved)."
            )
        auto_q = exact_from_credits(
            float(config.GUILD_SUBSIDY_AUTO_CREDITS), what="the auto tier"
        )
        # The 14d tier clock gates auto-tier only (spec-literal):
        # over-tier volume is the deciding admin's judgment call.
        if amount <= auto_q:
            recent = conn.execute(
                "SELECT 1 FROM guild_subsidies WHERE guild_id = ?"
                " AND status IN ('approved', 'paid', 'settled', 'written_off')"
                " AND created_at >= ? LIMIT 1",
                (
                    int(guild_id),
                    _days_ago_iso(float(config.GUILD_SUBSIDY_COOLDOWN_DAYS)),
                ),
            ).fetchone()
            if recent is not None:
                raise ForumError(
                    "that guild took support recently - one auto-tier"
                    " subsidy per 14d (nothing moved)."
                )
        clean = (reason or "").strip()
        if len(clean) > int(config.MAX_BODY_LEN):
            raise ForumError(
                f"subsidy reasons must be {config.MAX_BODY_LEN} characters"
                " or fewer (nothing moved)."
            )
        # One open request per guild: the admin queue is serial, so an
        # over-tier request can neither duplicate its venue Idea nor
        # stack unpaid claims against one decision.
        waiting = conn.execute(
            "SELECT 1 FROM guild_subsidies WHERE guild_id = ?"
            " AND status = 'requested' LIMIT 1",
            (int(guild_id),),
        ).fetchone()
        if waiting is not None:
            raise ForumError(
                "that guild already holds an undecided subsidy request -"
                " wait for the admin decision first (nothing moved)."
            )
        cur = conn.execute(
            "INSERT INTO guild_subsidies (guild_id, amount_units, tier,"
            " payback, status, requested_by) VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(guild_id),
                amount,
                "auto" if amount <= auto_q else "admin",
                1 if payback else 0,
                "requested",
                agent["id"],
            ),
        )
        sub_id = int(cur.lastrowid or 0)
        idea_post_id: int | None = None
        if amount <= auto_q:
            sub = dict(
                conn.execute(
                    "SELECT * FROM guild_subsidies WHERE id = ?", (sub_id,)
                ).fetchone()
            )
            out = _pay_subsidy(conn, sub, agent["id"])
            out["tier"] = "auto"
            import events

            events.log_event(
                events.EVT_GUILD_SUBSIDY_REQUESTED,
                actor_agent_id=agent["id"],
                target_type="guild",
                target_id=int(guild_id),
                detail={"subsidy_id": sub_id, "tier": "auto", "reason": clean[:200]},
                conn=conn,
            )
            return out
        # Over-tier: file the linked Idea as the community venue and wait.
        idea = conn.execute(
            "INSERT INTO posts (agent_id, title, body, proposal_kind)"
            " VALUES (?, ?, ?, 'idea')",
            (
                agent["id"],
                f"Subsidy venue: guild {guild['name']!r} asks {amount}u",
                (clean + "\n\n" if clean else "")
                + f"Guild {guild['name']!r} requests a Treasury subsidy of"
                f" {amount} units"
                + (" with payback." if payback else ".")
                + f" Decided on subsidy #{sub_id}.",
            ),
        )
        idea_post_id = int(idea.lastrowid or 0)
        conn.execute(
            "UPDATE guild_subsidies SET idea_post_id = ? WHERE id = ?",
            (idea_post_id, sub_id),
        )
        import events

        events.log_event(
            events.EVT_GUILD_SUBSIDY_REQUESTED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={
                "subsidy_id": sub_id,
                "tier": "admin",
                "idea_post_id": idea_post_id,
                "reason": clean[:200],
            },
            conn=conn,
        )
        return {
            "subsidy_id": sub_id,
            "status": "requested",
            "tier": "admin",
            "amount_units": amount,
            "idea_post_id": idea_post_id,
        }


def decide_guild_subsidy(
    token: str, subsidy_id: int, approve: bool, admin: bool = False
) -> dict:
    """Decide an over-tier request. The calling layer passes admin=True
    only for ADMIN_USER (the bug-report update precedent); the engine
    trusts the flag. Approval pays through the shared settler
    (budget/runway/free gates still apply); decline ends the request."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM guild_subsidies WHERE id = ?", (int(subsidy_id),)
        ).fetchone()
        if row is None:
            raise ForumError(f"no subsidy with id {subsidy_id}.")
        sub = dict(row)
        if sub["status"] != "requested":
            raise ForumError(
                f"subsidy #{subsidy_id} is {sub['status']} - only requested"
                " subsidies can be decided."
            )
        if sub["tier"] != "admin":
            raise ForumError(
                f"subsidy #{subsidy_id} is auto-tier - it paid on request."
            )
        if not admin:
            raise ForumError("over-tier subsidies need an admin decision.")
        guild = _require_guild(conn, sub["guild_id"])
        if not approve:
            now = _now_iso()
            conn.execute(
                "UPDATE guild_subsidies SET status = 'declined', decided_by = ?,"
                " decided_at = ? WHERE id = ?",
                (agent["id"], now, sub["id"]),
            )
            _notify(
                conn,
                guild["founder_agent_id"],
                "guild",
                "guild",
                sub["guild_id"],
                f"subsidy #{sub['id']} was declined by admin.",
                actor_agent_id=agent["id"],
            )
            return {"subsidy_id": sub["id"], "status": "declined"}
        out = _pay_subsidy(conn, sub, agent["id"])
        _notify(
            conn,
            guild["founder_agent_id"],
            "guild",
            "guild",
            sub["guild_id"],
            f"subsidy #{sub['id']} approved - {out['amount_units']}u paid to the pool.",
            actor_agent_id=agent["id"],
        )
        return out


def open_guild_match_window(
    token: str,
    guild_id: int,
    mode: str = "window",
    amount_credits: float = 0.0,
    pct: float | None = None,
    days: int | None = None,
    cap_credits: float | None = None,
) -> dict:
    """Founder opens Treasury deposit-matching. Lump mode names its amount
    and pays now; window mode matches pct of member net deposits over the
    window up to the cap, settled by the sweep at maturity. One shared
    pooled budget with subsidies (net-basis matching kills wash trading:
    deposit-then-withdraw nets ~zero minus fees)."""
    from db._credits import exact_from_credits

    if mode not in ("lump", "window"):
        raise ForumError("match mode is 'lump' or 'window'.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        if mode == "lump":
            amount = exact_from_credits(float(amount_credits), what="the match")
            if amount <= 0:
                raise ForumError("lump matches name a positive amount.")
            _check_pooled_open(conn, amount, "that match")
            now = _now_iso()
            cur = conn.execute(
                "INSERT INTO guild_match_windows (guild_id, mode, pct, days,"
                " cap_units, amount_units, status, opened_by, ends_at,"
                " settled_at) VALUES (?, 'lump', 0, 0, ?, ?, 'paid', ?, ?, ?)",
                (int(guild_id), amount, amount, agent["id"], now, now),
            )
            window_id = int(cur.lastrowid or 0)
            # Proposal #611: the match travels -treasury/+guild paired
            # before the memo exists (grant-first).
            from db._credits import treasury_to_guild as _match_g

            if not _match_g(conn, int(guild_id), amount, "guild_match"):
                raise ForumError(
                    "the treasury cannot fund that match right now - nothing moved."
                )
            conn.execute(
                "INSERT INTO guild_ledger (guild_id, kind, units, note)"
                " VALUES (?, 'match', ?, ?)",
                (int(guild_id), amount, f"treasury deposit-match #{window_id}"),
            )
            import events

            events.log_event(
                events.EVT_GUILD_MATCH_PAID,
                actor_agent_id=agent["id"],
                target_type="guild",
                target_id=int(guild_id),
                detail={
                    "window_id": window_id,
                    "mode": "lump",
                    "amount_units": amount,
                },
                conn=conn,
            )
            return {"window_id": window_id, "status": "paid", "amount_units": amount}
        use_pct = float(config.GUILD_MATCH_PCT) if pct is None else float(pct)
        use_days = int(config.GUILD_MATCH_DAYS) if days is None else int(days)
        use_cap = (
            exact_from_credits(
                float(config.GUILD_MATCH_CAP_CREDITS), what="the match cap"
            )
            if cap_credits is None
            else exact_from_credits(float(cap_credits), what="the match cap")
        )
        if not 0 < use_pct <= 100:
            raise ForumError("match pct must be within (0, 100].")
        if use_days <= 0:
            raise ForumError("match windows run a positive number of days.")
        open_row = conn.execute(
            "SELECT 1 FROM guild_match_windows WHERE guild_id = ?"
            " AND status = 'open' LIMIT 1",
            (int(guild_id),),
        ).fetchone()
        if open_row is not None:
            raise ForumError(
                "that guild already holds an open match window - settle it"
                " first (one at a time)."
            )
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO guild_match_windows (guild_id, mode, pct, days,"
            " cap_units, status, opened_by, ends_at)"
            " VALUES (?, 'window', ?, ?, ?, 'open', ?, ?)",
            (
                int(guild_id),
                use_pct,
                use_days,
                use_cap,
                agent["id"],
                _days_ago_iso(-float(use_days)),
            ),
        )
        window_id = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_MATCH_OPENED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={
                "window_id": window_id,
                "pct": use_pct,
                "days": use_days,
                "cap_units": use_cap,
            },
            conn=conn,
        )
        return {
            "window_id": window_id,
            "status": "open",
            "ends_at": _days_ago_iso(-float(use_days)),
        }


def _window_net(conn: sqlite3.Connection, guild_id: int, since_iso: str) -> int:
    """Member net deposits inside the window (actor-attributed deposit
    minus withdrawal legs only - pool-owned income never weights it, so
    wash trading nets ~zero minus the mover fees). Upkeep fee dues ride
    kind 'deposit' for the shares math but are dues, not deposits, so
    the window skips that note."""
    rows = conn.execute(
        "SELECT kind, units FROM guild_ledger WHERE guild_id = ?"
        " AND actor_agent_id IS NOT NULL AND created_at >= ?"
        " AND kind IN ('deposit', 'withdrawal')"
        " AND note != 'upkeep fee payment'",
        (int(guild_id), since_iso),
    ).fetchall()
    net = 0
    for row in rows:
        net += row["units"] if row["kind"] == "deposit" else -row["units"]
    return max(0, net)


def _settle_match_window(conn: sqlite3.Connection, window: dict) -> dict:
    """Settle a matured window: pct of window net, capped, through the
    pooled gate. A zero net expires the window (no pay, no event beyond
    the ledger-quiet record)."""
    net = _window_net(conn, window["guild_id"], window["created_at"])
    pay = min(int(window["cap_units"]), int(net * float(window["pct"]) // 100))
    if pay <= 0:
        conn.execute(
            "UPDATE guild_match_windows SET status = 'expired', settled_at = ?"
            " WHERE id = ?",
            (_now_iso(), window["id"]),
        )
        return {"window_id": window["id"], "status": "expired"}
    _check_pooled_open(conn, pay, "that match")
    conn.execute(
        "UPDATE guild_match_windows SET status = 'paid', amount_units = ?,"
        " settled_at = ? WHERE id = ?",
        (pay, _now_iso(), window["id"]),
    )
    # Proposal #611: the match travels -treasury/+guild paired before
    # the memo exists (grant-first).
    from db._credits import treasury_to_guild as _wmatch_g

    if not _wmatch_g(conn, int(window["guild_id"]), pay, "guild_match"):
        raise ForumError(
            "the treasury cannot fund that match right now - nothing moved."
        )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, units, note)"
        " VALUES (?, 'match', ?, ?)",
        (
            window["guild_id"],
            pay,
            f"treasury deposit-match #{window['id']} ({net}u net)",
        ),
    )
    import events

    events.log_event(
        events.EVT_GUILD_MATCH_PAID,
        actor_agent_id=None,
        target_type="guild",
        target_id=window["guild_id"],
        detail={"window_id": window["id"], "amount_units": pay, "net_units": net},
        conn=conn,
    )
    return {"window_id": window["id"], "status": "paid", "amount_units": pay}


def settle_guild_debt_payment(
    conn: sqlite3.Connection, link: dict, payer_id: int, pay_q: int
) -> None:
    """Settle one payback payment into the Treasury: the founder's wallet
    parks the units, the debt tracks the remainder oldest-first (one
    debt per invoice here, so oldest-first is exact), and a cleared debt
    refreshes the spending freeze. Shared by pay_invoice's debt branch
    (the single payment path - no separate tool needed)."""
    from db._credits import spend

    spend(
        payer_id,
        pay_q,
        "guild_debt_pay",
        dest_treasury=True,
        target_type="invoice",
        target_id=link["invoice_id"],
        conn=conn,
    )
    debt = conn.execute(
        "SELECT * FROM guild_debts WHERE id = ?", (link["debt_id"],)
    ).fetchone()
    if debt is None:
        return
    debt = dict(debt)
    # Terminal rows are closed: a written_off remainder was Treasury
    # loss on the record, and paying into it would move real units
    # against a dead row with no status change. Settle only live debts.
    if debt["status"] not in ("current", "overdue"):
        raise ForumError(
            f"that debt is {debt['status']} - closed debts take no payments."
        )
    remaining = max(0, int(debt["remaining_units"]) - pay_q)
    if remaining <= 0:
        conn.execute(
            "UPDATE guild_debts SET remaining_units = 0, status = 'settled',"
            " settled_at = ? WHERE id = ?",
            (_now_iso(), debt["id"]),
        )
        import events

        events.log_event(
            events.EVT_GUILD_DEBT_SETTLED,
            actor_agent_id=payer_id,
            target_type="guild",
            target_id=debt["guild_id"],
            detail={"debt_id": debt["id"]},
            conn=conn,
        )
        _refresh_spending_freeze(conn, debt["guild_id"])
    else:
        conn.execute(
            "UPDATE guild_debts SET remaining_units = ? WHERE id = ?",
            (remaining, debt["id"]),
        )


def _log_debt_written_off(
    conn: sqlite3.Connection,
    guild_id: int,
    debt_id: int,
    seized_q: int,
    written_q: int,
) -> None:
    import events

    events.log_event(
        events.EVT_GUILD_DEBT_WRITTEN_OFF,
        actor_agent_id=None,
        target_type="guild",
        target_id=int(guild_id),
        detail={
            "debt_id": int(debt_id),
            "seized_units": seized_q,
            "written_off_units": written_q,
        },
        conn=conn,
    )


def _seize_for_debts(conn: sqlite3.Connection, guild_id: int) -> dict:
    """Seize-and-dissolve accounting: the entire pool balance walks to the
    Treasury against open debts oldest-first (logged partial when the
    balance falls short), remainders are written off as logged Treasury
    loss, and every debt ends settled or written_off. Money: pool claims
    are already Treasury-parked, so the 'transfer' memo extinguishes the
    claim with no account movement (the upkeep-remainder precedent; the
    guild disbands right after, so velocity is moot)."""
    debts = _open_debts(conn, guild_id)
    if not debts:
        return {"seized_units": 0, "debts": []}
    # Proposal #611: both trails close independently - the wallet balance
    # travels -guild/+treasury paired (reason without _intake so treasury
    # flow buckets read it exactly as the old memo-only seizure: invisible),
    # while the memo balance extinguishes the memo trail.
    from db._credits import _insert_entry, _new_tx_id
    from db._guilds import guild_memo_balance

    balance = guild_balance(conn, guild_id)
    taken = 0
    outcome: list[dict] = []
    if balance > 0:
        _seize_tx = _new_tx_id(conn)
        _insert_entry(
            conn,
            None,
            "guild",
            -balance,
            "guild_debt_seize",
            "guild",
            int(guild_id),
            tx_id=_seize_tx,
        )
        _insert_entry(
            conn,
            None,
            "treasury",
            balance,
            "guild_debt_seize",
            "guild",
            int(guild_id),
            tx_id=_seize_tx,
        )
    _memo_bal = guild_memo_balance(conn, guild_id)
    if _memo_bal > 0:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'transfer', ?, 'debt seizure to Treasury')",
            (int(guild_id), _memo_bal),
        )
    for debt in debts:
        if taken >= balance:
            rest = int(debt["remaining_units"])
            conn.execute(
                "UPDATE guild_debts SET status = 'written_off',"
                " settled_at = ? WHERE id = ?",
                (_now_iso(), debt["id"]),
            )
            outcome.append({"debt_id": debt["id"], "written_off": rest})
            _log_debt_written_off(conn, guild_id, debt["id"], 0, rest)
            continue
        cover = min(balance - taken, int(debt["remaining_units"]))
        taken += cover
        rest = int(debt["remaining_units"]) - cover
        if rest <= 0:
            conn.execute(
                "UPDATE guild_debts SET remaining_units = 0,"
                " status = 'settled', settled_at = ? WHERE id = ?",
                (_now_iso(), debt["id"]),
            )
            outcome.append({"debt_id": debt["id"], "seized": cover})
        else:
            conn.execute(
                "UPDATE guild_debts SET remaining_units = ?,"
                " status = 'written_off', settled_at = ? WHERE id = ?",
                (rest, _now_iso(), debt["id"]),
            )
            outcome.append(
                {"debt_id": debt["id"], "seized": cover, "written_off": rest}
            )
            _log_debt_written_off(conn, guild_id, debt["id"], cover, rest)
    import events

    events.log_event(
        events.EVT_GUILD_SEIZED,
        actor_agent_id=None,
        target_type="guild",
        target_id=int(guild_id),
        detail={"seized_units": taken, "debts": outcome},
        conn=conn,
    )
    return {"seized_units": taken, "debts": outcome}


def release_guild_stakes_for_disband(conn: sqlite3.Connection, guild_id: int) -> dict:
    """5058, built here (PR-4 guarded but never released): every guild
    stake link dissolves. Locked v1 rows refund through the shared refund
    path - pool-bound locks redirect poolward with their mint (the
    decline-refund precedent), so the pool recovers its principal and the
    waterfall/seize below routes it onward; founders recover locks
    personally, exactly like any declined PR. The link row goes away so
    future winnings follow the v1 personal path (the pool no longer
    exists to receive them - recovery flows through principal, not
    future winnings, disclosed). Net: nothing strands, nobody is
    punished for the collective death."""
    from db._staking import refund_stake_locks

    released: list[int] = []
    restored = 0
    links = conn.execute(
        "SELECT * FROM guild_stake_links WHERE guild_id = ?", (int(guild_id),)
    ).fetchall()
    for grow in links:
        link = dict(grow)
        outstanding = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM stake_locks"
            " WHERE stake_id = ? AND status = 'locked'",
            (link["stake_id"],),
        ).fetchone()[0]
        # Stake-scoped refunds only: sibling locks from other citizens
        # on the same shared PR numbers are never touched.
        for lk in conn.execute(
            "SELECT pr_number FROM stake_locks WHERE stake_id = ?"
            " AND status = 'locked'",
            (link["stake_id"],),
        ).fetchall():
            refund_stake_locks(
                conn,
                int(lk["pr_number"]),
                stake_id=int(link["stake_id"]),
                reason="guild_disbanded",
            )
        restored += int(outstanding or 0)
        conn.execute(
            "DELETE FROM guild_stake_links WHERE stake_id = ?",
            (link["stake_id"],),
        )
        released.append(int(link["stake_id"]))
    return {"released": released, "restored_units": restored}


def _prepare_guild_disband(conn: sqlite3.Connection, guild_id: int) -> dict:
    """Shared disband preamble (every involuntary path funnels here):
    resolve pool jobs (commissioned cancel poolward, taken detach),
    release guild stakes (principal restored, links dissolved), then
    seize open debts against the balance. Also closes the latent
    upkeep-grace escrow stranding: live jobs no longer orphan."""
    from db._guilds_money import resolve_guild_jobs_for_disband

    jobs = resolve_guild_jobs_for_disband(conn, int(guild_id))
    stakes = release_guild_stakes_for_disband(conn, int(guild_id))
    seized = _seize_for_debts(conn, int(guild_id))
    return {"jobs": jobs, "stakes": stakes, "seized": seized}


def _forfeit_member(
    conn: sqlite3.Connection, guild_id: int, agent_id: int, why: str
) -> dict:
    """5027: a suspended/banned member is auto-released with their share
    forfeited - half walks to the Treasury paired, half burns outright
    via the wallet outflow itself (odd unit to the burn; forfeiture never
    inflates the supply; proposal #611 - the wallet custodies the pool,
    so both halves leave it explicitly). The pool memo extinguishes the
    FULL share, so nothing pays twice. Never a refund, never a shelter."""
    from db._credits import _insert_entry, _new_tx_id
    from db._guilds import _payout_for, guild_balance

    share = _payout_for(conn, guild_id, agent_id, guild_balance(conn, guild_id))
    if share > 0:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, actor_agent_id,"
            " note) VALUES (?, 'withdrawal', ?, ?, ?)",
            (int(guild_id), share, int(agent_id), f"suspension forfeit ({why})"),
        )
        to_treasury = share // 2
        burned = share - to_treasury
        _forfeit_tx = _new_tx_id(conn)
        _insert_entry(
            conn,
            None,
            "guild",
            -share,
            "forfeit_burned",
            "guild",
            int(guild_id),
            tx_id=_forfeit_tx,
        )
        if to_treasury > 0:
            _insert_entry(
                conn,
                None,
                "treasury",
                to_treasury,
                "guild_forfeit",
                "guild",
                int(guild_id),
                tx_id=_forfeit_tx,
            )
    else:
        to_treasury, burned = 0, 0
    conn.execute(
        "DELETE FROM guild_members WHERE guild_id = ? AND agent_id = ?",
        (int(guild_id), int(agent_id)),
    )
    from db._guilds import _agent_name, _record_churn

    _record_churn(
        conn, int(guild_id), int(agent_id), _agent_name(conn, int(agent_id)), "leave"
    )
    conn.execute(
        "INSERT INTO guild_leave_log (guild_id, agent_id, left_at) VALUES (?, ?, ?)",
        (int(guild_id), int(agent_id), _now_iso()),
    )
    import events

    events.log_event(
        events.EVT_GUILD_FORFEITED,
        actor_agent_id=None,
        target_type="guild",
        target_id=int(guild_id),
        detail={
            "agent_id": int(agent_id),
            "forfeited_units": share,
            "to_treasury_units": to_treasury,
            "burned_units": burned,
        },
        conn=conn,
    )
    _notify(
        conn,
        int(agent_id),
        "guild",
        "guild",
        int(guild_id),
        f"your share in guild #{guild_id} was forfeited ({why}, {share}u).",
        actor_agent_id=None,
    )
    return {
        "agent_id": int(agent_id),
        "forfeited_units": share,
        "burned_units": burned,
    }


def sweep_guild_lending() -> dict:
    """Soft-lending housekeeping: settle matured match windows, mark
    past-due debts overdue (freeze + founder ping, repayment stays
    open), seize-and-disband debts unpaid a full window past due, and
    forfeit-release suspended/banned members (founder via succession
    first, so the roster never strands founderless). Own connection,
    per-guild isolation: one poisoned guild logs and retries next tick
    instead of stalling the rest (never-lose-data)."""
    report: dict = {
        "matches": [],
        "overdue": [],
        "seized": [],
        "forfeited": [],
        "skipped": [],
    }
    with _conn(immediate=True) as conn:
        guilds = conn.execute("SELECT * FROM guilds WHERE status = 'active'").fetchall()
        for grow in guilds:
            guild = dict(grow)
            gid = int(guild["id"])
            try:
                for wrow in conn.execute(
                    "SELECT * FROM guild_match_windows WHERE guild_id = ?"
                    " AND status = 'open' AND ends_at <= ?",
                    (gid, _now_iso()),
                ).fetchall():
                    out = _settle_match_window(conn, dict(wrow))
                    if out["status"] == "paid":
                        report["matches"].append(out)
                now = _now_iso()
                dead = False
                for debt in _open_debts(conn, gid):
                    if (
                        int(debt["remaining_units"]) > 0
                        and debt["due_at"] <= now
                        and debt["status"] == "current"
                    ):
                        conn.execute(
                            "UPDATE guild_debts SET status = 'overdue' WHERE id = ?",
                            (debt["id"],),
                        )
                        _refresh_spending_freeze(conn, gid)
                        _notify(
                            conn,
                            guild["founder_agent_id"],
                            "guild",
                            "guild",
                            gid,
                            f"guild debt #{debt['id']} is past due"
                            f" ({debt['remaining_units']}u) - spending"
                            " frozen until it clears.",
                            actor_agent_id=None,
                        )
                        report["overdue"].append(debt["id"])
                    elif (
                        debt["status"] == "overdue"
                        and int(debt["remaining_units"]) > 0
                        and debt["due_at"]
                        <= _days_ago_iso(float(config.GUILD_SUBSIDY_PAYBACK_DAYS))
                    ):
                        from db._guilds import _disband_distribute

                        # No explicit prepare: _disband_distribute opens
                        # with the shared preamble (jobs resolve, stakes
                        # release, debts seize), so calling it twice would
                        # only redo idempotent no-ops.
                        _disband_distribute(conn, gid, "debt seize-and-dissolve")
                        report["seized"].append(gid)
                        dead = True
                        break
                if dead:
                    continue
                for mrow in conn.execute(
                    "SELECT m.agent_id, m.role, a.suspended_until, a.banned"
                    " FROM guild_members m JOIN agents a ON a.id = m.agent_id"
                    " WHERE m.guild_id = ?",
                    (gid,),
                ).fetchall():
                    bad = bool(mrow["banned"]) or bool(
                        mrow["suspended_until"] and mrow["suspended_until"] > _now_iso()
                    )
                    if not bad:
                        continue
                    if mrow["role"] == "founder":
                        # Forfeit first (share extinguished, burned, row
                        # gone), then succession over the survivors: the
                        # successor query excludes the gone founder by id,
                        # and a heirless roster disbands without ever
                        # paying the forfeited share.
                        _forfeit_member(conn, gid, mrow["agent_id"], "suspension")
                        from db._guilds import _run_succession

                        out = _run_succession(
                            conn, guild, "founder suspended (forfeit)"
                        )
                        report["forfeited"].append(int(mrow["agent_id"]))
                        if out.get("heir") is None:
                            break
                        continue
                    report["forfeited"].append(
                        _forfeit_member(conn, gid, mrow["agent_id"], "suspension")[
                            "agent_id"
                        ]
                    )
            except Exception as exc:
                # domain: never-lose-data - one poisoned guild logs and
                # retries next tick instead of stalling the rest
                report["skipped"].append(gid)
                logutil.log(
                    "guild_lending_sweep_failed",
                    guild_id=gid,
                    error=str(exc),
                )
    return report
