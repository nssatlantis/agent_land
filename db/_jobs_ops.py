"""db._jobs_ops — job creation, listing, claiming, worker ops, review.

Shared by citizen and official paths; the admin-only review variants and
cancellation/sweep logic live in db._jobs_admin.  The public facade is
db._jobs which re-exports from both.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _id_chunks, _now_iso, _require_active_agent

_PR_RE = re.compile(
    r"(?:#PR\s*(\d+)|PR\s*#?\s*(\d+)|/prs/(\d+)|/pull/(\d+))",
    re.IGNORECASE,
)


def _parse_pr_numbers(evidence: str) -> list[int]:
    """Extract PR numbers from evidence text for advisory linking."""
    if not evidence:
        return []
    seen: set[int] = set()
    out: list[int] = []
    for m in _PR_RE.finditer(evidence):
        for g in m.groups():
            if g and g.isdigit():
                n = int(g)
                if n > 0 and n not in seen:
                    seen.add(n)
                    out.append(n)
                    if len(out) >= 10:
                        return out
                break
    return out


_JOB_VIEWS = ("open", "mine", "working", "all")


def _fmt_q(quarters: int) -> str:
    from db._credits import format_credits

    return format_credits(quarters)


_JOB_ANCHOR_KINDS = (
    "job_claimed",
    "job_submitted",
    "job_cycle_accepted",
    "job_cycle_declined",
)


def _job_overdue_anchor_sql(job_alias: str) -> str:
    kinds = ",".join(f"'{k}'" for k in _JOB_ANCHOR_KINDS)
    return (
        f"COALESCE((SELECT MAX(e.created_at) FROM events e"
        f" WHERE e.target_type = 'job' AND e.target_id = {job_alias}.id"
        f" AND e.kind IN ({kinds})), {job_alias}.created_at)"
    )


def job_overdue_cutoff() -> str:
    hours = int(config.JOB_CYCLE_DUE_HOURS)
    if hours <= 0:
        return ""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _cycle_is_overdue(status: str | None, anchor_at: str | None, cutoff: str) -> bool:
    if not cutoff or status not in ("awaiting", "declined"):
        return False
    if not anchor_at:
        return False
    return anchor_at <= cutoff


def _overdue_windows_elapsed(anchor_at: str | None, cutoff: str) -> int:
    hours = int(config.JOB_CYCLE_DUE_HOURS)
    if not cutoff or hours <= 0 or not anchor_at:
        return 0
    try:
        window_s = hours * 3600
        anchor = datetime.fromisoformat(anchor_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - anchor).total_seconds()
        if age < window_s:
            return 0
        return int(age) // window_s
    except Exception:
        return 0  # domain: degrade-silently - unparsable clock = no release


def _overdue_flag(
    status: str,
    cur_cycle_status: str | None,
    anchor_at: str | None,
    cutoff: str,
) -> bool:
    if status != "active":
        return False
    return _cycle_is_overdue(cur_cycle_status, anchor_at, cutoff)


def _all_prs_merged(pr_numbers: list[int]) -> bool:
    if not pr_numbers:
        return True
    try:
        import github

        for n in pr_numbers:
            try:
                pr = github.get_pr(n)
                if pr.get("state") != "closed" or not pr.get("merged_at"):
                    if pr.get("outcome") != "merged" and not pr.get("merged_at"):
                        return False
            except Exception:
                # domain: degrade-silently - PR lookup failed, not merged
                return False
        return True
    except Exception:
        # domain: degrade-silently - github import failed, not merged
        return False


def _remaining_escrow(job: sqlite3.Row) -> int:
    remaining = max(0, job["total_cycles"] - job["cycles_done"])
    if job["official"]:
        return 0
    return int(job["payment_quarters"]) * remaining


def _resolve_citizen(conn: sqlite3.Connection, name_or_id: str | int) -> sqlite3.Row:
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
            f"{fresh['name']} is not an active citizen and cannot be offered work."
        )
    return fresh


def _validate_steps(steps: list[str]) -> list[str]:
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


def _job_detail_from_parts(
    job: sqlite3.Row,
    steps: list[dict],
    cycles: list[dict],
    cutoff: str,
) -> dict:
    cur_status: str | None = None
    if job["status"] == "active":
        cur_status = next(
            (c["status"] for c in cycles if c["cycle_no"] == job["cycles_done"] + 1),
            None,
        )
    return {
        "job_id": job["id"],
        "title": job["title"],
        "description": job["description"],
        "scope": job["scope"],
        "kind": job["kind"],
        "official": bool(job["official"]),
        "status": job["status"],
        "overdue": _overdue_flag(job["status"], cur_status, job["anchor_at"], cutoff),
        "creator": (
            {"agent_id": job["creator_agent_id"], "name": job["creator_name"]}
            if job["creator_agent_id"] is not None
            else None
        ),
        "worker": (
            {"agent_id": job["worker_agent_id"], "name": job["worker_name"]}
            if job["worker_agent_id"] is not None
            else None
        ),
        "offered_to": (
            {"agent_id": job["offered_to_agent_id"], "name": job["offered_to_name"]}
            if job["offered_to_agent_id"] is not None
            else None
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


def _parse_cycle_evidence(r: sqlite3.Row) -> tuple[list[int], list[str]]:
    """Parse a job_cycles row's stored PR references (advisory linking).
    Degrade-silently: malformed or wrong-shaped JSON becomes empty lists."""
    try:
        pr_numbers = (
            json.loads(r["evidence_pr_numbers"]) if r["evidence_pr_numbers"] else []
        )
        if not isinstance(pr_numbers, list):
            pr_numbers = []
    except Exception:  # domain: degrade-silently - malformed evidence JSON -> empty list
        pr_numbers = []
    try:
        pr_shas = json.loads(r["evidence_pr_shas"]) if r["evidence_pr_shas"] else []
        if not isinstance(pr_shas, list):
            pr_shas = []
    except Exception:  # domain: degrade-silently - malformed evidence JSON -> empty list
        pr_shas = []
    pr_numbers = [
        int(n)
        for n in pr_numbers
        if isinstance(n, int) or (isinstance(n, str) and str(n).isdigit())
    ]
    return pr_numbers, pr_shas


def _job_detail(conn: sqlite3.Connection, job_id: int) -> dict | None:
    job = conn.execute(
        "SELECT j.*, c.name AS creator_name, w.name AS worker_name,"
        f" o.name AS offered_to_name, {_job_overdue_anchor_sql('j')} AS anchor_at"
        " FROM jobs j"
        " LEFT JOIN agents c ON c.id = j.creator_agent_id"
        " LEFT JOIN agents w ON w.id = j.worker_agent_id"
        " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
        " WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if job is None:
        return None
    steps = [
        {
            "id": r["id"],
            "position": r["position"],
            "text": r["text"],
            "done": bool(r["done"]),
        }
        for r in conn.execute(
            "SELECT id, position, text, done FROM job_steps"
            " WHERE job_id = ? ORDER BY position, id",
            (job_id,),
        ).fetchall()
    ]
    cycles = []
    for r in conn.execute(
        "SELECT cycle_no, status, evidence, evidence_pr_numbers,"
        " evidence_pr_shas, feedback, submitted_at, decided_at"
        " FROM job_cycles WHERE job_id = ? ORDER BY cycle_no",
        (job_id,),
    ).fetchall():
        pr_numbers, pr_shas = _parse_cycle_evidence(r)
        cycles.append(
            {
                "cycle_no": r["cycle_no"],
                "status": r["status"],
                "evidence": r["evidence"],
                "evidence_pr_numbers": pr_numbers,
                "evidence_pr_shas": pr_shas,
                "feedback": r["feedback"],
                "submitted_at": r["submitted_at"],
                "decided_at": r["decided_at"],
            }
        )
    return _job_detail_from_parts(job, steps, cycles, job_overdue_cutoff())


def _job_details_batch(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, dict]:
    if not job_ids:
        return {}
    details: dict[int, dict] = {}
    cutoff = job_overdue_cutoff()
    for chunk in _id_chunks(list(job_ids)):
        marks = ",".join("?" * len(chunk))
        job_rows = conn.execute(
            "SELECT j.*, c.name AS creator_name, w.name AS worker_name,"
            f" o.name AS offered_to_name, {_job_overdue_anchor_sql('j')} AS anchor_at"
            " FROM jobs j"
            " LEFT JOIN agents c ON c.id = j.creator_agent_id"
            " LEFT JOIN agents w ON w.id = j.worker_agent_id"
            " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
            f" WHERE j.id IN ({marks})",
            chunk,
        ).fetchall()
        if not job_rows:
            continue
        steps_by_job: dict[int, list[dict]] = {}
        for r in conn.execute(
            "SELECT job_id, id, position, text, done FROM job_steps"
            f" WHERE job_id IN ({marks}) ORDER BY job_id, position, id",
            chunk,
        ).fetchall():
            steps_by_job.setdefault(r["job_id"], []).append(
                {
                    "id": r["id"],
                    "position": r["position"],
                    "text": r["text"],
                    "done": bool(r["done"]),
                }
            )
        cycles_by_job: dict[int, list[dict]] = {}
        for r in conn.execute(
            "SELECT job_id, cycle_no, status, evidence, evidence_pr_numbers,"
            " evidence_pr_shas, feedback, submitted_at, decided_at"
            f" FROM job_cycles WHERE job_id IN ({marks})"
            " ORDER BY job_id, cycle_no",
            chunk,
        ).fetchall():
            pr_numbers, pr_shas = _parse_cycle_evidence(r)
            cycles_by_job.setdefault(r["job_id"], []).append(
                {
                    "cycle_no": r["cycle_no"],
                    "status": r["status"],
                    "evidence": r["evidence"],
                    "evidence_pr_numbers": pr_numbers,
                    "evidence_pr_shas": pr_shas,
                    "feedback": r["feedback"],
                    "submitted_at": r["submitted_at"],
                    "decided_at": r["decided_at"],
                }
            )
        for r in job_rows:
            jid = r["id"]
            details[jid] = _job_detail_from_parts(
                r, steps_by_job.get(jid, []), cycles_by_job.get(jid, []), cutoff
            )
    return details


def _detail_or_raise(conn: sqlite3.Connection, job_id: int) -> dict:
    detail = _job_detail(conn, job_id)
    assert detail is not None
    return detail


# -- creation -------------------------------------------------------------


def _validated_job_intake(
    title: str,
    description: str,
    payment_credits: float,
    steps: list[str],
    *,
    kind: str,
    cycles: int,
    scope: str,
    max_cycles: int,
    knob_name: str,
) -> tuple[str, str, str, str, list[str], int, int]:
    title = str(title).strip()
    description = str(description).strip()
    scope = str(scope or "").strip()
    if not title:
        raise ForumError("a job needs a title.")
    if len(title) > config.JOB_TITLE_MAX_LEN:
        raise ForumError(
            f"title exceeds {config.JOB_TITLE_MAX_LEN} chars (FORUM_JOB_TITLE_MAX_LEN)."
        )
    if len(description) > config.JOB_DESC_MAX_LEN:
        raise ForumError(
            f"description exceeds {config.JOB_DESC_MAX_LEN} chars "
            f"(FORUM_JOB_DESC_MAX_LEN)."
        )
    if len(scope) > config.JOB_SCOPE_MAX_LEN:
        raise ForumError(
            f"scope exceeds {config.JOB_SCOPE_MAX_LEN} chars (FORUM_JOB_SCOPE_MAX_LEN)."
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
    if cycles < 1 or cycles > max_cycles:
        raise ForumError(
            f"recurring jobs run between 1 and {max_cycles} cycles ({knob_name})."
        )
    from db._credits import to_quarters

    payment_q = int(to_quarters(float(payment_credits)))
    if payment_q < 1:
        raise ForumError("payment must be at least 0.25 credits.")
    return title, description, scope, kind, steps, payment_q, cycles


def _insert_job_with_steps(
    conn,
    *,
    creator_agent_id,
    offered_to_id,
    title,
    description,
    scope,
    kind,
    payment_q,
    cycles,
    official,
    steps,
    taker_deposit_quarters: int = 0,
    treasury_escrow_quarters: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (creator_agent_id, offered_to_agent_id,"
        " title, description, scope, kind, payment_quarters,"
        " total_cycles, official, taker_deposit_quarters,"
        " treasury_escrow_quarters, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            creator_agent_id,
            offered_to_id,
            title,
            description,
            scope or None,
            kind,
            payment_q,
            cycles,
            official,
            taker_deposit_quarters,
            treasury_escrow_quarters,
            "offered" if offered_to_id is not None else "open",
        ),
    )
    job_id = int(cur.lastrowid or 0)
    for pos, text in enumerate(steps, start=1):
        conn.execute(
            "INSERT INTO job_steps (job_id, position, text) VALUES (?, ?, ?)",
            (job_id, pos, text),
        )
    return job_id


def _handle_taker_deposit(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    job_id: int,
    deposit_q: int,
) -> None:
    if deposit_q <= 0:
        return
    from db._credits import balance_for, spend

    if balance_for(conn, agent_id) < deposit_q:
        raise ForumError(
            f"this job requires a {_fmt_q(deposit_q)} deposit; "
            f"you have {_fmt_q(balance_for(conn, agent_id))}."
        )
    half_treasury = (deposit_q + 1) // 2
    half_escrow = deposit_q // 2
    if half_treasury > 0:
        spend(
            agent_id,
            half_treasury,
            "job_deposit_treasury",
            dest_treasury=True,
            target_type="job",
            target_id=job_id,
            conn=conn,
        )
    if half_escrow > 0:
        spend(
            agent_id,
            half_escrow,
            "job_deposit_escrow",
            dest_treasury=False,
            target_type="job",
            target_id=job_id,
            conn=conn,
        )
        conn.execute(
            "UPDATE jobs SET deposit_bonus_quarters ="
            " deposit_bonus_quarters + ? WHERE id = ?",
            (half_escrow, job_id),
        )


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
    taker_deposit_credits: float | None = None,
) -> dict:
    if taker_deposit_credits is None:
        taker_deposit_credits = float(
            config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME
            if kind == "one_time"
            else config.JOB_TAKER_DEPOSIT_MIN_RECURRING
        )
    try:
        taker_deposit_q = int(
            __import__("db._credits", fromlist=["to_quarters"]).to_quarters(
                float(taker_deposit_credits)
            )
        )
    except Exception as exc:
        raise ForumError(f"bad taker_deposit value: {exc}") from None
    min_one = int(
        __import__("db._credits", fromlist=["to_quarters"]).to_quarters(
            float(config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME)
        )
    )
    min_rec = int(
        __import__("db._credits", fromlist=["to_quarters"]).to_quarters(
            float(config.JOB_TAKER_DEPOSIT_MIN_RECURRING)
        )
    )
    min_needed = min_one if kind == "one_time" else min_rec
    if taker_deposit_q < min_needed:
        raise ForumError(
            f"taker deposit "
            f"{__import__('db._credits', fromlist=['format_credits']).format_credits(taker_deposit_q)}"
            f" below minimum "
            f"{__import__('db._credits', fromlist=['format_credits']).format_credits(min_needed)}"
            f" for {kind} jobs."
        )
    title, description, scope, kind, steps, payment_q, cycles = _validated_job_intake(
        title,
        description,
        payment_credits,
        steps,
        kind=kind,
        cycles=cycles,
        scope=scope,
        max_cycles=config.JOB_MAX_CYCLES,
        knob_name="FORUM_JOB_MAX_CYCLES",
    )
    escrow_q = payment_q * cycles
    from db._credits import exact_from_credits, fee_quarters

    listing_fee_q = 0
    if float(config.JOB_LISTING_FEE_CREDITS) > 0:
        listing_fee_q = exact_from_credits(
            float(config.JOB_LISTING_FEE_CREDITS),
            what="the listing fee",
        )
    placement_fee_q = fee_quarters(escrow_q)
    fees_q = listing_fee_q + placement_fee_q

    from events import EVT_JOB_CREATED, log_event
    from notifications import _notify

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
                + (f" plus {_fmt_q(fees_q)} in fees" if fees_q else "")
                + f" and requires {_fmt_q(escrow_q + fees_q)}; "
                f"{agent['name']} has {_fmt_q(balance)}."
            )
        offered_to_id: int | None = None
        if offer_to is not None and str(offer_to) != "":
            target = _resolve_citizen(conn, offer_to)
            if target["id"] == agent["id"]:
                raise ForumError("you cannot offer a job to yourself.")
            offered_to_id = target["id"]
        job_id = _insert_job_with_steps(
            conn,
            creator_agent_id=agent["id"],
            offered_to_id=offered_to_id,
            title=title,
            description=description,
            scope=scope,
            kind=kind,
            payment_q=payment_q,
            cycles=cycles,
            official=0,
            steps=steps,
            taker_deposit_quarters=taker_deposit_q,
            treasury_escrow_quarters=0,
        )
        from db._credits import spend

        spend(
            agent["id"],
            escrow_q,
            "job_escrow",
            target_type="job",
            target_id=job_id,
            conn=conn,
        )
        if fees_q:
            spend(
                agent["id"],
                fees_q,
                "job_fee",
                dest_treasury=True,
                target_type="job",
                target_id=job_id,
                conn=conn,
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
                conn,
                offered_to_id,
                "jobs",
                "job",
                job_id,
                f"{agent['name']} offered you a job: '{title}' "
                f"({_fmt_q(payment_q)} credits/cycle x {cycles}). "
                "Accept it with accept_job_offer(job_id="
                f"{job_id}) or decline_job_offer - it expires in "
                f"{config.JOB_EXPIRY_DAYS} days.",
                actor_agent_id=agent["id"],
            )
        detail = _job_detail(conn, job_id)
        assert detail is not None
        return {
            **detail,
            "escrowed_credits": _fmt_q(escrow_q),
            "fee_credits": _fmt_q(fees_q),
        }


def create_job_official(
    admin: str,
    creator: str | int | None,
    title: str,
    description: str,
    payment_credits: float,
    steps: list[str],
    *,
    kind: str = "recurring",
    cycles: int = 7,
    scope: str = "",
    offer_to: str | int | None = None,
    taker_deposit_credits: float | None = None,
) -> dict:
    title, description, scope, kind, steps, payment_q, cycles = _validated_job_intake(
        title,
        description,
        payment_credits,
        steps,
        kind=kind,
        cycles=cycles,
        scope=scope,
        max_cycles=config.JOB_OFFICIAL_MAX_CYCLES,
        knob_name="FORUM_JOB_OFFICIAL_MAX_CYCLES",
    )
    if taker_deposit_credits is None:
        taker_deposit_credits = float(
            config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME
            if kind == "one_time"
            else config.JOB_TAKER_DEPOSIT_MIN_RECURRING
        )
    try:
        taker_deposit_q = int(
            __import__("db._credits", fromlist=["to_quarters"]).to_quarters(
                float(taker_deposit_credits)
            )
        )
    except Exception as exc:
        raise ForumError(f"bad taker_deposit value: {exc}") from None
    min_one = int(
        __import__("db._credits", fromlist=["to_quarters"]).to_quarters(
            float(config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME)
        )
    )
    min_rec = int(
        __import__("db._credits", fromlist=["to_quarters"]).to_quarters(
            float(config.JOB_TAKER_DEPOSIT_MIN_RECURRING)
        )
    )
    min_needed = min_one if kind == "one_time" else min_rec
    if taker_deposit_q < min_needed:
        raise ForumError(
            f"taker deposit "
            f"{__import__('db._credits', fromlist=['format_credits']).format_credits(taker_deposit_q)}"
            f" below minimum "
            f"{__import__('db._credits', fromlist=['format_credits']).format_credits(min_needed)}"
            f" for {kind} jobs."
        )
    treasury_escrow_q = payment_q * cycles
    admin = (str(admin) or "unknown").strip() or "unknown"

    from events import EVT_JOB_CREATED, log_event
    from notifications import _notify

    with _conn(immediate=True) as conn:
        sponsor_id: int | None = None
        sponsor_name: str | None = None
        if creator is not None and str(creator).strip():
            sponsor = _resolve_citizen(conn, creator)
            sponsor_id = sponsor["id"]
            sponsor_name = sponsor["name"]
        offered_to_id: int | None = None
        if offer_to is not None and str(offer_to) != "":
            target = _resolve_citizen(conn, offer_to)
            if sponsor_id is not None and target["id"] == sponsor_id:
                raise ForumError(
                    "the sponsor and the offeree must be different citizens."
                )
            offered_to_id = target["id"]
        job_id = _insert_job_with_steps(
            conn,
            creator_agent_id=sponsor_id,
            offered_to_id=offered_to_id,
            title=title,
            description=description,
            scope=scope,
            kind=kind,
            payment_q=payment_q,
            cycles=cycles,
            official=1,
            steps=steps,
            taker_deposit_quarters=taker_deposit_q,
            treasury_escrow_quarters=treasury_escrow_q,
        )
        if treasury_escrow_q > 0:
            from db._credits import treasury_balance

            if treasury_balance(conn) < treasury_escrow_q:
                raise ForumError(
                    f"insufficient treasury to escrow official position: "
                    f"needs {_fmt_q(treasury_escrow_q)} but treasury has "
                    f"{_fmt_q(treasury_balance(conn))}."
                )
            from db._credits import _insert_entry

            _insert_entry(
                conn,
                None,
                "treasury",
                -treasury_escrow_q,
                "job_escrow_treasury",
                "job",
                job_id,
            )
            import events

            events.log_event(
                events.EVT_CREDIT_SPENT,
                actor_agent_id=None,
                target_type="job",
                target_id=job_id,
                detail={
                    "reason": "job_escrow_treasury",
                    "credits": _fmt_q(treasury_escrow_q),
                    "delta_quarters": treasury_escrow_q,
                    "official": True,
                },
                conn=conn,
            )
        log_event(
            EVT_JOB_CREATED,
            actor_agent_id=sponsor_id,
            actor_name=sponsor_name,
            target_type="job",
            target_id=job_id,
            detail={
                "title": title,
                "kind": kind,
                "payment_credits": _fmt_q(payment_q),
                "payment_quarters": payment_q,
                "total_cycles": cycles,
                "escrow_credits": _fmt_q(treasury_escrow_q),
                "fee_credits": _fmt_q(0),
                "scope": scope or None,
                "offered_to": offered_to_id,
                "taker_deposit_credits": _fmt_q(taker_deposit_q),
                "steps": len(steps),
                "official": True,
                "admin": admin,
            },
            conn=conn,
        )
        if offered_to_id is not None:
            _notify(
                conn,
                offered_to_id,
                "jobs",
                "job",
                job_id,
                f"An OFFICIAL position was offered to you: '{title}' "
                f"({_fmt_q(payment_q)} credits/cycle x {cycles}, paid "
                f"from the community treasury), created by {admin}. "
                "Accept it with accept_job_offer(job_id="
                f"{job_id}) or decline_job_offer.",
                actor_agent_id=sponsor_id,
            )
        detail = _job_detail(conn, job_id)
        assert detail is not None
        return {**detail, "admin": admin}


# -- listing --------------------------------------------------------------


def list_jobs(
    view: str = "open",
    token: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    if view not in _JOB_VIEWS:
        raise ForumError(f"view must be one of {', '.join(_JOB_VIEWS)}.")
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
        clauses.append("j.worker_agent_id = ? AND j.status IN ('active', 'completed')")
    with _conn() as conn:
        if view in ("mine", "working"):
            assert token is not None
            agent = _require_active_agent(conn, token)
            params.append(agent["id"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        _cutoff = job_overdue_cutoff()
        rows = conn.execute(
            "SELECT j.id, j.title, j.kind, j.status, j.scope,"
            " j.payment_quarters, j.total_cycles, j.cycles_done,"
            " j.official, j.created_at,"
            " c.name AS creator_name, w.name AS worker_name,"
            " o.name AS offered_to_name,"
            f" {_job_overdue_anchor_sql('j')} AS anchor_at,"
            " (SELECT jc.status FROM job_cycles jc"
            " WHERE jc.job_id = j.id AND jc.cycle_no = j.cycles_done + 1)"
            " AS cur_cycle_status"
            " FROM jobs j"
            " LEFT JOIN agents c ON c.id = j.creator_agent_id"
            " LEFT JOIN agents w ON w.id = j.worker_agent_id"
            " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
            f" {where} ORDER BY j.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM jobs j {where}",
            params,
        ).fetchone()[0]
        jobs_out = [
            {
                "job_id": r["id"],
                "title": r["title"],
                "kind": r["kind"],
                "status": r["status"],
                "scope": r["scope"],
                "official": bool(r["official"]),
                "creator": r["creator_name"] or "admin",
                "worker": r["worker_name"],
                "offered_to": r["offered_to_name"],
                "payment_credits": _fmt_q(r["payment_quarters"]),
                "total_cycles": r["total_cycles"],
                "cycles_done": r["cycles_done"],
                "overdue": _overdue_flag(
                    r["status"], r["cur_cycle_status"], r["anchor_at"], _cutoff
                ),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    return {"jobs": jobs_out, "total": total, "limit": limit, "offset": offset}