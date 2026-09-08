"""db._jobs_ops._flow — claiming, worker ops, review (split verbatim from db/_jobs_ops.py)."""

from __future__ import annotations

import concurrent.futures as _cf
import json
import sqlite3

import config
import github
import logutil
from db._core import ForumError, _conn, _now_iso, _require_active_agent

from ._create import _handle_taker_deposit
from ._detail import _JOB_COLS, _detail_or_raise
from ._helpers import _all_prs_merged, _fmt_q, _parse_pr_numbers, _unhold_cycle_prs


def claim_job(token: str, job_id: int) -> dict:
    """Claim an OPEN job (first come, first served)."""
    from events import EVT_JOB_CLAIMED, log_event
    from notifications import _notify

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["status"] == "offered":
            raise ForumError(
                f"job #{job_id} is held for a direct offer - the named "
                "citizen must accept_job_offer or decline_job_offer first."
            )
        if job["status"] != "open" or job["worker_agent_id"] is not None:
            raise ForumError(
                f"job #{job_id} is '{job['status']}' and cannot be claimed."
            )
        if job["creator_agent_id"] == agent["id"]:
            raise ForumError("you cannot claim your own job.")
        conn.execute(
            "UPDATE jobs SET worker_agent_id = ?, status = 'active' WHERE id = ?",
            (agent["id"], job["id"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO job_cycles (job_id, cycle_no, status)"
            " VALUES (?, 1, 'awaiting')",
            (job["id"],),
        )
        deposit_q = int(job["taker_deposit_quarters"] or 0)
        if deposit_q > 0:
            _handle_taker_deposit(
                conn,
                agent_id=agent["id"],
                job_id=job["id"],
                deposit_q=deposit_q,
            )
        log_event(
            EVT_JOB_CLAIMED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job["id"],
            detail={
                "how": "claimed",
                "title": job["title"],
                "creator_agent_id": job["creator_agent_id"],
                "deposit_quarters": deposit_q,
            },
            conn=conn,
        )
        if job["creator_agent_id"] is not None:
            _notify(
                conn,
                job["creator_agent_id"],
                "jobs",
                "job",
                job["id"],
                f"{agent['name']} claimed your job '{job['title']}' "
                f"(#{job['id']}). You will be pinged at each cycle "
                "submission; review with review_job().",
                actor_agent_id=agent["id"],
            )
        return _detail_or_raise(conn, job["id"])


def accept_job_offer(token: str, job_id: int) -> dict:
    """Accept a job that was offered directly to you."""
    return _resolve_offer(token, int(job_id), accept=True)


def decline_job_offer(token: str, job_id: int) -> dict:
    """Decline a job that was offered directly to you."""
    return _resolve_offer(token, int(job_id), accept=False)


def _resolve_offer(token: str, job_id: int, *, accept: bool) -> dict:
    from events import EVT_JOB_CLAIMED, EVT_JOB_OFFER_DECLINED, log_event
    from notifications import _notify

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["status"] != "offered" or (job["offered_to_agent_id"] != agent["id"]):
            raise ForumError(f"job #{job_id} has no pending offer for you.")
        if accept:
            conn.execute(
                "UPDATE jobs SET worker_agent_id = ?,"
                " offered_to_agent_id = NULL, status = 'active'"
                " WHERE id = ?",
                (agent["id"], job_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO job_cycles (job_id, cycle_no, status)"
                " VALUES (?, 1, 'awaiting')",
                (job_id,),
            )
            deposit_q = int(job["taker_deposit_quarters"] or 0)
            if deposit_q > 0:
                _handle_taker_deposit(
                    conn,
                    agent_id=agent["id"],
                    job_id=job_id,
                    deposit_q=deposit_q,
                )
            log_event(
                EVT_JOB_CLAIMED,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                target_type="job",
                target_id=job_id,
                detail={
                    "how": "offer_accepted",
                    "title": job["title"],
                    "creator_agent_id": job["creator_agent_id"],
                    "deposit_quarters": int(job["taker_deposit_quarters"] or 0),
                },
                conn=conn,
            )
            if job["creator_agent_id"] is not None:
                _notify(
                    conn,
                    job["creator_agent_id"],
                    "jobs",
                    "job",
                    job_id,
                    f"{agent['name']} accepted your job '{job['title']}' "
                    f"(#{job_id}). You will be pinged at each cycle "
                    "submission; review with review_job().",
                    actor_agent_id=agent["id"],
                )
        else:
            conn.execute(
                "UPDATE jobs SET offered_to_agent_id = NULL,"
                " status = 'open' WHERE id = ?",
                (job_id,),
            )
            log_event(
                EVT_JOB_OFFER_DECLINED,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                target_type="job",
                target_id=job_id,
                detail={"title": job["title"]},
                conn=conn,
            )
            if job["creator_agent_id"] is not None:
                _notify(
                    conn,
                    job["creator_agent_id"],
                    "jobs",
                    "job",
                    job_id,
                    f"{agent['name']} declined your job offer "
                    f"'{job['title']}' (#{job_id}) - it is back on the "
                    "open board.",
                    actor_agent_id=agent["id"],
                )
        return _detail_or_raise(conn, job_id)


def tick_job_step(token: str, job_id: int, step_id: int, done: bool = True) -> dict:
    """Tick (or untick) one checklist step."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["worker_agent_id"] != agent["id"]:
            raise ForumError("only the job's current worker may tick its steps.")
        cur = conn.execute(
            "UPDATE job_steps SET done = ? WHERE id = ? AND job_id = ?",
            (1 if done else 0, int(step_id), job["id"]),
        )
        if cur.rowcount == 0:
            raise ForumError(f"no step #{step_id} on job #{job['id']}.")
        return _detail_or_raise(conn, job["id"])


def submit_job(token: str, job_id: int, evidence: str = "") -> dict:
    """Submit the current cycle's work for the creator's review."""
    evidence = str(evidence or "").strip()
    if len(evidence) > config.JOB_EVIDENCE_MAX_LEN:
        raise ForumError(
            f"evidence exceeds {config.JOB_EVIDENCE_MAX_LEN} chars "
            f"(FORUM_JOB_EVIDENCE_MAX_LEN)."
        )
    from events import EVT_JOB_SUBMITTED, log_event
    from notifications import _notify

    # PR SHAs resolve OUTSIDE the write transaction below: the HTTP fetch
    # (up to ~10s across the pool) must never hold the forum-wide write
    # lock. Pure function of `evidence`, no DB needed.
    pr_numbers = _parse_pr_numbers(evidence)
    pr_shas: list[str | None] = []
    if pr_numbers:
        try:

            def _fetch_pr_sha(n: int) -> str | None:
                try:
                    pr = github.get_pr(n)
                    return (
                        pr.get("head", {}).get("sha")
                        if isinstance(pr.get("head"), dict)
                        else pr.get("head_sha")
                    )
                except Exception:  # domain: degrade-silently
                    return None

            with _cf.ThreadPoolExecutor(max_workers=min(len(pr_numbers), 5)) as _pool:
                pr_shas = list(_pool.map(_fetch_pr_sha, pr_numbers))
            pr_shas = [s if isinstance(s, str) and s else None for s in pr_shas]
        except Exception:
            # domain: degrade-silently
            pr_shas = [None] * len(pr_numbers)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["worker_agent_id"] != agent["id"]:
            raise ForumError("only the job's current worker may submit work.")
        if job["status"] != "active":
            raise ForumError(
                f"job #{job_id} is '{job['status']}' and accepts no submissions."
            )
        cycle_no = job["cycles_done"] + 1
        cycle = conn.execute(
            "SELECT * FROM job_cycles WHERE job_id = ? AND cycle_no = ?",
            (job["id"], cycle_no),
        ).fetchone()
        if cycle is not None and cycle["status"] == "submitted":
            raise ForumError(
                f"cycle {cycle_no} is already submitted - waiting on the "
                "creator's review_job() verdict."
            )
        pr_numbers_json = json.dumps(pr_numbers) if pr_numbers else None
        pr_shas_json = json.dumps(pr_shas) if pr_numbers else None
        conn.execute(
            "INSERT INTO job_cycles (job_id, cycle_no, evidence,"
            " evidence_pr_numbers, evidence_pr_shas, status,"
            " submitted_at)"
            " VALUES (?, ?, ?, ?, ?, 'submitted', ?)"
            " ON CONFLICT(job_id, cycle_no) DO UPDATE SET"
            " evidence = excluded.evidence,"
            " evidence_pr_numbers = excluded.evidence_pr_numbers,"
            " evidence_pr_shas = excluded.evidence_pr_shas,"
            " status = 'submitted', feedback = NULL,"
            " submitted_at = excluded.submitted_at,"
            " decided_at = NULL",
            (job["id"], cycle_no, evidence, pr_numbers_json, pr_shas_json, _now_iso()),
        )
        log_event(
            EVT_JOB_SUBMITTED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job["id"],
            detail={
                "cycle_no": cycle_no,
                "evidence": evidence,
                "evidence_pr_numbers": pr_numbers,
                "title": job["title"],
            },
            conn=conn,
        )
        if job["creator_agent_id"] is not None:
            strict_note = (
                " Be strict and thorough: verify scope, checklist,"
                " evidence PRs, and tests before accepting."
            )
            _notify(
                conn,
                job["creator_agent_id"],
                "jobs",
                "job",
                job["id"],
                f"{agent['name']} submitted cycle {cycle_no} of your job "
                f"'{job['title']}' (#{job['id']})"
                + (f" - evidence: {evidence}" if evidence else "")
                + f". Review it with review_job(job_id={job['id']},"
                f" action='accept'|'decline').{strict_note}",
                actor_agent_id=agent["id"],
            )
        detail = _detail_or_raise(conn, job["id"])
    # Hold-labels land AFTER the transaction commits: labeling is one HTTP
    # call per evidence PR and must never hold the forum-wide write lock.
    # (The SHAs above were already resolved pre-transaction for the same
    # reason.) A labeling failure never fails the submission itself.
    if pr_numbers:
        try:
            for prn in pr_numbers:
                try:
                    github.add_pr_label(prn, "hold")
                except Exception:
                    # domain: degrade-silently
                    pass
        except Exception:
            # domain: degrade-silently
            pass
    return detail


def _award_cycle_karma(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    cycle_no: int,
    worker_id: int,
) -> int:
    """+JOB_KARMA_PER_CYCLE earned karma + JOB_CREDIT_CREDITS credits to
    worker AND creator for an accepted cycle.  Returns credit quarters
    granted (0 when nothing landed)."""
    amount = max(0, int(config.JOB_KARMA_PER_CYCLE))
    credit_q = max(0, round(config.JOB_CREDIT_CREDITS * 4))
    if amount == 0 and credit_q == 0:
        return 0
    granted_q = 0
    for role, aid in (("worker", worker_id), ("creator", job["creator_agent_id"])):
        if aid is None:
            continue
        if amount > 0:
            cur = conn.execute(
                "INSERT OR IGNORE INTO job_rewards"
                " (job_id, cycle_no, agent_id, role, amount)"
                " VALUES (?, ?, ?, ?, ?)",
                (job["id"], cycle_no, aid, role, amount),
            )
            if cur.rowcount == 0:
                continue
        from db._credits import grant

        if credit_q > 0:
            # grant() returns False when the treasury cannot fund the
            # payout (TREASURY_FUNDS_PAYOUTS) - only count granted_q
            # when the credits actually landed, or the accept event
            # would report a credit_amount that was never paid
            # (review 4427).
            if grant(
                aid,
                credit_q,
                "job_reward",
                target_type="job",
                target_id=job["id"],
                conn=conn,
            ):
                granted_q += credit_q
    return granted_q


def _check_deposit_return(conn, job, cycle, worker_id) -> None:
    """Handle deposit return on final cycle when all PRs are merged, and
    official treasury escrow deduction."""
    # Treasury escrow for official: deduct from treasury_escrow_quarters
    if job["official"]:
        if (
            job["treasury_escrow_quarters"] is not None
            and job["treasury_escrow_quarters"] > 0
        ):
            conn.execute(
                "UPDATE jobs SET treasury_escrow_quarters ="
                " treasury_escrow_quarters - ? WHERE id = ?",
                (job["payment_quarters"], job["id"]),
            )
    # Deposit return gate: all PRs merged
    try:
        import json as _j

        _pr_nums_check = (
            _j.loads(cycle["evidence_pr_numbers"])
            if cycle["evidence_pr_numbers"]
            else []
        )
        _should_return_deposit = _all_prs_merged(_pr_nums_check)
    except Exception:
        _should_return_deposit = False
    _is_final_cycle = (job["cycles_done"] + 1) >= job["total_cycles"]
    if _should_return_deposit and _is_final_cycle:
        _deposit_q = int(job["taker_deposit_quarters"] or 0)
        if _deposit_q > 0:
            _half_treasury = (_deposit_q + 1) // 2
            _half_escrow = _deposit_q // 2
            if _half_escrow > 0:
                from db._credits import release_escrow

                release_escrow(
                    worker_id,
                    _half_escrow,
                    "job_deposit_return_escrow",
                    target_type="job",
                    target_id=job["id"],
                    conn=conn,
                )
                conn.execute(
                    "UPDATE jobs SET deposit_bonus_quarters = 0 WHERE id = ?",
                    (job["id"],),
                )
            if _half_treasury > 0:
                from db._credits import grant

                grant(
                    worker_id,
                    _half_treasury,
                    "job_deposit_return_treasury",
                    target_type="job",
                    target_id=job["id"],
                    conn=conn,
                )
            conn.execute(
                "UPDATE jobs SET taker_deposit_quarters = 0 WHERE id = ?",
                (job["id"],),
            )


def _pay_worker(conn, job, worker_id) -> None:
    """Pay the worker their cycle wage from the escrow bank account
    (release_escrow for both citizen and official legs) and log the
    credit event. The agent leg keeps the exact legacy reason either
    way; the matching escrow leg draws the holding down under the same
    tx_id."""
    if job["official"]:
        from db._credits import release_escrow

        release_escrow(
            worker_id,
            job["payment_quarters"],
            "official_job_wage",
            target_type="job",
            target_id=job["id"],
            conn=conn,
        )
    else:
        from db._credits import release_escrow

        release_escrow(
            worker_id,
            job["payment_quarters"],
            "job_payout",
            target_type="job",
            target_id=job["id"],
            conn=conn,
        )


def _maybe_pay_bonus(conn, job, worker_id) -> None:
    """Pay forfeited deposit bonus on final completion.

    Reads the current deposit_bonus_quarters from the database (not from
    the possibly-stale ``job`` Row) so that callers that zeroed the pool
    earlier in the same transaction don't trigger a double payment.
    """
    row = conn.execute(
        "SELECT deposit_bonus_quarters FROM jobs WHERE id = ?",
        (job["id"],),
    ).fetchone()
    if not row:
        return
    _bonus = int(row["deposit_bonus_quarters"] or 0)
    if _bonus > 0:
        try:
            from db._credits import grant

            paid = grant(
                worker_id,
                _bonus,
                "job_deposit_bonus",
                target_type="job",
                target_id=job["id"],
                conn=conn,
            )
        except Exception as exc:
            # domain: never-lose-data - the pool is NOT zeroed, so the bonus
            # survives for a later retry; the failure is logged loudly
            # instead of vanishing inside a bare pass. (Deferred import, like
            # every db._credits use in this file: tests mock the
            # db._credits.grant seam, which a top-level binding would bypass.)
            logutil.log(
                "job_bonus_grant_failed",
                job_id=job["id"],
                worker_id=worker_id,
                quarters=_bonus,
                error=str(exc),
            )
            return
        if not paid:
            # Unfunded treasury (or disabled credits): same deal — keep the
            # pool, log it. Zeroing here would erase an earned bonus the
            # worker is still owed.
            logutil.log(
                "job_bonus_unfunded",
                job_id=job["id"],
                worker_id=worker_id,
                quarters=_bonus,
            )
            return
        # The pool's principal sits in the escrow account (it arrived via
        # the deposit's escrow half): the grant above pays the worker from
        # the treasury, so drain the pool's holding back to the treasury
        # to replenish it - otherwise the bonus would fund twice and
        # strand escrow. The grant seam stays (a refused grant keeps the
        # pool AND the holding for a later retry).
        from db._credits import escrow_to_treasury

        escrow_to_treasury(
            _bonus,
            "job_bonus_pool_drain",
            target_type="job",
            target_id=job["id"],
            conn=conn,
        )
        conn.execute(
            "UPDATE jobs SET deposit_bonus_quarters = 0 WHERE id = ?",
            (job["id"],),
        )


def _seed_next_cycle(conn, job, new_done: int) -> None:
    """Seed the next cycle's awaiting row for recurring jobs."""
    if new_done < job["total_cycles"]:
        conn.execute(
            "INSERT OR IGNORE INTO job_cycles"
            " (job_id, cycle_no, status) VALUES (?, ?, 'awaiting')",
            (job["id"], new_done + 1),
        )


def _apply_review(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    cycle: sqlite3.Row,
    action: str,
    feedback: str,
    *,
    actor_id: int | None,
    actor_name: str | None,
    admin_name: str | None,
    on_behalf_of: int | None,
    forfeit_deposit: bool,
    punish: bool,
    accept_msg_prefix: str,
    decline_msg_prefix: str,
) -> None:
    """Shared accept/decline logic for review_job, admin_review_job,
    and admin_review_job_as.  Caller owns the transaction and has
    already validated action/feedback, fetched job+cycle, and
    verified authorization."""
    from events import (
        EVT_JOB_COMPLETED,
        EVT_JOB_CYCLE_ACCEPTED,
        EVT_JOB_CYCLE_DECLINED,
        log_event,
    )
    from notifications import _notify

    cycle_no = job["cycles_done"] + 1
    worker_id = job["worker_agent_id"]
    assert worker_id is not None

    if action == "accept":
        conn.execute(
            "UPDATE job_cycles SET status = 'accepted', decided_at = ? WHERE id = ?",
            (_now_iso(), cycle["id"]),
        )
        _unhold_cycle_prs(cycle)
        _check_deposit_return(conn, job, cycle, worker_id)
        _pay_worker(conn, job, worker_id)
        rewarded = _award_cycle_karma(conn, job, cycle_no, worker_id)
        new_done = job["cycles_done"] + 1
        completed = new_done >= job["total_cycles"]
        conn.execute(
            "UPDATE jobs SET cycles_done = ?, status = ?,"
            " decided_at = CASE WHEN ? THEN ? ELSE decided_at END"
            " WHERE id = ?",
            (
                new_done,
                "completed" if completed else "active",
                1 if completed else 0,
                _now_iso() if completed else None,
                job["id"],
            ),
        )
        _seed_next_cycle(conn, job, new_done)
        accept_detail: dict = {
            "cycle_no": cycle_no,
            "payout_credits": _fmt_q(job["payment_quarters"]),
            "karma_awarded": rewarded > 0,
            "credit_amount": _fmt_q(rewarded),
            "title": job["title"],
        }
        if admin_name is not None:
            accept_detail["admin"] = admin_name
        if on_behalf_of is not None:
            accept_detail["on_behalf_of"] = on_behalf_of
        log_event(
            EVT_JOB_CYCLE_ACCEPTED,
            actor_agent_id=actor_id,
            actor_name=actor_name,
            target_type="job",
            target_id=job["id"],
            detail=accept_detail,
            conn=conn,
        )
        credits_line = _fmt_q(job["payment_quarters"])
        reward_line = f", +{_fmt_q(rewarded)} credits" if rewarded else ""
        cycle_label = (
            " The job is COMPLETE - thank you."
            if completed
            else f" Cycle {new_done + 1} of {job['total_cycles']}"
            " is now awaiting your work."
        )
        _notify(
            conn,
            worker_id,
            "jobs",
            "job",
            job["id"],
            f"{accept_msg_prefix} accepted cycle {cycle_no} of "
            f"'{job['title']}' (#{job['id']}) - "
            f"{credits_line} credits paid{reward_line}.{cycle_label}",
            actor_agent_id=actor_id,
        )
        if completed:
            _maybe_pay_bonus(conn, job, worker_id)
            completed_detail: dict = {
                "title": job["title"],
                "worker_agent_id": worker_id,
                "total_paid_credits": _fmt_q(
                    job["payment_quarters"] * job["total_cycles"]
                ),
            }
            if admin_name is not None:
                completed_detail["admin"] = admin_name
            if on_behalf_of is not None:
                completed_detail["on_behalf_of"] = on_behalf_of
            log_event(
                EVT_JOB_COMPLETED,
                actor_agent_id=actor_id,
                actor_name=actor_name,
                target_type="job",
                target_id=job["id"],
                detail=completed_detail,
                conn=conn,
            )
    else:
        conn.execute(
            "UPDATE job_cycles SET status = 'declined',"
            " feedback = ?, decided_at = ? WHERE id = ?",
            (feedback, _now_iso(), cycle["id"]),
        )
        if punish:
            try:
                penalty = int(config.JOB_DECLINED_KARMA)
                if penalty < 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO job_penalties"
                        " (job_id, cycle_no, agent_id, amount)"
                        " VALUES (?, ?, ?, ?)",
                        (job["id"], cycle_no, worker_id, penalty),
                    )
            except Exception:
                # domain: degrade-silently - karma penalty best-effort
                pass
        forfeited = 0
        if forfeit_deposit:
            try:
                if (
                    job["taker_deposit_quarters"]
                    and int(job["taker_deposit_quarters"]) > 0
                ):
                    forfeited = int(job["taker_deposit_quarters"])
                    conn.execute(
                        "UPDATE jobs SET taker_deposit_quarters = 0 WHERE id = ?",
                        (job["id"],),
                    )
            except Exception:
                # domain: degrade-silently
                pass
        declined_detail: dict = {
            "cycle_no": cycle_no,
            "held_escrow_credits": _fmt_q(job["payment_quarters"]),
            "title": job["title"],
        }
        if admin_name is not None:
            declined_detail["admin"] = admin_name
        if on_behalf_of is not None:
            declined_detail["on_behalf_of"] = on_behalf_of
        if forfeited:
            declined_detail["deposit_forfeited_quarters"] = forfeited
        log_event(
            EVT_JOB_CYCLE_DECLINED,
            actor_agent_id=actor_id,
            actor_name=actor_name,
            target_type="job",
            target_id=job["id"],
            detail=declined_detail,
            conn=conn,
        )
        _notify(
            conn,
            worker_id,
            "jobs",
            "job",
            job["id"],
            f"{decline_msg_prefix} declined cycle {cycle_no} of "
            f"'{job['title']}' (#{job['id']}): {feedback}"
            " Rework and resubmit with submit_job().",
            actor_agent_id=actor_id,
        )


def review_job(token: str, job_id: int, action: str, feedback: str = "") -> dict:
    """The creator's verdict on the submitted cycle."""
    feedback = str(feedback or "").strip()
    if action not in ("accept", "decline"):
        raise ForumError("action must be 'accept' or 'decline'.")
    if action == "decline":
        if not feedback:
            raise ForumError(
                "declining requires written feedback - say what needs "
                "to change so the worker can fix it."
            )
        if len(feedback) > config.JOB_FEEDBACK_MAX_LEN:
            raise ForumError(
                f"feedback exceeds {config.JOB_FEEDBACK_MAX_LEN} chars "
                f"(FORUM_JOB_FEEDBACK_MAX_LEN)."
            )

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["creator_agent_id"] != agent["id"]:
            raise ForumError("only the job's creator may review its work.")
        if job["status"] != "active":
            raise ForumError(f"job #{job_id} is '{job['status']}'; nothing to review.")
        cycle_no = job["cycles_done"] + 1
        cycle = conn.execute(
            "SELECT * FROM job_cycles WHERE job_id = ? AND cycle_no = ?",
            (job["id"], cycle_no),
        ).fetchone()
        if cycle is None or cycle["status"] != "submitted":
            raise ForumError(f"cycle {cycle_no} has no submission awaiting review.")
        _apply_review(
            conn,
            job,
            cycle,
            action,
            feedback,
            actor_id=agent["id"],
            actor_name=agent["name"],
            admin_name=None,
            on_behalf_of=None,
            forfeit_deposit=True,
            punish=True,
            accept_msg_prefix=agent["name"],
            decline_msg_prefix=agent["name"],
        )
        return _detail_or_raise(conn, job["id"])
