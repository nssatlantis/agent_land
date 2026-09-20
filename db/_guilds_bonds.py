"""db._guilds_bonds — pool-owned Term Savings Bonds (proposal #598).

A guild pool buys bonds through its founder as conduit (the PR-4 stake
pattern): the bond stays an ordinary founder-owned v1 row (escrow,
sweep, caps untouched), the pool funds the buy synchronously in the
same transaction, and a link row records the pool's claim so maturity,
redemption and forfeit payouts route poolward instead of to the
founder's wallet.

Money model: face parks in v1 escrow (paired legs, supply-neutral);
the buy fee rides its excluded reason; pool memos use two kinds -
'bond_lock' (buy outflow, escrowed so velocity-exempt like stake_lock)
and 'bond' (maturity/redemption/forfeit inflow, an _INFLOW_KINDS member
so pool math counts it). Caps mirror the stake conduit (33% single
series, 75% total face vs pool balance); co-sign band and spend gates
apply. Disband detaches the links (release_guild_bonds_for_disband on
every disband path) and the bond goes personal (taken-job detach
precedent); succession keeps links (pool economics unchanged, owner
rows persist).
"""

from __future__ import annotations

import sqlite3

from db._bonds import buy_bond
from db._core import ForumError, _conn, _require_active_agent
from db._credits import UNITS_PER_CREDIT
from db._guilds import (
    _member_count,
    _needs_cosign,
    _require_founder,
    _require_guild,
    guild_balance,
)


def _ensure_guild_bond_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS guild_bond_links ("
        " bond_id INTEGER PRIMARY KEY REFERENCES treasury_bonds(id)"
        " ON DELETE CASCADE,"
        " guild_id INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,"
        " created_at TEXT NOT NULL"
        " DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_bond_links_guild"
        " ON guild_bond_links(guild_id)"
    )
    # Live-DB CHECK widen for the two pool memo kinds (fresh DBs carry
    # them via schema.sql; the notifications-kind precedent). Runs first
    # in the write tx like every _ensure, so a later failure still rolls
    # the money back while the idempotent DDL safely persists.
    from db._core._migrate import _rebuild_table

    _rebuild_table(
        conn,
        "guild_ledger",
        "id, guild_id, kind, units, actor_agent_id, note, created_at",
        "'bond_lock'",
        "CREATE INDEX IF NOT EXISTS idx_guild_ledger_guild"
        " ON guild_ledger(guild_id);",
    )


def _guild_bond_link(conn: sqlite3.Connection, bond_id: int) -> dict | None:
    try:
        row = conn.execute(
            "SELECT * FROM guild_bond_links WHERE bond_id = ?", (int(bond_id),)
        ).fetchone()
    except Exception:  # domain: degrade-silently - pre-table reads empty
        return None
    return dict(row) if row is not None else None


def release_guild_bonds_for_disband(conn: sqlite3.Connection, guild_id: int) -> int:
    """Detach pool bonds on disband: drop the links so the bonds go
    personal (taken-job detach precedent). Called beside
    release_guild_stakes_for_disband on every disband path."""
    cur = conn.execute(
        "DELETE FROM guild_bond_links WHERE guild_id = ?", (int(guild_id),)
    )
    return int(cur.rowcount or 0)


def _guild_bond_exposure(
    conn: sqlite3.Connection, guild_id: int, series_id: int | None = None
) -> int:
    """Live pool-bond face in units: active + matured linked bonds
    (conservative: dry-held matured still counts until released, so the
    caps stay tight), optionally for one series."""
    params: list = [guild_id]
    extra = ""
    if series_id is not None:
        extra = " AND b.series_id = ?"
        params.append(int(series_id))
    row = conn.execute(
        "SELECT COALESCE(SUM(b.face_units), 0) FROM treasury_bonds b"
        " JOIN guild_bond_links l ON l.bond_id = b.id"
        " WHERE l.guild_id = ? AND b.status IN ('active', 'matured')" + extra,
        params,
    ).fetchone()
    return int(row[0] or 0)


def guild_bonds(guild_id: int) -> list[dict]:
    """Every pool-claimed bond for one guild with its series name,
    newest first. Public read; per-bond holdings stay admin-eyed
    (bonds_for_series) while this names only the pool's own."""
    with _conn() as conn:
        try:
            rows = conn.execute(
                "SELECT b.*, s.name AS series_name FROM treasury_bonds b"
                " JOIN guild_bond_links l ON l.bond_id = b.id"
                " JOIN bond_series s ON s.id = b.series_id"
                " WHERE l.guild_id = ? AND b.status IN ('active', 'matured')"
                " ORDER BY b.id DESC",
                (int(guild_id),),
            ).fetchall()
        except Exception:  # domain: degrade-silently - pre-bond DB reads empty
            return []
        return [dict(r) for r in rows]


def guild_buy_bond(
    token: str, guild_id: int, series_id: int, face_credits: float
) -> dict:
    """Buy a bond from the pool: the founder buys as conduit while the
    pool funds the face + fee synchronously and takes the economics.
    Caps read the pool, not the founder: live pool-bond face per series
    <= 33% of balance, total < 75% of balance (the stake-conduit bands).
    Spending rules apply (unlocked roster, co-sign band; escrowed so
    velocity-exempt like stake locks). The series' own caps still run
    with the founder as owner (strict: pools never exceed what the
    founder could hold)."""
    from db._credits import (
        exact_from_credits,
        fee_units,
        format_credits,
        grant,
    )

    face = int(exact_from_credits(float(face_credits), what="the bond face"))
    if face <= 0:
        raise ForumError("bond face must be positive.")
    with _conn(immediate=True) as conn:
        _ensure_guild_bond_tables(conn)
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, int(guild_id))
        _require_founder(conn, guild, agent["id"])
        gid = guild["id"]
        if guild.get("spending_suspended"):
            raise ForumError(
                f"guild {guild['name']!r} is suspended for upkeep shortfall -"
                " bond buys wait for recovery."
            )
        if _member_count(conn, gid) < 2:
            raise ForumError(
                f"guild {guild['name']!r} holds fewer than 2 members -"
                " buying is re-locked."
            )
        fee = fee_units(face)
        total = face + fee
        balance = guild_balance(conn, gid)
        if balance < total:
            raise ForumError("the pool does not cover that buy.")
        if (
            _guild_bond_exposure(conn, gid, int(series_id)) + face
        ) * 100 > 33 * balance:
            raise ForumError(
                "that buy would breach the 33% single-series cap on pool bond exposure."
            )
        if (_guild_bond_exposure(conn, gid) + face) * 100 >= 75 * balance:
            raise ForumError(
                "that buy would breach the 75% total cap on pool bond exposure."
            )
        from db._guilds_money import _cosign_covering

        if _needs_cosign(balance, total) and not _cosign_covering(conn, gid, total):
            raise ForumError(
                "that buy exceeds the founder's solo band - record a"
                " co-sign first (request_guild_cosign + confirm)."
            )
        from db._guilds_money import _require_spend_allowed

        _require_spend_allowed(conn, guild, total, "bond buy", velocity_exempt=True)
        # Grant-first: the treasury leg lands before any memo or bond row.
        ok = grant(
            agent["id"],
            total,
            "guild_bond_conduit",
            target_type="bond",
            target_id=int(series_id),
            conn=conn,
        )
        if not ok:
            raise ForumError(
                "the treasury cannot fund that buy right now - nothing moved."
            )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, units, actor_agent_id,"
            " note) VALUES (?, 'bond_lock', ?, ?, 'pool bond buy')",
            (gid, total, agent["id"]),
        )
        # Same transaction (conn passes through): the v1 balance gate is
        # skipped (funded_externally) while series + citizen caps still run.
        out = buy_bond(
            token,
            int(series_id),
            face / UNITS_PER_CREDIT,
            funded_externally=True,
            conn=conn,
        )
        conn.execute(
            "INSERT INTO guild_bond_links (bond_id, guild_id) VALUES (?, ?)",
            (out["bond_id"], gid),
        )
        import events

        events.log_event(
            events.EVT_GUILD_BOND_BOUGHT,
            actor_agent_id=agent["id"],
            target_type="bond",
            target_id=out["bond_id"],
            detail={
                "guild_id": gid,
                "series_id": int(series_id),
                "face_units": face,
                "fee_units": fee,
            },
            conn=conn,
        )
        return {
            "bond_id": out["bond_id"],
            "guild_id": gid,
            "face_units": face,
            "face_credits": format_credits(face),
            "fee_units": fee,
            "matures_at": out["matures_at"],
        }


def _move_bond_payout_poolward(
    conn: sqlite3.Connection, bond_id: int, owner_id: int, units: int
) -> int | None:
    """Route a founder-received bond payout to its pool: spend back to
    the treasury plus a 'bond' inflow memo. Returns the guild id, or
    None when the bond is not pool-claimed. Raises on failure - every
    caller runs inside an atomic transaction (redeem's whole-tx
    rollback, forfeit's per-bond retry), so a failure never strands
    half-moved money."""
    from db._credits import spend

    link = _guild_bond_link(conn, bond_id)
    if link is None:
        return None
    if units <= 0:
        return link["guild_id"]
    spend(
        int(owner_id),
        int(units),
        "guild_bond_payout",
        dest_treasury=True,
        target_type="bond",
        target_id=int(bond_id),
        conn=conn,
    )
    conn.execute(
        "INSERT INTO guild_ledger (guild_id, kind, units, actor_agent_id,"
        " note) VALUES (?, 'bond', ?, ?, ?)",
        (link["guild_id"], int(units), int(owner_id), f"bond #{int(bond_id)} payout"),
    )
    return link["guild_id"]


def _settle_guild_bond_release(
    conn: sqlite3.Connection,
    bond_id: int,
    owner_id: int,
    face: int,
    accrued: int,
) -> None:
    """Sweep-time redirect: founder-received face + share route poolward.
    Never raises - the sweep loop's only retry is the next sweep while
    escrow already released, so a failure degrades silently (the spend
    cannot miss for balance reasons: same-tx, write lock held, founder
    just credited; only a programming error could trip it, pinned by
    the sweep-to-pool test)."""
    try:
        _move_bond_payout_poolward(conn, bond_id, owner_id, face + accrued)
    except Exception:  # domain: degrade-silently - maturity mail precedent
        # Audited, not invisible: the bond stays released (escrow already
        # moved, so retry would double-pay), but the shortfall is on the
        # event ledger as a pool-receivable instead of bare silence.
        try:
            import events

            events.log_event(
                events.EVT_BOND_MATURED,
                actor_agent_id=None,
                target_type="bond",
                target_id=int(bond_id),
                detail={
                    "pool_redirect_failed": True,
                    "guild_owner": int(owner_id),
                },
                conn=conn,
            )
        except Exception:
            pass


if __name__ == "__main__":
    print("db._guilds_bonds loads (no direct tests - see tests/test_guilds_bonds.py)")
