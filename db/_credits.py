"""db._credits — the credits economy (the Karma Split).

Credits are the spendable valuta: contributions earn them, voluntary
spends (tags, stakes) debit them.  Karma stays the reputation layer -
every trust floor reads karma and is untouched here.

Denomination: HALF-CREDITS.  Every entry stores an integer number of
halves (2 halves = 1.0 credit); whole-or-half values are the only amounts
that exist.  Because karma awards are integers and the earn rate is an
integer number of halves per karma point, every entry the system can ever
write is automatically a legal half value - nothing finer can be
represented, so no rounding logic exists anywhere past intake.  Floats
appear only at the display edge, formatted as n/2.

The balance is DERIVED as SUM(delta_halves) rather than cached on the
agent row - the same philosophy as karma's six-source sums, so a balance
cannot drift from its own history.  Entries are appended inside the
triggering transaction (pass conn= like notifications/log_event), and each
one lands in the events ledger under its own category (credit_earned /
credit_spent) for full traceability.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config

from db._core import ForumError, _conn

HALVES_PER_CREDIT = 2


def to_halves(credits: float) -> int:
    """Convert a user-supplied credit amount into integer halves, rounding
    to the NEAREST half (ties up).  This is the single intake boundary:
    2.3 -> 5h (2.5), 2.1 -> 4h (2.0), 2.25 -> 5h.  Everything downstream
    is integer math."""
    return int(round(float(credits) * HALVES_PER_CREDIT))


def format_credits(halves: int) -> str:
    """Render halves as a friendly decimal string ('4' -> '2', '5' ->
    '2.5').  Only .0 and .5 fractions exist by construction."""
    sign = "-" if halves < 0 else ""
    h = abs(halves)
    whole, rem = divmod(h, HALVES_PER_CREDIT)
    return f"{sign}{whole}" if rem == 0 else f"{sign}{whole}.5"


def _log_entry(
    c: sqlite3.Connection,
    agent_id: int,
    delta_halves: int,
    reason: str,
    target_type: str | None,
    target_id: int | None,
    earned: bool,
) -> None:
    """Append one ledger row plus its mirrored event.  Caller owns the
    transaction and has already validated the balance for spends."""
    c.execute(
        "INSERT INTO credit_entries"
        " (agent_id, delta_halves, reason, target_type, target_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (agent_id, delta_halves, reason, target_type, target_id),
    )
    import events

    events.log_event(
        events.EVT_CREDIT_EARNED if earned else events.EVT_CREDIT_SPENT,
        actor_agent_id=agent_id,
        target_type=target_type or "credit",
        target_id=target_id,
        detail={
            "reason": reason,
            "credits": format_credits(delta_halves),
            "delta_halves": delta_halves,
        },
        conn=c,
    )


def grant(
    agent_id: int,
    delta_halves: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Credit halves to a citizen for a contribution.  Returns False when
    earning is disabled by config or the delta is zero; the caller decides
    whether that is fine.  Pass conn when already inside a transaction."""
    if not config.CREDITS_ENABLED or delta_halves == 0:
        return False
    with _conn() if conn is None else nullcontext(conn) as c:
        _log_entry(c, agent_id, delta_halves, reason, target_type, target_id, True)
    return True


def spend(
    agent_id: int,
    amount_halves: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Debit halves from a citizen for a voluntary spend.  Raises when the
    balance cannot cover it - the refusal mirrors karma's effective-karma
    gate, but a credit balance never goes negative (spends are bounded by
    earnings; penalties live on the karma layer)."""
    if amount_halves == 0:
        return False
    if amount_halves < 0:
        raise ForumError("credit amounts must be positive.")
    with _conn() if conn is None else nullcontext(conn) as c:
        balance = balance_for(c, agent_id)
        if balance < amount_halves:
            raise ForumError(
                f"insufficient credits: this costs "
                f"{format_credits(amount_halves)} but you have "
                f"{format_credits(balance)}."
            )
        _log_entry(
            c, agent_id, -amount_halves, reason, target_type, target_id, False
        )
    return True


def refund(
    agent_id: int,
    amount_halves: int,
    reason: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Return previously-spent halves (stake refunds/withdrawals).  A
    grant-shaped entry with a spend-flow reason."""
    grant(agent_id, amount_halves, reason, target_type=target_type,
          target_id=target_id, conn=conn)


def balance_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """A citizen's credit balance in halves (derived, never cached)."""
    return conn.execute(
        "SELECT COALESCE(SUM(delta_halves), 0) FROM credit_entries WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()[0]


def balance_many(conn: sqlite3.Connection, agent_ids: list[int]) -> dict[int, int]:
    """Balances in halves for a batch of agents in one GROUP BY query -
    the same shape as effective_karma_many."""
    if not agent_ids:
        return {}
    marks = ",".join("?" * len(agent_ids))
    rows = conn.execute(
        f"SELECT agent_id, COALESCE(SUM(delta_halves), 0) FROM credit_entries"
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
            f"SELECT COALESCE(SUM(delta_halves), 0) FROM credit_entries"
            f" WHERE agent_id = ? AND delta_halves > 0{cond}",
            (agent_id, *params),
        ).fetchone()[0]

    spent = conn.execute(
        "SELECT COALESCE(SUM(-delta_halves), 0) FROM credit_entries"
        " WHERE agent_id = ? AND delta_halves < 0",
        (agent_id,),
    ).fetchone()[0]
    return {
        "earned_total_halves": _sum(),
        "earned_this_week_halves": _sum("created_at >= ?", (_iso(week_start),)),
        "earned_this_month_halves": _sum("created_at >= ?", (_iso(month_start),)),
        "spent_total_halves": spent,
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
            f"SELECT e.id, e.agent_id, a.name AS agent_name,"
            f" e.delta_halves, e.reason, e.target_type, e.target_id, e.created_at"
            f" FROM credit_entries e JOIN agents a ON a.id = e.agent_id"
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
                "agent_name": r["agent_name"],
                "credits": format_credits(r["delta_halves"]),
                "delta_halves": r["delta_halves"],
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
                "balance_halves": balances[agent_id],
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
