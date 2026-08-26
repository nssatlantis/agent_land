"""db._credits — the credits economy (the Karma Split, phase two: the treasury).

Credits are the spendable valuta: contributions earn them, voluntary
spends (tags, stakes) debit them, wallets transfer them.  Karma stays the
reputation layer - every trust floor reads karma and is untouched here.

Denomination: QUARTER-CREDITS.  Every entry stores an integer number of
quarters (4 quarters = 1.0 credit); whole, half and quarter values are
the only amounts that exist.  Because karma awards are integers and the
configured KARMA_TO_CREDIT_RATIO is validated to whole/half/quarter
precision, the earn rate is an exact integer number of quarters per
karma point - so every entry the system can ever write is automatically
a legal quarter value (
nothing finer can be represented, so no rounding logic exists anywhere
past intake.  Floats appear only at the display edge, formatted as n/4
(".0" / ".25" / ".5" / ".75").

The balance is DERIVED as SUM(delta_quarters) rather than cached on the
agent row - the same philosophy as karma's six-source sums, so a balance
cannot drift from its own history.  Entries are appended inside the
triggering transaction (pass conn= like notifications/log_event), and each
economic action lands in the events ledger under its own category
(credit_earned / credit_spent / credit_transferred / ...) for full
traceability.

ACCOUNTS (the treasury economy): the `account` column splits the one
append-only ledger into 'agent' rows (citizen wallets) and 'treasury'
rows (the community treasury, agent_id NULL).  Every payout, transfer,
fee and forfeiture is written as PAIRED single-entry legs (-from / +to),
while mints add to and burns subtract from the treasury - so at any
moment:

    total supply = SUM(delta_quarters) over ALL rows
    treasury     = SUM over account='treasury' rows
    circulating  = supply - treasury

When TREASURY_FUNDS_PAYOUTS is on, earnings are paid OUT of the treasury
(never minted from nothing); an empty treasury skips the payout and logs
a visible credit_payout_unfunded event - scarcity is real, and topping
the treasury back up is a governed mint (db._economy), never an automatic
side effect.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config

from db._core import ForumError, _conn, _require_active_agent

QUARTERS_PER_CREDIT = 4
TRANSFER_NOTE_MAX_LEN = 200


def to_quarters(credits: float) -> int:
    """Convert a user-supplied credit amount into integer quarters,
    rounding to the NEAREST quarter (ties up).  This is the single intake
    boundary: 2.3 -> 9q (2.25), 2.4 -> 10q (2.5).  Everything downstream
    is integer math."""
    return int(round(float(credits) * QUARTERS_PER_CREDIT))


def quarters_per_karma() -> int:
    """The earning rate in ledger units, derived from the configured
    KARMA_TO_CREDIT_RATIO. The ratio must itself be a whole/half/quarter
    value so integer karma awards map to exact quarter amounts - a finer
    ratio (0.1, 0.3...) is refused loudly instead of silently rounding
    citizens' income."""
    ratio = config.KARMA_TO_CREDIT_RATIO
    q = round(ratio * QUARTERS_PER_CREDIT)
    if abs(ratio * QUARTERS_PER_CREDIT - q) > 1e-9 or q < 0:
        raise ForumError(
            f"FORUM_KARMA_TO_CREDIT_RATIO must be a whole, half or "
            f"quarter value (got {ratio})."
        )
    return q


def exact_from_credits(credits: float, *, what: str) -> int:
    """Convert an EXACT price/amount from credits into quarters, refusing
    anything that is not whole/half/quarter. Used for configured prices -
    unlike to_quarters() (stake intake), mis-set prices must fail loudly,
    never silently snap.""" 
    q = round(float(credits) * QUARTERS_PER_CREDIT)
    if abs(float(credits) * QUARTERS_PER_CREDIT - q) > 1e-9:
        raise ForumError(
            f"{what} must be a whole, half or quarter credit value "
            f"(got {credits})."
        )
    return q


def format_credits(quarters: int) -> str:
    """Render quarters as a friendly decimal string ('8' -> '2', '9' ->
    '2.25', '10' -> '2.5').  Only .0/.25/.5/.75 fractions exist by
    construction."""
    sign = "-" if quarters < 0 else ""
    q = abs(quarters)
    whole, rem = divmod(q, QUARTERS_PER_CREDIT)
    frac = {0: "", 1: ".25", 2: ".5", 3: ".75"}[rem]
    return f"{sign}{whole}{frac}"


def _insert_entry(
    c: sqlite3.Connection,
    agent_id: int | None,
    account: str,
    delta_quarters: int,
    reason: str,
    target_type: str | None,
    target_id: int | None,
) -> None:
    """Append one ledger row.  Caller owns the transaction and has already
    validated balances; events are emitted by the public operations, one
    event per economic action (a transfer writes two rows, one event)."""
    c.execute(
        "INSERT INTO credit_entries"
        " (agent_id, delta_quarters, reason, target_type, target_id, account)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, delta_quarters, reason, target_type, target_id, account),
    )


def treasury_balance(conn: sqlite3.Connection) -> int:
    """The community treasury's balance in quarters (derived, never cached)."""
    return conn.execute(
        "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
        " WHERE account = 'treasury'"
    ).fetchone()[0]


def fee_quarters(amount_quarters: int) -> int:
    """The transaction fee for moving `amount_quarters`, rounded UP to
    whole quarters (the sender pays the rounding), 100% to the treasury.
    A tiny epsilon guards the ceil against float noise like 4 * 1.0 being
    4.000000000000001."""
    pct = max(0.0, float(config.TX_FEE_PERCENT))
    if pct == 0 or amount_quarters <= 0:
        return 0
    return int(math.ceil(amount_quarters * pct / 100.0 - 1e-9))


def grant(
    agent_id: int,
    delta_quarters: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Credit quarters to a citizen for a contribution.  With
    TREASURY_FUNDS_PAYOUTS on, the payout is drawn from the community
    treasury (-treasury / +agent pair inside one transaction); an empty
    treasury skips the payout entirely and logs a visible
    credit_payout_unfunded event - earnings are never minted from nothing.
    Returns False when earning is disabled by config, the delta is zero,
    or the treasury could not fund it; the caller decides whether that is
    fine.  Pass conn when already inside a transaction."""
    if not config.CREDITS_ENABLED or delta_quarters == 0:
        return False
    with _conn() if conn is None else nullcontext(conn) as c:
        if config.TREASURY_FUNDS_PAYOUTS:
            if treasury_balance(c) < delta_quarters:
                import events

                events.log_event(
                    events.EVT_CREDIT_PAYOUT_UNFUNDED,
                    actor_agent_id=None,
                    target_type="credit",
                    target_id=agent_id,
                    detail={
                        "reason": reason,
                        "credits": format_credits(delta_quarters),
                        "delta_quarters": delta_quarters,
                        "treasury_credits": format_credits(treasury_balance(c)),
                    },
                    conn=c,
                )
                return False
            _insert_entry(
                c, None, "treasury", -delta_quarters, "payout_source",
                target_type, target_id,
            )
            _insert_entry(
                c, agent_id, "agent", delta_quarters, reason,
                target_type, target_id,
            )
            import events

            events.log_event(
                events.EVT_CREDIT_EARNED,
                actor_agent_id=agent_id,
                target_type=target_type or "credit",
                target_id=target_id,
                detail={
                    "reason": reason,
                    "credits": format_credits(delta_quarters),
                    "delta_quarters": delta_quarters,
                    "funded_by": "treasury",
                },
                conn=c,
            )
            return True
        _insert_entry(
            c, agent_id, "agent", delta_quarters, reason,
            target_type, target_id,
        )
        import events

        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=agent_id,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": reason,
                "credits": format_credits(delta_quarters),
                "delta_quarters": delta_quarters,
            },
            conn=c,
        )
    return True


def spend(
    agent_id: int,
    amount_quarters: int,
    reason: str,
    *,
    dest_treasury: bool = False,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Debit quarters from a citizen for a voluntary spend.  Raises when
    the balance cannot cover it - the refusal mirrors karma's effective-
    karma gate, but a credit balance never goes negative (spends are
    bounded by earnings; penalties live on the karma layer).

    dest_treasury=True (tag costs) recycles the spent amount INTO the
    community treasury instead of destroying it - a paired -agent /
    +treasury write inside the same transaction.  Stake locks keep
    dest_treasury=False: their credits are merely locked, refunded later,
    so no second row exists until the refund pays out."""
    if amount_quarters == 0:
        return False
    if amount_quarters < 0:
        raise ForumError("credit amounts must be positive.")
    with _conn() if conn is None else nullcontext(conn) as c:
        balance = balance_for(c, agent_id)
        if balance < amount_quarters:
            raise ForumError(
                f"insufficient credits: this costs "
                f"{format_credits(amount_quarters)} but you have "
                f"{format_credits(balance)}."
            )
        _insert_entry(
            c, agent_id, "agent", -amount_quarters, reason,
            target_type, target_id,
        )
        if dest_treasury:
            _insert_entry(
                c, None, "treasury", amount_quarters, f"{reason}_intake",
                target_type, target_id,
            )
        import events

        detail: dict[str, object] = {
            "reason": reason,
            "credits": format_credits(amount_quarters),
            "delta_quarters": amount_quarters,
        }
        if dest_treasury:
            detail["to"] = "treasury"
        events.log_event(
            events.EVT_CREDIT_SPENT,
            actor_agent_id=agent_id,
            target_type=target_type or "credit",
            target_id=target_id,
            detail=detail,
            conn=c,
        )
    return True


def return_principal(
    agent_id: int,
    amount_quarters: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Return ESCROWED quarters to a citizen: stake refunds and stake
    payouts whose matching debit was written when the lock was taken.
    These are the second half of a principal move, never new income -
    they bypass treasury funding by definition (the value left a wallet
    when the lock was written; it re-enters circulation here)."""
    if not config.CREDITS_ENABLED or amount_quarters == 0:
        return False
    with _conn() if conn is None else nullcontext(conn) as c:
        _insert_entry(
            c, agent_id, "agent", amount_quarters, reason,
            target_type, target_id,
        )
        import events

        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=agent_id,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": reason,
                "credits": format_credits(amount_quarters),
                "delta_quarters": amount_quarters,
                "escrow_return": True,
            },
            conn=c,
        )
    return True


def refund(
    agent_id: int,
    amount_quarters: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Return previously-spent quarters (stake refunds/withdrawals).  A
    principal return with a stake-flow reason - never treasury-funded."""
    return_principal(agent_id, amount_quarters, reason,
                     target_type=target_type, target_id=target_id,
                     conn=conn)


# -- treasury operations (executed by db._economy's governance gate) -----


def mint(
    delta_quarters: int,
    reason: str,
    *,
    admin: str,
    proposal_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Create new credits in the community treasury (+treasury row).
    Total supply grows by exactly this amount.  Caller (db._economy)
    enforces the cap / proposal gates; this is the ledger primitive."""
    if delta_quarters <= 0:
        raise ForumError("mint amount must be positive.")
    with _conn() if conn is None else nullcontext(conn) as c:
        _insert_entry(
            c, None, "treasury", delta_quarters, reason, "economy", proposal_id,
        )
        import events

        detail: dict[str, object] = {
            "reason": reason,
            "credits": format_credits(delta_quarters),
            "delta_quarters": delta_quarters,
            "admin": admin,
        }
        if proposal_id is not None:
            detail["proposal_id"] = proposal_id
        events.log_event(
            events.EVT_CREDIT_MINTED,
            actor_agent_id=None,
            target_type="economy",
            target_id=proposal_id,
            detail=detail,
            conn=c,
        )
        return {
            "minted_quarters": delta_quarters,
            "minted_credits": format_credits(delta_quarters),
            "treasury_quarters": treasury_balance(c),
            "treasury_credits": format_credits(treasury_balance(c)),
        }


def burn(
    delta_quarters: int,
    reason: str,
    *,
    admin: str,
    proposal_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Destroy credits from the community treasury (-treasury row).  The
    treasury cannot go negative - burning more than it holds is refused.
    Caller (db._economy) enforces the cap / proposal gates."""
    if delta_quarters <= 0:
        raise ForumError("burn amount must be positive.")
    with _conn() if conn is None else nullcontext(conn) as c:
        if treasury_balance(c) < delta_quarters:
            raise ForumError(
                f"insufficient treasury credits: burning "
                f"{format_credits(delta_quarters)} but the treasury holds "
                f"{format_credits(treasury_balance(c))}."
            )
        _insert_entry(
            c, None, "treasury", -delta_quarters, reason, "economy", proposal_id,
        )
        import events

        detail: dict[str, object] = {
            "reason": reason,
            "credits": format_credits(delta_quarters),
            "delta_quarters": delta_quarters,
            "admin": admin,
        }
        if proposal_id is not None:
            detail["proposal_id"] = proposal_id
        events.log_event(
            events.EVT_CREDIT_BURNED,
            actor_agent_id=None,
            target_type="economy",
            target_id=proposal_id,
            detail=detail,
            conn=c,
        )
        return {
            "burned_quarters": delta_quarters,
            "burned_credits": format_credits(delta_quarters),
            "treasury_quarters": treasury_balance(c),
            "treasury_credits": format_credits(treasury_balance(c)),
        }


# -- wallet transfers -----------------------------------------------------


def _active_wallet(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row:
    """The agents row of an existing, non-banned, non-suspended citizen -
    both transfer endpoints must be active wallets (suspended citizens
    forfeit their credits anyway, and dead wallets must not receive)."""
    row = conn.execute(
        "SELECT id, name, banned, suspended_until FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if row is None:
        raise ForumError(f"no citizen with id {agent_id}.")
    if row["banned"]:
        raise ForumError(f"citizen {row['name']} is banned.")
    if row["suspended_until"] and row["suspended_until"] > now_iso:
        raise ForumError(f"citizen {row['name']} is suspended.")
    return row


def transfer_credits(
    sender_id: int,
    recipient: int | str,
    amount_quarters: int,
    note: str = "",
    *,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Move credits between wallets: citizen-to-citizen or citizen-to-
    treasury (recipient='treasury').  Charges the FORUM_TX_FEE_PERCENT fee
    (rounded up to whole quarters, 100% to the treasury) on top of the
    amount.  One transaction, paired ledger rows, ONE credit_transferred
    event.  Both endpoints must be active citizens; self-transfers and
    non-positive amounts are refused; the sender's balance must cover
    amount + fee."""
    if not config.CREDITS_ENABLED:
        raise ForumError("credits are disabled on this forum.")
    if amount_quarters <= 0:
        raise ForumError("transfer amount must be positive.")
    note = (note or "").strip()[:TRANSFER_NOTE_MAX_LEN]
    fee_q = fee_quarters(amount_quarters)
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        sender = _active_wallet(c, sender_id)
        to_treasury = isinstance(recipient, str) and recipient.strip().lower() == "treasury"
        recipient_row: sqlite3.Row | None = None
        if not to_treasury:
            rid = recipient
            if isinstance(rid, str):
                found = c.execute(
                    "SELECT id FROM agents WHERE name = ? COLLATE NOCASE",
                    (rid.strip(),),
                ).fetchone()
                if found is None:
                    raise ForumError(f"no citizen named '{rid.strip()}'.")
                rid = found["id"]
            if rid == sender_id:
                raise ForumError("you cannot transfer credits to yourself.")
            recipient_row = _active_wallet(c, rid)
        balance = balance_for(c, sender_id)
        needed = amount_quarters + fee_q
        if balance < needed:
            raise ForumError(
                f"insufficient credits: transferring "
                f"{format_credits(amount_quarters)}"
                + (f" + {format_credits(fee_q)} fee" if fee_q else "")
                + f" needs {format_credits(needed)}, you have "
                f"{format_credits(balance)}."
            )
        # Leg 1: leave the sender's wallet.
        _insert_entry(
            c, sender_id, "agent", -amount_quarters, "transfer_out",
            "agent", recipient_row["id"] if recipient_row else None,
        )
        # Leg 2: arrive in the destination wallet.
        if recipient_row is not None:
            _insert_entry(
                c, recipient_row["id"], "agent", amount_quarters,
                "transfer_in", "agent", sender_id,
            )
        else:
            _insert_entry(
                c, None, "treasury", amount_quarters, "transfer_intake",
                "agent", sender_id,
            )
        # Leg 3+4: the fee, always to the treasury.
        if fee_q:
            _insert_entry(
                c, sender_id, "agent", -fee_q, "transfer_fee",
                "treasury", None,
            )
            _insert_entry(
                c, None, "treasury", fee_q, "transfer_fee_intake",
                "agent", sender_id,
            )
        import events

        detail: dict[str, object] = {
            "from_name": sender["name"],
            "to_name": recipient_row["name"] if recipient_row else "Treasury",
            "credits": format_credits(amount_quarters),
            "delta_quarters": amount_quarters,
            "fee_credits": format_credits(fee_q),
            "note": note,
        }
        if recipient_row is not None:
            detail["to_agent_id"] = recipient_row["id"]
        else:
            detail["to_treasury"] = True
        events.log_event(
            events.EVT_CREDIT_TRANSFERRED,
            actor_agent_id=sender_id,
            target_type="agent" if recipient_row else "treasury",
            target_id=recipient_row["id"] if recipient_row else None,
            detail=detail,
            conn=c,
        )
        new_sender = balance_for(c, sender_id)
        return {
            "sent_quarters": amount_quarters,
            "sent_credits": format_credits(amount_quarters),
            "fee_quarters": fee_q,
            "fee_credits": format_credits(fee_q),
            "to_treasury": to_treasury,
            "to_agent_id": recipient_row["id"] if recipient_row else None,
            "to_name": detail["to_name"],
            "note": note,
            "new_balance_quarters": new_sender,
            "new_balance_credits": format_credits(new_sender),
        }


def transfer(
    token: str,
    recipient: int | str,
    amount_credits: float,
    note: str = "",
) -> dict:
    """Authenticated wallet transfer (the MCP entry point): resolves the
    sender from the token, converts the amount at quarter intake
    (nearest quarter, ties up), and moves the credits with the standard
    transaction fee."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
    quarters = to_quarters(amount_credits)
    if quarters <= 0:
        raise ForumError("transfer amount must be positive.")
    return transfer_credits(agent["id"], recipient, quarters, note=note)


# -- suspension forfeiture ------------------------------------------------


def forfeit_agent(
    agent_id: int, *, conn: sqlite3.Connection | None = None
) -> dict | None:
    """A suspended citizen loses ALL their credits: half goes to the
    community treasury, half is burned outright (floor division biases the
    odd quarter toward the burn - forfeiture never inflates the supply).
    Written inside the suspension's own transaction when conn is passed;
    a zero-balance citizen is a no-op.  One-way: reinstatement does not
    restore anything."""
    with _conn() if conn is None else nullcontext(conn) as c:
        balance = balance_for(c, agent_id)
        if balance <= 0:
            return None
        to_treasury = balance // 2
        burned = balance - to_treasury
        if to_treasury > 0:
            _insert_entry(
                c, agent_id, "agent", -to_treasury, "forfeit_to_treasury",
                "treasury", None,
            )
            _insert_entry(
                c, None, "treasury", to_treasury, "forfeit_intake",
                "agent", agent_id,
            )
        if burned > 0:
            _insert_entry(
                c, agent_id, "agent", -burned, "forfeit_burned",
                "treasury", None,
            )
        import events

        events.log_event(
            events.EVT_CREDIT_FORFEITED,
            actor_agent_id=None,
            target_type="agent",
            target_id=agent_id,
            detail={
                "forfeited_credits": format_credits(balance),
                "forfeited_quarters": balance,
                "to_treasury_credits": format_credits(to_treasury),
                "burned_credits": format_credits(burned),
            },
            conn=c,
        )
        return {
            "forfeited_quarters": balance,
            "to_treasury_quarters": to_treasury,
            "burned_quarters": burned,
        }


def balance_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """A citizen's credit balance in quarters (derived, never cached)."""
    return conn.execute(
        "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
        " WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()[0]


def balance_many(conn: sqlite3.Connection, agent_ids: list[int]) -> dict[int, int]:
    """Balances in quarters for a batch of agents in one GROUP BY query -
    the same shape as effective_karma_many."""
    if not agent_ids:
        return {}
    marks = ",".join("?" * len(agent_ids))
    rows = conn.execute(
        f"SELECT agent_id, COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
        f" WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall()
    found = {r[0]: r[1] for r in rows}
    return {aid: found.get(aid, 0) for aid in agent_ids}


def balances_for(agent_ids: list[int]) -> dict[int, int]:
    """Balances for a batch of agents, managing its own connection -
    the form server handlers call."""
    with _conn() as conn:
        return balance_many(conn, agent_ids)


def earned_summary(
    conn: sqlite3.Connection, agent_id: int
) -> dict[str, int]:
    """Earning windows for profile displays: total earned vs spent, plus
    earned since UTC week start (Monday) and month start."""
    now_dt = datetime.now(timezone.utc)
    week_start = (now_dt - timedelta(days=now_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _iso(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sum(extra_where: str = "", params: tuple = ()) -> int:
        cond = f" AND {extra_where}" if extra_where else ""
        return conn.execute(
            f"SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            f" WHERE agent_id = ? AND delta_quarters > 0{cond}",
            (agent_id, *params),
        ).fetchone()[0]

    spent = conn.execute(
        "SELECT COALESCE(SUM(-delta_quarters), 0) FROM credit_entries"
        " WHERE agent_id = ? AND delta_quarters < 0",
        (agent_id,),
    ).fetchone()[0]
    return {
        "earned_total_quarters": _sum(),
        "earned_this_week_quarters": _sum("created_at >= ?",
                                          (_iso(week_start),)),
        "earned_this_month_quarters": _sum("created_at >= ?",
                                           (_iso(month_start),)),
        "spent_total_quarters": spent,
    }


def history(
    agent_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """The public credits ledger, newest first.  Optional agent filter;
    every row names its reason and target so any citizen can audit any
    balance down to its entries."""
    with _conn() as conn:
        clauses, params = [], []
        if agent_id is not None:
            clauses.append("e.agent_id = ?")
            params.append(agent_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT e.id, e.agent_id, e.account,"
            f" COALESCE(a.name,"
            f"   CASE WHEN e.account = 'treasury' THEN 'Treasury' END)"
            f"   AS agent_name,"
            f" e.delta_quarters, e.reason, e.target_type, e.target_id,"
            f" e.created_at"
            f" FROM credit_entries e LEFT JOIN agents a ON a.id = e.agent_id"
            f"{where} ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM credit_entries e{where}", params
        ).fetchone()[0]
        entries = [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"] or "(deleted citizen)",
                "account": r["account"],
                "credits": format_credits(r["delta_quarters"]),
                "delta_quarters": r["delta_quarters"],
                "reason": r["reason"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "created_at": r["created_at"],
            }
            for r in rows[:limit]
        ]
        balances = (
            balance_many(conn, [agent_id]) if agent_id is not None else {}
        )
        summary = (
            {
                "balance_quarters": balances[agent_id],
                **earned_summary(conn, agent_id),
            }
            if agent_id is not None
            else {}
        )
        return {
            "entries": entries,
            "total": total,
            "has_more": len(rows) > limit,
            "summary": summary,
        }
