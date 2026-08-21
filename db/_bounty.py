"""db._bounty — bounty system: stake, withdraw, lock, pay, refund."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

from db._core import ForumError, _conn, _id_chunks, _now_iso, _require_active_agent
from db._proposal_status import _proposal_status_for
from notifications import _notify


# ── user-facing helpers ────────────────────────────────────────────────


def stake_bounty(
    token: str, proposal_id: int, per_pr: int, max_prs: int,
) -> dict:
    """Stake karma on a proposal as a bounty reward. The staker sets per-PR
    amount and max PRs (total exposure = per_pr × max_prs). The staker's
    effective_karma is checked at creation time; the actual deduction happens
    when a PR is opened (lock_bounties_for_pr). On merge, the staker's spend
    persists as a permanent debit and the PR opener receives a bounty_rewards
    credit (true transfer). On decline/close, the staker's spend is deleted
    (refund)."""
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
                f"staking a bounty of {per_pr} per PR x {max_prs} PRs = "
                f"{total} total karma requires {total} effective karma; "
                f"{agent['name']} has {ek}."
            )
        import config
        max_frac = config.BOUNTY_MAX_STAKE_FRACTION
        if max_frac > 0:
            row = conn.execute(
                "SELECT COALESCE(SUM(per_pr * (max_prs - paid_count"
                " - locked_count)), 0) FROM proposal_bounties"
                " WHERE staker_agent_id = ? AND status = 'active'",
                (agent["id"],),
            ).fetchone()
            current_exposure = row[0]
            cap = int(ek * max_frac)
            if current_exposure + total > cap:
                raise ForumError(
                    f"aggregate bounty exposure would be "
                    f"{current_exposure + total} (current {current_exposure}"
                    f" + new {total}), exceeding {max_frac:.0%} of your"
                    f" effective karma ({ek}, cap {cap})."
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
    """Create an admin-funded bounty. No karma deduction — staker_agent_id
    is NULL and no karma_spends rows are created."""
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
            " VALUES (?, NULL, ?, ?, 1)",
            (proposal_id, per_pr, max_prs),
        )
        bounty_id = cur.lastrowid
        log_event(
            EVT_BOUNTY_CREATED,
            actor_agent_id=None,
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
            actor_agent_id=None,
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
        if bounty["staker_agent_id"] is None:
            raise ForumError("admin-funded bounties cannot be withdrawn.")
        if bounty["staker_agent_id"] != agent["id"]:
            raise ForumError("only the staker may withdraw a bounty.")
        if bounty["status"] == "completed":
            raise ForumError(
                f"bounty #{bounty_id} is fully paid and cannot be withdrawn."
            )
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
        post_author = conn.execute(
            "SELECT agent_id FROM posts WHERE id = ?",
            (bounty["proposal_id"],),
        ).fetchone()
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
        _notify(
            conn, post_author["agent_id"], "proposal", "post",
            bounty["proposal_id"],
            f"{agent['name']} withdrew a bounty of {bounty['per_pr']} karma "
            f"per PR (max {bounty['max_prs']} PRs) from your proposal.",
            actor_agent_id=agent["id"],
        )
        from db._karma import effective_karma
        new_ek = effective_karma(conn, agent["id"])
    return {
        "bounty_id": bounty_id,
        "amount_released": bounty["per_pr"] * (
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
    increment locked_count, and insert a karma_spends row under the
    STAKER's agent_id (unless admin-funded). The karma_spend_id is stored
    on the lock for precise refund/pay tracking. Returns the number of
    bounties locked.

    NOTE: also called by the poller as a fallback before pay/refund —
    the UNIQUE(bounty_id, pr_number) constraint makes this idempotent."""
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
            spend_id = None
            if not b["admin_funded"]:
                spend_cur = c.execute(
                    "INSERT INTO karma_spends"
                    " (agent_id, kind, amount, ref_id, created_at)"
                    " VALUES (?, 'bounty_lock', ?, ?, ?)",
                    (b["staker_agent_id"], b["per_pr"], b["id"], _now_iso()),
                )
                spend_id = spend_cur.lastrowid
            try:
                c.execute(
                    "INSERT INTO bounty_locks"
                    " (bounty_id, pr_number, agent_id, amount, status,"
                    "  karma_spend_id)"
                    " VALUES (?, ?, ?, ?, 'locked', ?)",
                    (b["id"], pr_number, agent_id, b["per_pr"], spend_id),
                )
            except sqlite3.IntegrityError:
                # Already locked for this PR (idempotent) — roll back
                # the spend we just created.
                if spend_id is not None:
                    c.execute("DELETE FROM karma_spends WHERE id = ?",
                              (spend_id,))
                continue
            c.execute(
                "UPDATE proposal_bounties SET locked_count = locked_count + 1"
                " WHERE id = ?",
                (b["id"],),
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
            if b["staker_agent_id"] is not None:
                _notify(
                    c, b["staker_agent_id"], "proposal", "bounty_lock",
                    b["id"],
                    f"Bounty of {b['per_pr']} karma locked for PR "
                    f"#{pr_number}.",
                    actor_agent_id=agent_id,
                )
            locked += 1
        return locked


def pay_bounty_rewards(conn: sqlite3.Connection | None, pr_number: int) -> int:
    """Pay out bounty locks for a merged PR. For each locked bounty_lock:
    update status to paid, decrement locked_count, increment paid_count.

    Self-staking: when the PR opener is the bounty staker, the spend is
    refunded (deleted) instead of creating a bounty_rewards row — a transfer
    to yourself would be net-zero but inflate earned/spent.  The lock still
    records as 'paid' (the PR merged) and paid_count increments.

    Normal: the staker's karma_spends row PERSISTS as a permanent debit
    (true transfer), and a bounty_rewards row credits the PR opener.
    Admin-funded bounties have no spend to preserve.  Returns the number
    of bounties paid."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        locks = c.execute(
            "SELECT bl.id AS lock_id, bl.bounty_id, bl.agent_id, bl.amount,"
            " bl.karma_spend_id, b.staker_agent_id"
            " FROM bounty_locks bl"
            " JOIN proposal_bounties b ON b.id = bl.bounty_id"
            " WHERE bl.pr_number = ? AND bl.status = 'locked'",
            (pr_number,),
        ).fetchall()
        paid = 0
        from events import EVT_BOUNTY_PAID, log_event
        for lk in locks:
            self_stake = (
                lk["staker_agent_id"] is not None
                and lk["agent_id"] == lk["staker_agent_id"]
            )
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
            if self_stake:
                # Refund the staker's own spend — no transfer to yourself.
                if lk["karma_spend_id"] is not None:
                    c.execute(
                        "UPDATE bounty_locks SET karma_spend_id = NULL"
                        " WHERE id = ?",
                        (lk["lock_id"],),
                    )
                    c.execute(
                        "DELETE FROM karma_spends WHERE id = ?",
                        (lk["karma_spend_id"],),
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
                        "self_stake": True,
                    },
                    conn=c,
                )
                _notify(
                    c, lk["agent_id"], "pr", "bounty_reward",
                    lk["bounty_id"],
                    f"Your PR #{pr_number} merged; bounty of "
                    f"{lk['amount']} karma returned (self-stake).",
                )
            else:
                c.execute(
                    "INSERT INTO bounty_rewards"
                    " (bounty_id, pr_number, agent_id, amount)"
                    " VALUES (?, ?, ?, ?)",
                    (lk["bounty_id"], pr_number, lk["agent_id"],
                     lk["amount"]),
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
                _notify(
                    c, lk["agent_id"], "pr", "bounty_reward",
                    lk["bounty_id"],
                    f"Your PR #{pr_number} earned a bounty reward of "
                    f"{lk['amount']} karma.",
                )
            paid += 1

        # After the loop: check for bounty completions.
        if paid > 0:
            from events import (
                EVT_BOUNTY_COMPLETED,
                log_event as _log_bounty_event,
            )

            # Collect unique bounty IDs that were paid in this call.
            bounty_ids_paid = {lk["bounty_id"] for lk in locks}
            for bid in bounty_ids_paid:
                pb_row = c.execute(
                    "SELECT paid_count, locked_count, max_prs,"
                    " staker_agent_id, status"
                    " FROM proposal_bounties WHERE id = ?",
                    (bid,),
                ).fetchone()
                if (
                    pb_row["status"] != "completed"
                    and pb_row["paid_count"] == pb_row["max_prs"]
                    and pb_row["locked_count"] == 0
                ):
                    c.execute(
                        "UPDATE proposal_bounties"
                        " SET status = 'completed' WHERE id = ?",
                        (bid,),
                    )
                    _log_bounty_event(
                        EVT_BOUNTY_COMPLETED,
                        actor_agent_id=pb_row["staker_agent_id"],
                        target_type="proposal_bounty",
                        target_id=bid,
                        detail={"bounty_id": bid},
                        conn=c,
                    )
                    if pb_row["staker_agent_id"] is not None:
                        _notify(
                            c, pb_row["staker_agent_id"], "proposal",
                            "bounty_completed", bid,
                            f"Bounty #{bid} is now fully paid.",
                        )
        return paid


def refund_bounty_locks(conn: sqlite3.Connection | None, pr_number: int) -> int:
    """Refund bounty locks for a declined/closed PR. For each locked
    bounty_lock: update status to refunded, decrement locked_count,
    and delete the staker's karma_spends row (restoring their effective
    karma). Uses karma_spend_id for precise per-lock deletion.
    Returns the number of bounties refunded."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        locks = c.execute(
            "SELECT bl.id AS lock_id, bl.bounty_id, bl.agent_id, bl.amount,"
            " bl.karma_spend_id, b.staker_agent_id"
            " FROM bounty_locks bl"
            " JOIN proposal_bounties b ON b.id = bl.bounty_id"
            " WHERE bl.pr_number = ? AND bl.status = 'locked'",
            (pr_number,),
        ).fetchall()
        refunded = 0
        from events import EVT_BOUNTY_REFUNDED, log_event
        for lk in locks:
            c.execute(
                "UPDATE bounty_locks SET status = 'refunded',"
                " karma_spend_id = NULL WHERE id = ?",
                (lk["lock_id"],),
            )
            c.execute(
                "UPDATE proposal_bounties SET locked_count = locked_count - 1"
                " WHERE id = ?",
                (lk["bounty_id"],),
            )
            if lk["karma_spend_id"] is not None:
                c.execute(
                    "DELETE FROM karma_spends WHERE id = ?",
                    (lk["karma_spend_id"],),
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
            if lk["staker_agent_id"] is not None:
                _notify(
                    c, lk["staker_agent_id"], "proposal", "bounty_refund",
                    lk["bounty_id"],
                    f"Bounty lock of {lk['amount']} karma on PR #{pr_number}"
                    " was refunded (PR declined or closed).",
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


def list_proposal_bounties_batch(
    conn: sqlite3.Connection, proposal_ids: list[int],
) -> dict[int, list[dict]]:
    """Batch version of list_proposal_bounties: {proposal_id: [bounty, ...]}."""
    if not proposal_ids:
        return {}
    out: dict[int, list[dict]] = {pid: [] for pid in proposal_ids}
    for chunk in _id_chunks(proposal_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT b.id, b.proposal_id, b.staker_agent_id, a.name AS staker_name,"
            f" b.per_pr, b.max_prs, b.paid_count, b.locked_count,"
            f" b.status, b.admin_funded, b.created_at"
            f" FROM proposal_bounties b"
            f" LEFT JOIN agents a ON a.id = b.staker_agent_id"
            f" WHERE b.proposal_id IN ({marks})"
            f" ORDER BY b.proposal_id, b.id DESC",
            chunk,
        ).fetchall()
        for r in rows:
            d = dict(r)
            pid = d.pop("proposal_id")
            out[pid].append(d)
    return out


def _bounty_totals_batch(
    conn: sqlite3.Connection, proposal_ids: list[int],
) -> dict[int, dict]:
    """Batch version of bounty_total_for_proposal: {proposal_id: {total,
    count, available, locked, paid}} for all given proposal IDs at once."""
    if not proposal_ids:
        return {}
    out: dict[int, dict] = {}
    for chunk in _id_chunks(proposal_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT proposal_id,
                   COALESCE(SUM(
                     per_pr * (max_prs - paid_count - locked_count)
                   ), 0) AS available,
                   COALESCE(SUM(per_pr * locked_count), 0) AS locked,
                   COALESCE(SUM(per_pr * paid_count), 0) AS paid,
                   COUNT(*) AS count
            FROM proposal_bounties
            WHERE proposal_id IN ({marks}) AND status = 'active'
            GROUP BY proposal_id
            """,
            chunk,
        ).fetchall()
        for r in rows:
            out[r["proposal_id"]] = {
                "total": r["available"] + r["locked"] + r["paid"],
                "count": r["count"],
                "available": r["available"],
                "locked": r["locked"],
                "paid": r["paid"],
            }
    return out


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


def list_all_bounties(
    status: str | None = None,
) -> list[dict]:
    """All bounties across all proposals, newest first. For the /bounties
    viewer page. Optionally filter by status (active, withdrawn, refunded)."""
    sql = (
        "SELECT b.id, b.proposal_id, b.staker_agent_id, a.name AS staker_name,"
        " b.per_pr, b.max_prs, b.paid_count, b.locked_count,"
        " b.status, b.admin_funded, b.created_at,"
        " p.title AS proposal_title"
        " FROM proposal_bounties b"
        " LEFT JOIN agents a ON a.id = b.staker_agent_id"
        " LEFT JOIN posts p ON p.id = b.proposal_id"
    )
    params: list = []
    if status:
        sql += " WHERE b.status = ?"
        params.append(status)
    sql += " ORDER BY b.id DESC"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
