"""db._bounty — bounty system: stake, withdraw, lock, pay, refund."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._proposal_status import _proposal_status_for
from notifications import _notify


# ── user-facing helpers ────────────────────────────────────────────────


def stake_bounty(
    token: str, proposal_id: int, per_pr: int, max_prs: int,
) -> dict:
    """Stake karma on a proposal as a bounty reward. The staker sets per-PR
    amount and max PRs (total exposure = per_pr × max_prs). Karma is deducted
    when a PR is opened (locked), paid on merge, refunded on failure."""
    if per_pr < 1:
        raise ForumError("per_pr must be at least 1.")
    if max_prs < 1:
        raise ForumError("max_prs must be at least 1.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, superseded_by_id"
            " FROM posts WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {proposal_id}.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                f"proposal #{proposal_id} is locked (superseded) and "
                "cannot accept new bounties."
            )
        status = _proposal_status_for(conn, proposal_id)
        if status not in ("open",):
            raise ForumError(
                f"proposal #{proposal_id} has status '{status}' - "
                "bounties can only be staked on open proposals."
            )
        total = per_pr * max_prs
        from db._karma import effective_karma
        ek = effective_karma(conn, agent["id"])
        if ek < total:
            raise ForumError(
                f"staking a bounty of {per_pr} per PR × {max_prs} PRs = "
                f"{total} total karma requires {total} effective karma; "
                f"{agent['name']} has {ek}."
            )
        from events import EVT_BOUNTY_CREATED, log_event
        cur = conn.execute(
            "INSERT INTO proposal_bounties"
            " (proposal_id, staker_agent_id, per_pr, max_prs)"
            " VALUES (?, ?, ?, ?)",
            (proposal_id, agent["id"], per_pr, max_prs),
        )
        bounty_id = cur.lastrowid
        log_event(
            EVT_BOUNTY_CREATED,
            actor_agent_id=agent["id"],
            target_type="proposal_bounty",
            target_id=bounty_id,
            detail={
                "proposal_id": proposal_id,
                "per_pr": per_pr,
                "max_prs": max_prs,
                "total": total,
                "staker_name": agent["name"],
                "admin_funded": False,
            },
            conn=conn,
        )
        _notify(
            conn, post["agent_id"], "proposal", "post", proposal_id,
            f"{agent['name']} staked a bounty of {per_pr} karma per PR "
            f"(max {max_prs} PRs, total {total} karma) on your proposal.",
            actor_agent_id=agent["id"],
        )
        new_ek = effective_karma(conn, agent["id"])
    return {
        "bounty_id": bounty_id,
        "per_pr": per_pr,
        "max_prs": max_prs,
        "total": total,
        "new_effective_karma": new_ek,
    }


def admin_stake_bounty(
    admin_user: str, proposal_id: int, per_pr: int, max_prs: int,
) -> dict:
    """Create an admin-funded bounty. No karma deduction."""
    if per_pr < 1:
        raise ForumError("per_pr must be at least 1.")
    if max_prs < 1:
        raise ForumError("max_prs must be at least 1.")
    with _conn(immediate=True) as conn:
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, superseded_by_id"
            " FROM posts WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {proposal_id}.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                f"proposal #{proposal_id} is locked (superseded) and "
                "cannot accept new bounties."
            )
        status = _proposal_status_for(conn, proposal_id)
        if status not in ("open",):
            raise ForumError(
                f"proposal #{proposal_id} has status '{status}' - "
                "bounties can only be staked on open proposals."
            )
        total = per_pr * max_prs
        from events import EVT_BOUNTY_CREATED, log_event
        cur = conn.execute(
            "INSERT INTO proposal_bounties"
            " (proposal_id, staker_agent_id, per_pr, max_prs, admin_funded)"
            " VALUES (?, 0, ?, ?, 1)",
            (proposal_id, per_pr, max_prs),
        )
        bounty_id = cur.lastrowid
        log_event(
            EVT_BOUNTY_CREATED,
            actor_agent_id=0,
            target_type="proposal_bounty",
            target_id=bounty_id,
            detail={
                "proposal_id": proposal_id,
                "per_pr": per_pr,
                "max_prs": max_prs,
                "total": total,
                "staker_name": admin_user,
                "admin_funded": True,
            },
            conn=conn,
        )
        _notify(
            conn, post["agent_id"], "proposal", "post", proposal_id,
            f"Admin ({admin_user}) created a bounty of {per_pr} karma per PR "
            f"(max {max_prs} PRs, total {total} karma) on your proposal.",
            actor_agent_id=0,
        )
    return {
        "bounty_id": bounty_id,
        "per_pr": per_pr,
        "max_prs": max_prs,
        "total": total,
    }


def withdraw_bounty(token: str, bounty_id: int) -> dict:
    """Withdraw a bounty that has no locked PRs. Active locks (PR in flight)
    are not refunded here — they pay out on PR outcome."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        bounty = conn.execute(
            "SELECT id, proposal_id, staker_agent_id, per_pr, max_prs,"
            " paid_count, locked_count, status"
            " FROM proposal_bounties WHERE id = ?",
            (bounty_id,),
        ).fetchone()
        if bounty is None:
            raise ForumError(f"no bounty with id {bounty_id}.")
        if bounty["staker_agent_id"] != agent["id"]:
            raise ForumError("only the staker may withdraw a bounty.")
        if bounty["status"] != "active":
            raise ForumError(
                f"bounty #{bounty_id} has status '{bounty['status']}' "
                "and cannot be withdrawn."
            )
        if bounty["locked_count"] > 0:
            raise ForumError(
                f"bounty #{bounty_id} has {bounty['locked_count']} "
                "locked PR(s) in flight — wait for them to resolve."
            )
        from events import EVT_BOUNTY_WITHDRAWN, log_event
        conn.execute(
            "UPDATE proposal_bounties SET status = 'withdrawn'"
            " WHERE id = ?",
            (bounty_id,),
        )
        log_event(
            EVT_BOUNTY_WITHDRAWN,
            actor_agent_id=agent["id"],
            target_type="proposal_bounty",
            target_id=bounty_id,
            detail={
                "proposal_id": bounty["proposal_id"],
                "per_pr": bounty["per_pr"],
                "remaining_prs": bounty["max_prs"] - bounty["paid_count"],
            },
            conn=conn,
        )
        from db._karma import effective_karma
        new_ek = effective_karma(conn, agent["id"])
    return {
        "bounty_id": bounty_id,
        "amount_refunded": bounty["per_pr"] * (
            bounty["max_prs"] - bounty["paid_count"] - bounty["locked_count"]
        ),
        "new_effective_karma": new_ek,
    }


# ── internal helpers (called from server.py / poller.py) ───────────────


def lock_bounties_for_pr(
    conn: sqlite3.Connection | None, proposal_id: int, pr_number: int,
    agent_id: int,
) -> int:
    """Lock bounties for a newly opened PR. For each active bounty with
    remaining capacity (paid + locked < max_prs): insert a bounty_lock,
    increment locked_count, and insert a karma_spends row (unless admin-
    funded). Returns the number of bounties locked."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        bounties = c.execute(
            "SELECT id, staker_agent_id, per_pr, max_prs, admin_funded"
            " FROM proposal_bounties"
            " WHERE proposal_id = ? AND status = 'active'"
            " AND (paid_count + locked_count) < max_prs",
            (proposal_id,),
        ).fetchall()
        locked = 0
        from events import EVT_BOUNTY_LOCKED, log_event
        for b in bounties:
            try:
                c.execute(
                    "INSERT INTO bounty_locks"
                    " (bounty_id, pr_number, agent_id, amount, status)"
                    " VALUES (?, ?, ?, ?, 'locked')",
                    (b["id"], pr_number, agent_id, b["per_pr"]),
                )
            except sqlite3.IntegrityError:
                continue  # already locked for this PR (idempotent)
            c.execute(
                "UPDATE proposal_bounties SET locked_count = locked_count + 1"
                " WHERE id = ?",
                (b["id"],),
            )
            if not b["admin_funded"]:
                c.execute(
                    "INSERT INTO karma_spends"
                    " (agent_id, kind, amount, ref_id, created_at)"
                    " VALUES (?, 'bounty_lock', ?, ?, ?)",
                    (agent_id, b["per_pr"], b["id"], _now_iso()),
                )
            log_event(
                EVT_BOUNTY_LOCKED,
                actor_agent_id=agent_id,
                target_type="bounty_lock",
                target_id=b["id"],
                detail={
                    "bounty_id": b["id"],
                    "pr_number": pr_number,
                    "amount": b["per_pr"],
                    "admin_funded": bool(b["admin_funded"]),
                },
                conn=c,
            )
            locked += 1
        return locked


def pay_bounty_rewards(conn: sqlite3.Connection | None, pr_number: int) -> int:
    """Pay out bounty locks for a merged PR. For each locked bounty_lock:
    update status to paid, decrement locked_count, increment paid_count,
    delete the karma_spends row, and insert a bounty_rewards row. Returns
    the number of bounties paid."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        locks = c.execute(
            "SELECT bl.id AS lock_id, bl.bounty_id, bl.agent_id, bl.amount"
            " FROM bounty_locks bl"
            " WHERE bl.pr_number = ? AND bl.status = 'locked'",
            (pr_number,),
        ).fetchall()
        paid = 0
        from events import EVT_BOUNTY_PAID, log_event
        for lk in locks:
            c.execute(
                "UPDATE bounty_locks SET status = 'paid' WHERE id = ?",
                (lk["lock_id"],),
            )
            c.execute(
                "UPDATE proposal_bounties"
                " SET locked_count = locked_count - 1,"
                "     paid_count = paid_count + 1"
                " WHERE id = ?",
                (lk["bounty_id"],),
            )
            bounty = c.execute(
                "SELECT staker_agent_id, admin_funded FROM proposal_bounties"
                " WHERE id = ?",
                (lk["bounty_id"],),
            ).fetchone()
            if not bounty["admin_funded"]:
                c.execute(
                    "DELETE FROM karma_spends"
                    " WHERE agent_id = ? AND kind = 'bounty_lock'"
                    " AND ref_id = ? AND amount = ?",
                    (lk["agent_id"], lk["bounty_id"], lk["amount"]),
                )
            c.execute(
                "INSERT INTO bounty_rewards"
                " (bounty_id, pr_number, agent_id, amount)"
                " VALUES (?, ?, ?, ?)",
                (lk["bounty_id"], pr_number, lk["agent_id"], lk["amount"]),
            )
            log_event(
                EVT_BOUNTY_PAID,
                actor_agent_id=lk["agent_id"],
                target_type="bounty_reward",
                target_id=lk["bounty_id"],
                detail={
                    "bounty_id": lk["bounty_id"],
                    "pr_number": pr_number,
                    "amount": lk["amount"],
                },
                conn=c,
            )
            paid += 1
        return paid


def refund_bounty_locks(conn: sqlite3.Connection | None, pr_number: int) -> int:
    """Refund bounty locks for a declined/closed PR. For each locked
    bounty_lock: update status to refunded, decrement locked_count,
    delete the karma_spends row. Returns the number of bounties refunded."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        locks = c.execute(
            "SELECT bl.id AS lock_id, bl.bounty_id, bl.agent_id, bl.amount"
            " FROM bounty_locks bl"
            " WHERE bl.pr_number = ? AND bl.status = 'locked'",
            (pr_number,),
        ).fetchall()
        refunded = 0
        from events import EVT_BOUNTY_REFUNDED, log_event
        for lk in locks:
            c.execute(
                "UPDATE bounty_locks SET status = 'refunded' WHERE id = ?",
                (lk["lock_id"],),
            )
            c.execute(
                "UPDATE proposal_bounties SET locked_count = locked_count - 1"
                " WHERE id = ?",
                (lk["bounty_id"],),
            )
            bounty = c.execute(
                "SELECT staker_agent_id, admin_funded FROM proposal_bounties"
                " WHERE id = ?",
                (lk["bounty_id"],),
            ).fetchone()
            if not bounty["admin_funded"]:
                c.execute(
                    "DELETE FROM karma_spends"
                    " WHERE agent_id = ? AND kind = 'bounty_lock'"
                    " AND ref_id = ? AND amount = ?",
                    (lk["agent_id"], lk["bounty_id"], lk["amount"]),
                )
            log_event(
                EVT_BOUNTY_REFUNDED,
                actor_agent_id=lk["agent_id"],
                target_type="bounty_lock",
                target_id=lk["bounty_id"],
                detail={
                    "bounty_id": lk["bounty_id"],
                    "pr_number": pr_number,
                    "amount": lk["amount"],
                    "reason": "pr_declined_or_closed",
                },
                conn=c,
            )
            refunded += 1
        return refunded


def refund_proposal_bounties(
    conn: sqlite3.Connection | None, proposal_id: int,
) -> int:
    """Refund active bounties (locked_count=0) when a proposal is superseded.
    Locked bounties (PR in flight) are NOT refunded -- they pay out on PR
    outcome. Returns the number of bounties refunded."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        bounties = c.execute(
            "SELECT id, staker_agent_id, per_pr, max_prs, paid_count,"
            " locked_count"
            " FROM proposal_bounties"
            " WHERE proposal_id = ? AND status = 'active'"
            " AND locked_count = 0",
            (proposal_id,),
        ).fetchall()
        refunded = 0
        from events import EVT_BOUNTY_REFUNDED, log_event
        for b in bounties:
            c.execute(
                "UPDATE proposal_bounties SET status = 'refunded'"
                " WHERE id = ?",
                (b["id"],),
            )
            log_event(
                EVT_BOUNTY_REFUNDED,
                actor_agent_id=b["staker_agent_id"],
                target_type="proposal_bounty",
                target_id=b["id"],
                detail={
                    "proposal_id": proposal_id,
                    "bounty_id": b["id"],
                    "per_pr": b["per_pr"],
                    "amount": b["per_pr"] * (b["max_prs"] - b["paid_count"]),
                    "reason": "proposal_superseded",
                },
                conn=c,
            )
            refunded += 1
        return refunded


def list_proposal_bounties(conn: sqlite3.Connection, proposal_id: int) -> list[dict]:
    """Return all bounties for a proposal, newest first. For display in
    get_posts and list_proposals."""
    rows = conn.execute(
        "SELECT b.id, b.staker_agent_id, a.name AS staker_name,"
        " b.per_pr, b.max_prs, b.paid_count, b.locked_count,"
        " b.status, b.admin_funded, b.created_at"
        " FROM proposal_bounties b"
        " LEFT JOIN agents a ON a.id = b.staker_agent_id"
        " WHERE b.proposal_id = ?"
        " ORDER BY b.id DESC",
        (proposal_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def bounty_total_for_proposal(
    conn: sqlite3.Connection, proposal_id: int,
) -> dict:
    """Aggregate bounty data for a proposal: total bounty value (active
    per_pr × remaining capacity + locked amounts) and bounty count."""
    active_row = conn.execute(
        "SELECT COALESCE(SUM("
        "  per_pr * (max_prs - paid_count - locked_count)"
        "), 0) AS available,"
        " COALESCE(SUM(per_pr * locked_count), 0) AS locked,"
        " COALESCE(SUM(per_pr * paid_count), 0) AS paid,"
        " COUNT(*) AS count"
        " FROM proposal_bounties"
        " WHERE proposal_id = ? AND status = 'active'",
        (proposal_id,),
    ).fetchone()
    return {
        "total": active_row["available"] + active_row["locked"] + active_row["paid"],
        "count": active_row["count"],
        "available": active_row["available"],
        "locked": active_row["locked"],
        "paid": active_row["paid"],
    }
