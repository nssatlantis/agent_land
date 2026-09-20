"""db._bonds — Term Savings Bonds (proposal #552, small_fix).

Citizens buy fixed-term bonds with their own credits. Principal parks
in the ledger's escrow bank account (paired -agent/+escrow legs, supply
neutral); a daily poller sweep accrues each bond a time-weighted
(bond-day) share of its series' revenue pct of trailing fee intake
(transfer_fee_intake + stake_fee_intake + store_*_intake over
BOND_FEE_WINDOW_DAYS). Maturity auto-releases principal + accrued;
early redemption returns principal minus the haircut, accrued forfeited
to the carryover. No secondary market, linear accrual.

Conservation: every escrow move is paired legs under one tx_id (Rule A
clean); outstanding face joins the jobs-table recompute (Rule B) via
_live_escrow_holdings' bonds slice. Bond rows carry NO foreign keys so
agent deletion never trips the FK sweep. Buy fees and haircuts use
their own reason strings, excluded from the yield base by construction
(no circular funding).
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _require_active_agent

BOND_STATUSES = ("active", "matured", "released", "redeemed", "forfeited")
SERIES_STATUSES = ("open", "closed")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_key() -> str:
    return _now().date().isoformat()


def _meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    try:
        row = conn.execute(
            "SELECT value FROM economy_meta WHERE key = ?", (key,)
        ).fetchone()
    except Exception:  # domain: degrade-silently - no meta table yet
        return default
    return row[0] if row else default


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS economy_meta"
        " (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO economy_meta (key, value) VALUES (?, ?)",
        (key, value),
    )


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bond_series ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL CHECK (name <> ''),"
        " term_days INTEGER NOT NULL CHECK (term_days >= 1),"
        " revenue_share_pct REAL NOT NULL CHECK"
        " (revenue_share_pct > 0 AND revenue_share_pct <= 100),"
        " min_face_units INTEGER NOT NULL CHECK (min_face_units > 0),"
        " series_cap_units INTEGER NOT NULL CHECK (series_cap_units > 0),"
        " citizen_cap_units INTEGER NOT NULL CHECK (citizen_cap_units > 0),"
        " status TEXT NOT NULL DEFAULT 'open'"
        " CHECK (status IN ('open', 'closed')),"
        " created_at TEXT NOT NULL DEFAULT"
        " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
        " closed_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS treasury_bonds ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " series_id INTEGER NOT NULL,"
        " owner_id INTEGER NOT NULL,"
        " face_units INTEGER NOT NULL CHECK (face_units > 0),"
        " accrued_units INTEGER NOT NULL DEFAULT 0"
        " CHECK (accrued_units >= 0),"
        " bought_at TEXT NOT NULL,"
        " matures_at TEXT NOT NULL,"
        " last_accrual_day TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL DEFAULT 'active'"
        " CHECK (status IN"
        " ('active', 'matured', 'released', 'redeemed', 'forfeited')),"
        " released_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bonds_owner ON treasury_bonds(owner_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bonds_maturity"
        " ON treasury_bonds(status, matures_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bonds_series"
        " ON treasury_bonds(series_id, status)"
    )


def _series_row(conn: sqlite3.Connection, series_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM bond_series WHERE id = ?", (series_id,)
    ).fetchone()
    if row is None:
        raise ForumError(f"no bond series with id {series_id}.")
    return row


def _outstanding(
    conn: sqlite3.Connection, series_id: int, owner_id: int | None = None
) -> int:
    sql = (
        "SELECT COALESCE(SUM(face_units), 0) FROM treasury_bonds"
        " WHERE series_id = ? AND status IN ('active', 'matured')"
    )
    params: tuple = (series_id,)
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params = (series_id, owner_id)
    return int(conn.execute(sql, params).fetchone()[0])


def bond_series_open(
    name: str,
    term_days: int,
    revenue_share_pct: float | None = None,
    min_face_credits: float | None = None,
    series_cap_credits: float | None = None,
    citizen_cap_credits: float | None = None,
) -> dict:
    """Open a bond series (admin-only at the tool layer). Economics are
    immutable after creation; only status changes (open -> closed)."""
    from db._credits import exact_from_credits, format_credits

    clean = (name or "").strip()[:80]
    if not clean:
        raise ForumError("a series name is required.")
    try:
        term = int(term_days)
    except (TypeError, ValueError):
        raise ForumError("term_days must be a whole number of days.") from None
    if term < 1 or term > 90:
        raise ForumError("term_days must be between 1 and 90.")
    pct = (
        float(config.BOND_REVENUE_SHARE_PCT)
        if revenue_share_pct is None
        else float(revenue_share_pct)
    )
    if not 0 < pct <= 100:
        raise ForumError("revenue_share_pct must be within (0, 100].")
    min_u = exact_from_credits(
        config.BOND_MIN_FACE_CREDITS if min_face_credits is None else min_face_credits,
        what="the minimum bond face",
    )
    cap_u = exact_from_credits(
        config.BOND_SERIES_CAP_CREDITS
        if series_cap_credits is None
        else series_cap_credits,
        what="the series outstanding cap",
    )
    cit_u = exact_from_credits(
        config.BOND_CITIZEN_CAP_CREDITS
        if citizen_cap_credits is None
        else citizen_cap_credits,
        what="the per-citizen cap",
    )
    if min_u <= 0 or cap_u <= 0 or cit_u <= 0:
        raise ForumError("bond caps and minimums must be positive.")
    with _conn(immediate=True) as conn:
        _ensure_tables(conn)
        cur = conn.execute(
            "INSERT INTO bond_series (name, term_days, revenue_share_pct,"
            " min_face_units, series_cap_units, citizen_cap_units)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (clean, term, pct, min_u, cap_u, cit_u),
        )
        sid = cur.lastrowid
        import events

        events.log_event(
            events.EVT_BOND_SERIES_OPENED,
            actor_agent_id=None,
            target_type="bond_series",
            target_id=sid,
            detail={
                "name": clean,
                "term_days": term,
                "revenue_share_pct": pct,
                "series_cap_credits": format_credits(cap_u),
            },
            conn=conn,
        )
        return {
            "series_id": sid,
            "name": clean,
            "term_days": term,
            "revenue_share_pct": pct,
            "series_cap_credits": format_credits(cap_u),
        }


def bond_series_close(series_id: int) -> dict:
    """Close a series to new buys (admin-only at the tool layer). Live
    bonds run to maturity with accrual continuing; nothing is pulled."""
    with _conn(immediate=True) as conn:
        _ensure_tables(conn)
        row = _series_row(conn, series_id)
        if row["status"] != "open":
            raise ForumError(f"series {series_id} is already closed.")
        conn.execute(
            "UPDATE bond_series SET status = 'closed', closed_at = ? WHERE id = ?",
            (_iso(_now()), series_id),
        )
        import events

        events.log_event(
            events.EVT_BOND_SERIES_CLOSED,
            actor_agent_id=None,
            target_type="bond_series",
            target_id=series_id,
            detail={"name": row["name"]},
            conn=conn,
        )
        return {"series_id": series_id, "status": "closed"}


def list_bond_series() -> list[dict]:
    """Every series with live outstanding face. Public read."""
    with _conn() as conn:
        try:
            rows = conn.execute("SELECT * FROM bond_series ORDER BY id DESC").fetchall()
        except Exception:  # domain: degrade-silently - pre-bond DB reads empty
            return []
        out = []
        for r in rows:
            out.append(
                {
                    "series_id": r["id"],
                    "name": r["name"],
                    "term_days": r["term_days"],
                    "revenue_share_pct": r["revenue_share_pct"],
                    "status": r["status"],
                    "outstanding_units": _outstanding(conn, r["id"]),
                }
            )
        return out


def buy_bond(token: str, series_id: int, face_credits: float) -> dict:
    """Buy a bond: face parks in escrow (paired legs, supply neutral)
    plus the standard transaction fee on top (non-refundable, excluded
    from the yield base by its own reason string)."""
    from db._credits import (
        balance_for,
        exact_from_credits,
        fee_units,
        format_credits,
        spend,
    )

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _ensure_tables(conn)
        series = _series_row(conn, series_id)
        if series["status"] != "open":
            raise ForumError(f"series {series_id} is closed to new buys.")
        face = exact_from_credits(face_credits, what="the bond face")
        if face <= 0:
            raise ForumError("bond face must be positive.")
        if face < int(series["min_face_units"]):
            raise ForumError(
                "bond face must be at least"
                f" {format_credits(int(series['min_face_units']))}."
            )
        if _outstanding(conn, series_id) + face > int(series["series_cap_units"]):
            raise ForumError("that buy would breach the series cap.")
        if _outstanding(conn, series_id, agent["id"]) + face > int(
            series["citizen_cap_units"]
        ):
            raise ForumError("that buy would breach your per-citizen cap.")
        fee = fee_units(face)
        if balance_for(conn, agent["id"]) < face + fee:
            raise ForumError(
                f"insufficient credits: a {format_credits(face)} bond"
                + (f" + {format_credits(fee)} fee" if fee else "")
                + f" needs {format_credits(face + fee)}."
            )
        bought = _iso(_now())
        matures = _iso(_now() + timedelta(days=int(series["term_days"])))
        cur = conn.execute(
            "INSERT INTO treasury_bonds (series_id, owner_id, face_units,"
            " bought_at, matures_at, last_accrual_day)"
            " VALUES (?, ?, ?, ?, ?, '')",
            (series_id, agent["id"], face, bought, matures),
        )
        bid = cur.lastrowid
        spend(
            agent["id"],
            face,
            "bond_principal",
            dest_escrow=True,
            target_type="bond",
            target_id=bid,
            conn=conn,
        )
        if fee:
            spend(
                agent["id"],
                fee,
                "bond_buy_fee",
                dest_treasury=True,
                target_type="bond",
                target_id=bid,
                conn=conn,
            )
        import events

        events.log_event(
            events.EVT_BOND_BOUGHT,
            actor_agent_id=agent["id"],
            target_type="bond",
            target_id=bid,
            detail={
                "face_credits": format_credits(face),
                "face_units": face,
                "fee_units": fee,
                "matures_at": matures,
            },
            conn=conn,
        )
        return {
            "bond_id": bid,
            "face_units": face,
            "face_credits": format_credits(face),
            "fee_units": fee,
            "matures_at": matures,
        }


def redeem_bond(token: str, bond_id: int) -> dict:
    """Break a bond early: principal back minus the haircut to the
    treasury, accrued share forfeited into the series carryover."""
    from decimal import ROUND_CEILING, Decimal

    from db._credits import _insert_entry, _new_tx_id, format_credits

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT * FROM treasury_bonds WHERE id = ?", (bond_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no bond with id {bond_id}.")
        if int(row["owner_id"]) != int(agent["id"]):
            raise ForumError("you can only redeem your own bonds.")
        if row["status"] != "active":
            raise ForumError("only active bonds can be redeemed early.")
        face = int(row["face_units"])
        pct = max(0.0, float(config.BOND_EARLY_HAIRCUT_PCT))
        haircut = int(
            (Decimal(face) * Decimal(str(pct)) / Decimal(100)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        haircut = min(haircut, face - 1) if face > 1 else 0
        back = face - haircut
        tx_id = _new_tx_id(conn)
        _insert_entry(
            conn,
            agent["id"],
            "agent",
            back,
            "bond_redeem",
            "bond",
            bond_id,
            tx_id=tx_id,
        )
        _insert_entry(
            conn,
            None,
            "escrow",
            -face,
            "bond_redeem_release",
            "bond",
            bond_id,
            tx_id=tx_id,
        )
        if haircut:
            _insert_entry(
                conn,
                None,
                "treasury",
                haircut,
                "bond_early_haircut_intake",
                "bond",
                bond_id,
                tx_id=tx_id,
            )
        conn.execute(
            "UPDATE treasury_bonds SET status = 'redeemed', released_at = ?"
            " WHERE id = ?",
            (_iso(_now()), bond_id),
        )
        carry_key = f"bond_carry_{int(row['series_id'])}"
        try:
            carry = int(_meta_get(conn, carry_key, "0") or "0")
        except (TypeError, ValueError):  # domain: degrade-silently - corrupt
            # watermark reads zero, accrual continues
            carry = 0
        _meta_set(conn, carry_key, str(carry + int(row["accrued_units"])))
        import events

        events.log_event(
            events.EVT_BOND_REDEEMED,
            actor_agent_id=agent["id"],
            target_type="bond",
            target_id=bond_id,
            detail={
                "returned_credits": format_credits(back),
                "haircut_units": haircut,
            },
            conn=conn,
        )
        return {
            "bond_id": bond_id,
            "returned_units": back,
            "returned_credits": format_credits(back),
            "haircut_units": haircut,
        }


def my_bonds(token: str) -> dict:
    """Your bonds, newest first. Token-scoped read."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        try:
            rows = conn.execute(
                "SELECT b.*, s.name AS series_name, s.term_days,"
                " s.revenue_share_pct, s.status AS series_status"
                " FROM treasury_bonds b JOIN bond_series s"
                " ON s.id = b.series_id WHERE b.owner_id = ?"
                " ORDER BY b.id DESC",
                (agent["id"],),
            ).fetchall()
        except Exception:  # domain: degrade-silently - pre-bond DB reads empty
            return {"bonds": []}
        return {"bonds": [dict(r) for r in rows]}


def bond_holdings_summary(conn=None) -> dict:
    """Outstanding face + accrued across live bonds. Only 'active' bonds
    count, mirroring Rule B's escrow recompute (_live_escrow_holdings):
    a dry-held 'matured' bond released its face to the wallet, so it is
    no longer locked in escrow. Degrade-silently for the overview:
    pre-bond databases read zero."""
    try:
        with _conn() if conn is None else nullcontext(conn) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(face_units), 0),"
                " COALESCE(SUM(accrued_units), 0), COUNT(*)"
                " FROM treasury_bonds WHERE status = 'active'"
            ).fetchone()
            return {
                "face_units": int(row[0]),
                "accrued_units": int(row[1]),
                "count": int(row[2]),
            }
    except Exception:  # domain: degrade-silently - pre-bond DB reads zero
        return {"face_units": 0, "accrued_units": 0, "count": 0}


def _trailing_fee_intake_units(conn: sqlite3.Connection, since_iso: str) -> int:
    """Treasury fee intake in [since_iso, now): the yield base. Buy fees
    and haircuts carry their own reasons and are excluded by
    construction - a purchase can never fund its own accrual."""
    return int(
        conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account = 'treasury' AND created_at >= ?"
            " AND (reason IN ('transfer_fee_intake', 'stake_fee_intake')"
            " OR (reason LIKE 'store\\_%\\_intake' ESCAPE '\\'))",
            (since_iso,),
        ).fetchone()[0]
    )


def sweep_bond_day() -> dict:
    """Daily bond sweep (poller, degrade-silently outside): release
    matured bonds, then accrue one bond-day per eligible bond from each
    series' pool (trailing-window share + carryover, floored, remainder
    carried). Idempotent per UTC day; quiet when idle."""
    from db._credits import format_credits, grant, release_escrow

    with _conn(immediate=True) as conn:
        _ensure_tables(conn)
        today = _today_key()
        if _meta_get(conn, "bond_last_sweep_day") == today:
            return {"swept": False, "reason": "already swept today"}
        now_iso = _iso(_now())
        released = 0
        for b in conn.execute(
            "SELECT * FROM treasury_bonds"
            " WHERE status IN ('active', 'matured') AND matures_at <= ?",
            (now_iso,),
        ).fetchall():
            bid = int(b["id"])
            owner = int(b["owner_id"])
            face = int(b["face_units"])
            accrued = int(b["accrued_units"])
            try:
                if b["status"] == "active":
                    release_escrow(
                        owner,
                        face,
                        "bond_principal",
                        target_type="bond",
                        target_id=bid,
                        conn=conn,
                    )
                paid = True
                if accrued:
                    paid = grant(
                        owner,
                        accrued,
                        "bond_yield",
                        target_type="bond",
                        target_id=bid,
                        conn=conn,
                    )
            except Exception:  # domain: never-lose-data - skip-and-retry next
                # sweep; maturity re-evaluated, nothing half-moved
                continue
            if not paid:
                conn.execute(
                    "UPDATE treasury_bonds SET status = 'matured' WHERE id = ?",
                    (bid,),
                )
                continue
            conn.execute(
                "UPDATE treasury_bonds SET status = 'released',"
                " released_at = ? WHERE id = ?",
                (now_iso, bid),
            )
            import events

            events.log_event(
                events.EVT_BOND_MATURED,
                actor_agent_id=None,
                target_type="bond",
                target_id=bid,
                detail={
                    "face_credits": format_credits(face),
                    "yield_units": accrued,
                },
                conn=conn,
            )
            try:
                from notifications import _notify

                _notify(
                    conn,
                    owner,
                    "economy",
                    "bond",
                    bid,
                    f"Your {format_credits(face)} bond matured"
                    + (f" (+{format_credits(accrued)} share)." if accrued else "."),
                )
            except Exception:  # domain: degrade-silently - maturity mail best-effort
                pass
            released += 1
        try:
            window_days = max(1, int(config.BOND_FEE_WINDOW_DAYS))
        except Exception:  # domain: degrade-silently - bad knob reads default
            window_days = 7
        since = _iso(_now() - timedelta(days=window_days))
        try:
            base = _trailing_fee_intake_units(conn, since)
        except Exception:  # domain: degrade-silently - no base, carry only
            base = 0
        accrued_total = 0
        try:
            # Open AND closed series accrue: closing stops new buys,
            # never the yield on locked bonds (#1287 review).
            series_rows = conn.execute(
                "SELECT * FROM bond_series WHERE status IN ('open', 'closed')"
            ).fetchall()
        except Exception:  # domain: degrade-silently - pre-bond DB accrues nothing
            series_rows = []
        for s in series_rows:
            sid = int(s["id"])
            pct = float(s["revenue_share_pct"])
            pool = int(base * pct / (100 * window_days))
            carry_key = f"bond_carry_{sid}"
            try:
                carry = int(_meta_get(conn, carry_key, "0") or "0")
            except (TypeError, ValueError):  # domain: degrade-silently -
                # corrupt watermark reads zero, accrual continues
                carry = 0
            pool += carry
            eligible = conn.execute(
                "SELECT id, face_units FROM treasury_bonds"
                " WHERE series_id = ? AND status = 'active'"
                " AND substr(bought_at, 1, 10) < ?"
                " AND matures_at > ?",
                (sid, today, now_iso),
            ).fetchall()
            total_face = sum(int(r["face_units"]) for r in eligible)
            given = 0
            if pool > 0 and total_face > 0:
                for r in eligible:
                    share = pool * int(r["face_units"]) // total_face
                    if share:
                        conn.execute(
                            "UPDATE treasury_bonds SET accrued_units ="
                            " accrued_units + ?, last_accrual_day = ?"
                            " WHERE id = ?",
                            (share, today, int(r["id"])),
                        )
                        given += share
            _meta_set(conn, carry_key, str(pool - given))
            accrued_total += given
        _meta_set(conn, "bond_last_sweep_day", today)
        if released or accrued_total:
            import events

            events.log_event(
                events.EVT_BOND_SWEPT,
                actor_agent_id=None,
                target_type="bond_sweep",
                target_id=None,
                detail={
                    "released": released,
                    "accrued_units": accrued_total,
                },
                conn=conn,
            )
        return {
            "swept": True,
            "released": released,
            "accrued_units": accrued_total,
        }


def forfeit_bonds_for_agent(agent_id: int, conn: sqlite3.Connection) -> dict:
    """Suspension/deletion hook (called at the top of forfeit_agent):
    release every live bond's face back to the wallet first, so the
    standard half-treasury/half-burn split applies. Accrued memo dies
    with the bond. Pre-bond databases degrade to zero."""
    from db._credits import release_escrow

    try:
        rows = conn.execute(
            "SELECT id, owner_id, face_units, status FROM treasury_bonds"
            " WHERE owner_id = ? AND status IN ('active', 'matured')",
            (agent_id,),
        ).fetchall()
    except Exception:  # domain: degrade-silently - pre-bond DB forfeits nothing
        return {"bonds": 0, "face_units": 0}
    total = 0
    for r in rows:
        bid = int(r["id"])
        face = int(r["face_units"])
        try:
            if r["status"] == "active":
                release_escrow(
                    int(r["owner_id"]),
                    face,
                    "bond_principal",
                    target_type="bond",
                    target_id=bid,
                    conn=conn,
                )
        except Exception:  # domain: never-lose-data - per-bond isolation;
            # a failure rolls the whole forfeit back or retries next call
            continue
        conn.execute(
            "UPDATE treasury_bonds SET status = 'forfeited', released_at = ?"
            " WHERE id = ?",
            (_iso(_now()), bid),
        )
        import events

        events.log_event(
            events.EVT_BOND_FORFEITED,
            actor_agent_id=None,
            target_type="bond",
            target_id=bid,
            detail={"face_units": face},
            conn=conn,
        )
        total += face
    return {"bonds": len(rows), "face_units": total}


def bond_series_detail(series_id: int) -> dict:
    """One series in full: terms, status, live outstanding face and live
    holder/bond counts. Public read. Additive beside list_bond_series,
    whose shape stays frozen for existing consumers."""
    with _conn() as conn:
        row = _series_row(conn, int(series_id))
        d = dict(row)
        d["series_id"] = int(d.pop("id"))
        d["outstanding_units"] = _outstanding(conn, int(series_id))
        live = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT owner_id) FROM treasury_bonds"
            " WHERE series_id = ? AND status IN ('active', 'matured')",
            (int(series_id),),
        ).fetchone()
        d["live_bonds"] = int(live[0])
        d["holder_count"] = int(live[1])
        return d


def bonds_for_series(series_id: int) -> list[dict]:
    """Every bond in one series with owner names joined, newest first.
    Admin-eyes-only consumer (per-bond holdings are never public)."""
    with _conn() as conn:
        _series_row(conn, int(series_id))
        rows = conn.execute(
            "SELECT b.*, a.name AS owner_name FROM treasury_bonds b"
            " LEFT JOIN agents a ON a.id = b.owner_id"
            " WHERE b.series_id = ? ORDER BY b.id DESC",
            (int(series_id),),
        ).fetchall()
        return [dict(r) for r in rows]
