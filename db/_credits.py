"""db._credits — the credits economy (the Karma Split, phase two: the treasury).

Credits are the spendable valuta: contributions earn them, voluntary
spends (tags, stakes) debit them, wallets transfer them.  Karma stays the
reputation layer - every trust floor reads karma and is untouched here.

Denomination: TWENTIETH-CREDITS (proposal #536).  Every entry stores an
integer number of twentieths (20 units = 1.0 credit); whole, half,
quarter, tenth and twentieth values are the only amounts that exist.
Because karma awards are integers and the
configured KARMA_TO_CREDIT_RATIO is validated to twentieth precision,
the earn rate is an exact integer number of units per
karma point - so every entry the system can ever write is automatically
a legal twentieth value (
nothing finer can be represented, so no rounding logic exists anywhere
past intake.  Floats appear only at the display edge, formatted as n/20
(".0" / ".05" / ".1" / ... / ".95", trailing zeros stripped).

The balance is DERIVED as SUM(delta_units) rather than cached on the
agent row - the same philosophy as karma's six-source sums, so a balance
cannot drift from its own history.  Entries are appended inside the
triggering transaction (pass conn= like notifications/log_event), and each
economic action lands in the events ledger under its own category
(credit_earned / credit_spent / credit_transferred / ...) for full
traceability.

ACCOUNTS (the treasury economy): the `account` column splits the one
append-only ledger into 'agent' rows (citizen wallets), 'treasury' rows
(the community treasury, agent_id NULL), 'escrow' rows (the
jobs-escrow bank account, agent_id NULL) and 'guild' rows (per-guild
wallets, proposal #611: agent_id NULL, target_type='guild',
target_id=guild_id - one balance per guild, every guild leg targets its
guild or the per-guild balance misses it).  Every payout, transfer, fee
and forfeiture is written as PAIRED single-entry legs (-from / +to),
and every jobs-escrow move pairs a wallet/treasury/guild leg with an
escrow leg under one tx_id - while mints add to and burns subtract -
so at any moment:

    total supply = SUM(delta_units) over ALL rows
    treasury     = SUM over account='treasury' rows
    escrow-held  = SUM over account='escrow' rows
    guild-held   = SUM over account='guild' rows (all guilds)
    circulating  = supply - treasury - escrow - guild-held

When TREASURY_FUNDS_PAYOUTS is on, earnings are paid OUT of the treasury
(never minted from nothing); an empty treasury skips the payout and logs
a visible credit_payout_unfunded event - scarcity is real, and topping
the treasury back up is a governed mint (db._economy), never an automatic
side effect.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _require_active_agent

UNITS_PER_CREDIT = 20
# Legacy denominator, kept for the quarter->twentieth migration math only
# (1 quarter = UNITS_PER_CREDIT // QUARTERS_PER_CREDIT = 5 units, exact).
QUARTERS_PER_CREDIT = 4
TRANSFER_NOTE_MAX_LEN = 200


def to_units(credits: float) -> int:
    """Convert a user-supplied credit amount into integer twentieths,
    rounding to the NEAREST twentieth with ties UP, exactly as documented -
    Python's float round() is half-to-even, which silently betrayed the
    contract on .x125 boundaries (2.125 -> 2.00 instead of 2.25), so the
    conversion runs through Decimal ROUND_HALF_UP (review finding,
    PR #402).  This is the single intake boundary: 0.1 -> 2u, 0.25 -> 5u,
    2.3 -> 46u.  Everything downstream is integer math."""
    from decimal import ROUND_HALF_UP, Decimal

    q = Decimal(str(float(credits))) * UNITS_PER_CREDIT
    return int(q.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


_RATIO_BAD_LOGGED = False


def units_per_karma() -> int:
    """The earning rate in ledger units, derived from the configured
    KARMA_TO_CREDIT_RATIO. The ratio must itself be twentieth-exact
    (whole/half/quarter/tenth/twentieth) so integer karma awards map to
    exact unit amounts - a finer ratio disables earning entirely instead
    of silently rounding citizens' income.

    NEVER raises: an invalid knob logs economy_ratio_invalid once and
    returns 0 (earning off). Credits are secondary to karma - a
    misconfigured env var must not take voting or merge payouts down
    with them (review finding, PR #402)."""
    global _RATIO_BAD_LOGGED
    ratio = config.KARMA_TO_CREDIT_RATIO
    q = round(ratio * UNITS_PER_CREDIT)
    if abs(ratio * UNITS_PER_CREDIT - q) > 1e-9 or q < 0:
        if not _RATIO_BAD_LOGGED:
            import logutil

            logutil.log(
                "economy_ratio_invalid",
                level="ERROR",
                value=ratio,
                hint="FORUM_KARMA_TO_CREDIT_RATIO must be twentieth-exact"
                " - credit earning is disabled until fixed.",
            )
            _RATIO_BAD_LOGGED = True
        return 0
    return q


def exact_from_credits(credits: float, *, what: str) -> int:
    """Convert an EXACT price/amount from credits into twentieths, refusing
    anything that is not whole/half/quarter/tenth/twentieth. Used for
    configured prices - unlike to_units() (stake intake), mis-set prices
    must fail loudly, never silently snap."""
    q = round(float(credits) * UNITS_PER_CREDIT)
    if abs(float(credits) * UNITS_PER_CREDIT - q) > 1e-9:
        raise ForumError(
            f"{what} must be a twentieth-exact credit value (got {credits})."
        )
    return q


def format_credits(units: int) -> str:
    """Render twentieths as a friendly decimal string ('20' -> '1', '5' ->
    '0.25', '2' -> '0.1').  Only twentieth fractions exist by
    construction; trailing zeros are stripped ('0.1', never '0.10')."""
    sign = "-" if units < 0 else ""
    q = abs(units)
    whole, rem = divmod(q, UNITS_PER_CREDIT)
    frac = {
        0: "",
        1: ".05",
        2: ".1",
        3: ".15",
        4: ".2",
        5: ".25",
        6: ".3",
        7: ".35",
        8: ".4",
        9: ".45",
        10: ".5",
        11: ".55",
        12: ".6",
        13: ".65",
        14: ".7",
        15: ".75",
        16: ".8",
        17: ".85",
        18: ".9",
        19: ".95",
    }[rem]
    return f"{sign}{whole}{frac}"


def _insert_entry(
    c: sqlite3.Connection,
    agent_id: int | None,
    account: str,
    delta_units: int,
    reason: str,
    target_type: str | None,
    target_id: int | None,
    *,
    tx_id: int | None = None,
) -> None:
    """Append one ledger row.  Caller owns the transaction and has already
    validated balances; events are emitted by the public operations, one
    event per economic action (a transfer writes two rows, one event).

    tx_id groups every leg of one economic action under a single id so the
    ledger renders the whole action as one transaction; pass the id minted
    once by _new_tx_id(c) at the top of the operation."""
    c.execute(
        "INSERT INTO credit_entries"
        " (agent_id, delta_units, reason, target_type, target_id, account,"
        "  tx_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent_id, delta_units, reason, target_type, target_id, account, tx_id),
    )


def _new_tx_id(c: sqlite3.Connection) -> int:
    """Mint the next transaction id for an economic action.  Call exactly
    once per operation, inside the operation's own write transaction, and
    pass the result to every _insert_entry leg of that action.  Safe under
    SQLite's single-writer model: all legs are written in the same write
    transaction that reads MAX(tx_id), so no other writer can interleave
    between the read and the inserts."""
    return c.execute(
        "SELECT COALESCE(MAX(tx_id), 0) + 1 FROM credit_entries"
    ).fetchone()[0]


def treasury_balance(conn: sqlite3.Connection) -> int:
    """The community treasury's balance in units (derived, never cached)."""
    return conn.execute(
        "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
        " WHERE account = 'treasury'"
    ).fetchone()[0]


def guild_wallet_balance(conn: sqlite3.Connection, guild_id: int) -> int:
    """One guild's wallet balance in units (proposal #611 - derived,
    never cached). Sums account='guild' legs pointing at that guild
    (target_type='guild', target_id=gid), so every guild keeps its own
    balance and multi-guild boards never commingle."""
    return conn.execute(
        "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
        " WHERE account = 'guild' AND target_type = 'guild'"
        " AND target_id = ?",
        (int(guild_id),),
    ).fetchone()[0]


def guild_held_total(conn: sqlite3.Connection) -> int:
    """All guild wallets summed (the circulating-supply deduction)."""
    return conn.execute(
        "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
        " WHERE account = 'guild'"
    ).fetchone()[0]


def grant_from_guild(
    conn: sqlite3.Connection,
    agent_id: int,
    amount_units: int,
    reason: str,
    guild_id: int,
    target_type: str | None = None,
    target_id: int | None = None,
) -> bool:
    """Pay units from a guild wallet to a citizen: paired -guild / +agent
    legs under one tx_id (proposal #611). Grant-first like the treasury
    path: an underfunded guild wallet refuses (returns False) before any
    row exists, so pool claims stay backed. The caller owns the
    transaction (pass conn - a separate connection would deadlock against
    the caller's write lock). Emits one EVT_CREDIT_EARNED like grant(),
    so event consumers read pool payouts exactly as treasury payouts.
    Mirrors grant()'s kill switch: with credits disabled the payout is
    refused (False), never debited."""
    if not config.CREDITS_ENABLED:
        return False
    if amount_units <= 0:
        return False
    if guild_wallet_balance(conn, int(guild_id)) < amount_units:
        return False
    tx_id = _new_tx_id(conn)
    _insert_entry(
        conn,
        None,
        "guild",
        -amount_units,
        reason,
        "guild",
        int(guild_id),
        tx_id=tx_id,
    )
    _insert_entry(
        conn,
        agent_id,
        "agent",
        amount_units,
        reason,
        target_type,
        target_id,
        tx_id=tx_id,
    )
    import events

    events.log_event(
        events.EVT_CREDIT_EARNED,
        actor_agent_id=agent_id,
        target_type=target_type or "credit",
        target_id=target_id,
        detail={
            "reason": reason,
            "credits": format_credits(amount_units),
            "delta_units": amount_units,
            "guild_id": int(guild_id),
        },
        conn=conn,
    )
    return True


def guild_retain_withhold(
    conn: sqlite3.Connection,
    guild_id: int,
    settled_units: int,
    reason: str = "guild_retained",
) -> bool:
    """Retention record for pool-owned value withheld from a payout
    (proposal #611 - fee-arrears settlements and withdrawal fees: the
    pool memo extinguishes the FULL computed share while the member nets
    less, so the wallet rightly stays higher by the settled amount).
    Writes a net-zero -guild/+guild pair (same tx) that names the
    retention in history; conservation Rule-D reads it back as
    wallet - memo == SUM(+guild guild_retained legs). No-op on zero."""
    if settled_units <= 0:
        return False
    tx_id = _new_tx_id(conn)
    _insert_entry(
        conn,
        None,
        "guild",
        -settled_units,
        reason,
        "guild",
        int(guild_id),
        tx_id=tx_id,
    )
    _insert_entry(
        conn,
        None,
        "guild",
        settled_units,
        reason,
        "guild",
        int(guild_id),
        tx_id=tx_id,
    )
    return True


def treasury_to_guild(
    conn: sqlite3.Connection,
    guild_id: int,
    amount_units: int,
    reason: str,
    target_type: str | None = None,
    target_id: int | None = None,
) -> bool:
    """Move units from the community treasury into a guild wallet
    (proposal #611 - project grants, subsidies, deposit matches): paired
    -treasury / +guild legs under one tx_id, so the summed supply never
    moves and the outflow is visible in treasury flows. Treasury-gated
    (returns False) before any row exists - callers raise under their
    grant-first discipline. The caller owns the transaction."""
    if amount_units <= 0:
        return False
    if treasury_balance(conn) < amount_units:
        return False
    tx_id = _new_tx_id(conn)
    _insert_entry(
        conn,
        None,
        "treasury",
        -amount_units,
        reason,
        target_type if target_type is not None else "guild",
        target_id if target_id is not None else int(guild_id),
        tx_id=tx_id,
    )
    _insert_entry(
        conn,
        None,
        "guild",
        amount_units,
        f"{reason}_intake",
        "guild",
        int(guild_id),
        tx_id=tx_id,
    )
    return True


def fee_units(amount_units: int) -> int:
    """The transaction fee for moving `amount_units`, rounded UP to
    whole units (the sender pays the rounding), 100% to the treasury.
    Decimal arithmetic end-to-end: binary-float ceil drifted on large
    amounts / fractional percents, the same class to_units fixed by
    going Decimal (review M1)."""
    pct = max(0.0, float(config.TX_FEE_PERCENT))
    if pct == 0 or amount_units <= 0:
        return 0
    from decimal import ROUND_CEILING, Decimal

    fee = Decimal(amount_units) * Decimal(str(pct)) / Decimal(100)
    return int(fee.to_integral_value(rounding=ROUND_CEILING))


def grant(
    agent_id: int,
    delta_units: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Credit units to a citizen for a contribution.  With
    TREASURY_FUNDS_PAYOUTS on, the payout is drawn from the community
    treasury (-treasury / +agent pair inside one transaction); an empty
    treasury skips the payout entirely and logs a visible
    credit_payout_unfunded event - earnings are never minted from nothing.
    Returns False when earning is disabled by config, the delta is zero,
    or the treasury could not fund it; the caller decides whether that is
    fine.  Pass conn when already inside a transaction.

    Negative deltas are refused: judgment penalties live on the karma
    layer (CHARTER IX).  The content-vote path uses grant_earned(), which
    clamps flip-cancellations at the zero floor instead."""
    if not config.CREDITS_ENABLED or delta_units == 0:
        return False
    if delta_units < 0:
        raise ForumError(
            "credit grants must be non-negative - use grant_earned() for "
            "the vote-flip cancellation path."
        )
    # BEGIN IMMEDIATE: the treasury balance is checked and the paired
    # treasury/agent rows written as one atomic unit - a concurrent grant
    # cannot interleave between the check and the write (review 4426).
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        return _grant_positive(
            c,
            agent_id,
            delta_units,
            reason,
            target_type,
            target_id,
        )


def _grant_positive(
    c: sqlite3.Connection,
    agent_id: int,
    delta_units: int,
    reason: str,
    target_type: str | None,
    target_id: int | None,
) -> bool:
    """The funded/legacy positive-grant body shared by grant() and
    grant_earned().  Caller owns the connection/transaction."""
    import events

    if config.TREASURY_FUNDS_PAYOUTS:
        treasury_units = treasury_balance(c)
        if treasury_units < delta_units:
            events.log_event(
                events.EVT_CREDIT_PAYOUT_UNFUNDED,
                actor_agent_id=None,
                target_type="credit",
                target_id=agent_id,
                detail={
                    "reason": reason,
                    "credits": format_credits(delta_units),
                    "delta_units": delta_units,
                    "treasury_credits": format_credits(treasury_units),
                },
                conn=c,
            )
            _notify_unfunded_once_daily(
                c,
                agent_id,
                reason,
                delta_units,
            )
            return False
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            None,
            "treasury",
            -delta_units,
            "payout_source",
            target_type,
            target_id,
            tx_id=tx_id,
        )
        _insert_entry(
            c,
            agent_id,
            "agent",
            delta_units,
            reason,
            target_type,
            target_id,
            tx_id=tx_id,
        )
        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=agent_id,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": reason,
                "credits": format_credits(delta_units),
                "delta_units": delta_units,
                "funded_by": "treasury",
            },
            conn=c,
        )
        return True
    tx_id = _new_tx_id(c)
    _insert_entry(
        c,
        agent_id,
        "agent",
        delta_units,
        reason,
        target_type,
        target_id,
        tx_id=tx_id,
    )
    events.log_event(
        events.EVT_CREDIT_EARNED,
        actor_agent_id=agent_id,
        target_type=target_type or "credit",
        target_id=target_id,
        detail={
            "reason": reason,
            "credits": format_credits(delta_units),
            "delta_units": delta_units,
        },
        conn=c,
    )
    return True


def _notify_unfunded_once_daily(
    c: sqlite3.Connection,
    agent_id: int,
    reason: str,
    needed_units: int,
) -> None:
    """Tell the citizen their earning went unpaid - at most once per UTC
    day, so a burst of votes on an empty treasury cannot flood the
    mailbox. The event ledger stays the full audit trail; this is the
    personal signal for it (review: Agent7 round-4 #4)."""
    day_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )
    sent_today = c.execute(
        "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
        " AND kind = 'economy' AND ref_type = 'treasury'"
        " AND created_at >= ?",
        (agent_id, day_start),
    ).fetchone()[0]
    if sent_today:
        return
    from notifications import _notify

    _notify(
        c,
        agent_id,
        "economy",
        "treasury",
        None,
        f"A {reason} earning of {format_credits(needed_units)} credits "
        "could not be paid - the community treasury is empty. Earning "
        "resumes automatically once the treasury is refilled; you can "
        "watch it on the /economy page.",
        actor_agent_id=None,
    )


def grant_earned(
    agent_id: int,
    delta_units: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """The content-vote earn path: a new upvote grants, a flip cancels -
    but never past the zero floor.  A negative delta returns credits only
    up to the citizen's current balance (the rest of the cancellation is
    forgiven), so a wallet can never cross zero and a downvote-upvote
    cycle can never farm extra credits (review findings, PR #402).
    Penalties proper live on the karma layer."""
    if not config.CREDITS_ENABLED or delta_units == 0:
        return False
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        balance = balance_for(c, agent_id)
        if delta_units > 0:
            return _grant_positive(
                c,
                agent_id,
                delta_units,
                reason,
                target_type,
                target_id,
            )
        effective = max(delta_units, -balance)
        if effective == 0:
            return False
        # Cancellations carry their own reason so the profile's
        # spent_total can tell reversals of income apart from actual
        # spending (review note N2, PR #402).
        cancel_reason = f"{reason}_cancel"
        import events

        tx_id = _new_tx_id(c)
        if config.TREASURY_FUNDS_PAYOUTS:
            # The cancelled portion goes back to the treasury.
            _insert_entry(
                c,
                agent_id,
                "agent",
                effective,
                cancel_reason,
                target_type,
                target_id,
                tx_id=tx_id,
            )
            _insert_entry(
                c,
                None,
                "treasury",
                -effective,
                "payout_return",
                target_type,
                target_id,
                tx_id=tx_id,
            )
        else:
            _insert_entry(
                c,
                agent_id,
                "agent",
                effective,
                cancel_reason,
                target_type,
                target_id,
                tx_id=tx_id,
            )
        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=agent_id,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": cancel_reason,
                "credits": format_credits(effective),
                "delta_units": effective,
                "requested_delta_units": delta_units,
                "clamped_at_zero": effective != delta_units,
            },
            conn=c,
        )
        return True


def spend(
    agent_id: int,
    amount_units: int,
    reason: str,
    *,
    dest_treasury: bool = False,
    dest_escrow: bool = False,
    dest_guild: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Debit units from a citizen for a voluntary spend.  Raises when
    the balance cannot cover it - the refusal mirrors karma's effective-
    karma gate, but a credit balance never goes negative (spends are
    bounded by earnings; penalties live on the karma layer).

    dest_treasury=True (tag costs) recycles the spent amount INTO the
    community treasury instead of destroying it - a paired -agent /
    +treasury write inside the same transaction.  dest_escrow=True (job
    postings, taker-deposit escrow halves) parks the amount in the
    ledger's escrow bank account instead - a paired -agent / +escrow
    write (the escrow leg takes reason + "_held") under the same tx_id,
    so the summed supply never moves.  dest_guild=gid (proposal #611)
    parks the amount in that guild's wallet instead - a paired -agent /
    +guild write (the guild leg takes reason + "_intake" with
    target_type='guild', target_id=gid) under the same tx_id, so the
    summed supply never moves and each guild keeps its own balance.
    The three destinations are mutually exclusive.  Stake locks keep all
    False: their credits are merely
    locked, refunded later, so no second row exists until the refund
    pays out.

    The CREDITS_ENABLED master switch gates spends too: with credits
    disabled a spend is refused loudly rather than debiting a valuta
    nobody can earn (review finding, PR #402).  Principal settlements
    (return_principal) are deliberately exempt - escrowed stakes must
    always be able to return to their owners."""
    if not config.CREDITS_ENABLED:
        raise ForumError("credits are disabled on this forum.")
    if amount_units == 0:
        return False
    if amount_units < 0:
        raise ForumError("credit amounts must be positive.")
    if dest_guild is not None and int(dest_guild) <= 0:
        raise ForumError("dest_guild must be a guild id.")
    if sum([bool(dest_treasury), bool(dest_escrow), dest_guild is not None]) > 1:
        raise ForumError("spend takes at most one destination.")
    # BEGIN IMMEDIATE: the balance check and its debit form one atomic
    # step - a concurrent spend can't both pass the check and overspend
    # the wallet (review 4426).
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        balance = balance_for(c, agent_id)
        if balance < amount_units:
            raise ForumError(
                f"insufficient credits: this costs "
                f"{format_credits(amount_units)} but you have "
                f"{format_credits(balance)}."
            )
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            agent_id,
            "agent",
            -amount_units,
            reason,
            target_type,
            target_id,
            tx_id=tx_id,
        )
        if dest_treasury:
            _insert_entry(
                c,
                None,
                "treasury",
                amount_units,
                f"{reason}_intake",
                target_type,
                target_id,
                tx_id=tx_id,
            )
        if dest_escrow:
            _insert_entry(
                c,
                None,
                "escrow",
                amount_units,
                f"{reason}_held",
                target_type,
                target_id,
                tx_id=tx_id,
            )
        if dest_guild is not None:
            _insert_entry(
                c,
                None,
                "guild",
                amount_units,
                f"{reason}_intake",
                "guild",
                int(dest_guild),
                tx_id=tx_id,
            )
        import events

        detail: dict[str, object] = {
            "reason": reason,
            "credits": format_credits(amount_units),
            "delta_units": amount_units,
        }
        if dest_treasury:
            detail["to"] = "treasury"
        if dest_escrow:
            detail["to"] = "escrow"
        if dest_guild is not None:
            detail["to"] = "guild"
            detail["guild_id"] = int(dest_guild)
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
    amount_units: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Return ESCROWED units to a citizen: stake refunds and stake
    payouts whose matching debit was written when the lock was taken.
    These are the second half of a principal move, never new income -
    they bypass treasury funding by definition (the value left a wallet
    when the lock was written; it re-enters circulation here).  They also
    bypass the CREDITS_ENABLED kill switch: the matching debit happened
    while credits were on, so refusing the settlement would strand the
    citizen's own money (review finding, PR #402)."""
    if amount_units <= 0:
        return False
    with _conn() if conn is None else nullcontext(conn) as c:
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            agent_id,
            "agent",
            amount_units,
            reason,
            target_type,
            target_id,
            tx_id=tx_id,
        )
        import events

        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=agent_id,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": reason,
                "credits": format_credits(amount_units),
                "delta_units": amount_units,
                "escrow_return": True,
            },
            conn=c,
        )
    return True


def refund(
    agent_id: int,
    amount_units: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Return previously-spent units (stake refunds/withdrawals).  A
    principal return with a stake-flow reason - never treasury-funded."""
    return_principal(
        agent_id,
        amount_units,
        reason,
        target_type=target_type,
        target_id=target_id,
        conn=conn,
    )


def release_escrow(
    agent_id: int,
    amount_units: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Pay OUT of the escrow bank account to a citizen: job wages,
    escrow refunds and deposit returns whose matching intake was paired
    into escrow when the holding was taken. The agent leg keeps the
    EXACT legacy reason (today's return_principal callers pass theirs
    through unchanged); the escrow leg takes reason + "_release". Both
    legs share one tx_id, so supply never moves - the holding simply
    changes accounts. Like return_principal, exempt from CREDITS_ENABLED:
    escrowed principal must always be able to settle."""
    if amount_units <= 0:
        return False
    with _conn() if conn is None else nullcontext(conn) as c:
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            agent_id,
            "agent",
            amount_units,
            reason,
            target_type,
            target_id,
            tx_id=tx_id,
        )
        _insert_entry(
            c,
            None,
            "escrow",
            -amount_units,
            f"{reason}_release",
            target_type,
            target_id,
            tx_id=tx_id,
        )
        import events

        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=agent_id,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": reason,
                "credits": format_credits(amount_units),
                "delta_units": amount_units,
                "escrow_release": True,
            },
            conn=c,
        )
    return True


def treasury_to_escrow(
    amount_units: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Move principal from the treasury into the escrow bank account:
    official-position postings and re-activations. The treasury leg keeps
    the EXACT legacy reason ('job_escrow_treasury'); the escrow leg takes
    reason + "_held". Paired under one tx_id - supply never moves."""
    if amount_units <= 0:
        return False
    with _conn() if conn is None else nullcontext(conn) as c:
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            None,
            "treasury",
            -amount_units,
            reason,
            target_type,
            target_id,
            tx_id=tx_id,
        )
        _insert_entry(
            c,
            None,
            "escrow",
            amount_units,
            f"{reason}_held",
            target_type,
            target_id,
            tx_id=tx_id,
        )
        import events

        events.log_event(
            events.EVT_CREDIT_SPENT,
            actor_agent_id=None,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": reason,
                "credits": format_credits(amount_units),
                "delta_units": amount_units,
                "to": "escrow",
            },
            conn=c,
        )
    return True


def escrow_to_treasury(
    amount_units: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Move principal from the escrow bank account back to the treasury:
    official-position cancellations/expiries and stranded deposit-bonus
    pool drains. The treasury leg keeps the EXACT legacy reason; the
    escrow leg takes reason + "_release". Paired under one tx_id."""
    if amount_units <= 0:
        return False
    with _conn() if conn is None else nullcontext(conn) as c:
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            None,
            "escrow",
            -amount_units,
            f"{reason}_release",
            target_type,
            target_id,
            tx_id=tx_id,
        )
        _insert_entry(
            c,
            None,
            "treasury",
            amount_units,
            reason,
            target_type,
            target_id,
            tx_id=tx_id,
        )
        import events

        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=None,
            target_type=target_type or "credit",
            target_id=target_id,
            detail={
                "reason": reason,
                "credits": format_credits(amount_units),
                "delta_units": amount_units,
                "escrow_return": True,
            },
            conn=c,
        )
    return True


def guild_to_escrow(
    conn: sqlite3.Connection,
    guild_id: int,
    amount_units: int,
    reason: str,
    target_type: str | None = None,
    target_id: int | None = None,
) -> bool:
    """Move principal from a guild wallet into the escrow bank account
    (proposal #611 - pool-funded job commissions): paired -guild / +escrow
    legs under one tx_id, so the summed supply never moves and the
    escrow conservation audit still nets zero. Guild-gated (returns False)
    before any row exists. The caller owns the transaction."""
    if amount_units <= 0:
        return False
    if guild_wallet_balance(conn, int(guild_id)) < amount_units:
        return False
    tx_id = _new_tx_id(conn)
    _insert_entry(
        conn,
        None,
        "guild",
        -amount_units,
        reason,
        "guild",
        int(guild_id),
        tx_id=tx_id,
    )
    _insert_entry(
        conn,
        None,
        "escrow",
        amount_units,
        f"{reason}_held",
        target_type,
        target_id,
        tx_id=tx_id,
    )
    import events

    events.log_event(
        events.EVT_CREDIT_SPENT,
        actor_agent_id=None,
        target_type=target_type or "credit",
        target_id=target_id,
        detail={
            "reason": reason,
            "credits": format_credits(amount_units),
            "delta_units": amount_units,
            "to": "escrow",
            "guild_id": int(guild_id),
        },
        conn=conn,
    )
    return True


def escrow_to_guild(
    conn: sqlite3.Connection,
    guild_id: int,
    amount_units: int,
    reason: str,
    target_type: str | None = None,
    target_id: int | None = None,
) -> bool:
    """Move principal from the escrow bank account back into a guild
    wallet (proposal #611 - commissioned-job refunds, taken-job wages,
    disband cancels): paired -escrow / +guild legs under one tx_id. The
    guild leg takes reason + "_intake" with target_type='guild' so the
    per-guild balance counts it; treasury flow buckets never see it
    (guild account), preserving today's dashboard shapes."""
    if amount_units <= 0:
        return False
    tx_id = _new_tx_id(conn)
    _insert_entry(
        conn,
        None,
        "escrow",
        -amount_units,
        f"{reason}_release",
        target_type,
        target_id,
        tx_id=tx_id,
    )
    _insert_entry(
        conn,
        None,
        "guild",
        amount_units,
        f"{reason}_intake",
        "guild",
        int(guild_id),
        tx_id=tx_id,
    )
    import events

    events.log_event(
        events.EVT_CREDIT_EARNED,
        actor_agent_id=None,
        target_type=target_type or "credit",
        target_id=target_id,
        detail={
            "reason": reason,
            "credits": format_credits(amount_units),
            "delta_units": amount_units,
            "escrow_return": True,
            "guild_id": int(guild_id),
        },
        conn=conn,
    )
    return True


# -- treasury operations (executed by db._economy's governance gate) -----


def mint(
    delta_units: int,
    reason: str,
    *,
    admin: str,
    proposal_id: int | None = None,
    conn: sqlite3.Connection | None = None,
    reason_detail: str | None = None,
) -> dict:
    """Create new credits in the community treasury (+treasury row).
    Total supply grows by exactly this amount.  Caller (db._economy)
    enforces the cap / proposal gates; this is the ledger primitive."""
    if delta_units <= 0:
        raise ForumError("mint amount must be positive.")
    with _conn() if conn is None else nullcontext(conn) as c:
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            None,
            "treasury",
            delta_units,
            reason,
            "economy",
            proposal_id,
            tx_id=tx_id,
        )
        import events

        detail: dict[str, object] = {
            "reason": reason,
            "credits": format_credits(delta_units),
            "delta_units": delta_units,
            "admin": admin,
        }
        if proposal_id is not None:
            detail["proposal_id"] = proposal_id
        if reason_detail is not None:
            detail["reason_detail"] = reason_detail
        events.log_event(
            events.EVT_CREDIT_MINTED,
            actor_agent_id=None,
            target_type="economy",
            target_id=proposal_id,
            detail=detail,
            conn=c,
        )
        return {
            "minted_units": delta_units,
            "minted_credits": format_credits(delta_units),
            "treasury_units": treasury_balance(c),
            "treasury_credits": format_credits(treasury_balance(c)),
        }


def burn(
    delta_units: int,
    reason: str,
    *,
    admin: str,
    proposal_id: int | None = None,
    conn: sqlite3.Connection | None = None,
    reason_detail: str | None = None,
) -> dict:
    """Destroy credits from the community treasury (-treasury row).  The
    treasury cannot go negative - burning more than it holds is refused.
    Caller (db._economy) enforces the cap / proposal gates."""
    if delta_units <= 0:
        raise ForumError("burn amount must be positive.")
    with _conn() if conn is None else nullcontext(conn) as c:
        if treasury_balance(c) < delta_units:
            raise ForumError(
                f"insufficient treasury credits: burning "
                f"{format_credits(delta_units)} but the treasury holds "
                f"{format_credits(treasury_balance(c))}."
            )
        tx_id = _new_tx_id(c)
        _insert_entry(
            c,
            None,
            "treasury",
            -delta_units,
            reason,
            "economy",
            proposal_id,
            tx_id=tx_id,
        )
        import events

        detail: dict[str, object] = {
            "reason": reason,
            "credits": format_credits(delta_units),
            "delta_units": delta_units,
            "admin": admin,
        }
        if proposal_id is not None:
            detail["proposal_id"] = proposal_id
        if reason_detail is not None:
            detail["reason_detail"] = reason_detail
        events.log_event(
            events.EVT_CREDIT_BURNED,
            actor_agent_id=None,
            target_type="economy",
            target_id=proposal_id,
            detail=detail,
            conn=c,
        )
        return {
            "burned_units": delta_units,
            "burned_credits": format_credits(delta_units),
            "treasury_units": treasury_balance(c),
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
    amount_units: int,
    note: str = "",
    *,
    conn: sqlite3.Connection | None = None,
    notify_recipient: bool = True,
) -> dict:
    """Move credits between wallets: citizen-to-citizen or citizen-to-
    treasury (recipient='treasury' when no citizen owns that name - the
    name is reserved at registration, but a legacy citizen named
    'treasury' would win routing).  Charges the FORUM_TX_FEE_PERCENT fee
    (rounded up to whole units, 100% to the treasury) on top of the
    amount.  One transaction, paired ledger rows, ONE credit_transferred
    event.  Both endpoints must be active citizens; self-transfers and
    non-positive amounts are refused; the sender's balance must cover
    amount + fee.  Direct calls mail the recipient; internal settlements
    pass notify_recipient=False and mail under their own kind instead
    (pay_invoice's invoice-paid mail would otherwise double-ping)."""
    if not config.CREDITS_ENABLED:
        raise ForumError("credits are disabled on this forum.")
    if amount_units <= 0:
        raise ForumError("transfer amount must be positive.")
    note = (note or "").strip()[:TRANSFER_NOTE_MAX_LEN]
    fee_q = fee_units(amount_units)
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        sender = _active_wallet(c, sender_id)
        recipient_row: sqlite3.Row | None = None
        to_treasury = False
        if isinstance(recipient, str):
            needle = recipient.strip()
            named = c.execute(
                "SELECT id FROM agents WHERE name = ? COLLATE NOCASE",
                (needle,),
            ).fetchone()
            if named is not None:
                rid = named["id"]
            elif needle.lower() == "treasury":
                # No citizen owns the name: route to the community account.
                to_treasury = True
            else:
                raise ForumError(f"no citizen named '{needle}'.")
        else:
            rid = recipient
        if not to_treasury:
            if rid == sender_id:
                raise ForumError("you cannot transfer credits to yourself.")
            recipient_row = _active_wallet(c, rid)
        balance = balance_for(c, sender_id)
        needed = amount_units + fee_q
        if balance < needed:
            raise ForumError(
                f"insufficient credits: transferring "
                f"{format_credits(amount_units)}"
                + (f" + {format_credits(fee_q)} fee" if fee_q else "")
                + f" needs {format_credits(needed)}, you have "
                f"{format_credits(balance)}."
            )
        tx_id = _new_tx_id(c)
        # Leg 1: leave the sender's wallet.
        _insert_entry(
            c,
            sender_id,
            "agent",
            -amount_units,
            "transfer_out",
            "agent",
            recipient_row["id"] if recipient_row else None,
            tx_id=tx_id,
        )
        # Leg 2: arrive in the destination wallet.
        if recipient_row is not None:
            _insert_entry(
                c,
                recipient_row["id"],
                "agent",
                amount_units,
                "transfer_in",
                "agent",
                sender_id,
                tx_id=tx_id,
            )
        else:
            _insert_entry(
                c,
                None,
                "treasury",
                amount_units,
                "transfer_intake",
                "agent",
                sender_id,
                tx_id=tx_id,
            )
        # Leg 3+4: the fee, always to the treasury.
        if fee_q:
            _insert_entry(
                c,
                sender_id,
                "agent",
                -fee_q,
                "transfer_fee",
                "treasury",
                None,
                tx_id=tx_id,
            )
            _insert_entry(
                c,
                None,
                "treasury",
                fee_q,
                "transfer_fee_intake",
                "agent",
                sender_id,
                tx_id=tx_id,
            )
        import events

        detail: dict[str, object] = {
            "from_name": sender["name"],
            "to_name": recipient_row["name"] if recipient_row else "Treasury",
            "credits": format_credits(amount_units),
            "delta_units": amount_units,
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
        if notify_recipient and recipient_row is not None:
            # Direct wallet transfer (the MCP path): tell the recipient
            # their balance grew. Internal settlements suppress this and
            # mail under their own kind instead - notably pay_invoice,
            # whose invoice-paid mail would otherwise double-ping the
            # issuer for one money movement.
            from notifications import _notify

            body = f"{sender['name']} sent you {format_credits(amount_units)} credits."
            if note:
                body += f" Note: '{note}'."
            _notify(
                c,
                recipient_row["id"],
                "economy",
                "agent",
                sender_id,
                body,
                actor_agent_id=sender_id,
                actor_name=sender["name"],
            )
        new_sender = balance_for(c, sender_id)
        return {
            "sent_units": amount_units,
            "sent_credits": format_credits(amount_units),
            "fee_units": fee_q,
            "fee_credits": format_credits(fee_q),
            "to_treasury": to_treasury,
            "to_agent_id": recipient_row["id"] if recipient_row else None,
            "to_name": detail["to_name"],
            "note": note,
            "new_balance_units": new_sender,
            "new_balance_credits": format_credits(new_sender),
        }


def transfer(
    token: str,
    recipient: int | str,
    amount_credits: float,
    note: str = "",
) -> dict:
    """Authenticated wallet transfer (the MCP entry point): resolves the
    sender from the token, converts the amount at twentieth intake
    (nearest twentieth, ties up), and moves the credits with the standard
    transaction fee."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
    units = to_units(amount_credits)
    if units <= 0:
        raise ForumError("transfer amount must be positive.")
    return transfer_credits(agent["id"], recipient, units, note=note)


# -- suspension forfeiture ------------------------------------------------


def forfeit_agent(
    agent_id: int, *, conn: sqlite3.Connection | None = None
) -> dict | None:
    """A suspended citizen loses ALL their credits: half goes to the
    community treasury, half is burned outright (floor division biases the
    odd unit toward the burn - forfeiture never inflates the supply).
    Written inside the suspension's own transaction when conn is passed;
    a zero-balance citizen is a no-op.  One-way: reinstatement does not
    restore anything."""
    with _conn() if conn is None else nullcontext(conn) as c:
        # Term Savings Bonds (#552): release live bond face to the wallet
        # first, so the standard half-treasury/half-burn split below
        # applies. Deferred import: _bonds reads _core, never vice versa.
        from db._bonds import forfeit_bonds_for_agent

        forfeit_bonds_for_agent(agent_id, conn=c)
        balance = balance_for(c, agent_id)
        if balance <= 0:
            return None
        to_treasury = balance // 2
        burned = balance - to_treasury
        tx_id = _new_tx_id(c)
        if to_treasury > 0:
            _insert_entry(
                c,
                agent_id,
                "agent",
                -to_treasury,
                "forfeit_to_treasury",
                "treasury",
                None,
                tx_id=tx_id,
            )
            _insert_entry(
                c,
                None,
                "treasury",
                to_treasury,
                "forfeit_intake",
                "agent",
                agent_id,
                tx_id=tx_id,
            )
        if burned > 0:
            _insert_entry(
                c,
                agent_id,
                "agent",
                -burned,
                "forfeit_burned",
                "treasury",
                None,
                tx_id=tx_id,
            )
        import events

        events.log_event(
            events.EVT_CREDIT_FORFEITED,
            actor_agent_id=None,
            target_type="agent",
            target_id=agent_id,
            detail={
                "forfeited_credits": format_credits(balance),
                "forfeited_units": balance,
                "to_treasury_credits": format_credits(to_treasury),
                "burned_credits": format_credits(burned),
            },
            conn=c,
        )
        return {
            "forfeited_units": balance,
            "to_treasury_units": to_treasury,
            "burned_units": burned,
        }


def balance_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """A citizen's credit balance in units (derived, never cached)."""
    return conn.execute(
        "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()[0]


def balance_many(conn: sqlite3.Connection, agent_ids: list[int]) -> dict[int, int]:
    """Balances in units for a batch of agents in one GROUP BY query -
    the same shape as effective_karma_many."""
    if not agent_ids:
        return {}
    marks = ",".join("?" * len(agent_ids))
    rows = conn.execute(
        f"SELECT agent_id, COALESCE(SUM(delta_units), 0) FROM credit_entries"
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


def earned_summary(conn: sqlite3.Connection, agent_id: int) -> dict[str, int]:
    """Earning windows for profile displays: total earned vs spent, plus
    earned since UTC week start (Monday) and month start.  'Spent' means
    the citizen directed credits somewhere: voluntary spends, stake
    commitments, transfers and fees.  Flip-cancellations (income
    reversals) and forfeitures (judgment penalties, karma layer) are
    excluded - neither is spending (review note N2, PR #402)."""
    now_dt = datetime.now(timezone.utc)
    week_start = (now_dt - timedelta(days=now_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _iso(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    week_iso = _iso(week_start)
    month_iso = _iso(month_start)
    row = conn.execute(
        "SELECT"
        "  COALESCE(SUM(CASE WHEN delta_units > 0"
        "    THEN delta_units ELSE 0 END), 0),"
        "  COALESCE(SUM(CASE WHEN delta_units > 0 AND created_at >= ?"
        "    THEN delta_units ELSE 0 END), 0),"
        "  COALESCE(SUM(CASE WHEN delta_units > 0 AND created_at >= ?"
        "    THEN delta_units ELSE 0 END), 0),"
        # Not spending: flip-cancellations reverse income (they carry
        # their own *_cancel reason) and forfeitures are judgment
        # penalties that live on the karma layer - neither belongs in a
        # 'what did I spend' number (review note N2, PR #402).
        "  COALESCE(SUM(CASE WHEN delta_units < 0 AND reason NOT IN"
        "    ('post_vote_cancel','comment_vote_cancel',"
        "    'forfeit_to_treasury','forfeit_burned')"
        "    THEN -delta_units ELSE 0 END), 0),"
        "  COALESCE(SUM(delta_units), 0)"
        " FROM credit_entries WHERE agent_id = ?",
        (week_iso, month_iso, agent_id),
    ).fetchone()
    return {
        "earned_total_units": row[0],
        "earned_this_week_units": row[1],
        "earned_this_month_units": row[2],
        "spent_total_units": row[3],
        "balance_units": row[4],
    }


# Reason families for the /credits global page's category tabs (list
# 569): the named families bucketed by reason, earned/spent being the
# residual by sign.  Cancellations (the *_cancel reasons vote-flips
# write) are income reversals, not spending - they ride under no tab
# (earned_summary's note N2, PR #402, made the same cut).
_CREDIT_TRANSFER_REASONS = frozenset(
    {
        "transfer_out",
        "transfer_in",
        "transfer_intake",
        "transfer_fee",
        "transfer_fee_intake",
    }
)
_CREDIT_MINT_REASONS = frozenset({"genesis", "admin_mint", "proposal_mint"})
_CREDIT_BURN_REASONS = frozenset({"admin_burn", "proposal_burn", "forfeit_burned"})
_CREDIT_FORFEIT_REASONS = frozenset(
    {
        "forfeit_to_treasury",
        "forfeit_burned",
        "forfeit_intake",
    }
)
_CREDIT_NAMED_FAMILIES = (
    _CREDIT_TRANSFER_REASONS
    | _CREDIT_MINT_REASONS
    | _CREDIT_BURN_REASONS
    | _CREDIT_FORFEIT_REASONS
)
CREDIT_CATEGORIES = (
    "all",
    "earned",
    "spent",
    "transfers",
    "minted",
    "burned",
    "forfeited",
    "jobs",
    "tags",
    "stakes",
    "store",
    "bonds",
    "guilds",
    "treasury",
)


def _category_clause(category: str) -> tuple[str, list[object]]:
    """The WHERE-clause fragment (with placeholders) that restricts a
    history() query to one category.  Named families are matched by
    reason; earned/spent are the residual agent rows by sign, excluding
    cancellations (income reversals are neither)."""
    if category == "all":
        return "", []
    if category == "transfers":
        return (
            "e.reason IN (" + ", ".join("?" for _ in _CREDIT_TRANSFER_REASONS) + ")",
            list(_CREDIT_TRANSFER_REASONS),
        )
    if category == "minted":
        return (
            "e.reason IN (" + ", ".join("?" for _ in _CREDIT_MINT_REASONS) + ")",
            list(_CREDIT_MINT_REASONS),
        )
    if category == "burned":
        return (
            "e.reason IN (" + ", ".join("?" for _ in _CREDIT_BURN_REASONS) + ")",
            list(_CREDIT_BURN_REASONS),
        )
    if category == "forfeited":
        return (
            "e.reason IN (" + ", ".join("?" for _ in _CREDIT_FORFEIT_REASONS) + ")",
            list(_CREDIT_FORFEIT_REASONS),
        )
    if category == "jobs":
        return "LOWER(e.reason) LIKE '%job%'", []
    if category == "tags":
        return "LOWER(e.reason) LIKE '%tag%'", []
    if category == "stakes":
        return (
            "(LOWER(e.reason) LIKE '%stake%' OR LOWER(e.reason) LIKE '%bounty%')",
            [],
        )
    if category == "store":
        return "LOWER(e.reason) LIKE '%store%'", []
    if category == "bonds":
        return "LOWER(e.reason) LIKE '%bond%'", []
    if category == "guilds":
        return "LOWER(e.reason) LIKE '%guild%'", []
    if category == "treasury":
        fams = _CREDIT_MINT_REASONS | _CREDIT_BURN_REASONS
        return (
            "e.reason IN (" + ", ".join("?" for _ in fams) + ")",
            list(fams),
        )
    if category == "earned":
        cond = " AND ".join(
            (
                "e.account = 'agent'",
                "e.delta_units > 0",
                "e.reason NOT IN ("
                + ", ".join("?" for _ in _CREDIT_NAMED_FAMILIES)
                + ")",
                "e.reason NOT LIKE '%_cancel'",
            )
        )
        return cond, list(_CREDIT_NAMED_FAMILIES)
    if category == "spent":
        cond = " AND ".join(
            (
                "e.account = 'agent'",
                "e.delta_units < 0",
                "e.reason NOT IN ("
                + ", ".join("?" for _ in _CREDIT_NAMED_FAMILIES)
                + ")",
                "e.reason NOT LIKE '%_cancel'",
            )
        )
        return cond, list(_CREDIT_NAMED_FAMILIES)
    raise ForumError(
        f"unknown credit history category: {category!r}. Valid: "
        + ", ".join(CREDIT_CATEGORIES)
        + "."
    )


def history(
    agent_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    min_units: int | None = None,
    max_units: int | None = None,
    guild_id: int | None = None,
) -> dict:
    """The public credits ledger, newest first.  Optional agent filter;
    every row names its reason and target so any citizen can audit any
    balance down to its entries.  Optional category filter (one of
    CREDIT_CATEGORIES) restricts rows to that reason family or sign.
    Optional min/max_units bound the absolute credit amount.
    Optional guild_id keeps only legs touching that guild
    (target_type='guild'), entry-by-entry - the pool's credit-side
    trail beside its guild_ledger memos."""
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    with _conn() as conn:
        clauses: list[str] = []
        params: list[object] = []
        if agent_id is not None:
            clauses.append("e.agent_id = ?")
            params.append(agent_id)
        if guild_id is not None:
            clauses.append("e.target_type = 'guild' AND e.target_id = ?")
            params.append(int(guild_id))
        if category is not None:
            fclause, fparams = _category_clause(category)
            if fclause:
                clauses.append(fclause)
                params.extend(fparams)
        if min_units is not None:
            clauses.append("ABS(e.delta_units) >= ?")
            params.append(min_units)
        if max_units is not None:
            clauses.append("ABS(e.delta_units) <= ?")
            params.append(max_units)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT e.id, e.agent_id, e.account,"
            f" COALESCE(a.name,"
            f"   CASE WHEN e.account = 'treasury' THEN 'Treasury' END)"
            f"   AS agent_name,"
            f" sea.name_color AS agent_color,"
            f" ta.name AS target_name, seta.name_color AS target_color,"
            f" e.delta_units, e.reason, e.target_type, e.target_id,"
            f" e.tx_id, e.created_at"
            f" FROM credit_entries e"
            f" LEFT JOIN agents a ON a.id = e.agent_id"
            f" LEFT JOIN store_entitlements sea ON sea.agent_id = a.id"
            f" LEFT JOIN agents ta ON ta.id = e.target_id"
            f" AND e.target_type = 'agent'"
            f" LEFT JOIN store_entitlements seta ON seta.agent_id = ta.id"
            f"{where} ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        ).fetchall()
        # The global view counts treasury rows too (agent_id IS NULL):
        # they are rendered as 'Treasury' entries above, so the total must
        # include them or pagination would drift. Per-agent views filter
        # on agent_id and never see them. A short limit+1 page already
        # proves the total (offset + returned) - COUNT only when the page
        # is full or overshoots past the tail (empty page past offset
        # proves nothing: the true total sits below the offset).
        if len(rows) <= limit and (offset == 0 or rows):
            total = offset + len(rows[:limit])
        else:
            total = conn.execute(
                f"SELECT COUNT(*) FROM credit_entries e{where}", params
            ).fetchone()[0]
        entries = [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"] or "(deleted citizen)",
                "agent_color": r["agent_color"],
                "account": r["account"],
                "credits": format_credits(r["delta_units"]),
                "delta_units": r["delta_units"],
                "reason": r["reason"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "target_name": r["target_name"],
                "target_color": r["target_color"],
                "tx_id": r["tx_id"],
                "created_at": r["created_at"],
            }
            for r in rows[:limit]
        ]
        summary = earned_summary(conn, agent_id) if agent_id is not None else {}
        return {
            "entries": entries,
            "total": total,
            "has_more": len(rows) > limit,
            "summary": summary,
        }


def group_transactions(entries: list[dict]) -> list[dict]:
    """Collapse flat credit-history entries into one descriptor per
    tx_id group, so the ledger can render a multi-leg action (a treasury
    payout, a transfer, a forfeiture) as a single from -> to transaction
    instead of one row per leg.  Legacy rows (tx_id None) and single-leg
    actions pass through as one-entry groups, unchanged.  Returns
    descriptors in ledger order (newest group first), each carrying
    tx_id, from/to names + accounts, the principal amount that moved, any
    transfer fee, the primary reason, and the group's newest created_at.
    Pure read; the caller decides how to display it."""
    groups: dict[object, list[dict]] = {}
    order: list[object] = []
    for e in entries:
        key = e["tx_id"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    return [_group_one_transaction(groups[k]) for k in order]


def _group_one_transaction(legs: list[dict]) -> dict:
    """Derive a single from -> to descriptor for one tx_id's legs.  The
    wallet credit (positive agent leg) names the recipient; a transfer's
    fee legs fold into the fee total; the largest non-fee debit names the
    sender.  Falls back gracefully for pure debits and treasury-only
    actions."""
    to_leg = next(
        (l for l in legs if l["account"] == "agent" and l["delta_units"] > 0),
        None,
    )
    if to_leg is None:
        to_leg = next(
            (l for l in legs if l["account"] == "guild" and l["delta_units"] > 0),
            None,
        )
    if to_leg is None:
        to_leg = next(
            (l for l in legs if l["account"] == "treasury" and l["delta_units"] > 0),
            None,
        )
    non_fee_neg = [
        l
        for l in legs
        if l["delta_units"] < 0
        and l["reason"] not in ("transfer_fee", "transfer_fee_intake")
    ]
    from_leg = max(non_fee_neg, key=lambda l: -l["delta_units"], default=None)
    # A transfer writes the fee twice: the sender's `transfer_fee` debit and
    # the treasury's `transfer_fee_intake` mirror of that same fee (#B69).
    # Summing both nets to zero, so the whole-ledger grouping reported every
    # transfer as fee-free and viewer/_money.py blanked the fee column (it
    # renders only `if fee_units`). "Fee paid" is the outflow, so count the
    # negative legs; db/_bonds.py already treats the intake leg as the
    # treasury-side revenue source, not a second payment.
    fee_total = -sum(
        l["delta_units"]
        for l in legs
        if l["reason"] in ("transfer_fee", "transfer_fee_intake")
        and l["delta_units"] < 0
    )
    if to_leg is not None:
        amount_units = to_leg["delta_units"]
        reason = to_leg["reason"]
    elif from_leg is not None:
        amount_units = -from_leg["delta_units"]
        reason = from_leg["reason"]
    else:
        amount_units = 0
        reason = legs[0]["reason"] if legs else ""
    return {
        "tx_id": legs[0]["tx_id"],
        "created_at": max(l["created_at"] for l in legs),
        "from_name": _leg_party(from_leg),
        "from_account": from_leg["account"] if from_leg else None,
        "from_agent_id": from_leg.get("agent_id") if from_leg else None,
        "from_color": from_leg.get("agent_color") if from_leg else None,
        "to_name": _leg_party(to_leg),
        "to_account": to_leg["account"] if to_leg else None,
        "to_agent_id": to_leg.get("agent_id") if to_leg else None,
        "to_color": to_leg.get("agent_color") if to_leg else None,
        "amount_units": amount_units,
        "credits": format_credits(amount_units),
        "fee_units": max(0, fee_total),
        "credit": to_leg is not None and to_leg["account"] == "agent",
        "reason": reason,
        "leg_count": len(legs),
        "legs": legs,
    }


def _leg_party(leg: dict | None) -> str | None:
    """The display name of a ledger leg's account: the citizen's name,
    'Treasury' for the community account, 'Escrow' for the
    jobs-escrow bank account, or 'Guild #N' for a per-guild wallet
    (proposal #611 - the wallet leg carries target_type='guild')."""
    if leg is None:
        return None
    if leg["account"] == "treasury":
        return "Treasury"
    if leg["account"] == "escrow":
        return "Escrow"
    if leg["account"] == "guild":
        gid = leg.get("target_id")
        return f"Guild #{gid}" if gid else "Guild pool"
    return leg.get("agent_name") or "(deleted citizen)"


def top_movers(limit: int = 5) -> list[dict]:
    """The week's biggest wallet movers: per-citizen earned and spent
    unit sums over the trailing 7 days, most active first.  Read-only
    aggregate for the /credits global page's top-movers panel."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT e.agent_id, COALESCE(a.name, '(deleted citizen)')"
            "   AS agent_name,"
            " se.name_color AS agent_color,"
            " COALESCE(SUM(CASE WHEN e.delta_units > 0"
            "   THEN e.delta_units ELSE 0 END), 0) AS earned_units,"
            " COALESCE(SUM(CASE WHEN e.delta_units < 0"
            "   THEN -e.delta_units ELSE 0 END), 0) AS spent_units"
            " FROM credit_entries e"
            " LEFT JOIN agents a ON a.id = e.agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE e.account = 'agent' AND e.created_at >= ?"
            " GROUP BY e.agent_id"
            " ORDER BY (earned_units + spent_units) DESC, e.agent_id"
            " LIMIT ?",
            (since, limit),
        ).fetchall()
        return [
            {
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "agent_color": r["agent_color"],
                "earned_units": r["earned_units"],
                "spent_units": r["spent_units"],
            }
            for r in rows
        ]
