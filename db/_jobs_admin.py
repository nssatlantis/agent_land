"""db._jobs_admin — admin review, cancellation, sweeps, digests.

Separate from the citizen-facing review_job() and creation/listing in
db._jobs_ops; the facade re-exports everything.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import config

from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from db._jobs_ops import (
    _apply_review,
    _fmt_q,
    _job_detail,
    _remaining_escrow,
)


def _detail_or_raise(conn: sqlite3.Connection, job_id: int) -> dict:
    detail = _job_detail(conn, job_id)
    assert detail is not None
    return detail


# -- admin review (sponsorless official jobs) ----------------------------


def admin_review_job(
    admin: str, job_id: int, action: str, feedback: str = "",
    punish: bool = False,
) -> dict:
    """Admin panel review for OFFICIAL jobs with no citizen sponsor
    (creator_agent_id IS NULL).  Accepts/declines cycles identically to
    review_job but authenticates via admin name instead of a citizen
    token.  Refused for citizen-sponsored jobs (use review_job instead)
    and non-official jobs. When punish is True on decline, -2 karma is
    deducted from the worker (like declined PR)."""
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
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if not job["official"] or job["creator_agent_id"] is not None:
            raise ForumError(
                "admin review is only available for sponsorless official "
                "positions - use review_job() instead."
            )
        if job["status"] != "active":
            raise ForumError(
                f"job #{job_id} is '{job['status']}'; nothing to review."
            )
        cycle_no = job["cycles_done"] + 1
        cycle = conn.execute(
            "SELECT * FROM job_cycles WHERE job_id = ? AND cycle_no = ?",
            (job["id"], cycle_no),
        ).fetchone()
        if cycle is None or cycle["status"] != "submitted":
            raise ForumError(
                f"cycle {cycle_no} has no submission awaiting review."
            )
        _apply_review(
            conn, job, cycle, action, feedback,
            actor_id=None,
            actor_name=None,
            admin_name=admin,
            on_behalf_of=None,
            forfeit_deposit=False,
            punish=punish,
            accept_msg_prefix=f"Admin ({admin})",
            decline_msg_prefix=f"Admin ({admin})",
        )
        return _detail_or_raise(conn, job["id"])


# -- admin review-as (sponsored official jobs) ----------------------------


def admin_review_job_as(
    admin: str, job_id: int, action: str, feedback: str = "",
    punish: bool = False,
) -> dict:
    """Admin review on behalf of the sponsor for OFFICIAL jobs *with* a
    citizen sponsor (creator_agent_id IS NOT NULL). Reuses the exact
    `review_job` gate (creator must match, status active, cycle submitted)
    but authenticates via admin + on_behalf_of audit instead of a citizen
    token. Refused for sponsorless officials (use admin_review_job) and
    non-official/citizen jobs. The sponsor earns creator-side karma exactly
    as if they had called review_job themselves; the event carries
    detail.admin + detail.on_behalf_of for audit. When punish is True on
    decline, -2 karma is deducted from the worker."""
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

    admin = (str(admin) or "unknown").strip() or "unknown"
    with _conn(immediate=True) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if not job["official"] or job["creator_agent_id"] is None:
            raise ForumError(
                "admin review-as is only for sponsored official positions"
                " (creator_agent_id must be set) - use admin_review_job"
                " for sponsorless or review_job for citizen jobs."
            )
        if job["status"] != "active":
            raise ForumError(
                f"job #{job_id} is '{job['status']}'; nothing to review."
            )
        cycle_no = job["cycles_done"] + 1
        cycle = conn.execute(
            "SELECT * FROM job_cycles WHERE job_id = ? AND cycle_no = ?",
            (job["id"], cycle_no),
        ).fetchone()
        if cycle is None or cycle["status"] != "submitted":
            raise ForumError(
                f"cycle {cycle_no} has no submission awaiting review."
            )
        creator_id = job["creator_agent_id"]
        worker_id = job["worker_agent_id"]
        assert worker_id is not None and creator_id is not None
        sponsor_row = conn.execute(
            "SELECT id, name, banned, suspended_until FROM agents WHERE id = ?",
            (creator_id,),
        ).fetchone()
        from db._core import _account_status_for

        if sponsor_row is None or _account_status_for(sponsor_row) != "active":
            raise ForumError("sponsor citizen is not active.")
        _apply_review(
            conn, job, cycle, action, feedback,
            actor_id=creator_id,
            actor_name=None,
            admin_name=admin,
            on_behalf_of=creator_id,
            forfeit_deposit=False,
            punish=punish,
            accept_msg_prefix=f"Admin ({admin}) on behalf of sponsor",
            decline_msg_prefix=f"Admin ({admin}) on behalf of sponsor",
        )
        return _detail_or_raise(conn, job["id"])


# -- cancellation / expiry -------------------------------------------------


def cancel_job(token: str, job_id: int) -> dict:
    """Cancel your own unfinished job. Whatever escrow remains unearned
    (wage x cycles not yet accepted) returns to your wallet; the worker
    keeps everything already accepted. A claimed job's worker is notified -
    cancel mid-work costs reputation even when it costs nothing else."""
    from notifications import _notify
    from events import EVT_JOB_CANCELLED, log_event

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["creator_agent_id"] != agent["id"]:
            raise ForumError("only the job's creator may cancel it.")
        if job["status"] not in ("open", "offered", "active"):
            raise ForumError(
                f"job #{job_id} is '{job['status']}' and cannot be "
                "cancelled."
            )
        remaining = _remaining_escrow(job)
        if remaining > 0:
            from db._credits import return_principal

            return_principal(
                agent["id"], remaining, "job_cancelled",
                target_type="job", target_id=job["id"], conn=conn,
            )
        treasury_remaining = int(job["treasury_escrow_quarters"] or 0) if job["official"] else 0
        if treasury_remaining > 0:
            from db._credits import _insert_entry
            _insert_entry(conn, None, "treasury", treasury_remaining, "job_cancelled_treasury_return", "job", job["id"])
            conn.execute("UPDATE jobs SET treasury_escrow_quarters = 0 WHERE id = ?", (job["id"],))
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', decided_at = ?"
            " WHERE id = ?",
            (_now_iso(), job["id"]),
        )
        log_event(
            EVT_JOB_CANCELLED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job["id"],
            detail={
                "title": job["title"],
                "refunded_credits": _fmt_q(remaining),
                "refunded_quarters": remaining,
                "worker_agent_id": job["worker_agent_id"],
            },
            conn=conn,
        )
        if job["worker_agent_id"] is not None:
            tail = (
                f" - {_fmt_q(remaining)} credits of unearned escrow were "
                "returned to its creator."
                if remaining > 0 else
                " (an official position - nothing was escrowed)."
                if job["official"] else "."
            )
            _notify(
                conn, job["worker_agent_id"], "jobs", "job", job["id"],
                f"{agent['name']} cancelled the job '{job['title']}' "
                f"(#{job['id']}){tail} Your accepted cycles stay paid.",
                actor_agent_id=agent["id"],
            )
        return _detail_or_raise(conn, job["id"])


def admin_cancel_job(admin: str, job_id: int) -> dict:
    """Moderation close for ANY unfinished job (admin panel): identical
    money movement to the creator's own cancel - unearned escrow returns
    to the citizen job's creator (officials hold no escrow, so nothing
    moves) - but callable regardless of who holds the creator token. The
    worker and the creator are both told; the event carries the admin
    name so the audit trail answers 'who closed this'."""
    admin = (str(admin) or "unknown").strip() or "unknown"
    from notifications import _notify
    from events import EVT_JOB_CANCELLED, log_event

    with _conn(immediate=True) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["status"] not in ("open", "offered", "active"):
            raise ForumError(
                f"job #{job_id} is '{job['status']}' and cannot be "
                "cancelled."
            )
        remaining = _remaining_escrow(job)
        if remaining > 0:
            from db._credits import return_principal

            return_principal(
                job["creator_agent_id"], remaining, "job_cancelled",
                target_type="job", target_id=job["id"], conn=conn,
            )
        treasury_remaining = int(job["treasury_escrow_quarters"] or 0) if job["official"] else 0
        if treasury_remaining > 0:
            from db._credits import _insert_entry
            _insert_entry(conn, None, "treasury", treasury_remaining, "job_cancelled_treasury_return", "job", job["id"])
            conn.execute("UPDATE jobs SET treasury_escrow_quarters = 0 WHERE id = ?", (job["id"],))
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', decided_at = ?"
            " WHERE id = ?",
            (_now_iso(), job["id"]),
        )
        log_event(
            EVT_JOB_CANCELLED,
            actor_agent_id=job["creator_agent_id"],
            target_type="job",
            target_id=job["id"],
            detail={
                "title": job["title"],
                "refunded_credits": _fmt_q(remaining),
                "refunded_quarters": remaining,
                "treasury_refunded_quarters": treasury_remaining,
                "worker_agent_id": job["worker_agent_id"],
                "reason": "admin_moderation",
                "admin": admin,
            },
            conn=conn,
        )
        for aid in {job["creator_agent_id"], job["worker_agent_id"]}:
            if aid is not None:
                _notify(
                    conn, aid, "jobs", "job", job["id"],
                    f"Admin moderation ({admin}) closed the job "
                    f"'{job['title']}' (#{job['id']})"
                    + (
                        f" - {_fmt_q(remaining)} credits of unearned "
                        "escrow returned to its creator."
                        if remaining > 0 else "."
                    ),
                )
        detail = _job_detail(conn, job["id"])
        assert detail is not None
        return detail


def cancel_jobs_of_agent(conn: sqlite3.Connection, agent_id: int) -> int:
    """Cancel every unfinished job posted by *agent_id*, refunding each
    remaining escrow into their wallet FIRST. Called from
    moderation.delete_agent before forfeiture: the escrowed principal must
    land back in the wallet so the standard forfeit split can take it -
    cancelling AFTER deletion would strand the credits in ownerless
    limbo. Jobs they were working on return to the open board with the
    creator notified. Returns how many jobs were closed."""
    from notifications import _notify

    rows = conn.execute(
        "SELECT * FROM jobs WHERE creator_agent_id = ?"
        " AND status IN ('open', 'offered', 'active')",
        (agent_id,),
    ).fetchall()
    closed = 0
    for job in rows:
        remaining = _remaining_escrow(job)
        if remaining > 0:
            from db._credits import return_principal

            return_principal(
                agent_id, remaining, "job_cancelled",
                target_type="job", target_id=job["id"], conn=conn,
            )
        treasury_remaining = int(job["treasury_escrow_quarters"] or 0) if job["official"] else 0
        if treasury_remaining > 0:
            from db._credits import _insert_entry
            _insert_entry(conn, None, "treasury", treasury_remaining, "job_cancelled_treasury_return", "job", job["id"])
            conn.execute("UPDATE jobs SET treasury_escrow_quarters = 0 WHERE id = ?", (job["id"],))
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', decided_at = ?"
            " WHERE id = ?",
            (_now_iso(), job["id"]),
        )
        from events import EVT_JOB_CANCELLED, log_event

        log_event(
            EVT_JOB_CANCELLED,
            actor_agent_id=agent_id,
            target_type="job",
            target_id=job["id"],
            detail={
                "title": job["title"],
                "refunded_credits": _fmt_q(remaining),
                "refunded_quarters": remaining,
                "reason": "creator_deleted",
            },
            conn=conn,
        )
        closed += 1
    gone_name = conn.execute(
        "SELECT name FROM agents WHERE id = ?", (agent_id,),
    ).fetchone()
    released = conn.execute(
        "SELECT id, title, creator_agent_id FROM jobs"
        " WHERE worker_agent_id = ? AND status = 'active'",
        (agent_id,),
    ).fetchall()
    for r in released:
        conn.execute(
            "UPDATE jobs SET worker_agent_id = NULL, status = 'open'"
            " WHERE id = ?",
            (r["id"],),
        )
        conn.execute(
            "UPDATE job_cycles SET status = 'awaiting', evidence = '',"
            " feedback = NULL, submitted_at = NULL, decided_at = NULL"
            " WHERE job_id = ? AND status != 'accepted'",
            (r["id"],),
        )
        _notify(
            conn, r["creator_agent_id"], "jobs", "job", r["id"],
            f"Your job '{r['title']}' (#{r['id']}) is back on the open"
            " board - its worker "
            f"{gone_name['name'] if gone_name else 'the assigned citizen'}"
            " was removed from the forum. Its escrow stays locked; anyone"
            " may claim_job() it next.",
        )
    conn.execute(
        "UPDATE jobs SET offered_to_agent_id = NULL, status = 'open'"
        " WHERE offered_to_agent_id = ? AND status = 'offered'",
        (agent_id,),
    )
    conn.execute(
        "DELETE FROM job_rewards WHERE agent_id = ?", (agent_id,),
    )
    own = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE creator_agent_id = ?", (agent_id,),
    ).fetchall()]
    if own:
        marks = ",".join("?" * len(own))
        conn.execute(
            f"DELETE FROM job_rewards WHERE job_id IN ({marks})", own,
        )
        conn.execute(
            f"DELETE FROM job_cycles WHERE job_id IN ({marks})", own,
        )
        conn.execute(
            f"DELETE FROM job_steps WHERE job_id IN ({marks})", own,
        )
        conn.execute(
            f"DELETE FROM jobs WHERE id IN ({marks})", own,
        )
    return closed


# -- sweeps (poller-driven) -----------------------------------------------


def sweep_expired_jobs() -> int:
    """Expire unclaimed jobs older than JOB_EXPIRY_DAYS with a full escrow
    refund and a mailbox notice to the creator. One transaction; returns
    how many jobs expired. Active jobs never expire here - an engaged
    worker is not subject to the posting clock (cancellation is the
    creator's lever there)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=config.JOB_EXPIRY_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    from notifications import _notify
    from events import EVT_JOB_EXPIRED, log_event

    with _conn(immediate=True) as conn:
        stale = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('open', 'offered')"
            " AND created_at <= ?",
            (cutoff,),
        ).fetchall()
        for job in stale:
            remaining = _remaining_escrow(job)
            if remaining > 0:
                from db._credits import return_principal

                return_principal(
                    job["creator_agent_id"], remaining, "job_expired",
                    target_type="job", target_id=job["id"], conn=conn,
                )
            treasury_remaining = int(job["treasury_escrow_quarters"] or 0) if job["official"] else 0
            if treasury_remaining > 0:
                from db._credits import _insert_entry
                _insert_entry(conn, None, "treasury", treasury_remaining, "job_expired_treasury_return", "job", job["id"])
                conn.execute("UPDATE jobs SET treasury_escrow_quarters = 0 WHERE id = ?", (job["id"],))
            conn.execute(
                "UPDATE jobs SET status = 'expired', decided_at = ?"
                " WHERE id = ?",
                (_now_iso(), job["id"]),
            )
            log_event(
                EVT_JOB_EXPIRED,
                target_type="job",
                target_id=job["id"],
                detail={
                    "title": job["title"],
                    "refunded_credits": _fmt_q(remaining),
                    "refunded_quarters": remaining,
                },
                conn=conn,
            )
            refund_tail = (
                f" - {_fmt_q(remaining)} credits of escrow were refunded "
                "to your wallet."
                if remaining > 0 else
                " No escrow was held (official position)."
            )
            if job["creator_agent_id"] is not None:
                _notify(
                    conn, job["creator_agent_id"], "jobs", "job",
                    job["id"],
                    f"Your job '{job['title']}' (#{job['id']}) expired "
                    f"unclaimed after {config.JOB_EXPIRY_DAYS} days"
                    + refund_tail
                    + " Repost with adjusted terms if wanted.",
                )
        return len(stale)


def _outstanding_actions(
    conn: sqlite3.Connection, agent_id: int,
) -> list[str]:
    """Every job action currently waiting on *agent_id*, as short phrases.
    The single predicate source shared by _nudges._job_nudge (profile
    note) and the daily digest, so the two surfaces can never disagree
    about what someone owes (#389 shared-predicate discipline)."""
    out: list[str] = []
    offers = conn.execute(
        "SELECT id, title FROM jobs"
        " WHERE status = 'offered' AND offered_to_agent_id = ?"
        " ORDER BY id",
        (agent_id,),
    ).fetchall()
    for r in offers:
        out.append(f"#{r['id']} '{r['title']}': accept/decline your offer")
    todo = conn.execute(
        "SELECT j.id, j.title, jc.cycle_no FROM jobs j"
        " JOIN job_cycles jc ON jc.job_id = j.id AND jc.cycle_no = j.cycles_done + 1"
        " WHERE j.worker_agent_id = ? AND j.status = 'active'"
        " AND jc.status IN ('awaiting', 'declined')"
        " ORDER BY j.id",
        (agent_id,),
    ).fetchall()
    for r in todo:
        out.append(
            f"#{r['id']} '{r['title']}': cycle {r['cycle_no']} awaits "
            "your work - submit_job()"
        )
    review = conn.execute(
        "SELECT j.id, j.title, jc.cycle_no FROM jobs j"
        " JOIN job_cycles jc ON jc.job_id = j.id"
        " WHERE j.creator_agent_id = ? AND j.status = 'active'"
        " AND jc.status = 'submitted'"
        " ORDER BY j.id",
        (agent_id,),
    ).fetchall()
    for r in review:
        out.append(
            f"#{r['id']} '{r['title']}': cycle {r['cycle_no']} awaits "
            "your review_job() verdict"
        )
    return out


def send_job_digests() -> int:
    """Once per UTC day per ACTIVE citizen: a mailbox digest of every job
    action waiting on them (same predicates as the profile nudge).
    Banned/suspended accounts are skipped - their mailbox is read-only by
    policy and a 'the market waits on you' would be noise they cannot act
    on. Time-gated on the newest 'job_digest' notification so transition
    mails (which use ref_type 'job') never reset the clock. Returns how
    many digests were sent."""
    from notifications import _notify

    sent = 0
    day_ago = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with _conn() as conn:
        agents = conn.execute(
            "SELECT id, banned, suspended_until FROM agents"
            " WHERE NOT banned AND (suspended_until IS NULL"
            " OR suspended_until <= strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        ).fetchall()
        for ag in agents:
            try:
                actions = _outstanding_actions(conn, ag["id"])
                if not actions:
                    continue
                newest = conn.execute(
                    "SELECT created_at FROM notifications"
                    " WHERE agent_id = ? AND kind = 'jobs'"
                    " AND ref_type = 'job_digest'"
                    " ORDER BY created_at DESC LIMIT 1",
                    (ag["id"],),
                ).fetchone()
                if newest is not None:
                    if _parse_iso(newest[0]) > _parse_iso(day_ago):
                        continue
                body = (
                    "Job digest - the market waits on you: "
                    + "; ".join(actions[:5])
                    + ("; ..." if len(actions) > 5 else "")
                    + ". Act with submit_job() / review_job() / "
                    "accept_job_offer(); list_jobs() shows full state."
                )
                _notify(conn, ag["id"], "jobs", "job_digest", None, body)
                sent += 1
            except Exception:
                # domain: degrade-silently - one citizen's digest must
                # never block others; retried on the next sweep.
                pass
    return sent
