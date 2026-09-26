"""db._jobs_ops._flow — claiming, worker ops, review (split verbatim from db/_jobs_ops.py)."""

from __future__ import annotations

import concurrent.futures as _cf
import json
import sqlite3

import config
import github
import logutil
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._credits import UNITS_PER_CREDIT

from ._create import _handle_taker_deposit, _resolve_citizen
from ._detail import _JOB_COLS, _detail_or_raise
from ._helpers import (
    _all_prs_merged,
    _fmt_q,
    _parse_pr_numbers,
    _unhold_cycle_prs,
    job_cycle_opens_at,
)


def claim_job(token: str, job_id: int, guild_id: int | None = None) -> dict:
    """Claim an OPEN job (first come, first served). guild_id (proposal
    #525) takes the job as a guild executor: the claimer must be a
    member, the wage routes poolward on accept while worker karma +
    reward stay personal, and leaving detaches the job back to personal.
    Taker deposits (when set) still come from the claimer's own wallet."""
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
                "citizen must decide_job_offer (action='accept'/'decline') first."
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
        if guild_id is not None:
            from db._guilds import _require_guild, _require_member
            from db._guilds_money import link_taken_job

            _require_guild(conn, int(guild_id))
            _require_member(conn, int(guild_id), agent["id"])
            link_taken_job(conn, job["id"], int(guild_id), agent["id"])
        conn.execute(
            "INSERT OR IGNORE INTO job_cycles (job_id, cycle_no, status)"
            " VALUES (?, 1, 'awaiting')",
            (job["id"],),
        )
        deposit_q = int(job["taker_deposit_units"] or 0)
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
                "deposit_units": deposit_q,
            },
            conn=conn,
        )
        if job["creator_agent_id"] is not None:
            body = (
                f"{agent['name']} claimed your job '{job['title']}' "
                f"(#{job['id']}). You will be pinged at each cycle "
                "submission; review with review_job()."
            )
            if deposit_q > 0:
                body += (
                    f" {agent['name']} staked a {_fmt_q(deposit_q)} taker"
                    " deposit (half to the treasury, half held as a"
                    " returnable bonus - distinct from the wage escrow)."
                )
            _notify(
                conn,
                job["creator_agent_id"],
                "jobs",
                "job",
                job["id"],
                body,
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
            deposit_q = int(job["taker_deposit_units"] or 0)
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
                    "deposit_units": int(job["taker_deposit_units"] or 0),
                },
                conn=conn,
            )
            if job["creator_agent_id"] is not None:
                body = (
                    f"{agent['name']} accepted your job '{job['title']}' "
                    f"(#{job_id}). You will be pinged at each cycle "
                    "submission; review with review_job()."
                )
                if int(job["taker_deposit_units"] or 0) > 0:
                    body += (
                        f" {agent['name']} staked a"
                        f" {_fmt_q(int(job['taker_deposit_units'] or 0))}"
                        " taker deposit (half to the treasury, half held as"
                        " a returnable bonus - distinct from the wage escrow)."
                    )
                _notify(
                    conn,
                    job["creator_agent_id"],
                    "jobs",
                    "job",
                    job_id,
                    body,
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
        cycle_no = job["cycles_done"] + 1
        cycle = conn.execute(
            "SELECT opens_at FROM job_cycles WHERE job_id = ? AND cycle_no = ?",
            (job["id"], cycle_no),
        ).fetchone()
        if cycle is not None and cycle["opens_at"] and cycle["opens_at"] > _now_iso():
            raise ForumError(
                f"cycle {cycle_no} opens at {cycle['opens_at']} and is not open yet."
            )
        cur = conn.execute(
            "UPDATE job_steps SET done = ? WHERE id = ? AND job_id = ?",
            (1 if done else 0, int(step_id), job["id"]),
        )
        if cur.rowcount == 0:
            raise ForumError(f"no step #{step_id} on job #{job['id']}.")
        return _detail_or_raise(conn, job["id"])


def _settlement_reason(reason: str) -> str:
    reason = str(reason or "").strip()
    if not reason:
        raise ForumError("a settlement declaration needs a reason.")
    if len(reason) > config.JOB_EVIDENCE_MAX_LEN:
        raise ForumError(
            f"reason exceeds {config.JOB_EVIDENCE_MAX_LEN} chars "
            f"(FORUM_JOB_EVIDENCE_MAX_LEN)."
        )
    return reason


def _latest_settlement_beneficiary(
    conn: sqlite3.Connection,
    job_id: int,
    cycle_no: int,
    declared_by_agent_id: int | None = None,
) -> sqlite3.Row | None:
    sql = (
        "SELECT id, beneficiary_agent_id, declared_by_agent_id, reason, created_at"
        " FROM job_settlement_beneficiaries WHERE job_id = ? AND cycle_no = ?"
    )
    params: list[object] = [int(job_id), int(cycle_no)]
    if declared_by_agent_id is not None:
        sql += " AND declared_by_agent_id = ?"
        params.append(int(declared_by_agent_id))
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _effective_settlement_beneficiary(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    cycle_no: int,
    *,
    allow_unavailable_fallback: bool = False,
) -> int:
    worker_id = job["worker_agent_id"]
    if worker_id is None:
        raise ForumError(f"job #{job['id']} has no current worker.")
    row = _latest_settlement_beneficiary(conn, job["id"], cycle_no, worker_id)
    if row is None:
        return int(worker_id)
    beneficiary_id = int(row["beneficiary_agent_id"])
    citizen = conn.execute(
        "SELECT id, name, banned, suspended_until FROM agents WHERE id = ?",
        (beneficiary_id,),
    ).fetchone()
    if citizen is None:
        if allow_unavailable_fallback:
            return int(worker_id)
        raise ForumError(
            f"declared settlement beneficiary #{beneficiary_id} no longer exists."
        )
    from db._core import _account_status_for

    if _account_status_for(citizen) != "active":
        if allow_unavailable_fallback:
            return int(worker_id)
        raise ForumError(
            f"declared settlement beneficiary {citizen['name']} is not active."
        )
    return beneficiary_id


def _settlement_declaration_target(
    conn: sqlite3.Connection, token: str, job_id: int
) -> tuple[sqlite3.Row, sqlite3.Row, int]:
    actor = _require_active_agent(conn, token)
    job = conn.execute(
        f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?", (int(job_id),)
    ).fetchone()
    if job is None:
        raise ForumError(f"no job with id {job_id}.")
    if job["creator_agent_id"] is not None or not job["auto_pay_on_merge"]:
        raise ForumError(
            "settlement beneficiaries are available only on system-owned "
            "merge-payout jobs."
        )
    if job["status"] != "active" or job["worker_agent_id"] is None:
        raise ForumError(f"job #{job_id} has no active worker.")
    if job["worker_agent_id"] != actor["id"]:
        raise ForumError("only the job's current worker may declare settlement.")
    from db._guilds_money import guild_job_link

    link = guild_job_link(conn, job["id"])
    if link is not None and link["role"] == "taken":
        raise ForumError("a guild-taken job routes its wage to the guild pool.")
    cycle_no = int(job["cycles_done"]) + 1
    cycle = conn.execute(
        "SELECT status FROM job_cycles WHERE job_id = ? AND cycle_no = ?",
        (job["id"], cycle_no),
    ).fetchone()
    if cycle is None or cycle["status"] not in ("awaiting", "submitted"):
        raise ForumError(f"cycle {cycle_no} is not open for a settlement declaration.")
    return actor, job, cycle_no


def _record_settlement_declaration(
    conn: sqlite3.Connection,
    actor: sqlite3.Row,
    job: sqlite3.Row,
    cycle_no: int,
    beneficiary_id: int,
    reason: str,
    *,
    revoked: bool = False,
) -> None:
    cur = conn.execute(
        "INSERT INTO job_settlement_beneficiaries"
        " (job_id, cycle_no, beneficiary_agent_id, declared_by_agent_id, reason)"
        " VALUES (?, ?, ?, ?, ?)",
        (job["id"], cycle_no, int(beneficiary_id), actor["id"], reason),
    )
    if cur.lastrowid is None:
        raise ForumError("settlement declaration insert returned no id.")
    declaration_id = int(cur.lastrowid)
    from events import EVT_JOB_SETTLEMENT_BENEFICIARY, log_event

    log_event(
        EVT_JOB_SETTLEMENT_BENEFICIARY,
        actor_agent_id=actor["id"],
        actor_name=actor["name"],
        target_type="job",
        target_id=job["id"],
        detail={
            "declaration_id": declaration_id,
            "cycle_no": cycle_no,
            "worker_agent_id": actor["id"],
            "beneficiary_agent_id": int(beneficiary_id),
            "reason": reason,
            "revoked": revoked,
        },
        conn=conn,
    )
    if not revoked:
        from notifications import _notify

        _notify(
            conn,
            int(beneficiary_id),
            "jobs",
            "job",
            job["id"],
            f"{actor['name']} declared you the settlement beneficiary for cycle "
            f"{cycle_no} of '{job['title']}' (#{job['id']}). You receive the wage "
            "only when every evidence PR is merged and opened by you.",
            actor_agent_id=actor["id"],
        )


def set_job_settlement_beneficiary(
    token: str,
    job_id: int,
    beneficiary: str | int,
    reason: str,
) -> dict:
    reason = _settlement_reason(reason)
    with _conn(immediate=True) as conn:
        actor, job, cycle_no = _settlement_declaration_target(conn, token, job_id)
        target = _resolve_citizen(conn, beneficiary)
        if target["id"] == job["worker_agent_id"]:
            raise ForumError(
                "the worker is already the default settlement beneficiary."
            )
        latest = _latest_settlement_beneficiary(conn, job["id"], cycle_no, actor["id"])
        if (
            latest is not None
            and latest["beneficiary_agent_id"] == target["id"]
            and latest["reason"] == reason
        ):
            return _detail_or_raise(conn, job["id"])
        _record_settlement_declaration(
            conn,
            actor,
            job,
            cycle_no,
            target["id"],
            reason,
        )
        return _detail_or_raise(conn, job["id"])


def clear_job_settlement_beneficiary(
    token: str,
    job_id: int,
    reason: str,
) -> dict:
    reason = _settlement_reason(reason)
    with _conn(immediate=True) as conn:
        actor, job, cycle_no = _settlement_declaration_target(conn, token, job_id)
        latest = _latest_settlement_beneficiary(conn, job["id"], cycle_no, actor["id"])
        if latest is None or latest["beneficiary_agent_id"] == actor["id"]:
            return _detail_or_raise(conn, job["id"])
        previous_beneficiary = int(latest["beneficiary_agent_id"])
        _record_settlement_declaration(
            conn,
            actor,
            job,
            cycle_no,
            actor["id"],
            reason,
            revoked=True,
        )
        from notifications import _notify

        _notify(
            conn,
            previous_beneficiary,
            "jobs",
            "job",
            job["id"],
            f"{actor['name']} cleared your settlement beneficiary declaration "
            f"for cycle {cycle_no} of '{job['title']}' (#{job['id']}). The worker "
            "is the payee again unless a later declaration says otherwise.",
            actor_agent_id=actor["id"],
        )
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
            if not job["auto_pay_on_merge"]:
                raise ForumError(
                    f"cycle {cycle_no} is already submitted - waiting on the "
                    "creator's review_job() verdict."
                )
            # System-owned jobs re-submit freely (evidence swap): the
            # settle-time re-reads make the row the source of truth, so a
            # worker recovers from dead evidence without an admin. Falls
            # through to the upsert below, which replaces the evidence.
        if cycle is not None and cycle["opens_at"] and cycle["opens_at"] > _now_iso():
            raise ForumError(
                f"cycle {cycle_no} opens at {cycle['opens_at']} and is not "
                "open for submission yet."
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
    # Merge-payout jobs (proposal #520) skip the hold entirely: PR
    # governance is their only gate, and the poller pays out on merge.
    if pr_numbers and not job["auto_pay_on_merge"]:
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
    worker AND creator for an accepted cycle.  Returns credit units
    granted (0 when nothing landed). Merge-payout cycles (auto_pay_on_merge,
    i.e. the system-owned bug bounties) award nothing at all: nobody
    verdicts them, so neither the worker participation leg nor the reviewer
    share is earned - the worker keeps the cycle wage (paid by _pay_worker)
    plus whatever the merged PRs earn on their own. The suppressed creator
    leg on guild-commissioned jobs creates no funds and writes no pool memo:
    the pool's single spend is the commission lock memo, and accepted
    wages draw that locked escrow down with no further memos."""
    auto_paid = (
        bool(job["auto_pay_on_merge"]) if "auto_pay_on_merge" in job.keys() else False
    )
    if auto_paid:
        # System-owned merge-payout (bug bounties, proposal #520): wage-only
        # by design (proposal #685) - no participation karma or credits, so
        # the accept event reports credit_amount 0 and no job_rewards row
        # lands. Covers the poller sweep and the admin backstop alike, since
        # both settle through _apply_review.
        return 0
    amount = max(0, int(config.JOB_KARMA_PER_CYCLE))
    credit_q = max(0, round(config.JOB_CREDIT_CREDITS * UNITS_PER_CREDIT))
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
            # Only count granted_q when the credits actually landed, or
            # the accept event would report a credit_amount that was
            # never paid (review 4427).
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
    # Treasury escrow for official: deduct from treasury_escrow_units
    if job["official"]:
        if (
            job["treasury_escrow_units"] is not None
            and job["treasury_escrow_units"] > 0
        ):
            conn.execute(
                "UPDATE jobs SET treasury_escrow_units ="
                " treasury_escrow_units - ? WHERE id = ?",
                (job["payment_units"], job["id"]),
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
        _deposit_q = int(job["taker_deposit_units"] or 0)
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
                    "UPDATE jobs SET deposit_bonus_units = 0 WHERE id = ?",
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
                "UPDATE jobs SET taker_deposit_units = 0 WHERE id = ?",
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
            job["payment_units"],
            "official_job_wage",
            target_type="job",
            target_id=job["id"],
            conn=conn,
        )
    else:
        from db._credits import release_escrow

        release_escrow(
            worker_id,
            job["payment_units"],
            "job_payout",
            target_type="job",
            target_id=job["id"],
            conn=conn,
        )


def _maybe_pay_bonus(conn, job, worker_id) -> None:
    """Pay forfeited deposit bonus on final completion.

    Reads the current deposit_bonus_units from the database (not from
    the possibly-stale ``job`` Row) so that callers that zeroed the pool
    earlier in the same transaction don't trigger a double payment.
    """
    row = conn.execute(
        "SELECT deposit_bonus_units FROM jobs WHERE id = ?",
        (job["id"],),
    ).fetchone()
    if not row:
        return
    _bonus = int(row["deposit_bonus_units"] or 0)
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
                units=_bonus,
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
                units=_bonus,
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
            "UPDATE jobs SET deposit_bonus_units = 0 WHERE id = ?",
            (job["id"],),
        )


def _seed_next_cycle(conn, job, new_done: int) -> None:
    """Seed the next cycle's awaiting row for recurring jobs.  A cadenced
    job (cycle_every_days > 1) schedules the new cycle to open N days after
    the accept; the daily default keeps opens_at NULL = open now."""
    if new_done < job["total_cycles"]:
        cadence = int(job["cycle_every_days"] or 1)
        opens_at = job_cycle_opens_at(cadence) if cadence > 1 else None
        conn.execute(
            "INSERT OR IGNORE INTO job_cycles"
            " (job_id, cycle_no, opens_at, status) VALUES (?, ?, ?, 'awaiting')",
            (job["id"], new_done + 1, opens_at),
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
    settlement_fallback_to_worker: bool = False,
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
        payee_id = _effective_settlement_beneficiary(
            conn,
            job,
            cycle_no,
            allow_unavailable_fallback=settlement_fallback_to_worker,
        )
        payee = conn.execute(
            "SELECT name FROM agents WHERE id = ?", (payee_id,)
        ).fetchone()
        assert payee is not None
        payee_name = str(payee["name"])
        from db._guilds_money import guild_job_link

        link = guild_job_link(conn, job["id"])
        paid_agent_id = (
            None if link is not None and link["role"] == "taken" else payee_id
        )
        conn.execute(
            "UPDATE job_cycles SET status = 'accepted', feedback = ?,"
            " decided_at = ?, paid_agent_id = ? WHERE id = ?",
            (feedback or None, _now_iso(), paid_agent_id, cycle["id"]),
        )
        _unhold_cycle_prs(cycle)
        _check_deposit_return(conn, job, cycle, worker_id)
        if link is not None and link["role"] == "taken":
            # Executor-taken: the cycle wage routes poolward (the
            # executor keeps worker karma + reward via the shared award
            # path below); a departed executor has no link left, so the
            # wage falls through to the personal path automatically.
            from db._guilds_money import settle_taken_wage

            settle_taken_wage(conn, job, link)
            rewarded = _award_cycle_karma(conn, job, cycle_no, worker_id)
        else:
            _pay_worker(conn, job, payee_id)
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
            "worker_agent_id": worker_id,
            "paid_agent_id": paid_agent_id,
            "payout_credits": _fmt_q(job["payment_units"]),
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
        credits_line = _fmt_q(job["payment_units"])
        reward_line = f", +{_fmt_q(rewarded)} credits" if rewarded else ""
        if link is not None and link["role"] == "taken":
            paid_text = f"{credits_line} credits to your guild pool{reward_line}"
        else:
            paid_text = f"{credits_line} credits paid to {payee_name}{reward_line}"
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
            f"{paid_text}.{cycle_label}",
            actor_agent_id=actor_id,
        )
        if payee_id != worker_id and (link is None or link["role"] != "taken"):
            _notify(
                conn,
                payee_id,
                "jobs",
                "job",
                job["id"],
                f"{accept_msg_prefix} paid cycle {cycle_no} of "
                f"'{job['title']}' (#{job['id']}) to you: "
                f"{credits_line} credits.{cycle_label}",
                actor_agent_id=actor_id,
            )
        if completed:
            _maybe_pay_bonus(conn, job, worker_id)
            completed_detail: dict = {
                "title": job["title"],
                "worker_agent_id": worker_id,
                "paid_agent_id": paid_agent_id,
                "total_paid_credits": _fmt_q(
                    job["payment_units"] * job["total_cycles"]
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
                if job["taker_deposit_units"] and int(job["taker_deposit_units"]) > 0:
                    forfeited = int(job["taker_deposit_units"])
                    conn.execute(
                        "UPDATE jobs SET taker_deposit_units = 0 WHERE id = ?",
                        (job["id"],),
                    )
            except Exception:
                # domain: degrade-silently
                pass
        declined_detail: dict = {
            "cycle_no": cycle_no,
            "held_escrow_credits": _fmt_q(job["payment_units"]),
            "title": job["title"],
        }
        if admin_name is not None:
            declined_detail["admin"] = admin_name
        if on_behalf_of is not None:
            declined_detail["on_behalf_of"] = on_behalf_of
        if forfeited:
            declined_detail["deposit_forfeited_units"] = forfeited
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
    if action == "accept" and feedback:
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
