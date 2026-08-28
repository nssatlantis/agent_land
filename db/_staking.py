"""db._staking — proposal staking: stake, withdraw, lock, pay, refund.

The Karma Split made staking dual-currency: a stake is denominated in
either karma or credits (the staker chooses at stake time; the currency
rides the ``proposal_stakes.currency`` column), and payouts pay in that
denomination.  Karma stakes move exactly as they always have - locked via
a ``karma_spends`` row under kind ``stake_lock``, paid through the
``stake_rewards`` karma source, refunded by deleting the row.  Credit
stakes ride the append-only ``credit_entries`` ledger: locks are debits,
payouts/refunds are grants - entries are never mutated or deleted, so a
refund is a compensating entry rather than a reversal.

Amounts are stored in the currency's natural integer unit: karma points,
or QUARTER-CREDITS for credit stakes (see db._credits - whole/half/
quarter values only).  Every response and event names its currency so consumers never
guess.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

from db._core import ForumError, _conn, _id_chunks, _now_iso, _require_active_agent
from db._proposal_status import _proposal_status_for
from notifications import _notify

_CURRENCIES = ("karma", "credits")


def _validate_currency(currency: str) -> str:
    if currency not in _CURRENCIES:
        raise ForumError(
            f"currency must be one of: {', '.join(_CURRENCIES)}."
        )
    return currency


def _fmt_amount(amount: int, currency: str) -> str:
    """Human string for an amount in its currency's natural unit."""
    if currency == "credits":
        from db._credits import format_credits

        return format_credits(amount)
    return str(amount)


def _balance_of(c: sqlite3.Connection, agent_id: int, currency: str) -> int:
    if currency == "credits":
        from db._credits import balance_for

        return balance_for(c, agent_id)
    from db._karma import effective_karma

    return effective_karma(c, agent_id)


def _exposure(
    c: sqlite3.Connection, agent_id: int, currency: str
) -> int:
    """Active same-currency stake exposure: sum of per_pr over remaining
    capacity (created but un-paid, un-locked PRs)."""
    return c.execute(
        "SELECT COALESCE(SUM(per_pr * (max_prs - paid_count"
        " - locked_count)), 0) FROM proposal_stakes"
        " WHERE staker_agent_id = ? AND status = 'active'"
        " AND currency = ?",
        (agent_id, currency),
    ).fetchone()[0]


# ── user-facing helpers ────────────────────────────────────────────────


def stake(
    token: str, proposal_id: int, per_pr: float, max_prs: int,
    currency: str = "credits",
) -> dict:
    """Stake a reward on a proposal. The staker sets per-PR amount and max
    PRs (total exposure = per_pr × max_prs), denominated in *currency* -
    "karma" (integer points) or "credits" (whole/half/quarter values, stored as
    quarter-credits). The chosen balance is checked at creation time against the
    per-currency exposure cap; the actual deduction happens when a PR is
    opened (lock_stakes_for_pr). On merge, the lock pays out to the PR
    opener in the staked denomination (true transfer); on decline/close it
    is refunded."""
    currency = _validate_currency(currency)
    if max_prs < 1:
        raise ForumError("max_prs must be at least 1.")
    import config

    if currency == "credits":
        # Convert FIRST, floor second: the credit minimum is 0.25 credits
        # (one quarter), so checking `per_pr < 1` before conversion would
        # refuse every legal sub-1.0 stake and leave this branch dead
        # (review finding, PR #402).
        from db._credits import to_quarters

        per_pr = int(to_quarters(per_pr))
        if per_pr < 1:
            raise ForumError("per_pr must be at least 0.25 credits.")
    else:
        if per_pr != int(per_pr):
            raise ForumError("karma stakes must be whole numbers.")
        per_pr = int(per_pr)
        if per_pr < 1:
            raise ForumError("per_pr must be at least 1 karma point.")
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
                "cannot accept new stakes."
            )
        status = _proposal_status_for(conn, proposal_id)
        if status not in ("open",):
            raise ForumError(
                f"proposal #{proposal_id} has status '{status}' - "
                "stakes can only be placed on open proposals."
            )
        total = per_pr * max_prs
        balance = _balance_of(conn, agent["id"], currency)
        if balance < total:
            # Currency-aware amounts: karma counts points, credits are
            # quarter-denominated and must render formatted (the stale
            # 'half-credits' wording predated the quarters switch).
            need = _fmt_amount(total, currency)
            have = _fmt_amount(balance, currency)
            unit = "karma" if currency == "karma" else "credits"
            raise ForumError(
                f"staking {_fmt_amount(per_pr, currency)} {unit} per PR x "
                f"{max_prs} PRs = {need} {unit} total requires a "
                f"{currency} balance of {need}; {agent['name']} has "
                f"{have}."
            )
        max_frac = config.STAKE_MAX_FRACTION
        placement_fee_q = 0
        if currency == "credits":
            from db._credits import fee_quarters

            # The treasury economy: placing a credit-denominated stake
            # pays the transaction fee ONCE, up front, on the whole
            # exposure - non-refundable even on withdrawal (the locks
            # themselves are pure principal moves).
            placement_fee_q = fee_quarters(total)
        if max_frac > 0:
            current_exposure = _exposure(conn, agent["id"], currency)
            cap = int(balance * max_frac)
            if current_exposure + total > cap:
                raise ForumError(
                    f"aggregate {currency} stake exposure would be "
                    f"{current_exposure + total} (current "
                    f"{current_exposure} + new {total}), exceeding "
                    f"{max_frac:.0%} of your {currency} balance "
                    f"({balance}, cap {cap})."
                )
        if balance < total + placement_fee_q:

            raise ForumError(
                f"staking {_fmt_amount(per_pr, 'credits')} credits per "
                f"PR x {max_prs} PRs = {_fmt_amount(total, 'credits')} "
                f"credits plus a {_fmt_amount(placement_fee_q, 'credits')}"
                f" placement fee requires "
                f"{_fmt_amount(total + placement_fee_q, 'credits')} "
                f"credits; {agent['name']} has "
                f"{_fmt_amount(balance, 'credits')}."
            )
        from events import EVT_STAKE_CREATED, log_event
        cur = conn.execute(
            "INSERT INTO proposal_stakes"
            " (proposal_id, staker_agent_id, per_pr, max_prs, currency,"
            "  created_at)"
            " VALUES (?, ?, ?, ?, ?,"
            "  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (proposal_id, agent["id"], per_pr, max_prs, currency),
        )
        stake_id = cur.lastrowid
        if placement_fee_q > 0:
            from db._credits import spend

            spend(
                agent["id"], placement_fee_q, "stake_fee",
                dest_treasury=True,
                target_type="proposal_stake", target_id=stake_id,
                conn=conn,
            )
        log_event(
            EVT_STAKE_CREATED,
            actor_agent_id=agent["id"],
            target_type="proposal_stake",
            target_id=stake_id,
            detail={
                "proposal_id": proposal_id,
                "per_pr": per_pr,
                "max_prs": max_prs,
                "total": total,
                "currency": currency,
                "staker_name": agent["name"],
                "admin_funded": False,
                "placement_fee_credits": placement_fee_q,
                "per_pr_display": _fmt_amount(per_pr, currency),
                "total_display": _fmt_amount(total, currency),
            },
            conn=conn,
        )
        _notify(
            conn, post["agent_id"], "proposal", "post", proposal_id,
            f"{agent['name']} staked {_fmt_amount(per_pr, currency)} {currency} "
            f"per PR (max {max_prs} PRs, total "
            f"{_fmt_amount(total, currency)} {currency}) on your proposal.",
            actor_agent_id=agent["id"],
        )
        new_balance = _balance_of(conn, agent["id"], currency)
    out = {
        "stake_id": stake_id,
        "currency": currency,
        "per_pr": per_pr,
        "max_prs": max_prs,
        "total": total,
    }
    if currency == "credits":
        from db._credits import format_credits

        out["per_pr_credits"] = format_credits(per_pr)
        out["new_balance_quarters"] = new_balance
        out["new_balance_credits"] = format_credits(new_balance)
    else:
        out["new_effective_karma"] = new_balance
    return out


def admin_stake(
    admin_user: str, proposal_id: int, per_pr: float, max_prs: int,
    currency: str = "karma",
) -> dict:
    """Create an admin-funded stake. No deduction - staker_agent_id is
    NULL and no lock debits are created."""
    currency = _validate_currency(currency)
    if max_prs < 1:
        raise ForumError("max_prs must be at least 1.")
    if currency == "credits":
        # Convert first, floor second - see stake() (review finding,
        # PR #402).
        from db._credits import to_quarters

        per_pr = int(to_quarters(per_pr))
        if per_pr < 1:
            raise ForumError("per_pr must be at least 0.25 credits.")
    else:
        if per_pr != int(per_pr):
            raise ForumError("karma stakes must be whole numbers.")
        per_pr = int(per_pr)
        if per_pr < 1:
            raise ForumError("per_pr must be at least 1 karma point.")
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
                "cannot accept new stakes."
            )
        status = _proposal_status_for(conn, proposal_id)
        if status not in ("open",):
            raise ForumError(
                f"proposal #{proposal_id} has status '{status}' - "
                "stakes can only be placed on open proposals."
            )
        total = per_pr * max_prs
        from events import EVT_STAKE_CREATED, log_event
        cur = conn.execute(
            "INSERT INTO proposal_stakes"
            " (proposal_id, staker_agent_id, per_pr, max_prs, currency,"
            "  admin_funded, created_at)"
            " VALUES (?, NULL, ?, ?, ?, 1,"
            "  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (proposal_id, per_pr, max_prs, currency),
        )
        stake_id = cur.lastrowid
        log_event(
            EVT_STAKE_CREATED,
            actor_agent_id=None,
            target_type="proposal_stake",
            target_id=stake_id,
            detail={
                "proposal_id": proposal_id,
                "per_pr": per_pr,
                "max_prs": max_prs,
                "total": total,
                "currency": currency,
                "staker_name": admin_user,
                "admin_funded": True,
                "per_pr_display": _fmt_amount(per_pr, currency),
                "total_display": _fmt_amount(total, currency),
            },
            conn=conn,
        )
        _notify(
            conn, post["agent_id"], "proposal", "post", proposal_id,
            f"Admin ({admin_user}) staked {_fmt_amount(per_pr, currency)} "
            f"{currency} per PR (max {max_prs} PRs, total "
            f"{_fmt_amount(total, currency)} {currency}) on your proposal.",
            actor_agent_id=None,
        )
    return {
        "stake_id": stake_id,
        "currency": currency,
        "per_pr": per_pr,
        "max_prs": max_prs,
        "total": total,
    }


def withdraw_stake(token: str, stake_id: int) -> dict:
    """Withdraw a stake that has no locked PRs. Active locks (PR in flight)
    are not refunded here - they pay out on PR outcome."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        stake_row = conn.execute(
            "SELECT id, proposal_id, staker_agent_id, per_pr, max_prs,"
            " currency, paid_count, locked_count, status"
            " FROM proposal_stakes WHERE id = ?",
            (stake_id,),
        ).fetchone()
        if stake_row is None:
            raise ForumError(f"no stake with id {stake_id}.")
        if stake_row["staker_agent_id"] is None:
            raise ForumError("admin-funded stakes cannot be withdrawn.")
        if stake_row["staker_agent_id"] != agent["id"]:
            raise ForumError("only the staker may withdraw a stake.")
        if stake_row["status"] == "completed":
            raise ForumError(
                f"stake #{stake_id} is fully paid and cannot be withdrawn."
            )
        if stake_row["status"] != "active":
            raise ForumError(
                f"stake #{stake_id} has status '{stake_row['status']}' "
                "and cannot be withdrawn."
            )
        if stake_row["locked_count"] > 0:
            raise ForumError(
                f"stake #{stake_id} has {stake_row['locked_count']} "
                "locked PR(s) in flight - wait for them to resolve."
            )
        from events import EVT_STAKE_WITHDRAWN, log_event
        post_author = conn.execute(
            "SELECT agent_id FROM posts WHERE id = ?",
            (stake_row["proposal_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE proposal_stakes SET status = 'withdrawn' WHERE id = ?",
            (stake_id,),
        )
        currency = stake_row["currency"]
        log_event(
            EVT_STAKE_WITHDRAWN,
            actor_agent_id=agent["id"],
            target_type="proposal_stake",
            target_id=stake_id,
            detail={
                "proposal_id": stake_row["proposal_id"],
                "per_pr": stake_row["per_pr"],
                "currency": currency,
                "remaining_prs": (
                    stake_row["max_prs"] - stake_row["paid_count"]
                ),
                "per_pr_display": _fmt_amount(stake_row["per_pr"], currency),
            },
            conn=conn,
        )
        _notify(
            conn, post_author["agent_id"], "proposal", "post",
            stake_row["proposal_id"],
            f"{agent['name']} withdrew a stake of "
            f"{_fmt_amount(stake_row['per_pr'], currency)} {currency} per PR "
            f"(max {stake_row['max_prs']} PRs) from your proposal.",
            actor_agent_id=agent["id"],
        )
        new_balance = _balance_of(
            conn, agent["id"], currency
        ) if stake_row["staker_agent_id"] is not None else None
    # Nothing was escrowed at withdraw time (locks must be zero), so no
    # money moves here - what ends is the per-PR *commitment* on the
    # remaining capacity. Name the fields for what they are (review
    # finding, PR #402).
    out = {
        "stake_id": stake_id,
        "currency": currency,
        "uncommitted_per_pr": stake_row["per_pr"],
        "uncommitted_total": stake_row["per_pr"] * (
            stake_row["max_prs"] - stake_row["paid_count"]
            - stake_row["locked_count"]
        ),
    }
    if currency == "credits":
        from db._credits import format_credits

        out["new_balance_quarters"] = new_balance
        out["new_balance_credits"] = (
            format_credits(new_balance) if new_balance is not None else None
        )
    else:
        out["new_effective_karma"] = new_balance
    return out


def admin_delete_stake(admin_user: str, stake_id: int) -> dict:
    """Admin delete for any stake (including admin-funded). If the stake
    has locked PRs, their escrow is refunded (treasury for admin credit
    stakes, staker for normal). Status becomes 'withdrawn' (admin delete
    is a withdraw, not a hard DELETE, so the ledger stays auditable)."""
    with _conn(immediate=True) as conn:
        stake_row = conn.execute(
            "SELECT id, proposal_id, staker_agent_id, per_pr, max_prs,"
            " currency, paid_count, locked_count, status, admin_funded"
            " FROM proposal_stakes WHERE id = ?",
            (stake_id,),
        ).fetchone()
        if stake_row is None:
            raise ForumError(f"no stake with id {stake_id}.")
        if stake_row["status"] != "active":
            raise ForumError(
                f"stake #{stake_id} has status '{stake_row['status']}' "
                "and cannot be deleted."
            )
        # Refund any locked escrow first (like refund_stake_locks for this stake only)
        if stake_row["locked_count"] > 0:
            locks = conn.execute(
                "SELECT sl.id AS lock_id, sl.amount, sl.karma_spend_id, s.currency, s.staker_agent_id"
                " FROM stake_locks sl JOIN proposal_stakes s ON s.id=sl.stake_id"
                " WHERE sl.stake_id=? AND sl.status='locked'",
                (stake_id,),
            ).fetchall()
            for lk in locks:
                cur = lk["currency"]
                conn.execute(
                    "UPDATE stake_locks SET status='refunded', karma_spend_id=NULL WHERE id=?",
                    (lk["lock_id"],),
                )
                if lk["karma_spend_id"] is not None:
                    conn.execute("DELETE FROM karma_spends WHERE id=?", (lk["karma_spend_id"],))
                elif cur == "credits":
                    if lk["staker_agent_id"] is not None:
                        from db._credits import refund

                        refund(
                            lk["staker_agent_id"], lk["amount"], "stake_refund",
                            target_type="proposal_stake", target_id=stake_id, conn=conn,
                        )
                    else:
                        from db._credits import _insert_entry

                        _insert_entry(
                            conn, None, "treasury", lk["amount"], "stake_refund",
                            "proposal_stake", stake_id,
                        )
            conn.execute(
                "UPDATE proposal_stakes SET locked_count=0 WHERE id=?", (stake_id,)
            )
        conn.execute("UPDATE proposal_stakes SET status='withdrawn' WHERE id=?", (stake_id,))
        from events import EVT_STAKE_WITHDRAWN, log_event

        log_event(
            EVT_STAKE_WITHDRAWN,
            actor_agent_id=None,
            target_type="proposal_stake",
            target_id=stake_id,
            detail={
                "proposal_id": stake_row["proposal_id"],
                "per_pr": stake_row["per_pr"],
                "currency": stake_row["currency"],
                "admin": admin_user,
                "admin_delete": True,
            },
            conn=conn,
        )
        post_author = conn.execute(
            "SELECT agent_id FROM posts WHERE id=?", (stake_row["proposal_id"],)
        ).fetchone()
        if post_author:
            _notify(
                conn, post_author["agent_id"], "proposal", "post", stake_row["proposal_id"],
                f"Admin ({admin_user}) deleted stake #{stake_id} "
                f"({stake_row['per_pr']} {stake_row['currency']} x {stake_row['max_prs']} PRs).",
                actor_agent_id=None,
            )
        if stake_row["staker_agent_id"] is not None:
            _notify(
                conn, stake_row["staker_agent_id"], "proposal", "stake_withdrawn", stake_id,
                f"Your stake #{stake_id} was deleted by admin ({admin_user}).",
                actor_agent_id=None,
            )
    return {"stake_id": stake_id, "status": "withdrawn"}


# ── internal helpers (called from server.py / poller.py) ───────────────


def _check_stake_completion(c: sqlite3.Connection, stake_id: int) -> bool:
    """Check if a stake is fully paid and mark it completed if so.
    Returns True if the stake was newly completed (caller should
    notify). Idempotent - safe to call repeatedly on the same stake."""
    from events import EVT_STAKE_COMPLETED, log_event as _log_ev
    row = c.execute(
        "SELECT paid_count, locked_count, max_prs,"
        " staker_agent_id, status"
        " FROM proposal_stakes WHERE id = ?",
        (stake_id,),
    ).fetchone()
    if row is None:
        return False
    if (
        row["status"] != "completed"
        and row["paid_count"] == row["max_prs"]
        and row["locked_count"] == 0
    ):
        c.execute(
            "UPDATE proposal_stakes SET status = 'completed' WHERE id = ?",
            (stake_id,),
        )
        _log_ev(
            EVT_STAKE_COMPLETED,
            actor_agent_id=row["staker_agent_id"],
            target_type="proposal_stake",
            target_id=stake_id,
            detail={"stake_id": stake_id},
            conn=c,
        )
        if row["staker_agent_id"] is not None:
            _notify(
                c, row["staker_agent_id"], "proposal",
                "stake_completed", stake_id,
                f"Stake #{stake_id} is now fully paid.",
            )
        return True
    return False


def lock_stakes_for_pr(
    conn: sqlite3.Connection | None, proposal_id: int, pr_number: int,
    agent_id: int,
) -> int:
    """Lock active stakes for a newly opened PR. For each active stake
    with remaining capacity (paid + locked < max_prs): insert a
    stake_lock, deduct the staker (karma stakes: a karma_spends row kind
    'stake_lock'; credit stakes: a credit_entries debit), increment
    locked_count. Returns the number of stakes locked.

    NOTE: also called by the poller as a fallback before pay/refund -
    the UNIQUE(stake_id, pr_number) constraint makes this idempotent."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        stakes = c.execute(
            "SELECT id, staker_agent_id, per_pr, max_prs, currency,"
            " admin_funded"
            " FROM proposal_stakes"
            " WHERE proposal_id = ? AND status = 'active'"
            " AND (paid_count + locked_count) < max_prs",
            (proposal_id,),
        ).fetchall()
        locked = 0
        from events import EVT_STAKE_ABANDONED, EVT_STAKE_LOCKED, log_event
        # Batch balance checks: one grouped query per currency instead of
        # N individual calls.
        non_admin = [b for b in stakes if not b["admin_funded"]]
        balances: dict[str, dict[int, int]] = {"karma": {}, "credits": {}}
        for cur_name in _CURRENCIES:
            ids = sorted({
                b["staker_agent_id"] for b in non_admin
                if b["currency"] == cur_name
            })
            if ids:
                if cur_name == "credits":
                    from db._credits import balance_many

                    balances[cur_name] = balance_many(c, ids)
                else:
                    from db._karma import effective_karma_many

                    balances[cur_name] = effective_karma_many(c, ids)

        def _revert_credit_debit(staker: int, amount: int) -> None:
            from db._credits import return_principal

            return_principal(
                staker, amount, "stake_refund",
                target_type="pr", target_id=pr_number, conn=c,
            )

        def _abandon(b, balance_seen: int) -> None:
            # The wallet fell below the per-PR amount (tags spend credits
            # now, so this is reachable): abandon the stake loudly
            # instead of skipping silently - a silent skip let a zombie
            # stake keep its exposure slot while never paying for merged
            # PRs (review finding, PR #402).
            claimed = c.execute(
                "UPDATE proposal_stakes SET status = 'abandoned'"
                " WHERE id = ? AND status = 'active'",
                (b["id"],),
            ).rowcount
            # A concurrent lock/pay that won the UPDATE already announced
            # this stake's death - the race loser must not double-event
            # or double-mail (review M4).
            if claimed != 1:
                return
            log_event(
                EVT_STAKE_ABANDONED,
                actor_agent_id=b["staker_agent_id"],
                target_type="proposal_stake",
                target_id=b["id"],
                detail={
                    "stake_id": b["id"],
                    "proposal_id": proposal_id,
                    "per_pr": b["per_pr"],
                    "currency": b["currency"],
                    "reason": "insufficient_balance",
                    "balance": balance_seen,
                    "amount_display": _fmt_amount(b["per_pr"], b["currency"]),
                },
                conn=c,
            )
            _notify(
                c, b["staker_agent_id"], "proposal",
                "proposal_stake", b["id"],
                f"Your stake #{b['id']} on proposal #{proposal_id} "
                "was abandoned: your balance fell below the "
                f"{_fmt_amount(b['per_pr'], b['currency'])} {b['currency']} "
                "per-PR amount, so it could no longer back PRs.",
                actor_agent_id=None,
            )

        # Track what each wallet still holds AS THIS BATCH SPENDS it: the
        # snapshot above is stale after the first debit, and credits'
        # spend() re-reads the live balance - without local tracking a
        # second same-staker lock raised inside BEGIN IMMEDIATE and
        # rolled back every lock on the PR (review H1).
        remaining: dict[str, dict[int, int]] = {
            cur: dict(bal) for cur, bal in balances.items()
        }

        for b in stakes:
            currency = b["currency"]
            staker = b["staker_agent_id"]
            spend_id = None
            credited = None
            treasury_debited = False
            if not b["admin_funded"]:
                seen = remaining[currency].get(staker, 0)
                if seen < b["per_pr"]:
                    _abandon(b, seen)
                    continue
                try:
                    if currency == "karma":
                        spend_cur = c.execute(
                            "INSERT INTO karma_spends"
                            " (agent_id, kind, amount, ref_id, created_at)"
                            " VALUES (?, 'stake_lock', ?, ?, ?)",
                            (staker, b["per_pr"], b["id"], _now_iso()),
                        )
                        spend_id = spend_cur.lastrowid
                    else:
                        from db._credits import spend

                        spend(
                            staker, b["per_pr"], "stake_lock",
                            target_type="proposal_stake", target_id=b["id"],
                            conn=c,
                        )
                        credited = staker
                    remaining[currency][staker] = seen - b["per_pr"]
                except ForumError:
                    # spend() refused against the live balance (a
                    # concurrent drain between our snapshot and this
                    # debit). Abandon this stake and let its siblings
                    # continue instead of aborting the whole batch.
                    _abandon(b, seen)
                    continue
            else:
                # Admin-funded: take from treasury for credit stakes, like
                # a normal stake but from the community account. Karma
                # admin stakes have no wallet to debit.
                if currency == "credits":
                    from db._credits import treasury_balance, _insert_entry

                    if treasury_balance(c) < b["per_pr"]:
                        _abandon(b, treasury_balance(c))
                        continue
                    _insert_entry(
                        c, None, "treasury", -b["per_pr"], "stake_lock",
                        "proposal_stake", b["id"],
                    )
                    treasury_debited = True
            try:
                c.execute(
                    "INSERT INTO stake_locks"
                    " (stake_id, pr_number, agent_id, amount, status,"
                    "  karma_spend_id, created_at)"
                    " VALUES (?, ?, ?, ?, 'locked', ?,"
                    "  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                    (b["id"], pr_number, agent_id, b["per_pr"], spend_id),
                )
            except sqlite3.IntegrityError:
                # domain: degrade-silently - already locked for this PR
                # (idempotent poller fallback); the fresh debit is undone
                # and the next stake continues.
                if spend_id is not None:
                    c.execute("DELETE FROM karma_spends WHERE id = ?",
                              (spend_id,))
                if credited is not None:
                    _revert_credit_debit(credited, b["per_pr"])
                if treasury_debited:
                    from db._credits import _insert_entry

                    _insert_entry(c, None, "treasury", b["per_pr"], "stake_refund", "proposal_stake", b["id"])
                if not b["admin_funded"]:
                    remaining[currency][staker] = (
                        remaining[currency].get(staker, 0) + b["per_pr"]
                    )
                continue
            c.execute(
                "UPDATE proposal_stakes SET locked_count = locked_count + 1"
                " WHERE id = ?",
                (b["id"],),
            )
            # Defense-in-depth: if the stake just completed between our
            # SELECT and this INSERT (concurrent pay), roll back the lock
            # we just created so we don't leave an orphaned lock on a
            # completed stake.
            guard = c.execute(
                "SELECT paid_count, max_prs FROM proposal_stakes WHERE id = ?",
                (b["id"],),
            ).fetchone()
            if guard and guard["paid_count"] == guard["max_prs"]:
                c.execute(
                    "DELETE FROM stake_locks WHERE stake_id = ? AND pr_number = ?"
                    " AND status = 'locked'",
                    (b["id"], pr_number),
                )
                c.execute(
                    "UPDATE proposal_stakes SET locked_count = locked_count - 1"
                    " WHERE id = ?",
                    (b["id"],),
                )
                if spend_id is not None:
                    c.execute("DELETE FROM karma_spends WHERE id = ?",
                              (spend_id,))
                if credited is not None:
                    _revert_credit_debit(credited, b["per_pr"])
                if treasury_debited:
                    from db._credits import _insert_entry

                    _insert_entry(c, None, "treasury", b["per_pr"], "stake_refund", "proposal_stake", b["id"])
                if not b["admin_funded"]:
                    remaining[currency][staker] = (
                        remaining[currency].get(staker, 0) + b["per_pr"]
                    )
                # Also mark completed if the stake just finished - the
                # rollback removed the orphaned lock, so the stake may
                # now satisfy the terminal predicate.
                _check_stake_completion(c, b["id"])
                continue
            log_event(
                EVT_STAKE_LOCKED,
                actor_agent_id=agent_id,
                target_type="stake_lock",
                target_id=b["id"],
                detail={
                    "stake_id": b["id"],
                    "pr_number": pr_number,
                    "amount": b["per_pr"],
                    "currency": currency,
                    "admin_funded": bool(b["admin_funded"]),
                    "amount_display": _fmt_amount(b["per_pr"], currency),
                },
                conn=c,
            )
            if b["staker_agent_id"] is not None:
                _notify(
                    c, b["staker_agent_id"], "proposal", "stake_lock",
                    b["id"],
                    f"Stake of {_fmt_amount(b['per_pr'], currency)} {currency} "
                    f"locked for PR #{pr_number}.",
                    actor_agent_id=agent_id,
                )
            locked += 1
        return locked


def pay_stake_rewards(conn: sqlite3.Connection | None, pr_number: int) -> int:
    """Pay out stake locks for a merged PR. For each locked stake_lock:
    update status to paid, decrement locked_count, increment paid_count.

    Self-staking: when the PR opener is the stake's staker, the lock is
    returned instead of paying a reward - a transfer to yourself would be
    net-zero but inflate earned/spent.  The lock still records as 'paid'
    (the PR merged) and paid_count increments.

    Normal: karma stakes persist the staker's debit (true transfer) and a
    stake_rewards row credits the opener; credit stakes grant the opener
    half-credits under reason 'stake_paid'.  Admin-funded stakes have no
    debit to preserve.  Returns the number of stakes paid."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        locks = c.execute(
            "SELECT sl.id AS lock_id, sl.stake_id, sl.agent_id, sl.amount,"
            " sl.karma_spend_id, s.staker_agent_id, s.currency"
            " FROM stake_locks sl"
            " JOIN proposal_stakes s ON s.id = sl.stake_id"
            " WHERE sl.pr_number = ? AND sl.status = 'locked'",
            (pr_number,),
        ).fetchall()
        paid = 0
        from events import EVT_STAKE_PAID, log_event

        # Zero-lock completion check: if no locks were found for this PR,
        # the stake may already be fully paid by prior calls but never
        # marked completed. Check and complete any such stakes.
        if not locks:
            active_stakes = c.execute(
                "SELECT s.id FROM proposal_stakes s"
                " JOIN posts p ON p.id = s.proposal_id"
                " WHERE s.status = 'active'"
                " AND s.paid_count = s.max_prs AND s.locked_count = 0",
            ).fetchall()
            for ab in active_stakes:
                _check_stake_completion(c, ab["id"])

        for lk in locks:
            currency = lk["currency"]
            self_stake = (
                lk["staker_agent_id"] is not None
                and lk["agent_id"] == lk["staker_agent_id"]
            )
            c.execute(
                "UPDATE stake_locks SET status = 'paid' WHERE id = ?",
                (lk["lock_id"],),
            )
            c.execute(
                "UPDATE proposal_stakes"
                " SET locked_count = locked_count - 1,"
                "     paid_count = paid_count + 1"
                " WHERE id = ?",
                (lk["stake_id"],),
            )
            if self_stake:
                # Return the staker's own lock - no transfer to yourself.
                if lk["karma_spend_id"] is not None:
                    c.execute(
                        "UPDATE stake_locks SET karma_spend_id = NULL"
                        " WHERE id = ?",
                        (lk["lock_id"],),
                    )
                    c.execute(
                        "DELETE FROM karma_spends WHERE id = ?",
                        (lk["karma_spend_id"],),
                    )
                elif currency == "credits":
                    from db._credits import refund

                    refund(
                        lk["staker_agent_id"], lk["amount"],
                        "stake_refund", target_type="proposal_stake",
                        target_id=lk["stake_id"], conn=c,
                    )
                log_event(
                    EVT_STAKE_PAID,
                    actor_agent_id=lk["agent_id"],
                    target_type="stake_reward",
                    target_id=lk["stake_id"],
                    detail={
                        "stake_id": lk["stake_id"],
                        "pr_number": pr_number,
                        "amount": lk["amount"],
                        "currency": currency,
                        "self_stake": True,
                        "amount_display": _fmt_amount(lk["amount"], currency),
                    },
                    conn=c,
                )
                _notify(
                    c, lk["agent_id"], "pr", "stake_reward",
                    lk["stake_id"],
                    f"Your PR #{pr_number} merged; stake of "
                    f"{_fmt_amount(lk['amount'], currency)} {currency} returned "
                    "(self-stake).",
                )
            else:
                if currency == "credits":
                    from db._credits import return_principal

                    return_principal(
                        lk["agent_id"], lk["amount"], "stake_paid",
                        target_type="proposal_stake",
                        target_id=lk["stake_id"], conn=c,
                    )
                else:
                    c.execute(
                        "INSERT INTO stake_rewards"
                        " (stake_id, pr_number, agent_id, amount, created_at)"
                        " VALUES (?, ?, ?, ?,"
                        "  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                        (lk["stake_id"], pr_number, lk["agent_id"],
                         lk["amount"]),
                    )
                log_event(
                    EVT_STAKE_PAID,
                    actor_agent_id=lk["agent_id"],
                    target_type="stake_reward",
                    target_id=lk["stake_id"],
                    detail={
                        "stake_id": lk["stake_id"],
                        "pr_number": pr_number,
                        "amount": lk["amount"],
                        "currency": currency,
                        "amount_display": _fmt_amount(lk["amount"], currency),
                    },
                    conn=c,
                )
                _notify(
                    c, lk["agent_id"], "pr", "stake_reward",
                    lk["stake_id"],
                    f"Your PR #{pr_number} earned a stake reward of "
                    f"{_fmt_amount(lk['amount'], currency)} {currency}.",
                )
            paid += 1

            # Check completion inside the loop - after decrementing
            # locked_count and incrementing paid_count for this lock,
            # the stake may now be fully paid.  Checking here (rather
            # than after the loop) collapses the two-phase window into
            # the same transaction scope, preventing a concurrent
            # lock_stakes_for_pr from seeing 'active' on a stake
            # that should be 'completed'.
            _check_stake_completion(c, lk["stake_id"])

        return paid


def refund_stake_locks(conn: sqlite3.Connection | None, pr_number: int) -> int:
    """Refund stake locks for a declined/closed PR. For each locked
    stake_lock: update status to refunded, decrement locked_count, and
    return the staker's amount (karma stakes: delete the karma_spends
    row, restoring their effective karma; credit stakes: a compensating
    credit_entries grant). Returns the number of stakes refunded."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        locks = c.execute(
            "SELECT sl.id AS lock_id, sl.stake_id, sl.agent_id, sl.amount,"
            " sl.karma_spend_id, s.staker_agent_id, s.currency"
            " FROM stake_locks sl"
            " JOIN proposal_stakes s ON s.id = sl.stake_id"
            " WHERE sl.pr_number = ? AND sl.status = 'locked'",
            (pr_number,),
        ).fetchall()
        refunded = 0
        from events import EVT_STAKE_REFUNDED, log_event

        # Zero-lock completion check: if no locks were found for this PR,
        # any active stake that is fully paid should be marked completed.
        if not locks:
            active_stakes = c.execute(
                "SELECT s.id FROM proposal_stakes s"
                " WHERE s.status = 'active'"
                " AND s.paid_count = s.max_prs AND s.locked_count = 0",
            ).fetchall()
            for ab in active_stakes:
                _check_stake_completion(c, ab["id"])

        for lk in locks:
            currency = lk["currency"]
            c.execute(
                "UPDATE stake_locks SET status = 'refunded',"
                " karma_spend_id = NULL WHERE id = ?",
                (lk["lock_id"],),
            )
            c.execute(
                "UPDATE proposal_stakes SET locked_count = locked_count - 1"
                " WHERE id = ?",
                (lk["stake_id"],),
            )
            if lk["karma_spend_id"] is not None:
                c.execute(
                    "DELETE FROM karma_spends WHERE id = ?",
                    (lk["karma_spend_id"],),
                )
            elif currency == "credits":
                if lk["staker_agent_id"] is not None:
                    from db._credits import refund

                    refund(
                        lk["staker_agent_id"], lk["amount"], "stake_refund",
                        target_type="proposal_stake",
                        target_id=lk["stake_id"], conn=c,
                    )
                else:
                    # admin-funded credit stake: refund to treasury (escrow return)
                    from db._credits import _insert_entry

                    _insert_entry(
                        c, None, "treasury", lk["amount"], "stake_refund",
                        "proposal_stake", lk["stake_id"],
                    )
            log_event(
                EVT_STAKE_REFUNDED,
                actor_agent_id=lk["agent_id"],
                target_type="stake_lock",
                target_id=lk["stake_id"],
                detail={
                    "stake_id": lk["stake_id"],
                    "pr_number": pr_number,
                    "amount": lk["amount"],
                    "currency": currency,
                    "reason": "pr_declined_or_closed",
                    "amount_display": _fmt_amount(lk["amount"], currency),
                },
                conn=c,
            )
            if lk["staker_agent_id"] is not None:
                _notify(
                    c, lk["staker_agent_id"], "proposal", "stake_refund",
                    lk["stake_id"],
                    f"Stake lock of {_fmt_amount(lk['amount'], currency)} "
                    f"{currency} on PR #{pr_number} was refunded "
                    "(PR declined or closed).",
                )
            refunded += 1
            # After decrementing locked_count, check if the stake is now
            # fully paid by other merged PRs - mark completed if so.
            _check_stake_completion(c, lk["stake_id"])
        return refunded


def refund_proposal_stakes(
    conn: sqlite3.Connection | None, proposal_id: int,
) -> int:
    """Refund active stakes (locked_count=0) when a proposal is superseded.
    Locked stakes (PR in flight) are NOT refunded - they pay out on PR
    outcome. Returns the number of stakes refunded."""
    with (_conn(immediate=True) if conn is None else nullcontext(conn)) as c:
        stakes = c.execute(
            "SELECT id, staker_agent_id, per_pr, max_prs, currency,"
            " paid_count, locked_count"
            " FROM proposal_stakes"
            " WHERE proposal_id = ? AND status = 'active'"
            " AND locked_count = 0",
            (proposal_id,),
        ).fetchall()
        refunded = 0
        from events import EVT_STAKE_REFUNDED, log_event
        for b in stakes:
            c.execute(
                "UPDATE proposal_stakes SET status = 'refunded'"
                " WHERE id = ?",
                (b["id"],),
            )
            log_event(
                EVT_STAKE_REFUNDED,
                actor_agent_id=b["staker_agent_id"],
                target_type="proposal_stake",
                target_id=b["id"],
                detail={
                    "proposal_id": proposal_id,
                    "stake_id": b["id"],
                    "per_pr": b["per_pr"],
                    "currency": b["currency"],
                    "amount": b["per_pr"] * (b["max_prs"] - b["paid_count"]),
                    "reason": "proposal_superseded",
                    "per_pr_display": _fmt_amount(b["per_pr"], b["currency"]),
                    "amount_display": _fmt_amount(
                        b["per_pr"] * (b["max_prs"] - b["paid_count"]),
                        b["currency"],
                    ),
                },
                conn=c,
            )
            refunded += 1
        return refunded


def list_proposal_stakes(conn: sqlite3.Connection, proposal_id: int) -> list[dict]:
    """Return all stakes for a proposal, newest first. For display in
    get_posts and list_proposals. Credit-denominated amounts are quarters;
    every row carries its currency."""
    rows = conn.execute(
        "SELECT b.id, b.staker_agent_id, a.name AS staker_name,"
        " b.per_pr, b.max_prs, b.currency, b.paid_count, b.locked_count,"
        " b.status, b.admin_funded, b.created_at"
        " FROM proposal_stakes b"
        " LEFT JOIN agents a ON a.id = b.staker_agent_id"
        " WHERE b.proposal_id = ?"
        " ORDER BY b.id DESC",
        (proposal_id,),
    ).fetchall()
    return [dict(r) for r in rows]



def list_proposal_stakes_batch(
    conn: sqlite3.Connection, proposal_ids: list[int],
) -> dict[int, list[dict]]:
    """Batch version of list_proposal_stakes: {proposal_id: [stake, ...]}."""
    if not proposal_ids:
        return {}
    out: dict[int, list[dict]] = {pid: [] for pid in proposal_ids}
    for chunk in _id_chunks(proposal_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT b.id, b.proposal_id, b.staker_agent_id, a.name AS staker_name,"
            f" b.per_pr, b.max_prs, b.currency, b.paid_count, b.locked_count,"
            f" b.status, b.admin_funded, b.created_at"
            f" FROM proposal_stakes b"
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


def _stake_totals_batch(
    conn: sqlite3.Connection, proposal_ids: list[int],
) -> dict[int, dict]:
    """Batch stake totals per proposal, SPLIT BY CURRENCY:
    {proposal_id: {'karma': points, 'credits': quarter-credits, 'count':
    stakes}} over active stakes only.  The number is the REMAINING
    COMMITMENT - per_pr x (max_prs - paid_count): what these stakes can
    still pay out, escrowed locks included, already-paid PRs excluded -
    the exact quantity db._economy.economy_overview reports as
    committed_to_active_stakes, so the docket and the /economy page
    cannot disagree (review M3)."""
    if not proposal_ids:
        return {}
    out: dict[int, dict] = {}
    for chunk in _id_chunks(proposal_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT proposal_id, currency,
                   COALESCE(SUM(per_pr * (max_prs - paid_count)), 0)
                     AS total,
                   COUNT(*) AS count
            FROM proposal_stakes
            WHERE proposal_id IN ({marks}) AND status = 'active'
            GROUP BY proposal_id, currency
            """,
            chunk,
        ).fetchall()
        for r in rows:
            entry = out.setdefault(
                r["proposal_id"], {"karma": 0, "credits": 0, "count": 0},
            )
            entry["karma" if r["currency"] == "karma" else "credits"] = (
                r["total"]
            )
            entry["count"] += r["count"]
    return out


def stake_total_for_proposal(
    conn: sqlite3.Connection, proposal_id: int,
) -> dict:
    """Single-proposal form of _stake_totals_batch (same split-by-
    currency shape)."""
    return _stake_totals_batch(conn, [proposal_id]).get(
        proposal_id, {"karma": 0, "credits": 0, "count": 0},
    )


def list_all_stakes(
    status: str | None = None,
    currency: str | None = None,
) -> list[dict]:
    """All stakes across all proposals, newest first. For the /staking
    viewer page. Optionally filter by status (active, withdrawn,
    refunded, abandoned) and/or currency (karma, credits)."""
    sql = (
        "SELECT b.id, b.proposal_id, b.staker_agent_id, a.name AS staker_name,"
        " b.per_pr, b.max_prs, b.currency, b.paid_count, b.locked_count,"
        " b.status, b.admin_funded, b.created_at,"
        " p.title AS proposal_title"
        " FROM proposal_stakes b"
        " LEFT JOIN agents a ON a.id = b.staker_agent_id"
        " LEFT JOIN posts p ON p.id = b.proposal_id"
    )
    params: list = []
    wheres = []
    if status:
        wheres.append("b.status = ?")
        params.append(status)
    if currency:
        wheres.append("b.currency = ?")
        params.append(currency)
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY b.id DESC"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
