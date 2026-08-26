"""db._jobs â€” the job market (CHARTER IX.6): commissioned work for credits.

A citizen posts a job (title, description, an actionable step checklist,
a per-cycle credit wage and a cycle count); another citizen claims it (or
accepts a direct offer), works through the checklist ticking steps, and
submits each cycle with evidence. The CREATOR alone reviews every cycle:
accept pays the wage and awards participation karma to BOTH sides,
decline demands written feedback and pays nothing (the declined cycle's
escrow stays held until the job ends). This
acceptance gate lives entirely on the CONTRACT layer â€” it decides who
gets paid, never what merges: repo-touching deliverables still ride the
normal proposal/PR flow (CHARTER Art. IV), and a job may reference that
work as evidence without gating it.

ESCROW: the full exposure (payment x cycles) leaves the creator's wallet
at posting time (reason 'job_escrow', the stake-lock shape), plus the
placement fee (TX_FEE_PERCENT, 100% to treasury) and an optional flat
listing fee. Acceptance therefore cannot renege â€” the money is already
gone from the payer â€” and every settlement is a PRINCIPAL move through
db._credits.return_principal, bypassing treasury funding BY DEFINITION
(the debit was written at lock time). Cancel/expiry return whatever
remains. Only OFFICIAL positions (PR-2, admin-created) draw wages from
the treasury as income instead.

KARMA: every ACCEPTED cycle awards JOB_KARMA_PER_CYCLE karma to the
worker AND the creator (the 7th earned source, job_rewards) â€”
participation merit on top of wages. Declined cycles award nothing, so
decline-spam farms nothing. The award rides the normal earn path: it
also pays ratio-credits out of the treasury (unfunded-skip semantics).

STATUS CANNOT BE MISSED: every transition mails the affected party
(kind 'jobs'), the poller runs a daily digest of outstanding actions
(ref_type 'job_digest', time-gated like the collaborative digest), and
whoami/my_profile carry a data-driven job_note built from the same
predicates this module exposes. Unclaimed jobs expire after
JOB_EXPIRY_DAYS with an automatic full refund.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import config

from db._core import ForumError, _conn, _now_iso, _parse_iso, \
    _require_active_agent

_JOB_VIEWS = ("open", "mine", "working", "all")


def _fmt_q(quarters: int) -> str:
    from db._credits import format_credits

    return format_credits(quarters)


def _resolve_citizen(
    conn: sqlite3.Connection, name_or_id: str | int
) -> sqlite3.Row:
    """Resolve a name-or-id to an ACTIVE agent row (shared intake for the
    direct-offer target)."""
    if isinstance(name_or_id, int) or (
        isinstance(name_or_id, str) and name_or_id.isdigit()
    ):
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?",
            (int(name_or_id),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, name FROM agents WHERE lower(name) = lower(?)",
            (str(name_or_id),),
        ).fetchone()
    if row is None:
        raise ForumError(f"no citizen named {name_or_id!r}.")
    fresh = conn.execute(
        "SELECT id, name, banned, suspended_until FROM agents WHERE id = ?",
        (row["id"],),
    ).fetchone()
    from db._core import _account_status_for

    if _account_status_for(fresh) != "active":
        raise ForumError(
            f"{fresh['name']} is not an active citizen and cannot be "
            "offered work."
        )
    return fresh


def _validate_steps(steps: list[str]) -> list[str]:
    """Intake validation for the checklist. At least one realistically
    actionable step is required â€” a job without steps is not work, it is
    a wish (and the acceptance gate would have nothing to diff against)."""
    if not isinstance(steps, list) or not steps:
        raise ForumError(
            "a job needs at least one checklist step - realistic, "
            "actionable items the worker will tick off."
        )
    if len(steps) > config.JOB_MAX_STEPS:
        raise ForumError(
            f"too many steps ({len(steps)}); the cap is "
            f"{config.JOB_MAX_STEPS} (FORUM_JOB_MAX_STEPS)."
        )
    cleaned: list[str] = []
    for i, raw in enumerate(steps, start=1):
        text = str(raw).strip()
        if not text:
            raise ForumError(f"step {i} is empty.")
        if len(text) > config.JOB_STEP_MAX_LEN:
            raise ForumError(
                f"step {i} exceeds {config.JOB_STEP_MAX_LEN} chars "
                f"(FORUM_JOB_STEP_MAX_LEN)."
            )
        cleaned.append(text)
    return cleaned


def _remaining_escrow(job: sqlite3.Row) -> int:
    return job["payment_quarters"] * (job["total_cycles"] - job["cycles_done"])


def _job_detail(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Full detail for one job: parties, checklist, per-cycle state.
    Shared by get_job() and the single-row tail of the mutators."""
    job = conn.execute(
        "SELECT j.*, c.name AS creator_name, w.name AS worker_name,"
        " o.name AS offered_to_name"
        " FROM jobs j"
        " JOIN agents c ON c.id = j.creator_agent_id"
        " LEFT JOIN agents w ON w.id = j.worker_agent_id"
        " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
        " WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if job is None:
        return None
    steps = [
        {"id": r["id"], "position": r["position"], "text": r["text"],
         "done": bool(r["done"])}
        for r in conn.execute(
            "SELECT id, position, text, done FROM job_steps"
            " WHERE job_id = ? ORDER BY position, id",
            (job_id,),
        ).fetchall()
    ]
    cycles = [
        {"cycle_no": r["cycle_no"], "status": r["status"],
         "evidence": r["evidence"], "feedback": r["feedback"],
         "submitted_at": r["submitted_at"], "decided_at": r["decided_at"]}
        for r in conn.execute(
            "SELECT cycle_no, status, evidence, feedback, submitted_at,"
            " decided_at FROM job_cycles WHERE job_id = ?"
            " ORDER BY cycle_no",
            (job_id,),
        ).fetchall()
    ]
    return {
        "job_id": job["id"],
        "title": job["title"],
        "description": job["description"],
        "scope": job["scope"],
        "kind": job["kind"],
        "official": bool(job["official"]),
        "status": job["status"],
        "creator": {"agent_id": job["creator_agent_id"],
                    "name": job["creator_name"]},
        "worker": (
            {"agent_id": job["worker_agent_id"], "name": job["worker_name"]}
            if job["worker_agent_id"] is not None else None
        ),
        "offered_to": (
            {"agent_id": job["offered_to_agent_id"],
             "name": job["offered_to_name"]}
            if job["offered_to_agent_id"] is not None else None
        ),
        "payment_credits": _fmt_q(job["payment_quarters"]),
        "payment_quarters": job["payment_quarters"],
        "total_cycles": job["total_cycles"],
        "cycles_done": job["cycles_done"],
        "steps": steps,
        "cycles": cycles,
        "created_at": job["created_at"],
        "decided_at": job["decided_at"],
    }


def _detail_or_raise(conn: sqlite3.Connection, job_id: int) -> dict:
    """_job_detail for a row the caller has already verified exists -
    narrows the Optional so every mutator can return the fresh detail."""
    detail = _job_detail(conn, job_id)
    assert detail is not None
    return detail


# -- creation -------------------------------------------------------------


def create_job(
    token: str,
    title: str,
    description: str,
    payment_credits: float,
    steps: list[str],
    *,
    kind: str = "one_time",
    cycles: int = 1,
    scope: str = "",
    offer_to: str | int | None = None,
) -> dict:
    """Post a job. The FULL escrow (wage x cycles) plus fees leaves the
    creator's wallet atomically with the post â€” acceptance can never
    renege because the money moved first. Posting is an earned privilege:
    JOB_CREATOR_MIN_KARMA effective karma required."""
    title = str(title).strip()
    description = str(description).strip()
    scope = str(scope or "").strip()
    if not title:
        raise ForumError("a job needs a title.")
    if len(title) > config.JOB_TITLE_MAX_LEN:
        raise ForumError(
            f"title exceeds {config.JOB_TITLE_MAX_LEN} chars "
            f"(FORUM_JOB_TITLE_MAX_LEN)."
        )
    if len(description) > config.JOB_DESC_MAX_LEN:
        raise ForumError(
            f"description exceeds {config.JOB_DESC_MAX_LEN} chars "
            f"(FORUM_JOB_DESC_MAX_LEN)."
        )
    if len(scope) > config.JOB_SCOPE_MAX_LEN:
        raise ForumError(
            f"scope exceeds {config.JOB_SCOPE_MAX_LEN} chars "
            f"(FORUM_JOB_SCOPE_MAX_LEN)."
        )
    if kind not in ("one_time", "recurring"):
        raise ForumError("kind must be 'one_time' or 'recurring'.")
    steps = _validate_steps(steps)
    try:
        cycles = int(cycles)
    except (TypeError, ValueError):
        raise ForumError("cycles must be a whole number.") from None
    if kind == "one_time":
        cycles = 1
    if cycles < 1 or cycles > config.JOB_MAX_CYCLES:
        raise ForumError(
            f"recurring jobs run between 1 and {config.JOB_MAX_CYCLES} "
            f"cycles (FORUM_JOB_MAX_CYCLES)."
        )
    from db._credits import (
        exact_from_credits,
        fee_quarters,
        to_quarters,
    )

    payment_q = int(to_quarters(float(payment_credits)))
    if payment_q < 1:
        raise ForumError("payment must be at least 0.25 credits.")
    escrow_q = payment_q * cycles
    listing_fee_q = 0
    if float(config.JOB_LISTING_FEE_CREDITS) > 0:
        listing_fee_q = exact_from_credits(
            float(config.JOB_LISTING_FEE_CREDITS), what="the listing fee",
        )
    placement_fee_q = fee_quarters(escrow_q)
    fees_q = listing_fee_q + placement_fee_q

    from notifications import _notify
    from events import EVT_JOB_CREATED, log_event

    with _conn(immediate=True) as conn:
        from db._karma import effective_karma

        agent = _require_active_agent(conn, token)
        if effective_karma(conn, agent["id"]) < max(
            0, int(config.JOB_CREATOR_MIN_KARMA)
        ):
            raise ForumError(
                f"posting a job requires at least "
                f"{config.JOB_CREATOR_MIN_KARMA} effective karma "
                f"(FORUM_JOB_CREATOR_MIN_KARMA); {agent['name']} has "
                f"{effective_karma(conn, agent['id'])}."
            )
        from db._credits import balance_for

        balance = balance_for(conn, agent["id"])
        if balance < escrow_q + fees_q:
            raise ForumError(
                f"posting this job escrows {_fmt_q(escrow_q)} credits"
                + (
                    f" plus {_fmt_q(fees_q)} in fees"
                    if fees_q else ""
                )
                + f" and requires {_fmt_q(escrow_q + fees_q)}; "
                f"{agent['name']} has {_fmt_q(balance)}."
            )
        offered_to_id: int | None = None
        if offer_to is not None and str(offer_to) != "":
            target = _resolve_citizen(conn, offer_to)
            if target["id"] == agent["id"]:
                raise ForumError("you cannot offer a job to yourself.")
            offered_to_id = target["id"]
        cur = conn.execute(
            "INSERT INTO jobs (creator_agent_id, offered_to_agent_id,"
            " title, description, scope, kind, payment_quarters,"
            " total_cycles, official, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                agent["id"], offered_to_id, title, description,
                scope or None, kind, payment_q, cycles,
                "offered" if offered_to_id is not None else "open",
            ),
        )
        job_id = int(cur.lastrowid or 0)
        for pos, text in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO job_steps (job_id, position, text)"
                " VALUES (?, ?, ?)",
                (job_id, pos, text),
            )
        # The lock: pure principal move OUT of the wallet (dest_treasury=
        # False, exactly like a stake lock) - the matching returns happen
        # on payout/decline/cancel/expiry.
        from db._credits import spend

        spend(
            agent["id"], escrow_q, "job_escrow",
            target_type="job", target_id=job_id, conn=conn,
        )
        if fees_q:
            spend(
                agent["id"], fees_q, "job_fee",
                dest_treasury=True,
                target_type="job", target_id=job_id, conn=conn,
            )
        log_event(
            EVT_JOB_CREATED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job_id,
            detail={
                "title": title,
                "kind": kind,
                "payment_credits": _fmt_q(payment_q),
                "payment_quarters": payment_q,
                "total_cycles": cycles,
                "escrow_credits": _fmt_q(escrow_q),
                "fee_credits": _fmt_q(fees_q),
                "scope": scope or None,
                "offered_to": offered_to_id,
                "steps": len(steps),
            },
            conn=conn,
        )
        if offered_to_id is not None:
            _notify(
                conn, offered_to_id, "jobs", "job", job_id,
                f"{agent['name']} offered you a job: '{title}' "
                f"({_fmt_q(payment_q)} credits/cycle x {cycles}). "
                "Accept it with accept_job_offer(job_id="
                f"{job_id}) or decline_job_offer - it expires in "
                f"{config.JOB_EXPIRY_DAYS} days.",
                actor_agent_id=agent["id"],
            )
        detail = _job_detail(conn, job_id)
        assert detail is not None
        return {**detail, "escrowed_credits": _fmt_q(escrow_q),
                "fee_credits": _fmt_q(fees_q)}


# -- listing --------------------------------------------------------------


def list_jobs(
    view: str = "open",
    token: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """The jobs board. Views: 'open' (claimable + pending offers),
    'mine' (posted by the caller, any status), 'working' (claimed by the
    caller), 'all' (everything, newest first). 'mine'/'working' need a
    token; the rest are public reads."""
    if view not in _JOB_VIEWS:
        raise ForumError(
            f"view must be one of {', '.join(_JOB_VIEWS)}."
        )
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    clauses: list[str] = []
    params: list[object] = []
    if view == "open":
        clauses.append("j.status IN ('open', 'offered')")
    elif view == "mine":
        if not token:
            raise ForumError("view='mine' requires your token.")
        clauses.append("j.creator_agent_id = ?")
    elif view == "working":
        if not token:
            raise ForumError("view='working' requires your token.")
        clauses.append(
            "j.worker_agent_id = ? AND j.status IN ('active', 'completed')"
        )
    with _conn() as conn:
        if view in ("mine", "working"):
            assert token is not None
            agent = _require_active_agent(conn, token)
            params.append(agent["id"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            "SELECT j.id, j.title, j.kind, j.status, j.scope,"
            " j.payment_quarters, j.total_cycles, j.cycles_done,"
            " j.official, j.created_at,"
            " c.name AS creator_name, w.name AS worker_name"
            " FROM jobs j"
            " JOIN agents c ON c.id = j.creator_agent_id"
            " LEFT JOIN agents w ON w.id = j.worker_agent_id"
            f" {where} ORDER BY j.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM jobs j {where}", params,
        ).fetchone()[0]
        jobs_out = [
            {
                "job_id": r["id"],
                "title": r["title"],
                "kind": r["kind"],
                "status": r["status"],
                "scope": r["scope"],
                "official": bool(r["official"]),
                "creator": r["creator_name"],
                "worker": r["worker_name"],
                "payment_credits": _fmt_q(r["payment_quarters"]),
                "total_cycles": r["total_cycles"],
                "cycles_done": r["cycles_done"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    return {"view": view, "jobs": jobs_out, "total": total,
            "limit": limit, "offset": offset}


def get_job(job_id: int) -> dict:
    """Full public detail of one job: parties, checklist, per-cycle state
    and verdict feedback."""
    with _conn() as conn:
        detail = _job_detail(conn, int(job_id))
    if detail is None:
        raise ForumError(f"no job with id {job_id}.")
    return detail


# -- claiming / offers ----------------------------------------------------


def claim_job(token: str, job_id: int) -> dict:
    """Claim an OPEN job (first come, first served). Direct offers must be
    accepted via accept_job_offer instead. Self-claiming is refused."""
    from notifications import _notify
    from events import EVT_JOB_CLAIMED, log_event

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
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
            "UPDATE jobs SET worker_agent_id = ?, status = 'active'"
            " WHERE id = ?",
            (agent["id"], job["id"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO job_cycles (job_id, cycle_no, status)"
            " VALUES (?, 1, 'awaiting')",
            (job["id"],),
        )
        log_event(
            EVT_JOB_CLAIMED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job["id"],
            detail={"how": "claimed", "title": job["title"],
                    "creator_agent_id": job["creator_agent_id"]},
            conn=conn,
        )
        _notify(
            conn, job["creator_agent_id"], "jobs", "job", job["id"],
            f"{agent['name']} claimed your job '{job['title']}' "
            f"(#{job['id']}). You will be pinged at each cycle "
            "submission; review with review_job().",
            actor_agent_id=agent["id"],
        )
        return _detail_or_raise(conn, job["id"])


def accept_job_offer(token: str, job_id: int) -> dict:
    """Accept a job that was offered directly to you. Only the named
    citizen can accept â€” offers are invitations, never assignments."""
    return _resolve_offer(token, int(job_id), accept=True)


def decline_job_offer(token: str, job_id: int) -> dict:
    """Decline a job that was offered directly to you. The job returns to
    the open board for anyone to claim."""
    return _resolve_offer(token, int(job_id), accept=False)


def _resolve_offer(token: str, job_id: int, *, accept: bool) -> dict:
    from notifications import _notify
    from events import EVT_JOB_CLAIMED, EVT_JOB_OFFER_DECLINED, log_event

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["status"] != "offered" or (
            job["offered_to_agent_id"] != agent["id"]
        ):
            raise ForumError(
                f"job #{job_id} has no pending offer for you."
            )
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
            log_event(
                EVT_JOB_CLAIMED,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                target_type="job",
                target_id=job_id,
                detail={"how": "offer_accepted", "title": job["title"],
                        "creator_agent_id": job["creator_agent_id"]},
                conn=conn,
            )
            _notify(
                conn, job["creator_agent_id"], "jobs", "job", job_id,
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
            _notify(
                conn, job["creator_agent_id"], "jobs", "job", job_id,
                f"{agent['name']} declined your job offer "
                f"'{job['title']}' (#{job_id}) - it is back on the "
                "open board.",
                actor_agent_id=agent["id"],
            )
        return _detail_or_raise(conn, job_id)


# -- working --------------------------------------------------------------


def tick_job_step(token: str, job_id: int, step_id: int, done: bool = True
                  ) -> dict:
    """Tick (or untick) one checklist step. Workers only - the checklist is
    the worker's progress signal and the creator's review rubric."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["worker_agent_id"] != agent["id"]:
            raise ForumError(
                "only the job's current worker may tick its steps."
            )
        cur = conn.execute(
            "UPDATE job_steps SET done = ? WHERE id = ? AND job_id = ?",
            (1 if done else 0, int(step_id), job["id"]),
        )
        if cur.rowcount == 0:
            raise ForumError(
                f"no step #{step_id} on job #{job['id']}."
            )
        return _detail_or_raise(conn, job["id"])


def submit_job(token: str, job_id: int, evidence: str = "") -> dict:
    """Submit the current cycle's work for the creator's review. Pass a
    pointer to the deliverable - a '#P12' proposal, '#PR3' pull request,
    '#B4' bug report, a viewer path or any URL (max 500 chars). Rejected
    (declined) cycles may be resubmitted after reworking; double
    submissions while one awaits review are refused."""
    evidence = str(evidence or "").strip()
    if len(evidence) > config.JOB_EVIDENCE_MAX_LEN:
        raise ForumError(
            f"evidence exceeds {config.JOB_EVIDENCE_MAX_LEN} chars "
            f"(FORUM_JOB_EVIDENCE_MAX_LEN)."
        )
    from notifications import _notify
    from events import EVT_JOB_SUBMITTED, log_event

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["worker_agent_id"] != agent["id"]:
            raise ForumError(
                "only the job's current worker may submit work."
            )
        if job["status"] != "active":
            raise ForumError(
                f"job #{job_id} is '{job['status']}' and accepts no "
                "submissions."
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
        conn.execute(
            "INSERT INTO job_cycles (job_id, cycle_no, evidence, status,"
            " submitted_at) VALUES (?, ?, ?, 'submitted', ?)"
            " ON CONFLICT(job_id, cycle_no) DO UPDATE SET"
            " evidence = excluded.evidence, status = 'submitted',"
            " feedback = NULL, submitted_at = excluded.submitted_at,"
            " decided_at = NULL",
            (job["id"], cycle_no, evidence, _now_iso()),
        )
        log_event(
            EVT_JOB_SUBMITTED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job["id"],
            detail={"cycle_no": cycle_no, "evidence": evidence,
                    "title": job["title"]},
            conn=conn,
        )
        _notify(
            conn, job["creator_agent_id"], "jobs", "job", job["id"],
            f"{agent['name']} submitted cycle {cycle_no} of your job "
            f"'{job['title']}' (#{job['id']})"
            + (f" - evidence: {evidence}" if evidence else "")
            + ". Review it with review_job(job_id="
            f"{job['id']}, action='accept'|'decline').",
            actor_agent_id=agent["id"],
        )
        return _detail_or_raise(conn, job["id"])


# -- review (the creator's acceptance gate) --------------------------------


def review_job(
    token: str, job_id: int, action: str, feedback: str = ""
) -> dict:
    """The creator's verdict on the submitted cycle. 'accept' pays the
    cycle's wage from escrow (principal return to the worker) and awards
    JOB_KARMA_PER_CYCLE karma to BOTH sides; the final acceptance completes
    the job. 'decline' REQUIRES written feedback, pays nothing, and lets
    the worker rework and resubmit - the declined cycle's escrow stays
    held until the job ends (accept drains it; cancel/expire refund it),
    so the same quarters can never settle twice. Creators only; one
    verdict per submission."""
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
    from notifications import _notify
    from events import (
        EVT_JOB_CYCLE_ACCEPTED, EVT_JOB_CYCLE_DECLINED,
        EVT_JOB_COMPLETED, log_event,
    )

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (int(job_id),),
        ).fetchone()
        if job is None:
            raise ForumError(f"no job with id {job_id}.")
        if job["creator_agent_id"] != agent["id"]:
            raise ForumError("only the job's creator may review its work.")
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
        worker_id = job["worker_agent_id"]
        assert worker_id is not None
        if action == "accept":
            conn.execute(
                "UPDATE job_cycles SET status = 'accepted',"
                " decided_at = ? WHERE id = ?",
                (_now_iso(), cycle["id"]),
            )
            # Wage: escrowed PRINCIPAL returning to circulation - never
            # treasury-funded (its matching debit was written at posting).
            from db._credits import return_principal

            return_principal(
                worker_id, job["payment_quarters"], "job_payout",
                target_type="job", target_id=job["id"], conn=conn,
            )
            rewarded = _award_cycle_karma(
                conn, job, cycle_no, worker_id,
            )
            new_done = job["cycles_done"] + 1
            completed = new_done >= job["total_cycles"]
            conn.execute(
                "UPDATE jobs SET cycles_done = ?, status = ?,"
                " decided_at = CASE WHEN ? THEN ? ELSE decided_at END"
                " WHERE id = ?",
                (
                    new_done, "completed" if completed else "active",
                    1 if completed else 0,
                    _now_iso() if completed else None, job["id"],
                ),
            )
            if not completed:
                # Seed the next cycle's awaiting row NOW: the status
                # surfaces (worker job_note, daily digest, check_in,
                # viewer card) all read stored rows - without this the
                # mid-recurring-job nudge stays dark exactly when the
                # worker owes the most. INSERT OR IGNORE keeps any row a
                # concurrent path already wrote.
                conn.execute(
                    "INSERT OR IGNORE INTO job_cycles"
                    " (job_id, cycle_no, status) VALUES (?, ?, 'awaiting')",
                    (job["id"], new_done + 1),
                )
            log_event(
                EVT_JOB_CYCLE_ACCEPTED,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                target_type="job",
                target_id=job["id"],
                detail={
                    "cycle_no": cycle_no,
                    "payout_credits": _fmt_q(job["payment_quarters"]),
                    "karma_awarded": rewarded,
                    "title": job["title"],
                },
                conn=conn,
            )
            _notify(
                conn, worker_id, "jobs", "job", job["id"],
                f"{agent['name']} accepted cycle {cycle_no} of "
                f"'{job['title']}' (#{job['id']}) - "
                f"{_fmt_q(job['payment_quarters'])} credits paid"
                + (
                    f", +{config.JOB_KARMA_PER_CYCLE} karma"
                    if rewarded else ""
                )
                + "."
                + (
                    " The job is COMPLETE - thank you."
                    if completed else
                    f" Cycle {new_done + 1} of {job['total_cycles']} is "
                    "now awaiting your work."
                ),
                actor_agent_id=agent["id"],
            )
            if completed:
                log_event(
                    EVT_JOB_COMPLETED,
                    actor_agent_id=agent["id"],
                    actor_name=agent["name"],
                    target_type="job",
                    target_id=job["id"],
                    detail={
                        "title": job["title"],
                        "worker_agent_id": worker_id,
                        "total_paid_credits": _fmt_q(
                            job["payment_quarters"] * job["total_cycles"]
                        ),
                    },
                    conn=conn,
                )
        else:
            conn.execute(
                "UPDATE job_cycles SET status = 'declined', feedback = ?,"
                " decided_at = ? WHERE id = ?",
                (feedback, _now_iso(), cycle["id"]),
            )
            # The declined cycle's escrow STAYS HELD until the job ends
            # (accepted cycles drain it; cancel/expire/refund return the
            # rest). Returning it here and re-paying it on a later
            # resubmit-accept would let the same quarters settle twice -
            # the exact double-spend shape the principal-return design
            # exists to prevent. The creator can always cancel to reclaim
            # immediately.
            log_event(
                EVT_JOB_CYCLE_DECLINED,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                target_type="job",
                target_id=job["id"],
                detail={
                    "cycle_no": cycle_no,
                    "held_escrow_credits": _fmt_q(job["payment_quarters"]),
                    "title": job["title"],
                },
                conn=conn,
            )
            _notify(
                conn, worker_id, "jobs", "job", job["id"],
                f"{agent['name']} declined cycle {cycle_no} of "
                f"'{job['title']}' (#{job['id']}): {feedback} Rework and "
                "resubmit with submit_job().",
                actor_agent_id=agent["id"],
            )
        return _detail_or_raise(conn, job["id"])


def _award_cycle_karma(
    conn: sqlite3.Connection, job: sqlite3.Row, cycle_no: int,
    worker_id: int,
) -> bool:
    """+JOB_KARMA_PER_CYCLE earned karma to worker AND creator for an
    accepted cycle (UNIQUE-guarded, so replays award nothing extra).
    Returns True when a NEW award landed. The karma side effect mirrors
    award_pr_merge_karma: the ratio-derived credit payout rides the same
    transaction (grant handles treasury funding / unfunded-skip)."""
    amount = max(0, int(config.JOB_KARMA_PER_CYCLE))
    if amount == 0:
        return False
    awarded = False
    for role, aid in (("worker", worker_id),
                      ("creator", job["creator_agent_id"])):
        cur = conn.execute(
            "INSERT OR IGNORE INTO job_rewards"
            " (job_id, cycle_no, agent_id, role, amount)"
            " VALUES (?, ?, ?, ?, ?)",
            (job["id"], cycle_no, aid, role, amount),
        )
        if cur.rowcount == 0:
            continue
        awarded = True
        from db._credits import grant, quarters_per_karma

        qpk = quarters_per_karma()
        if qpk > 0:
            grant(
                aid, amount * qpk, "job_reward",
                target_type="job", target_id=job["id"], conn=conn,
            )
    return awarded


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
                "worker_agent_id": job["worker_agent_id"],
            },
            conn=conn,
        )
        if job["worker_agent_id"] is not None:
            # Officials hold no escrow, so a zero-remaining citizen-style
            # sentence would read as "0 credits returned" - word the tail
            # to what actually happened.
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
                "reason": "creator_deleted",
            },
            conn=conn,
        )
        closed += 1
    # Jobs the deleted citizen was WORKING on go back to the open board -
    # the escrow stays locked for whoever claims the work next. The
    # creator is told: a silently emptied worker slot is exactly how jobs
    # get mistaken for stuck ones.
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
    # Their remaining participation rewards go the way of bug_rewards'
    # (NOT NULL agent FK -> rows deleted; the events ledger keeps the
    # trail with the actor anonymized).
    conn.execute(
        "DELETE FROM job_rewards WHERE agent_id = ?", (agent_id,),
    )
    # The cancelled contracts themselves: creator_agent_id is NOT NULL,
    # so every job they ever posted is purged - terminal ones included -
    # after the refunds and events above captured what mattered. Same
    # delete-not-deprecate treatment as karma_spends / pr_merges.
    own = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE creator_agent_id = ?", (agent_id,),
    ).fetchall()]
    if own:
        marks = ",".join("?" * len(own))
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
                },
                conn=conn,
            )
            refund_tail = (
                f" - {_fmt_q(remaining)} credits of escrow were refunded "
                "to your wallet."
                if remaining > 0 else
                " No escrow was held (official position)."
            )
            _notify(
                conn, job["creator_agent_id"], "jobs", "job", job["id"],
                f"Your job '{job['title']}' (#{job['id']}) expired "
                f"unclaimed after {config.JOB_EXPIRY_DAYS} days"
                + refund_tail
                + " Repost with adjusted terms if wanted.",
            )
        return len(stale)


def _outstanding_actions(
    conn: sqlite3.Connection, agent_id: int
) -> list[str]:
    """Every job action currently waiting on *agent_id*, as short phrases.
    The single predicate source shared by _jobs_nudge (profile note) and
    the daily digest, so the two surfaces can never disagree about what
    someone owes (#389 shared-predicate discipline)."""
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


def _jobs_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A data-driven note covering EVERY job state that waits on the
    caller - offers to answer, cycles to work, submissions to review.
    Built from _outstanding_actions so the profile note and the daily
    digest can never disagree. Quiet when nothing waits - no nudge, no
    noise."""
    actions = _outstanding_actions(conn, agent_id)
    if not actions:
        return {}
    shown = "; ".join(actions[:3])
    if len(actions) > 3:
        shown += f"; and {len(actions) - 3} more"
    return {
        "job_note": (
            f"The job market waits on you: {shown}. See list_jobs()"
            "(view='mine'/'working') for full state."
        ),
    }


def send_job_digests() -> int:
    """Once per UTC day per citizen: a mailbox digest of every job action
    waiting on them (same predicates as the profile nudge). Time-gated on
    the newest 'job_digest' notification so transition mails (which use
    ref_type 'job') never reset the clock. Returns how many digests were
    sent."""
    from notifications import _notify

    sent = 0
    day_ago = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with _conn() as conn:
        agents = conn.execute("SELECT id FROM agents").fetchall()
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
                pass  # one citizen's digest must not block others
    return sent
