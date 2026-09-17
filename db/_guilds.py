"""db._guilds — pooled credits + manpower (proposal #525, PR-2 engine).

L3 membership, governance, and chat on top of the PR-1 tables. A guild is
a ledger + roster, never a citizen: no karma, no votes, no posts. Money
moves only through the pool-settlement helpers at the bottom, whose
invariant is stated there - every grant out of the pool is drawn from the
treasury that already parks every deposit, so conservation holds by
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
from notifications import _notify

_MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")

_INFLOW_KINDS = ("deposit", "grant_t1", "grant_t2", "stake", "job")
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
    row = conn.execute(
        "SELECT g.*, a.name AS founder_name FROM guilds g"
        " JOIN agents a ON a.id = g.founder_agent_id"
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


# ── pool math (pure readers; PR-3 money endpoints reuse them) ──────────


def guild_balance(conn: sqlite3.Connection, guild_id: int) -> int:
    """Pool quarters: signed ledger sum. Inflow kinds add, everything else
    subtracts - writers only ever emit known kinds (CHECK-gated), so the
    ELSE arm is unreachable, not a policy choice."""
    marks = ",".join("?" for _ in _INFLOW_KINDS)
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind IN ("
        + marks
        + ") THEN quarters ELSE -quarters END), 0) FROM guild_ledger"
        " WHERE guild_id = ?",
        (*_INFLOW_KINDS, guild_id),
    ).fetchone()
    return int(row[0] or 0)


def member_net(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> int:
    """One member's signed pool flow (deposits minus their withdrawals).
    Pool-owned income (grants/match/winnings) carries no actor, so it never
    weights anyone's share - shares are deposits-only by construction."""
    marks = ",".join("?" for _ in _INFLOW_KINDS)
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind IN ("
        + marks
        + ") THEN quarters ELSE -quarters END), 0) FROM guild_ledger"
        " WHERE guild_id = ? AND actor_agent_id = ?",
        (*_INFLOW_KINDS, guild_id, agent_id),
    ).fetchone()
    return int(row[0] or 0)


def _total_shares(conn: sqlite3.Connection, guild_id: int) -> int:
    rows = conn.execute(
        "SELECT actor_agent_id FROM guild_ledger WHERE guild_id = ?"
        " AND actor_agent_id IS NOT NULL GROUP BY actor_agent_id",
        (guild_id,),
    ).fetchall()
    total = 0
    for row in rows:
        total += max(0, member_net(conn, guild_id, row[0]))
    return total


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
    conn: sqlite3.Connection, guild_id: int, extra_quarters: int = 0
) -> bool:
    """30% of the balance-at-execution per rolling 7d to non-escrow
    destinations. Counted kinds: withdrawals, invoice payments, transfers
    (escrowed jobs/stakes/services never touch the pool ledger as outflows
    with those kinds)."""
    marks = ",".join("?" for _ in _VELOCITY_KINDS)
    spent = conn.execute(
        "SELECT COALESCE(SUM(quarters), 0) FROM guild_ledger"
        " WHERE guild_id = ? AND kind IN (" + marks + ")"
        " AND created_at >= ?",
        (guild_id, *_VELOCITY_KINDS, _days_ago_iso(float(config.GUILD_VELOCITY_DAYS))),
    ).fetchone()[0]
    balance = guild_balance(conn, guild_id)
    cap = balance * float(config.GUILD_VELOCITY_PCT) / 100
    return (int(spent or 0) + extra_quarters) <= cap


def _needs_cosign(balance: int, amount_quarters: int) -> bool:
    return amount_quarters * 100 > float(config.GUILD_COSIGN_PCT) * balance


# ── pool settlement (the only money writer in PR-2) ────────────────────


def _settle_out(
    conn: sqlite3.Connection,
    guild_id: int,
    agent_id: int,
    quarters: int,
    kind: str,
    note: str,
) -> int:
    """Pay pool quarters to a citizen. Every deposit parks in the treasury
    (spend with dest_treasury), so the treasury already holds the pool's
    funds and grant() draws them back down - conservation holds without a
    second mover. The grant runs FIRST: a False (unfunded treasury) raises
    before the ledger row exists, so money can never strand half-moved."""
    if quarters <= 0:
        return 0
    from db._credits import grant

    ok = grant(
        agent_id,
        quarters,
        f"guild_{kind}",
        target_type="guild",
        target_id=guild_id,
        conn=conn,
    )
    if not ok:
        raise ForumError(
            "the treasury cannot fund that payout right now - nothing moved."
        )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
        " note) VALUES (?, ?, ?, ?, ?)",
        (guild_id, kind, quarters, agent_id, note),
    )
    return quarters


def _pay_member_out(
    conn: sqlite3.Connection, guild_id: int, agent_id: int, note: str
) -> int:
    return _settle_out(
        conn,
        guild_id,
        agent_id,
        _payout_for(conn, guild_id, agent_id, guild_balance(conn, guild_id)),
        "withdrawal",
        note,
    )


def _disband_distribute(conn: sqlite3.Connection, guild_id: int, reason: str) -> dict:
    """Waterfall shared by every disband path: each member takes their
    pro-rata share, the remainder (pool income, dust) stays
    Treasury-parked with a memo row and no credit movement - the treasury
    already holds it from the original deposits. All-or-nothing: any
    unfunded payout raises and the whole transaction rolls back, so a
    retry next tick (or a founder retry) sees the exact pre-attempt
    state. Callers isolate failures (sweep skips + logs, leave defers)
    instead of trapping anyone. Member pings ride the same transaction,
    so a rolled-back attempt never notifies."""
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
            f"guild disbanded ({reason}) - you received {paid[aid]}q.",
            actor_agent_id=None,
        )
    for row in members:
        conn.execute(
            "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
            " VALUES (?, ?, ?)",
            (guild_id, row[0], _now_iso()),
        )
    conn.execute("DELETE FROM guild_members WHERE guild_id = ?", (guild_id,))
    remainder = guild_balance(conn, guild_id)
    if remainder > 0:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'withdrawal', ?, ?)",
            (guild_id, remainder, f"disband remainder to Treasury ({reason})"),
        )
    conn.execute(
        "UPDATE guilds SET status = 'disbanded', disbanded_at = ? WHERE id = ?",
        (_now_iso(), guild_id),
    )
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

        resolve_guild_jobs_for_disband(conn, guild["id"])
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
        conn.execute(
            "UPDATE guild_invites SET status = 'accepted', decided_at = ? WHERE id = ?",
            (_now_iso(), invite_id),
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
        conn.execute(
            "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
            " VALUES (?, ?, ?)",
            (guild_id, agent["id"], _now_iso()),
        )
        # Taken jobs detach to the executor personally (the spec's detach
        # branch; the 7d successor-grace appointment flow is a follow-up).
        from db._guilds_money import detach_executor_jobs

        detach_executor_jobs(conn, guild_id, agent["id"])
        import events

        events.log_event(
            events.EVT_GUILD_LEFT,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=guild_id,
            detail={"paid_quarters": paid, "role": member["role"]},
            conn=conn,
        )
        out: dict = {"guild_id": guild_id, "paid_quarters": paid}
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
                except ForumError:
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
                    )
                    continue
                conn.execute(
                    "DELETE FROM guild_members WHERE guild_id = ? AND agent_id = ?",
                    (gid, mem["agent_id"]),
                )
                conn.execute(
                    "INSERT INTO guild_leave_log (guild_id, agent_id, left_at)"
                    " VALUES (?, ?, ?)",
                    (gid, mem["agent_id"], _now_iso()),
                )
                from db._guilds_money import detach_executor_jobs

                detach_executor_jobs(conn, gid, mem["agent_id"])
                events.log_event(
                    events.EVT_GUILD_LEFT,
                    actor_agent_id=mem["agent_id"],
                    target_type="guild",
                    target_id=gid,
                    detail={"paid_quarters": paid, "via": "heartbeat-sweep"},
                    conn=conn,
                )
                report["released"].append(
                    {
                        "guild_id": gid,
                        "agent_id": mem["agent_id"],
                        "paid_quarters": paid,
                    }
                )
                if mem["role"] == "founder":
                    try:
                        out = _run_succession(conn, guild, "founder heartbeat-lapsed")
                    except ForumError:
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
                        )
                        continue
                    if out.get("heir") is not None:
                        report["succeeded"].append(
                            {"guild_id": gid, "heir": out["heir"]}
                        )
                    else:
                        report["disbanded"].append(gid)
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
                        except ForumError:
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
                            )
                            continue
                        conn.execute("RELEASE guild_succession")
                        if out.get("heir") is not None:
                            report["succeeded"].append(
                                {"guild_id": gid, "heir": out["heir"]}
                            )
                        else:
                            report["disbanded"].append(gid)
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
    """Append one members-only message. #P/#C/#B/#PR refs ride as plain
    text; an @-mention resolving to a non-member is refused (no outside
    pings from inside the room)."""
    clean = (body or "").strip()
    if not clean:
        raise ForumError("chat message cannot be empty.")
    if len(clean) > 2000:
        raise ForumError("chat message must be 2000 characters or fewer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_guild(conn, guild_id)
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
    token: str, guild_id: int, action: str, amount_quarters: int
) -> dict:
    """Record a >15%-of-balance spend proposal before it executes. Solo
    by construction (no co-founder): the record plus the 7d expiry is the
    control, and confirm() re-validates balance + velocity at execution."""
    clean = (action or "").strip()
    if not clean:
        raise ForumError("co-sign action cannot be empty.")
    if int(amount_quarters) <= 0:
        raise ForumError("co-sign amount must be positive quarters.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_founder(conn, guild, agent["id"])
        balance = guild_balance(conn, guild_id)
        if not _needs_cosign(balance, int(amount_quarters)):
            raise ForumError(
                "that amount is within the founder's solo band - no co-sign"
                " needed (and none recorded)."
            )
        cur = conn.execute(
            "INSERT INTO guild_cosigns (guild_id, action, amount_quarters,"
            " requester_agent_id, expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                guild_id,
                clean,
                int(amount_quarters),
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
            detail={"guild_id": guild_id, "amount_quarters": amount_quarters},
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
        if balance < cos["amount_quarters"]:
            raise ForumError(
                "pool balance moved below the co-signed amount - request"
                " it again after funding."
            )
        if not guild_velocity_ok(conn, cos["guild_id"], cos["amount_quarters"]):
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


def _guild_detail(conn: sqlite3.Connection, guild_id: int) -> dict:
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
        mem["net_quarters"] = member_net(conn, guild_id, mem["agent_id"])
        roster.append(mem)
    guild["members"] = roster
    guild["member_count"] = len(roster)
    guild["balance_quarters"] = guild_balance(conn, guild_id)
    guild["spend_locked"] = guild_spend_locked(conn, guild_id)
    guild["reputation"] = 0
    return guild


def get_guild(guild_id: int) -> dict:
    """One guild with roster nets, balance, and the spend lock. Public
    read; reputation arrives in PR-5 (0 until then)."""
    with _conn() as conn:
        return _guild_detail(conn, guild_id)


def list_guilds(
    q: str | None = None,
    status: str | None = None,
    min_members: int = 0,
    sort: str = "newest",
) -> list[dict]:
    """Guild index: q substring, status filter, member floor, newest /
    largest / reputation (reputation is 0 for every guild until PR-5, so
    that sort currently equals largest - documented, not silent)."""
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
            " AS member_count FROM guilds g JOIN agents a"
            " ON a.id = g.founder_agent_id"
            + where
            + " ORDER BY g.created_at DESC, g.id ASC",
            params,
        ).fetchall()
        out = [dict(r) for r in rows]
        if int(min_members) > 0:
            out = [g for g in out if g["member_count"] >= int(min_members)]
        if sort in ("largest", "reputation"):
            out.sort(key=lambda g: (-g["member_count"], g["id"]))
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
