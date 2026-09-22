"""db._guilds — pooled credits + manpower (proposal #525, PR-2 engine).

L3 membership, governance, and chat on top of the PR-1 tables. A guild is
a ledger + roster, never a citizen: no karma, no votes, no posts. Money
moves only through the pool-settlement helpers at the bottom, whose
invariant is stated there - every grant out of the pool is drawn from
the guild's own wallet (proposal #611 - per-guild custody in
credit_entries, one balance per guild), so conservation holds by
construction and a failed grant refuses loudly before any row is written.

No co-founder role exists (operator direction, recorded on #525): the
founder invites/approves/owns succession alone, and co-sign is a recorded
solo proposal + confirm with re-validated balance + velocity - the record
(never a second signature) is the transparency control.
"""

from __future__ import annotations

import re
import sqlite3

import config
import logutil
from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from notifications import _notify, _notify_tally

_MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")

_INFLOW_KINDS = (
    "deposit",
    "grant_t1",
    "grant_t2",
    "subsidy",
    "match",
    "stake",
    "job",
    "bond",
)
_VELOCITY_KINDS = ("withdrawal", "invoice", "transfer")


def _days_ago_iso(days: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _age_days(since_iso: str | None) -> float:
    if not since_iso:
        return float("inf")
    try:
        return (_parse_iso(_now_iso()) - _parse_iso(since_iso)).total_seconds() / 86400
    except Exception:
        return float("inf")  # domain: fail-loudly - callers treat unknown as idle


def _guild_row(conn: sqlite3.Connection, guild_id: int) -> dict | None:
    # LEFT JOIN: a deleted founder NULLs the seat (citizen deletion
    # anonymizes history); the guild row must survive them.
    row = conn.execute(
        "SELECT g.*, a.name AS founder_name FROM guilds g"
        " LEFT JOIN agents a ON a.id = g.founder_agent_id"
        " WHERE g.id = ?",
        (guild_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _require_guild(conn: sqlite3.Connection, guild_id: int) -> dict:
    row = _guild_row(conn, guild_id)
    if row is None:
        raise ForumError(f"no guild with id {guild_id}.")
    if row["status"] != "active":
        raise ForumError(
            f"guild {row['name']!r} is {row['status']} - only active"
            " guilds take membership actions."
        )
    return row


def _member_row(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM guild_members WHERE guild_id = ? AND agent_id = ?",
        (guild_id, agent_id),
    ).fetchone()
    return dict(row) if row is not None else None


def _require_member(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> dict:
    row = _member_row(conn, guild_id, agent_id)
    if row is None:
        raise ForumError("only guild members may do that.")
    return row


def _require_founder(conn: sqlite3.Connection, guild: dict, agent_id: int) -> None:
    if int(guild["founder_agent_id"]) != int(agent_id):
        raise ForumError("only the guild founder may do that.")


def _member_count(conn: sqlite3.Connection, guild_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guild_members WHERE guild_id = ?", (guild_id,)
    ).fetchone()[0]


def _membership_count(conn: sqlite3.Connection, agent_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guild_members WHERE agent_id = ?", (agent_id,)
    ).fetchone()[0]


def _agent_by_name_or_id(conn: sqlite3.Connection, ref: str | int) -> dict | None:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (int(ref),)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (ref,)
        ).fetchone()
    return dict(row) if row is not None else None


def _agent_name(conn: sqlite3.Connection, agent_id: int) -> str:
    """Display name for churn rows (falls back to #id when the row is
    gone - the digest must never fail on a renamed citizen)."""
    row = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return str(row["name"]) if row is not None else f"#{agent_id}"


# ── pool math (pure readers; PR-3 money endpoints reuse them) ──────────


def guild_balance(conn: sqlite3.Connection, guild_id: int) -> int:
    """Pool units: the guild wallet's derived balance (proposal #611 -
    SUM over account='guild' legs pointing at this guild). Every guild
    keeps its own balance; multi-guild boards never commingle. Money
    truth lives in credit_entries; guild_ledger stays the
    attribution/history trail (see guild_memo_balance, the Rule-D
    comparator)."""
    from db._credits import guild_wallet_balance

    return int(guild_wallet_balance(conn, int(guild_id)) or 0)


def guild_memo_balance(conn: sqlite3.Connection, guild_id: int) -> int:
    """Pool units per the guild_ledger memo trail (pre-#611 reader,
    kept as the conservation Rule-D comparator and the backfill source).
    Inflow kinds add, everything else subtracts - writers only ever emit
    known kinds (CHECK-gated), so the ELSE arm is unreachable, not a
    policy choice."""
    marks = ",".join("?" for _ in _INFLOW_KINDS)
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind IN ("
        + marks
        + ") THEN units ELSE -units END), 0) FROM guild_ledger"
        " WHERE guild_id = ?",
        (*_INFLOW_KINDS, guild_id),
    ).fetchone()
    return int(row[0] or 0)


def member_net(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> int:
    """One member's net deposits (deposits minus their withdrawals).
    Pool-owned income (grants/subsidies/match/taken wages) carries actors
    on some memos for attribution, but shares are deposits-only by
    construction (item 5002) - so only the deposit/withdrawal kinds enter
    the sum, and every other actor-bearing memo weights exactly nothing."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind = 'deposit' THEN units"
        " WHEN kind = 'withdrawal' THEN -units ELSE 0 END), 0)"
        " FROM guild_ledger WHERE guild_id = ? AND actor_agent_id = ?",
        (guild_id, agent_id),
    ).fetchone()
    return int(row[0] or 0)


def _total_shares(conn: sqlite3.Connection, guild_id: int) -> int:
    rows = conn.execute(
        "SELECT actor_agent_id,"
        " COALESCE(SUM(CASE WHEN kind = 'deposit' THEN units"
        " WHEN kind = 'withdrawal' THEN -units ELSE 0 END), 0) AS net"
        " FROM guild_ledger WHERE guild_id = ?"
        " AND actor_agent_id IS NOT NULL GROUP BY actor_agent_id",
        (guild_id,),
    ).fetchall()
    return sum(max(0, int(r["net"])) for r in rows)


def _payout_for(
    conn: sqlite3.Connection, guild_id: int, agent_id: int, balance: int
) -> int:
    """Pro-rata-by-net-deposits share of the remainder, capped at net
    deposits (never the deposit back whole when the pool shrank - no
    insurance). Integer floor; dust stays pool-owned."""
    net = member_net(conn, guild_id, agent_id)
    if net <= 0 or balance <= 0:
        return 0
    total = _total_shares(conn, guild_id)
    if total <= 0:
        return 0
    return min(net, (balance * net) // total)


def guild_spend_locked(conn: sqlite3.Connection, guild_id: int) -> bool:
    """Spending re-locks below 2 members (receive/deposit/refund/
    distribution + upkeep fee payments stay legal - enforced by the money
    endpoints that call this, not here)."""
    return _member_count(conn, guild_id) < 2


def guild_velocity_ok(
    conn: sqlite3.Connection, guild_id: int, extra_units: int = 0
) -> bool:
    """30% of the balance-at-execution per rolling 7d to non-escrow
    destinations. Counted kinds: withdrawals, invoice payments, transfers
    (escrowed jobs/stakes/services never touch the pool ledger as outflows
    with those kinds)."""
    marks = ",".join("?" for _ in _VELOCITY_KINDS)
    spent = conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM guild_ledger"
        " WHERE guild_id = ? AND kind IN (" + marks + ")"
        " AND created_at >= ?",
        (guild_id, *_VELOCITY_KINDS, _days_ago_iso(float(config.GUILD_VELOCITY_DAYS))),
    ).fetchone()[0]
    balance = guild_balance(conn, guild_id)
    cap = balance * float(config.GUILD_VELOCITY_PCT) / 100
    return (int(spent or 0) + extra_units) <= cap


def _needs_cosign(balance: int, amount_units: int) -> bool:
    return amount_units * 100 > float(config.GUILD_COSIGN_PCT) * balance


# ── pool settlement (the only money writer in PR-2) ────────────────────


def _settle_out(
    conn: sqlite3.Connection,
    guild_id: int,
    agent_id: int,
    units: int,
    kind: str,
    note: str,
) -> int:
    """Pay pool units to a citizen. The pool is custodied in its own
    wallet (proposal #611), so grant_from_guild() draws the guild's own
    funds down - conservation holds without a second mover. The grant
    runs FIRST: a False (underfunded pool) raises before the ledger row
    exists, so money can never strand half-moved."""
    if units <= 0:
        return 0
    from db._credits import grant_from_guild

    ok = grant_from_guild(
        conn,
        agent_id,
        units,
        f"guild_{kind}",
        int(guild_id),
        target_type="guild",
        target_id=guild_id,
    )
    if not ok:
        raise ForumError("the pool cannot fund that payout right now - nothing moved.")
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, units, actor_agent_id,"
        " note) VALUES (?, ?, ?, ?, ?)",
        (guild_id, kind, units, agent_id, note),
    )
    return units


def _pay_member_out(
    conn: sqlite3.Connection, guild_id: int, agent_id: int, note: str
) -> int:
    """Pay one member's pro-rata share, minus any fee-arrears withhold.
    The pool memo always extinguishes the FULL computed share while the
    wallet grant pays only the net (retained withholds stay pool-owned
    via a guild_retained pair) - otherwise a withheld share would stay
    ledger-entitled and pay twice. Grant-first still holds: an
    underfunded pool raises before memo or arrears move."""
    from db._guilds_treasury import _apply_arrears_withhold

    gross = _payout_for(conn, guild_id, agent_id, guild_balance(conn, guild_id))
    if gross <= 0:
        return 0
    from db._credits import grant_from_guild, guild_retain_withhold

    net, settled = _apply_arrears_withhold(conn, guild_id, agent_id, gross)
    if net > 0:
        ok = grant_from_guild(
            conn,
            agent_id,
            net,
            "guild_withdrawal",
            int(guild_id),
            target_type="guild",
            target_id=guild_id,
        )
        if not ok:
            raise ForumError(
                "the pool cannot fund that payout right now - nothing moved."
            )
    if settled > 0:
        guild_retain_withhold(conn, int(guild_id), settled)
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, units, actor_agent_id,"
        " note) VALUES (?, 'withdrawal', ?, ?, ?)",
        (guild_id, gross, agent_id, note),
    )
    return net


def _clear_emptied(conn: sqlite3.Connection, guild_id: int) -> None:
    """Clear a stale emptied_at stamp when the roster gains a row: without
    this, a rejoined guild would inherit its earlier empty clock and face
    instant timeout-disband on its next empty."""
    conn.execute("UPDATE guilds SET emptied_at = NULL WHERE id = ?", (guild_id,))


_CHURN_DIGEST_LIMIT = 8


def _record_churn(
    conn: sqlite3.Connection,
    guild_id: int,
    agent_id: int,
    agent_name: str,
    kind: str,
) -> None:
    """Roster-churn accumulator (item 5039): a join or leave lands here as
    a row, and the sweep emits one digest ping per current member instead
    of a ping per event. Targeted pings (invites, verdicts, payouts) are
    untouched - only roster announcements batch."""
    conn.execute(
        "INSERT INTO guild_churn (guild_id, agent_id, agent_name, kind)"
        " VALUES (?, ?, ?, ?)",
        (guild_id, agent_id, agent_name, kind),
    )


def _sweep_churn_digest(conn: sqlite3.Connection, guild_id: int) -> int:
    """Emit the pending roster digest (item 5039): "2 joined (a, b),
    1 left (c)" as ONE unread row per current member, refreshed while
    unread (the vote-tally digest contract) - a member who already read
    gets a fresh row on new churn instead. Returns members pinged."""
    from notifications import _format_tally_names, _notify_tally

    rows = conn.execute(
        "SELECT agent_name, kind FROM guild_churn WHERE guild_id = ? ORDER BY id ASC",
        (guild_id,),
    ).fetchall()
    if not rows:
        return 0
    members = conn.execute(
        "SELECT agent_id FROM guild_members WHERE guild_id = ? ORDER BY id",
        (guild_id,),
    ).fetchall()
    if not members:
        conn.execute("DELETE FROM guild_churn WHERE guild_id = ?", (guild_id,))
        return 0
    joined = [r["agent_name"] for r in rows if r["kind"] == "join"]
    left = [r["agent_name"] for r in rows if r["kind"] == "leave"]
    parts = []
    if joined:
        parts.append(
            f"{len(joined)} joined ({_format_tally_names(joined, _CHURN_DIGEST_LIMIT)})"
        )
    if left:
        parts.append(
            f"{len(left)} left ({_format_tally_names(left, _CHURN_DIGEST_LIMIT)})"
        )
    body = "Roster: " + ", ".join(parts)
    # Ping first, consume after: a mid-loop failure leaves the rows for
    # the next tick, and the tally refresh makes the retry dupe-free
    # (same unread row refreshed, never a second row).
    for mrow in members:
        _notify_tally(
            conn,
            mrow["agent_id"],
            "guild",
            "guild",
            guild_id,
            body,
            match_prefix="Roster: ",
        )
    conn.execute("DELETE FROM guild_churn WHERE guild_id = ?", (guild_id,))
    return len(members)


def _live_guild_locks(conn: sqlite3.Connection, guild_id: int) -> int:
    """Live pool claims: open/offered/active job links plus active stake
    links. Fee invoices are bills, not locks; debts refuse force paths
    separately under their own seize clock."""
    jobs = conn.execute(
        "SELECT COUNT(*) FROM guild_job_links l JOIN jobs j ON j.id = l.job_id"
        " WHERE l.guild_id = ? AND j.status IN ('open', 'offered', 'active')",
        (guild_id,),
    ).fetchone()[0]
    stakes = conn.execute(
        "SELECT COUNT(*) FROM guild_stake_links l JOIN proposal_stakes s"
        " ON s.id = l.stake_id WHERE l.guild_id = ? AND s.status = 'active'",
        (guild_id,),
    ).fetchone()[0]
    return int(jobs or 0) + int(stakes or 0)


def _force_release_empty_guild(
    conn: sqlite3.Connection,
    guild_id: int,
    actor_agent_id: int | None = None,
) -> dict:
    """Release an ownerless (zero-member) guild: resolve live job/stake
    locks inline, then run the standard waterfall. Open debts refuse -
    their seize clock owns them, and force must never steal debt
    collateral. Members must already be zero; the caller owns that check
    for the sweep (which verified it) and the admin tool (which states
    it). Shared by both so manual and automatic releases cannot drift."""
    from db._guilds_lending import _open_debts, release_guild_stakes_for_disband
    from db._guilds_money import resolve_guild_jobs_for_disband

    grow = conn.execute(
        "SELECT status FROM guilds WHERE id = ?", (guild_id,)
    ).fetchone()
    if grow is None:
        raise ForumError(f"no guild with id {guild_id}.")
    if grow[0] != "active":
        # domain: fail-loudly - a terminal (or suspended) guild is never
        # re-released: the waterfall ran once and the name already freed
        raise ForumError(
            "that guild is not active - force-release is for active,"
            " ownerless guilds only."
        )
    if _member_count(conn, guild_id) > 0:
        raise ForumError(
            "that guild still holds members - force-release is for"
            " ownerless guilds only."
        )
    if _open_debts(conn, guild_id):
        raise ForumError(
            "that guild holds open debts - the seize clock owns them,"
            " force cannot take debt collateral."
        )
    resolve_guild_jobs_for_disband(conn, guild_id, actor_agent_id)
    release_guild_stakes_for_disband(conn, guild_id)
    from db._guilds_bonds import release_guild_bonds_for_disband

    release_guild_bonds_for_disband(conn, guild_id)
    return _disband_distribute(conn, guild_id, "empty force-release")


def _admin_agent(conn: sqlite3.Connection, admin: str) -> dict:
    """Resolve a human admin by name (the jobs-admin precedent): the
    admin panel session-authenticates, so engine functions take the name,
    not a token. Refuses unknown, suspended, or banned admins."""
    name = (admin or "").strip()
    row = conn.execute(
        "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is None:
        raise ForumError("unknown admin.")
    agent = dict(row)
    now = _now_iso()
    if agent.get("banned"):
        raise ForumError("that admin is banned.")
    if agent.get("suspended_until") and agent["suspended_until"] > now:
        raise ForumError("that admin is suspended.")
    return agent


def admin_release_empty_guild(admin: str, guild_id: int) -> dict:
    """Admin releases a stuck ownerless guild (item 4997): resolves
    locks inline, then runs the standard waterfall. Open debts refuse.
    Admin-only by construction (admin panel session gate)."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        out = _force_release_empty_guild(conn, guild_id, agent["id"])
        import events

        events.log_event(
            events.EVT_GUILD_DISBANDED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={"via": "admin-force-release"},
            conn=conn,
        )
        out["guild_id"] = int(guild_id)
        return out


def admin_freeze_guild(admin: str, guild_id: int, reason: str = "") -> dict:
    """Admin freezes pool spending (item 5060): sets spending_suspended
    with the admin as actor, on top of whatever the sweeps hold. Never
    claws back disbursed funds - the flag only gates new spends."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        guild = _require_guild(conn, guild_id)
        if guild["status"] != "active":
            raise ForumError("only an active guild can be frozen.")
        clean = (reason or "").strip()[:200]
        conn.execute(
            "UPDATE guilds SET spending_suspended = 1, suspended_by = ?,"
            " suspend_reason = ?, suspended_at = ? WHERE id = ?",
            (agent["id"], clean, _now_iso(), guild_id),
        )
        import events

        events.log_event(
            events.EVT_GUILD_FROZEN,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={"reason": clean, "frozen": True},
            conn=conn,
        )
        return {"guild_id": int(guild_id), "frozen": True, "reason": clean}


def admin_unfreeze_guild(admin: str, guild_id: int) -> dict:
    """Admin lifts a manual freeze. Sweep-owned freezes (delinquency,
    upkeep) clear through their own paths and are untouched here."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        _require_guild(conn, guild_id)
        conn.execute(
            "UPDATE guilds SET spending_suspended = 0, suspended_by = NULL,"
            " suspend_reason = '', suspended_at = NULL WHERE id = ?",
            (guild_id,),
        )
        import events

        events.log_event(
            events.EVT_GUILD_FROZEN,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={"frozen": False},
            conn=conn,
        )
        return {"guild_id": int(guild_id), "frozen": False}


def admin_release_guild_member(
    admin: str, guild_id: int, member: str | int, mode: str = "refund"
) -> dict:
    """Admin removes a member (item 5060): 'refund' pays their pro-rata
    remainder through the normal payout path, 'forfeit' runs the
    suspension forfeit (half Treasury-parked, half burn). Either way the
    roster row goes, leave is logged, and taken jobs park in grace."""
    from db._guilds_lending import _forfeit_member
    from db._guilds_money import park_executor_grace

    if mode not in ("refund", "forfeit"):
        raise ForumError("release mode is 'refund' or 'forfeit'.")
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        _require_guild(conn, guild_id)
        target = _agent_by_name_or_id(conn, member)
        if target is None:
            raise ForumError("no citizen matches that name or id.")
        mem = _require_member(conn, guild_id, target["id"])
        if mem["role"] == "founder":
            raise ForumError(
                "the founder cannot be released - succession (leave) or disband first."
            )
        if mode == "forfeit":
            out = _forfeit_member(conn, guild_id, target["id"], "admin release")
            return {"guild_id": int(guild_id), **out}
        paid = _pay_member_out(
            conn, guild_id, target["id"], "admin release pro-rata remainder"
        )
        conn.execute(
            "DELETE FROM guild_members WHERE guild_id = ? AND agent_id = ?",
            (guild_id, target["id"]),
        )
        conn.execute(
            "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
            " VALUES (?, ?, ?)",
            (guild_id, target["id"], _now_iso()),
        )
        park_executor_grace(conn, guild_id, target["id"])
        import events

        events.log_event(
            events.EVT_GUILD_LEFT,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={"paid_units": paid, "via": "admin-release"},
            conn=conn,
        )
        return {"guild_id": int(guild_id), "agent_id": int(target["id"]), "paid": paid}


def admin_delete_guild_chat(admin: str, message_id: int) -> dict:
    """Admin deletes any guild chat message (item 5060): same [deleted]
    tombstone as founder deletes, attributed to the admin."""
    with _conn(immediate=True) as conn:
        agent = _admin_agent(conn, admin)
        row = conn.execute(
            "SELECT * FROM guild_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no guild message with id {message_id}.")
        msg = dict(row)
        if msg["deleted_at"] is not None:
            raise ForumError("that message is already deleted.")
        conn.execute(
            "UPDATE guild_messages SET deleted_at = ?, deleted_by = ? WHERE id = ?",
            (_now_iso(), agent["id"], message_id),
        )
        import events

        events.log_event(
            events.EVT_GUILD_CHAT_DELETED,
            actor_agent_id=agent["id"],
            target_type="guild_message",
            target_id=message_id,
            detail={"guild_id": msg["guild_id"], "via": "admin"},
            conn=conn,
        )
        return {"message_id": message_id, "deleted": True}


def _free_guild_name(conn: sqlite3.Connection, guild_id: int) -> str:
    """Free a disbanded guild's name (item 5066): the row keeps its
    history under a suffixed name no founding can collide with (the id
    rides along; a pre-squatted suffix falls through to a counter, so
    the rename itself can never fail the disband it belongs to). Every
    disband path calls this after flipping the status."""
    crow = conn.execute("SELECT name FROM guilds WHERE id = ?", (guild_id,)).fetchone()
    base = f"{crow['name']} (disbanded #{guild_id})" if crow else f"#{guild_id}"
    candidate, n = base, 0
    while True:
        try:
            conn.execute(
                "UPDATE guilds SET name = ? WHERE id = ?",
                (candidate, guild_id),
            )
            return candidate
        except sqlite3.IntegrityError:
            # domain: never-lose-data - a squatted suffix retries with a
            # counter instead of failing the disband mid-waterfall
            n += 1
            if n > 100:
                raise ForumError(
                    "the disbanded name cannot be freed - try again later."
                ) from None
            candidate = f"{base} {n}"


def _disband_distribute(conn: sqlite3.Connection, guild_id: int, reason: str) -> dict:
    """Waterfall shared by every disband path: each member takes their
    pro-rata share from the guild wallet, the remainder (pool income,
    dust) moves -guild/+treasury paired with a memo row (proposal #611 -
    the wallet custodies the pool, so the remainder must travel). All-or-nothing: any
    unfunded payout raises and the whole transaction rolls back, so a
    retry next tick (or a founder retry) sees the exact pre-attempt
    state. Callers isolate failures (sweep skips + logs, leave defers)
    instead of trapping anyone. Member pings ride the same transaction,
    so a rolled-back attempt never notifies."""
    from db._guilds_lending import _prepare_guild_disband

    _prepare_guild_disband(conn, guild_id)
    paid: dict[int, int] = {}
    members = conn.execute(
        "SELECT agent_id FROM guild_members WHERE guild_id = ? ORDER BY id",
        (guild_id,),
    ).fetchall()
    for row in members:
        aid = row[0]
        paid[aid] = _pay_member_out(
            conn, guild_id, aid, f"disband distribution ({reason})"
        )
        _notify(
            conn,
            aid,
            "guild",
            "guild",
            guild_id,
            f"guild disbanded ({reason}) - you received {paid[aid]}u.",
            actor_agent_id=None,
        )
    for row in members:
        conn.execute(
            "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
            " VALUES (?, ?, ?)",
            (guild_id, row[0], _now_iso()),
        )
    from db._guilds_treasury import _void_open_arrears

    _void_open_arrears(conn, guild_id)
    conn.execute("DELETE FROM guild_members WHERE guild_id = ?", (guild_id,))
    # No roster left to digest to: drop pending churn with the roster.
    conn.execute("DELETE FROM guild_churn WHERE guild_id = ?", (guild_id,))
    # Both trails close independently: the wallet remainder travels
    # -guild/+treasury paired, while the memo remainder (which can differ
    # by retained arrears-withholds, pool-owned either way) extinguishes
    # the memo trail. Either one is zero-skipped on its own.
    remainder_wallet = guild_balance(conn, guild_id)
    if remainder_wallet > 0:
        from db._credits import _insert_entry, _new_tx_id

        tx_id = _new_tx_id(conn)
        _insert_entry(
            conn,
            None,
            "guild",
            -remainder_wallet,
            "guild_disband_remainder",
            "guild",
            int(guild_id),
            tx_id=tx_id,
        )
        _insert_entry(
            conn,
            None,
            "treasury",
            remainder_wallet,
            "guild_disband_remainder",
            "guild",
            int(guild_id),
            tx_id=tx_id,
        )
    remainder_memo = guild_memo_balance(conn, guild_id)
    if remainder_memo > 0:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, note)"
            " VALUES (?, 'withdrawal', ?, ?)",
            (
                guild_id,
                remainder_memo,
                f"disband remainder to Treasury ({reason})",
            ),
        )
    conn.execute(
        "UPDATE guilds SET status = 'disbanded', disbanded_at = ? WHERE id = ?",
        (_now_iso(), guild_id),
    )
    _free_guild_name(conn, guild_id)
    return {"paid": paid, "disbanded": True}


def _successor_id(
    conn: sqlite3.Connection, guild_id: int, founder_id: int
) -> int | None:
    """Longest-tenured (earliest joined) member who is neither the founder
    nor banned nor idle past GUILD_IDLE_DAYS nor suspended. None means disband."""
    rows = conn.execute(
        "SELECT m.agent_id, m.joined_at, a.last_seen_at, a.created_at,"
        " a.suspended_until, a.banned FROM guild_members m JOIN agents a"
        " ON a.id = m.agent_id WHERE m.guild_id = ? AND m.agent_id != ?"
        " ORDER BY m.joined_at ASC, m.id ASC",
        (guild_id, founder_id),
    ).fetchall()
    idle_after = float(config.GUILD_IDLE_DAYS)
    now = _now_iso()
    for row in rows:
        if row["banned"]:
            continue
        if row["suspended_until"] and row["suspended_until"] > now:
            continue
        seen = row["last_seen_at"] or row["created_at"]
        if _age_days(seen) > idle_after:
            continue
        return int(row["agent_id"])
    return None


def _run_succession(conn: sqlite3.Connection, guild: dict, why: str) -> dict:
    import events

    heir = _successor_id(conn, guild["id"], guild["founder_agent_id"])
    if heir is None:
        # System actor (not the deposed founder): _notify self-noops
        # when recipient == actor, and the deposed must hear this.
        _notify(
            conn,
            guild["founder_agent_id"],
            "guild",
            "guild",
            guild["id"],
            f"guild {guild['name']!r} disbanded for want of an heir ({why});"
            " distribution running.",
            actor_agent_id=None,
        )
        # Resolve pool-funded jobs first: cancelling returns their escrow
        # pool-parked (taken ones detach), so the distribution below can
        # never pay out money that is still locked in job escrow.
        from db._guilds_money import resolve_guild_jobs_for_disband

        resolve_guild_jobs_for_disband(conn, guild["id"], actor_agent_id=None)
        out = _disband_distribute(conn, guild["id"], f"no heir ({why})")
        events.log_event(
            events.EVT_GUILD_DISBANDED,
            actor_agent_id=guild["founder_agent_id"],
            target_type="guild",
            target_id=guild["id"],
            detail={"why": why, "heir": None},
            conn=conn,
        )
        return {"heir": None, **out}
    conn.execute(
        "UPDATE guild_members SET role = 'founder' WHERE guild_id = ? AND agent_id = ?",
        (guild["id"], heir),
    )
    conn.execute(
        "UPDATE guilds SET founder_agent_id = ? WHERE id = ?",
        (heir, guild["id"]),
    )
    heir_name = conn.execute("SELECT name FROM agents WHERE id = ?", (heir,)).fetchone()
    _notify(
        conn,
        guild["founder_agent_id"],
        "guild",
        "guild",
        guild["id"],
        f"you no longer steward guild {guild['name']!r} ({why}) -"
        f" {(heir_name['name'] if heir_name else heir)} inherits.",
        actor_agent_id=None,
    )
    events.log_event(
        events.EVT_GUILD_SUCCEEDED,
        actor_agent_id=guild["founder_agent_id"],
        target_type="guild",
        target_id=guild["id"],
        detail={"why": why, "heir": heir},
        conn=conn,
    )
    _notify(
        conn,
        heir,
        "guild",
        "guild",
        guild["id"],
        f"you inherit the founder seat of guild {guild['name']!r} ({why}).",
        actor_agent_id=guild["founder_agent_id"],
    )
    return {"heir": heir}


# ── founding ───────────────────────────────────────────────────────────


def found_guild(token: str, name: str) -> dict:
    """Found a guild: 1cr to the Treasury, >=12 karma, solo allowed.
    Caps: 1 active founded, 3 concurrent memberships, 10 live guilds
    society-wide, 14d re-found cooldown after a voluntary disband."""
    from db._credits import exact_from_credits, spend
    from db._karma import effective_karma

    clean = (name or "").strip()
    if not clean:
        raise ForumError("guild name cannot be empty.")
    if len(clean) > int(config.GUILD_NAME_MAX_LEN):
        raise ForumError(
            f"guild name must be {config.GUILD_NAME_MAX_LEN} characters or fewer."
        )
    fee_q = int(
        exact_from_credits(float(config.GUILD_FOUND_COST_CREDITS), what="guild cost")
    )
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        if effective_karma(conn, agent["id"]) < int(config.GUILD_FOUND_KARMA):
            raise ForumError(
                f"founding a guild needs {config.GUILD_FOUND_KARMA} effective karma."
            )
        founded = conn.execute(
            "SELECT COUNT(*) FROM guilds WHERE founder_agent_id = ?"
            " AND status = 'active'",
            (agent["id"],),
        ).fetchone()[0]
        if founded >= 1:
            raise ForumError(
                "you already steward an active guild - disband it before"
                " founding another."
            )
        if _membership_count(conn, agent["id"]) >= int(config.GUILD_MAX_MEMBERSHIPS):
            raise ForumError(
                f"at most {config.GUILD_MAX_MEMBERSHIPS} concurrent guild"
                " memberships per citizen."
            )
        live = conn.execute(
            "SELECT COUNT(*) FROM guilds WHERE status = 'active'"
        ).fetchone()[0]
        if live >= int(config.GUILD_MAX_GUILDS):
            raise ForumError(
                f"at most {config.GUILD_MAX_GUILDS} live guilds society-wide"
                " (GUILD_MAX_GUILDS) - one must disband first."
            )
        last_disband = conn.execute(
            "SELECT MAX(disbanded_at) FROM guilds WHERE founder_agent_id = ?"
            " AND status = 'disbanded'",
            (agent["id"],),
        ).fetchone()[0]
        if last_disband and _age_days(last_disband) < float(config.GUILD_REFOUND_DAYS):
            raise ForumError(
                f"a {config.GUILD_REFOUND_DAYS}d cooldown follows a voluntary"
                " disband before you may found again."
            )
        try:
            cur = conn.execute(
                "INSERT INTO guilds (name, founder_agent_id) VALUES (?, ?)",
                (clean, agent["id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise ForumError(
                f"a guild named {clean!r} already exists (names are unique"
                " regardless of case)."
            ) from exc
        guild_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO guild_members (guild_id, agent_id, role, heartbeat_at)"
            " VALUES (?, ?, 'founder', ?)",
            (guild_id, agent["id"], _now_iso()),
        )
        if fee_q:
            spend(
                agent["id"],
                fee_q,
                "guild_found_cost",
                dest_treasury=True,
                target_type="guild",
                target_id=guild_id,
                conn=conn,
            )
        import events

        events.log_event(
            events.EVT_GUILD_CREATED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"name": clean},
            conn=conn,
        )
        # Same-transaction read: get_guild() opens its own connection and
        # could never see this still-uncommitted row.
        return _guild_detail(conn, guild_id)


# ── invites + join requests ────────────────────────────────────────────


def invite_guild_member(token: str, guild_id: int, invitee: str | int) -> dict:
    """Founder invites one citizen: 7d accept/decline, mailbox ping."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        target = _agent_by_name_or_id(conn, invitee)
        if target is None:
            raise ForumError("no citizen matches that name or id.")
        if target["id"] == agent["id"]:
            raise ForumError("you are already the founder - no invite needed.")
        if _member_row(conn, guild_id, target["id"]) is not None:
            raise ForumError(f"{target['name']!r} is already a member.")
        if _member_count(conn, guild_id) >= int(config.GUILD_MAX_MEMBERS):
            raise ForumError(
                f"guild {guild['name']!r} is full"
                f" ({config.GUILD_MAX_MEMBERS} members max)."
            )
        live = conn.execute(
            "SELECT id FROM guild_invites WHERE guild_id = ? AND agent_id = ?"
            " AND status = 'proposed'",
            (guild_id, target["id"]),
        ).fetchone()
        if live is not None:
            raise ForumError(f"{target['name']!r} already holds an invite.")
        cur = conn.execute(
            "INSERT INTO guild_invites (guild_id, agent_id, invited_by,"
            " expires_at) VALUES (?, ?, ?, ?)",
            (
                guild_id,
                target["id"],
                agent["id"],
                _days_ago_iso(-float(config.GUILD_INVITE_DAYS)),
            ),
        )
        invite_id = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_INVITED,
            actor_agent_id=agent["id"],
            target_type="guild_invite",
            target_id=invite_id,
            detail={"guild_id": guild_id, "invitee": target["id"]},
            conn=conn,
        )
        _notify(
            conn,
            target["id"],
            "guild",
            "guild_invite",
            invite_id,
            f"{agent['name']} invites you to guild {guild['name']!r}"
            f" (expires in {config.GUILD_INVITE_DAYS}d).",
            actor_agent_id=agent["id"],
        )
        return {"invite_id": invite_id, "guild_id": guild_id, "invitee": target["name"]}


def respond_guild_invite(token: str, invite_id: int, accept: bool) -> dict:
    """Accept (join) or decline an invite. Expired invites refuse."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM guild_invites WHERE id = ?", (invite_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no invite with id {invite_id}.")
        inv = dict(row)
        if int(inv["agent_id"]) != int(agent["id"]):
            raise ForumError("that invite names another citizen.")
        if inv["status"] != "proposed":
            raise ForumError(f"that invite is already {inv['status']}.")
        if inv["expires_at"] <= _now_iso():
            conn.execute(
                "UPDATE guild_invites SET status = 'expired', decided_at = ?"
                " WHERE id = ?",
                (_now_iso(), invite_id),
            )
            raise ForumError("that invite expired - ask for a fresh one.")
        guild = _require_guild(conn, inv["guild_id"])
        if not accept:
            conn.execute(
                "UPDATE guild_invites SET status = 'declined', decided_at = ?"
                " WHERE id = ?",
                (_now_iso(), invite_id),
            )
            _notify(
                conn,
                inv["invited_by"],
                "guild",
                "guild",
                guild["id"],
                f"{agent['name']} declined your invite to guild {guild['name']!r}.",
                actor_agent_id=agent["id"],
            )
            return {"invite_id": invite_id, "accepted": False}
        if _member_count(conn, guild["id"]) >= int(config.GUILD_MAX_MEMBERS):
            raise ForumError(f"guild {guild['name']!r} filled before you accepted.")
        if _membership_count(conn, agent["id"]) >= int(config.GUILD_MAX_MEMBERSHIPS):
            raise ForumError(
                f"at most {config.GUILD_MAX_MEMBERSHIPS} concurrent guild"
                " memberships per citizen."
            )
        if _member_row(conn, guild["id"], agent["id"]) is not None:
            # Stale invite (joined via another path meanwhile): refuse
            # without touching the row - expiry reaps it, and a deny-mark
            # here would roll back with the raise below anyway.
            raise ForumError("you are already a member.")
        _rejoin_cooldown_ok(conn, guild["id"], agent["id"])
        try:
            conn.execute(
                "INSERT INTO guild_members (guild_id, agent_id, heartbeat_at)"
                " VALUES (?, ?, ?)",
                (guild["id"], agent["id"], _now_iso()),
            )
        except sqlite3.IntegrityError:
            raise ForumError("you are already a member.") from None
        _clear_emptied(conn, guild["id"])
        _record_churn(conn, guild["id"], agent["id"], agent["name"], "join")
        conn.execute(
            "UPDATE guild_invites SET status = 'accepted', decided_at = ? WHERE id = ?",
            (_now_iso(), invite_id),
        )
        _notify(
            conn,
            inv["invited_by"],
            "guild",
            "guild",
            guild["id"],
            f"{agent['name']} accepted your invite to guild {guild['name']!r}.",
            actor_agent_id=agent["id"],
        )
        import events

        events.log_event(
            events.EVT_GUILD_JOINED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild["id"],
            detail={"via": "invite"},
            conn=conn,
        )
        return {"invite_id": invite_id, "accepted": True, "guild_id": guild["id"]}


def request_guild_join(token: str, guild_id: int, message: str = "") -> dict:
    """Ask to join an open-enrollment guild (invite_only guilds refuse)."""
    clean = (message or "").strip()
    if len(clean) > 1000:
        raise ForumError("join message must be 1000 characters or fewer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        enrollment = (guild.get("enrollment") or "invite_only").lower()
        if enrollment != "open":
            raise ForumError(
                f"guild {guild['name']!r} is invite-only - ask the founder"
                " for an invite."
            )
        if _member_row(conn, guild_id, agent["id"]) is not None:
            raise ForumError("you are already a member.")
        _rejoin_cooldown_ok(conn, guild_id, agent["id"])
        dup = conn.execute(
            "SELECT id FROM guild_join_requests WHERE guild_id = ? AND agent_id = ?"
            " AND status = 'open'",
            (guild_id, agent["id"]),
        ).fetchone()
        if dup is not None:
            raise ForumError("you already have an open join request.")
        try:
            cur = conn.execute(
                "INSERT INTO guild_join_requests (guild_id, agent_id, message,"
                " expires_at) VALUES (?, ?, ?, ?)",
                (
                    guild_id,
                    agent["id"],
                    clean,
                    _days_ago_iso(-float(config.GUILD_JOIN_REQUEST_DAYS)),
                ),
            )
        except sqlite3.IntegrityError:
            raise ForumError("you already have an open join request.") from None
        req_id = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_JOIN_REQUESTED,
            actor_agent_id=agent["id"],
            target_type="guild_join_request",
            target_id=req_id,
            detail={"guild_id": guild_id},
            conn=conn,
        )
        _notify(
            conn,
            guild["founder_agent_id"],
            "guild",
            "guild_join_request",
            req_id,
            f"{agent['name']} asks to join guild {guild['name']!r}.",
            actor_agent_id=agent["id"],
        )
        return {"request_id": req_id, "guild_id": guild_id}


def respond_guild_join(token: str, request_id: int, approve: bool) -> dict:
    """Founder approves or denies an open join request."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM guild_join_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no join request with id {request_id}.")
        req = dict(row)
        guild = _require_guild(conn, req["guild_id"])
        _require_founder(conn, guild, agent["id"])
        if req["status"] != "open":
            raise ForumError(f"that request is already {req['status']}.")
        verdict = "approved" if approve else "denied"
        if approve:
            req_agent = conn.execute(
                "SELECT banned, suspended_until FROM agents WHERE id = ?",
                (req["agent_id"],),
            ).fetchone()
            if (
                req_agent is None
                or req_agent["banned"]
                or (
                    req_agent["suspended_until"]
                    and req_agent["suspended_until"] > _now_iso()
                )
            ):
                # No deny-mark: it would roll back with the raise below,
                # and a lifted suspension must stay approvable until the
                # request expires on its own.
                raise ForumError(
                    "that citizen is suspended, banned, or gone - request cannot be approved."
                )
            if _member_count(conn, guild["id"]) >= int(config.GUILD_MAX_MEMBERS):
                raise ForumError(f"guild {guild['name']!r} is full.")
            if _membership_count(conn, req["agent_id"]) >= int(
                config.GUILD_MAX_MEMBERSHIPS
            ):
                raise ForumError("that citizen is at the membership cap.")
            if _member_row(conn, guild["id"], req["agent_id"]) is not None:
                raise ForumError("that citizen is already a member.")
            _rejoin_cooldown_ok(conn, guild["id"], req["agent_id"])
            try:
                conn.execute(
                    "INSERT INTO guild_members (guild_id, agent_id, heartbeat_at)"
                    " VALUES (?, ?, ?)",
                    (guild["id"], req["agent_id"], _now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ForumError("that citizen is already a member.") from None
            _clear_emptied(conn, guild["id"])
            _record_churn(
                conn,
                guild["id"],
                req["agent_id"],
                _agent_name(conn, req["agent_id"]),
                "join",
            )
        conn.execute(
            "UPDATE guild_join_requests SET status = ?, decided_at = ?,"
            " decided_by = ? WHERE id = ?",
            (verdict, _now_iso(), agent["id"], request_id),
        )
        import events

        events.log_event(
            events.EVT_GUILD_JOIN_ANSWERED,
            actor_agent_id=agent["id"],
            target_type="guild_join_request",
            target_id=request_id,
            detail={"verdict": verdict},
            conn=conn,
        )
        _notify(
            conn,
            req["agent_id"],
            "guild",
            "guild_join_request",
            request_id,
            f"your request to join guild {guild['name']!r} was {verdict}.",
            actor_agent_id=agent["id"],
        )
        return {"request_id": request_id, "verdict": verdict}


def set_guild_enrollment(token: str, guild_id: int, enrollment: str) -> dict:
    """Founder flips open <-> invite_only."""
    clean = (enrollment or "").strip().lower()
    if clean not in ("open", "invite_only"):
        raise ForumError("enrollment is 'open' or 'invite_only'.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        conn.execute("UPDATE guilds SET enrollment = ? WHERE id = ?", (clean, guild_id))
        import events

        events.log_event(
            events.EVT_GUILD_ENROLLMENT,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"enrollment": clean},
            conn=conn,
        )
        return {"guild_id": guild_id, "enrollment": clean}


def rename_guild(token: str, guild_id: int, name: str) -> dict:
    """Founder renames the guild (NOCASE-unique, non-empty, length-capped
    like founding). The old name frees the moment the row updates."""
    clean = (name or "").strip()
    if not clean:
        raise ForumError("guild name cannot be empty.")
    if len(clean) > int(config.GUILD_NAME_MAX_LEN):
        raise ForumError(
            f"guild name must be {config.GUILD_NAME_MAX_LEN} characters or fewer."
        )
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        if clean.lower() == guild["name"].lower():
            return {"guild_id": guild_id, "name": guild["name"]}
        try:
            conn.execute("UPDATE guilds SET name = ? WHERE id = ?", (clean, guild_id))
        except sqlite3.IntegrityError as exc:
            raise ForumError(
                f"guild name {clean!r} is taken - pick a distinct name."
            ) from exc
        import events

        events.log_event(
            events.EVT_GUILD_RENAMED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"old_name": guild["name"], "new_name": clean},
            conn=conn,
        )
        return {"guild_id": guild_id, "name": clean}


def edit_guild_mission(token: str, guild_id: int, mission: str) -> dict:
    """Founder sets the guild mission (≤200 chars, empty clears). Logged
    on the founder-action ledger like every other founder act."""
    clean = (mission or "").strip()
    if len(clean) > 200:
        raise ForumError("guild mission must be 200 characters or fewer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        conn.execute("UPDATE guilds SET mission = ? WHERE id = ?", (clean, guild_id))
        import events

        events.log_event(
            events.EVT_GUILD_MISSION,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"mission": clean[:200]},
            conn=conn,
        )
        return {"guild_id": guild_id, "mission": clean}


# ── leave / heartbeat / succession ─────────────────────────────────────


def _rejoin_cooldown_ok(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> None:
    """Refuse EVERY membership-entry path inside the 14d rejoin window -
    invite accepts, join approvals, join requests, and rejoins alike.
    The frozen text promises the cooldown on rejoining, and a founder
    re-invite (or an approved open request) resets counters exactly like
    a rejoin, so all four gates read the same leave log."""
    last = conn.execute(
        "SELECT MAX(left_at) FROM guild_leave_log WHERE guild_id = ? AND agent_id = ?",
        (guild_id, agent_id),
    ).fetchone()[0]
    if last and _age_days(last) < float(config.GUILD_REJOIN_DAYS):
        raise ForumError(
            f"a {config.GUILD_REJOIN_DAYS}d cooldown follows leaving before you may rejoin the same guild."
        )


def leave_guild(token: str, guild_id: int) -> dict:
    """Free exit anytime with a pro-rata-by-net-deposits refund of the
    remainder, capped at net deposits (never the deposit back whole when
    the pool shrank - no insurance). Leaving resets counters: the roster
    row is deleted and the money trail stays in the ledger. No kicks
    exist; the founder leaving fires succession (heir or disband)."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        member = _require_member(conn, guild_id, agent["id"])
        paid = _pay_member_out(conn, guild_id, agent["id"], "leave pro-rata remainder")
        conn.execute(
            "DELETE FROM guild_members WHERE guild_id = ? AND agent_id = ?",
            (guild_id, agent["id"]),
        )
        _record_churn(conn, guild_id, agent["id"], agent["name"], "leave")
        conn.execute(
            "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
            " VALUES (?, ?, ?)",
            (guild_id, agent["id"], _now_iso()),
        )
        # Guild Plan v1 (proposal #584): a leaver's plan ownerships
        # vacate with a journal entry (founder reassigns) - never dangle.
        try:
            from db._guilds_plans import vacate_plan_owners

            vacate_plan_owners(conn, guild_id, agent["id"])
        except (
            Exception
        ):  # domain: degrade-silently - vacancy advisory, leave never fails
            pass
        # Taken jobs park in successor grace (item 5009): the pool keeps
        # its wage claim for 7d while a successor may be appointed; only
        # the sweep's lapse detaches them.
        from db._guilds_money import park_executor_grace

        park_executor_grace(conn, guild_id, agent["id"])
        import events

        events.log_event(
            events.EVT_GUILD_LEFT,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"paid_units": paid, "role": member["role"]},
            conn=conn,
        )
        out: dict = {"guild_id": guild_id, "paid_units": paid}
        if member["role"] == "founder":
            # Deliberately uncaught: if the succession's disband cannot
            # fund every share, the whole transaction (including this
            # leave) rolls back and the founder stays. Catching it here
            # would COMMIT a founderless guild with unpaid members -
            # stranding the guild is worse than refusing the exit.
            out["succession"] = _run_succession(conn, guild, "founder left")
        return out


def rejoin_guild(token: str, guild_id: int) -> dict:
    """Fresh rejoin after the 14d same-guild cooldown, via invite or open
    enrollment - counters never restore (the roster row is new)."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        if _member_row(conn, guild_id, agent["id"]) is not None:
            raise ForumError("you are already a member.")
        _rejoin_cooldown_ok(conn, guild_id, agent["id"])
        enrollment = (guild.get("enrollment") or "invite_only").lower()
        if enrollment != "open":
            raise ForumError(
                f"guild {guild['name']!r} is invite-only - ask the founder"
                " for an invite."
            )
        if _member_count(conn, guild_id) >= int(config.GUILD_MAX_MEMBERS):
            raise ForumError(f"guild {guild['name']!r} is full.")
        if _membership_count(conn, agent["id"]) >= int(config.GUILD_MAX_MEMBERSHIPS):
            raise ForumError(
                f"at most {config.GUILD_MAX_MEMBERSHIPS} concurrent guild"
                " memberships per citizen."
            )
        conn.execute(
            "INSERT INTO guild_members (guild_id, agent_id, heartbeat_at)"
            " VALUES (?, ?, ?)",
            (guild_id, agent["id"], _now_iso()),
        )
        _clear_emptied(conn, guild_id)
        _record_churn(conn, guild_id, agent["id"], agent["name"], "join")
        import events

        events.log_event(
            events.EVT_GUILD_JOINED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"via": "rejoin"},
            conn=conn,
        )
        return {"guild_id": guild_id, "rejoined": True}


def heartbeat_guild(token: str, guild_id: int) -> dict:
    """Stamp the 14d membership confirm. Missing 2 consecutive heartbeats
    auto-releases (the sweep, not this call)."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_guild(conn, guild_id)
        _require_member(conn, guild_id, agent["id"])
        conn.execute(
            "UPDATE guild_members SET heartbeat_at = ? WHERE guild_id = ?"
            " AND agent_id = ?",
            (_now_iso(), guild_id, agent["id"]),
        )
        import events

        events.log_event(
            events.EVT_GUILD_HEARTBEAT,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={},
            conn=conn,
        )
        return {"guild_id": guild_id, "heartbeat_at": _now_iso()}


def sweep_guild_memberships() -> dict:
    """System sweep (poller wiring lands in PR-5): heartbeat releases,
    founder succession (idle/suspended), and expiry of invites, join
    requests, and co-signs. Per-entry isolation: one unfunded payout logs
    and retries next interval instead of stalling its neighbours
    (never-lose-data)."""

    report: dict = {
        "released": [],
        "succeeded": [],
        "disbanded": [],
        "expired": 0,
        "polls_closed": 0,
        "grace_expired": 0,
        "skipped": [],
    }
    miss_after = float(config.GUILD_HEARTBEAT_DAYS) * 2
    with _conn(immediate=True) as conn:
        import events

        guilds = conn.execute("SELECT * FROM guilds WHERE status = 'active'").fetchall()
        for grow in guilds:
            guild = dict(grow)
            gid = guild["id"]
            members = conn.execute(
                "SELECT * FROM guild_members WHERE guild_id = ? ORDER BY id",
                (gid,),
            ).fetchall()
            for mrow in members:
                mem = dict(mrow)
                last = mem["heartbeat_at"] or mem["joined_at"]
                if _age_days(last) <= miss_after:
                    continue
                try:
                    paid = _pay_member_out(
                        conn,
                        gid,
                        mem["agent_id"],
                        "heartbeat auto-release pro-rata",
                    )
                except Exception as exc:
                    # domain: never-lose-data - any payout failure (funds
                    # or infra) skips this member; the next tick retries.
                    report["skipped"].append(
                        {
                            "guild_id": gid,
                            "agent_id": mem["agent_id"],
                            "why": "payout-failed",
                        }
                    )
                    logutil.log(
                        "guild_sweep_payout_failed",
                        guild_id=gid,
                        agent_id=mem["agent_id"],
                        error=str(exc),
                    )
                    continue
                try:
                    conn.execute(
                        "DELETE FROM guild_members WHERE guild_id = ? AND agent_id = ?",
                        (gid, mem["agent_id"]),
                    )
                    _record_churn(
                        conn,
                        gid,
                        mem["agent_id"],
                        _agent_name(conn, mem["agent_id"]),
                        "leave",
                    )
                    conn.execute(
                        "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
                        " VALUES (?, ?, ?)",
                        (gid, mem["agent_id"], _now_iso()),
                    )
                    from db._guilds_money import park_executor_grace

                    park_executor_grace(conn, gid, mem["agent_id"])
                    events.log_event(
                        events.EVT_GUILD_LEFT,
                        actor_agent_id=mem["agent_id"],
                        target_type="guild",
                        target_id=gid,
                        detail={"paid_units": paid, "via": "heartbeat-sweep"},
                        conn=conn,
                    )
                    report["released"].append(
                        {
                            "guild_id": gid,
                            "agent_id": mem["agent_id"],
                            "paid_units": paid,
                        }
                    )
                    if mem["role"] == "founder":
                        try:
                            out = _run_succession(
                                conn, guild, "founder heartbeat-lapsed"
                            )
                        except Exception as exc:
                            # domain: never-lose-data - any succession
                            # failure defers this founder; the next tick
                            # retries (the outer tail guard is the backstop).
                            report["skipped"].append(
                                {
                                    "guild_id": gid,
                                    "agent_id": mem["agent_id"],
                                    "why": "succession-failed",
                                }
                            )
                            logutil.log(
                                "guild_sweep_succession_failed",
                                guild_id=gid,
                                why="founder heartbeat-lapsed",
                                error=str(exc),
                            )
                            continue
                        if out.get("heir") is not None:
                            report["succeeded"].append(
                                {"guild_id": gid, "heir": out["heir"]}
                            )
                        else:
                            report["disbanded"].append(gid)
                except Exception as exc:
                    # domain: never-lose-data - post-payout tail
                    # (release rows, detach, events, succession)
                    # isolates per member like the payout above.
                    report["skipped"].append(
                        {
                            "guild_id": gid,
                            "agent_id": mem["agent_id"],
                            "why": "release-failed",
                        }
                    )
                    logutil.log(
                        "guild_sweep_payout_failed",
                        guild_id=gid,
                        agent_id=mem["agent_id"],
                        error=str(exc),
                    )
                    continue
            founder = conn.execute(
                "SELECT a.* FROM agents a WHERE a.id = ?",
                (guild["founder_agent_id"],),
            ).fetchone()
            if founder is not None:
                still = _member_row(conn, gid, founder["id"])
                if still is not None and still["role"] == "founder":
                    fro = dict(founder)
                    now = _now_iso()
                    suspended = bool(
                        fro["suspended_until"] and fro["suspended_until"] > now
                    )
                    seen = fro["last_seen_at"] or fro["created_at"]
                    idle = _age_days(seen) > float(config.GUILD_IDLE_DAYS)
                    if suspended or idle:
                        # Demote first: the deposed founder stays a member
                        # (the forfeit path owns removal) but must not keep
                        # the founder role - otherwise the roster holds two
                        # founders and a later sweep could re-promote them.
                        # Savepoint: a failed succession rolls the demotion
                        # back with it, so a retry never meets a headless
                        # guild whose founder stamp is already gone.
                        conn.execute("SAVEPOINT guild_succession")
                        try:
                            conn.execute(
                                "UPDATE guild_members SET role = 'member'"
                                " WHERE guild_id = ? AND agent_id = ?",
                                (gid, founder["id"]),
                            )
                            out = _run_succession(
                                conn,
                                guild,
                                "founder suspended" if suspended else "founder idle",
                            )
                        except Exception as exc:
                            # domain: never-lose-data - any succession
                            # failure rolls the demotion back with it and
                            # defers; the next tick retries a founded guild.
                            conn.execute("ROLLBACK TO SAVEPOINT guild_succession")
                            conn.execute("RELEASE guild_succession")
                            report["skipped"].append(
                                {
                                    "guild_id": gid,
                                    "agent_id": founder["id"],
                                    "why": "succession-failed",
                                }
                            )
                            logutil.log(
                                "guild_sweep_succession_failed",
                                guild_id=gid,
                                why=(
                                    "founder suspended" if suspended else "founder idle"
                                ),
                                error=str(exc),
                            )
                            continue
                        conn.execute("RELEASE guild_succession")
                        if out.get("heir") is not None:
                            report["succeeded"].append(
                                {"guild_id": gid, "heir": out["heir"]}
                            )
                        else:
                            report["disbanded"].append(gid)
            # Successor-grace lapse (item 5009): parked taken links whose
            # clock ran out detach to the executor personally.
            from db._guilds_money import detach_executor_jobs

            try:
                lapsed = conn.execute(
                    "SELECT job_id FROM guild_job_links WHERE guild_id = ?"
                    " AND role = 'taken' AND grace_until IS NOT NULL"
                    " AND grace_until <= ?",
                    (gid, _now_iso()),
                ).fetchall()
            except Exception:
                # domain: degrade-silently - a corrupt clock reads empty;
                # the next tick retries the read, nothing detaches blind
                lapsed = []
            for lrow in lapsed:
                try:
                    erow = conn.execute(
                        "SELECT executor_agent_id FROM guild_job_links"
                        " WHERE job_id = ?",
                        (lrow[0],),
                    ).fetchone()
                    detach_executor_jobs(conn, gid, int(erow[0]))
                    report["grace_expired"] += 1
                except Exception as exc:
                    # domain: never-lose-data - one poisoned link logs and
                    # retries next tick instead of stalling its neighbours
                    report["skipped"].append(
                        {"guild_id": gid, "job_id": lrow[0], "why": "grace-failed"}
                    )
                    logutil.log(
                        "guild_sweep_grace_failed",
                        guild_id=gid,
                        job_id=lrow[0],
                        error=str(exc),
                    )
            # Empty-with-locks timeout (item 4997): an ownerless guild
            # holding live locks stamps emptied_at; 14d later the sweep
            # force-releases and disbands. Open debts refuse (their seize
            # clock owns them); no-lock empties disband at once.
            from db._guilds_lending import _open_debts

            if not conn.execute(
                "SELECT 1 FROM guild_members WHERE guild_id = ?", (gid,)
            ).fetchone():
                try:
                    # Fresh status: earlier in-tick work (succession
                    # disband) may have closed this guild already.
                    live = conn.execute(
                        "SELECT status FROM guilds WHERE id = ?", (gid,)
                    ).fetchone()
                    if live is None or live[0] != "active":
                        continue
                    if _open_debts(conn, gid):
                        continue
                    if _live_guild_locks(conn, gid):
                        if guild.get("emptied_at") is None:
                            conn.execute(
                                "UPDATE guilds SET emptied_at = ? WHERE id = ?",
                                (_now_iso(), gid),
                            )
                            continue
                        if _age_days(guild["emptied_at"]) <= float(
                            config.GUILD_EMPTY_TIMEOUT_DAYS
                        ):
                            continue
                    _force_release_empty_guild(conn, gid)
                    report["disbanded"].append(gid)
                except ForumError as exc:
                    # domain: degrade-silently - a refused/debt-held empty
                    # guild stays put; the next tick retries the release
                    report["skipped"].append({"guild_id": gid, "why": str(exc)[:120]})
                    logutil.log(
                        "guild_sweep_empty_failed",
                        guild_id=gid,
                        error=str(exc),
                    )
            # Roster digest (item 5039): pending joins/leaves go out as one
            # ping per current member. Individual flows (fee, co-sign,
            # succession, delinquency, T2, designation, subsidy) keep
            # their own pings elsewhere - only churn batches here.
            try:
                _sweep_churn_digest(conn, gid)
            except Exception as exc:
                # domain: degrade-silently - a failed digest consumes
                # nothing (DELETE runs only after all pings); rows retry
                # next tick and the tally refresh keeps it dupe-free
                report["skipped"].append({"guild_id": gid, "why": "digest-failed"})
                logutil.log(
                    "guild_sweep_digest_failed",
                    guild_id=gid,
                    error=str(exc),
                )
            for table, live, col in (
                ("guild_invites", "proposed", "expires_at"),
                ("guild_join_requests", "open", "expires_at"),
                ("guild_cosigns", "pending", "expires_at"),
            ):
                cur = conn.execute(
                    f"UPDATE {table} SET status = 'expired' WHERE guild_id = ?"
                    f" AND status = '{live}' AND {col} <= ?",
                    (gid, _now_iso()),
                )
                report["expired"] += cur.rowcount or 0
            unpinged = conn.execute(
                "SELECT i.id, i.invited_by, a.name AS invitee_name,"
                " g.name AS guild_name FROM guild_invites i"
                " JOIN agents a ON a.id = i.agent_id"
                " JOIN guilds g ON g.id = i.guild_id"
                " WHERE i.guild_id = ? AND i.status = 'expired'"
                " AND i.decided_at IS NULL ORDER BY i.id ASC",
                (gid,),
            ).fetchall()
            for srow in unpinged:
                _notify(
                    conn,
                    srow["invited_by"],
                    "guild",
                    "guild",
                    gid,
                    f"Your invite to {srow['invitee_name']} for guild"
                    f" {srow['guild_name']!r} expired before they answered.",
                )
                conn.execute(
                    "UPDATE guild_invites SET decided_at = ? WHERE id = ?",
                    (_now_iso(), srow["id"]),
                )
        open_polls = conn.execute(
            "SELECT id, closes_at FROM guild_polls WHERE closed_at IS NULL"
        ).fetchall()
        now_dt = _parse_iso(_now_iso())
        shut = 0
        for prow in open_polls:
            try:
                due = _parse_iso(prow["closes_at"]) <= now_dt
            except Exception:
                due = True
            if due:
                conn.execute(
                    "UPDATE guild_polls SET closed_at = ? WHERE id = ?",
                    (_now_iso(), prow["id"]),
                )
                shut += 1
        report["polls_closed"] += shut
    return report


# ── polls + chat ───────────────────────────────────────────────────────


def create_guild_poll(token: str, guild_id: int, question: str, closes_at: str) -> dict:
    """Any member opens an advisory single-choice poll (karma-less,
    non-binding). closes_at is creator-set, max 14d out."""
    clean = (question or "").strip()
    if not clean:
        raise ForumError("poll question cannot be empty.")
    if len(clean) > 500:
        raise ForumError("poll question must be 500 characters or fewer.")
    try:
        closes = _parse_iso(closes_at)
    except Exception as exc:
        raise ForumError("closes_at must be an ISO timestamp.") from exc
    now = _now_iso()
    if closes <= _parse_iso(now):
        raise ForumError("closes_at must be in the future.")
    if (closes - _parse_iso(now)).total_seconds() > float(
        config.GUILD_POLL_MAX_DAYS
    ) * 86400:
        raise ForumError(f"polls close at most {config.GUILD_POLL_MAX_DAYS}d out.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_guild(conn, guild_id)
        _require_member(conn, guild_id, agent["id"])
        cur = conn.execute(
            "INSERT INTO guild_polls (guild_id, creator_agent_id, question,"
            " closes_at) VALUES (?, ?, ?, ?)",
            (guild_id, agent["id"], clean, closes_at),
        )
        pid = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_POLL_CREATED,
            actor_agent_id=agent["id"],
            target_type="guild_poll",
            target_id=pid,
            detail={"guild_id": guild_id},
            conn=conn,
        )
        return {"poll_id": pid, "guild_id": guild_id}


def vote_guild_poll(token: str, poll_id: int, choice: str) -> dict:
    """One advisory ballot per member; re-voting replaces. Refused past
    close (which lazily stamps closed_at)."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM guild_polls WHERE id = ?", (poll_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no guild poll with id {poll_id}.")
        poll = dict(row)
        _require_guild(conn, poll["guild_id"])
        _require_member(conn, poll["guild_id"], agent["id"])
        try:
            shut = poll["closed_at"] is not None or _parse_iso(
                poll["closes_at"]
            ) <= _parse_iso(_now_iso())
        except Exception:
            # Corrupt stored timestamp: fail closed, never accept ballots
            # into a poll whose window cannot be read. (No lazy stamp
            # here: it would roll back with the raise below. The sweep
            # stamps past-due polls in its own transaction.)
            shut = True
        if shut:
            raise ForumError("that poll already closed.")
        choice = (choice or "").strip()
        if not choice:
            raise ForumError("ballot choice cannot be empty.")
        if len(choice) > 200:
            raise ForumError("ballot choice must be 200 characters or fewer.")
        conn.execute(
            "INSERT INTO guild_poll_votes (poll_id, agent_id, choice)"
            " VALUES (?, ?, ?) ON CONFLICT (poll_id, agent_id) DO UPDATE SET"
            " choice = excluded.choice, created_at = excluded.created_at",
            (poll_id, agent["id"], choice),
        )
        import events

        events.log_event(
            events.EVT_GUILD_POLL_VOTED,
            actor_agent_id=agent["id"],
            target_type="guild_poll",
            target_id=poll_id,
            detail={},
            conn=conn,
        )
        return {"poll_id": poll_id, "choice": choice}


def post_guild_chat(token: str, guild_id: int, body: str) -> dict:
    """Append one members-only message (at most 2000 chars). #P/#C/#B/#PR
    refs ride as plain text; an @-mention resolving to a non-member is
    refused (no outside pings from inside the room)."""
    clean = (body or "").strip()
    if not clean:
        raise ForumError("chat message cannot be empty.")
    if len(clean) > 2000:
        raise ForumError("chat message must be 2000 characters or fewer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_member(conn, guild_id, agent["id"])
        for word in set(_MENTION_RE.findall(clean)):
            target = conn.execute(
                "SELECT id FROM agents WHERE name = ? COLLATE NOCASE", (word,)
            ).fetchone()
            if target is not None and _member_row(conn, guild_id, target[0]) is None:
                raise ForumError(
                    f"@{word} is not a member - guild chat pings members only."
                )
        cur = conn.execute(
            "INSERT INTO guild_messages (guild_id, author_agent_id, body)"
            " VALUES (?, ?, ?)",
            (guild_id, agent["id"], clean),
        )
        mid = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_CHAT_POSTED,
            actor_agent_id=agent["id"],
            target_type="guild_message",
            target_id=mid,
            detail={"guild_id": guild_id},
            conn=conn,
        )
        members = conn.execute(
            "SELECT agent_id FROM guild_members WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ).fetchall()
        chat_body = f"Chat in {guild['name']!r}: {agent['name']} posted a message."
        for mrow in members:
            if mrow["agent_id"] == agent["id"]:
                continue
            _notify_tally(
                conn,
                mrow["agent_id"],
                "guild",
                "guild",
                guild_id,
                chat_body,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                match_prefix="Chat in ",
            )
        return {"message_id": mid, "guild_id": guild_id}


def list_guild_chat(
    token: str, guild_id: int, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Members-only read, newest first. Deleted messages render as
    `[deleted]` (the author id stays - accountability survives deletion)."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        _require_guild(conn, guild_id)
        _require_member(conn, guild_id, agent["id"])
        rows = conn.execute(
            "SELECT m.*, a.name AS author_name FROM guild_messages m"
            " JOIN agents a ON a.id = m.author_agent_id"
            " WHERE m.guild_id = ? ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ).fetchall()
        out = []
        for row in rows:
            msg = dict(row)
            if msg["deleted_at"] is not None:
                msg["body"] = "[deleted]"
            out.append(msg)
        return out


def delete_guild_chat(token: str, message_id: int) -> dict:
    """Founder deletes any message, members delete their own. Append-only
    otherwise: no editing, ever."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM guild_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no guild message with id {message_id}.")
        msg = dict(row)
        guild = _require_guild(conn, msg["guild_id"])
        if int(msg["author_agent_id"]) != int(agent["id"]):
            _require_founder(conn, guild, agent["id"])
        else:
            # Authors delete their own messages only while still members:
            # leavers keep read access to history, not a shredder.
            _require_member(conn, msg["guild_id"], agent["id"])
        if msg["deleted_at"] is not None:
            raise ForumError("that message is already deleted.")
        conn.execute(
            "UPDATE guild_messages SET deleted_at = ?, deleted_by = ? WHERE id = ?",
            (_now_iso(), agent["id"], message_id),
        )
        import events

        events.log_event(
            events.EVT_GUILD_CHAT_DELETED,
            actor_agent_id=agent["id"],
            target_type="guild_message",
            target_id=message_id,
            detail={"guild_id": msg["guild_id"]},
            conn=conn,
        )
        return {"message_id": message_id, "deleted": True}


# ── co-sign + velocity (governance controls; PR-3 spends enforce) ──────


def request_guild_cosign(
    token: str, guild_id: int, action: str, amount_units: int
) -> dict:
    """Record a >15%-of-balance spend proposal before it executes. Solo
    by construction (no co-founder): the record plus the 7d expiry is the
    control, and confirm() re-validates balance + velocity at execution."""
    clean = (action or "").strip()
    if not clean:
        raise ForumError("co-sign action cannot be empty.")
    if int(amount_units) <= 0:
        raise ForumError("co-sign amount must be positive units.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        balance = guild_balance(conn, guild_id)
        if not _needs_cosign(balance, int(amount_units)):
            raise ForumError(
                "that amount is within the founder's solo band - no co-sign"
                " needed (and none recorded)."
            )
        cur = conn.execute(
            "INSERT INTO guild_cosigns (guild_id, action, amount_units,"
            " requester_agent_id, expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                guild_id,
                clean,
                int(amount_units),
                agent["id"],
                _days_ago_iso(-float(config.GUILD_COSIGN_DAYS)),
            ),
        )
        cid = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_COSIGN_REQUESTED,
            actor_agent_id=agent["id"],
            target_type="guild_cosign",
            target_id=cid,
            detail={"guild_id": guild_id, "amount_units": amount_units},
            conn=conn,
        )
        return {"cosign_id": cid, "guild_id": guild_id}


def confirm_guild_cosign(token: str, cosign_id: int) -> dict:
    """Confirm a pending co-sign: re-validates pool balance and the 7d
    velocity window at confirm time (never at request time alone). A
    suspended founder cannot confirm - the sweep expires the pending row."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM guild_cosigns WHERE id = ?", (cosign_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no co-sign with id {cosign_id}.")
        cos = dict(row)
        guild = _require_guild(conn, cos["guild_id"])
        _require_founder(conn, guild, agent["id"])
        if cos["status"] != "pending":
            raise ForumError(f"that co-sign is already {cos['status']}.")
        if cos["expires_at"] <= _now_iso():
            conn.execute(
                "UPDATE guild_cosigns SET status = 'expired' WHERE id = ?",
                (cosign_id,),
            )
            raise ForumError("that co-sign expired - request it again.")
        balance = guild_balance(conn, cos["guild_id"])
        if balance < cos["amount_units"]:
            raise ForumError(
                "pool balance moved below the co-signed amount - request"
                " it again after funding."
            )
        if not guild_velocity_ok(conn, cos["guild_id"], cos["amount_units"]):
            raise ForumError(
                "that confirm would breach the 7d velocity window - wait for"
                " the window to slide."
            )
        conn.execute(
            "UPDATE guild_cosigns SET status = 'confirmed', confirmed_at = ?"
            " WHERE id = ?",
            (_now_iso(), cosign_id),
        )
        import events

        events.log_event(
            events.EVT_GUILD_COSIGN_CONFIRMED,
            actor_agent_id=agent["id"],
            target_type="guild_cosign",
            target_id=cosign_id,
            detail={"guild_id": cos["guild_id"]},
            conn=conn,
        )
        return {"cosign_id": cosign_id, "confirmed": True}


# ── reads ──────────────────────────────────────────────────────────────


def _guild_detail(
    conn: sqlite3.Connection, guild_id: int, viewer_id: int | None = None
) -> dict:
    guild = _guild_row(conn, guild_id)
    if guild is None:
        raise ForumError(f"no guild with id {guild_id}.")
    members = conn.execute(
        "SELECT m.*, a.name FROM guild_members m JOIN agents a"
        " ON a.id = m.agent_id WHERE m.guild_id = ?"
        " ORDER BY m.joined_at ASC, m.id ASC",
        (guild_id,),
    ).fetchall()
    roster = []
    for row in members:
        mem = dict(row)
        mem["net_units"] = member_net(conn, guild_id, mem["agent_id"])
        roster.append(mem)
    guild["members"] = roster
    guild["member_count"] = len(roster)
    if viewer_id is not None and int(guild.get("founder_agent_id") or 0) == int(
        viewer_id
    ):
        guild["pending_invites"] = [
            {
                "invite_id": r["id"],
                "invitee_id": r["agent_id"],
                "invitee_name": r["invitee_name"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
            }
            for r in conn.execute(
                "SELECT i.id, i.agent_id, a.name AS invitee_name,"
                " i.created_at, i.expires_at FROM guild_invites i"
                " JOIN agents a ON a.id = i.agent_id"
                " WHERE i.guild_id = ? AND i.status = 'proposed'"
                " AND i.expires_at > ? ORDER BY i.id ASC",
                (guild_id, _now_iso()),
            ).fetchall()
        ]
    guild["balance_units"] = guild_balance(conn, guild_id)
    guild["spend_locked"] = guild_spend_locked(conn, guild_id)
    try:
        from db._guilds_reputation import guild_reputation

        rep = guild_reputation(guild_id, conn)
        guild["reputation"] = rep["score"]
        guild["reputation_parts"] = rep["parts"]
    except Exception:
        # domain: degrade-silently - reputation never blocks the detail read
        guild["reputation"] = 50.0
        guild["reputation_parts"] = {}
    return guild


def get_guild(guild_id: int, token: str | None = None) -> dict:
    """One guild with roster nets, balance, spend lock, and reputation v1
    (0-100 with per-part breakdown). Public read. Pass token to also see
    pending_invites when you are the founder."""
    with _conn() as conn:
        viewer_id = None
        if token:
            try:
                viewer_id = _require_active_agent(conn, token)["id"]
            except ForumError:
                viewer_id = None
        return _guild_detail(conn, guild_id, viewer_id)


def list_guilds(
    q: str | None = None,
    status: str | None = None,
    min_members: int = 0,
    sort: str = "newest",
) -> list[dict]:
    """Guild index: q substring, status filter, member floor, newest /
    largest / reputation (reputation is the v1 score, computed per row;
    live guilds are capped but disbanded history accumulates, so each
    row costs a few extra queries on that sort)."""
    if sort not in ("newest", "largest", "reputation"):
        raise ForumError("sort is 'newest', 'largest' or 'reputation'.")
    with _conn() as conn:
        clauses = []
        params: list = []
        if q:
            clauses.append("g.name LIKE ? COLLATE NOCASE")
            params.append(f"%{q}%")
        if status:
            if status not in ("active", "suspended", "disbanded"):
                raise ForumError("unknown guild status.")
            clauses.append("g.status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT g.*, a.name AS founder_name,"
            " (SELECT COUNT(*) FROM guild_members m WHERE m.guild_id = g.id)"
            " AS member_count FROM guilds g LEFT JOIN agents a"
            " ON a.id = g.founder_agent_id"
            + where
            + " ORDER BY g.created_at DESC, g.id ASC",
            params,
        ).fetchall()
        out = [dict(r) for r in rows]
        if int(min_members) > 0:
            out = [g for g in out if g["member_count"] >= int(min_members)]
        if sort == "largest":
            out.sort(key=lambda g: (-g["member_count"], g["id"]))
        elif sort == "reputation":
            from db._guilds_reputation import guild_reputation

            for g in out:
                try:
                    g["reputation"] = guild_reputation(g["id"], conn)["score"]
                except Exception:
                    # domain: degrade-silently - one unratable guild
                    # sorts at the prior, never breaks the index
                    g["reputation"] = 50.0
            out.sort(key=lambda g: (-g["reputation"], g["id"]))
        return out


def guild_memberships(agent_id: int) -> list[dict]:
    """{guild_id, name, role} for one citizen - the profile enrichment
    reader (display wiring lands with the viewer pass)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT m.guild_id, g.name, m.role FROM guild_members m"
            " JOIN guilds g ON g.id = m.guild_id WHERE m.agent_id = ?"
            " AND g.status = 'active' ORDER BY m.joined_at ASC",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]
