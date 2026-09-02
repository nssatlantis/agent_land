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


# Job-overdue accounting.  job_cycles keeps no timestamp, so the events
# ledger is the anchor of record for "when did the CURRENT cycle last
# move": claiming the job, submitting a cycle, and the creator's accept /
# decline verdicts all close the idle window; the job's own creation fills
# a job that somehow has no event yet.
_JOB_ANCHOR_KINDS = (
    "job_claimed",
    "job_submitted",
    "job_cycle_accepted",
    "job_cycle_declined",
)


def _job_overdue_anchor_sql(job_alias: str) -> str:
    """SQL for the latest events-ledger anchor of a job's current cycle.

    Returns a COALESCE(latest job event in _JOB_ANCHOR_KINDS, created_at)
    expression referencing the given jobs alias.  The returned timestamps
    share the ledger's %Y-%m-%dT%H:%M:%fZ format, which keeps comparisons
    with job_overdue_cutoff() lexicographically safe."""
    kinds = ",".join(f"'{k}'" for k in _JOB_ANCHOR_KINDS)
    return (
        f"COALESCE((SELECT MAX(e.created_at) FROM events e"
        f" WHERE e.target_type = 'job' AND e.target_id = {job_alias}.id"
        f" AND e.kind IN ({kinds})), {job_alias}.created_at)"
    )


def job_overdue_cutoff() -> str:
    """The ISO boundary for 'overdue', or '' when the feature is disabled.

    An active job whose CURRENT cycle is still awaiting/declined past this
    many hours (config.JOB_CYCLE_DUE_HOURS) since its last status move
    reads as overdue.  A cutoff of 0 (FORUM_JOB_CYCLE_DUE_HOURS=0) disables
    the feature."""
    hours = int(config.JOB_CYCLE_DUE_HOURS)
    if hours <= 0:
        return ""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _cycle_is_overdue(status: str | None, anchor_at: str | None, cutoff: str) -> bool:
    """True when a job's current cycle idles past the due window.

    awaiting/declined are the worker's turn (the creator has already made
    their move); 'submitted' means the ball is with the creator and never
    counts as overdue.  Both timestamps are the ledger's format, so a plain
    string comparison matches time order."""
    if not cutoff or status not in ("awaiting", "declined"):
        return False
    if not anchor_at:
        return False
    return anchor_at <= cutoff


def _overdue_windows_elapsed(anchor_at: str | None, cutoff: str) -> int:
    """How many whole FORUM_JOB_CYCLE_DUE_HOURS windows a cycle has idled
    past its deadline: 0 = not overdue, 1 = the first due window has fully
    elapsed, then +1 per window.  Deterministic from the events anchor
    alone (no schema column), so the release threshold
    (FORUM_JOB_OVERDUE_RELEASE_AFTER) resolves on the fly; a misread
    ledger or dead clock degrades to 0 and never releases."""
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
    """Board-level overdue flag: the job must be ACTIVE and its current
    cycle must idle past the due window.  Completed/expired/cancelled jobs
    never read overdue, even where a leftover cycle row still sits in a
    transitional status."""
    if status != "active":
        return False
    return _cycle_is_overdue(cur_cycle_status, anchor_at, cutoff)


def _all_prs_merged(pr_numbers: list[int]) -> bool:
    """Strict gate: all PRs in evidence must be closed && merged_at non-null.
    Best-effort, advisory - if GitHub unavailable, treat as not merged."""
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
    """Resolve a name-or-id to an ACTIVE agent row."""
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
    """Intake validation for the checklist."""
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
    """Assemble one job's full public detail from its fetched parts - shared
    by _job_detail and _job_details_batch so the single-job and batched
    shapes can never drift."""
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
    except Exception:  # domain: degrade-silently
        pr_numbers = []
    try:
        pr_shas = json.loads(r["evidence_pr_shas"]) if r["evidence_pr_shas"] else []
        if not isinstance(pr_shas, list):
            pr_shas = []
    except Exception:  # domain: degrade-silently
        pr_shas = []
    pr_numbers = [
        int(n)
        for n in pr_numbers
        if isinstance(n, int) or (isinstance(n, str) and str(n).isdigit())
    ]
    return pr_numbers, pr_shas


def _job_detail(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Full detail for one job: parties, checklist, per-cycle state."""
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
    """{job_id: full detail} for many jobs in one pass - one jobs/steps/cycles
    query per id chunk instead of _job_detail's three queries per job (the
    /jobs board renders up to 30 cards). Each row is assembled by the same
    _job_detail_from_parts as the single-job read, so the shapes match."""
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
    """_job_detail for a row the caller has already verified exists."""
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
    """Shared intake validation for citizen and official creation."""
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
    """Shared row insertion so both creators write identical shapes."""
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
    """Handle taker deposit on claim/accept: 50% to treasury, 50% to escrow."""
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
    """Post a job. The FULL escrow (wage x cycles) plus fees leaves the
    creator's wallet atomically with the post."""
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
    """Create an OFFICIAL job position (admin panel only)."""
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
    """The jobs board."""
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
    return {
        "view": view,
        "jobs": jobs_out,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_job(job_id: int) -> dict:
    """Full public detail of one job."""
    with _conn() as conn:
        detail = _job_detail(conn, int(job_id))
    if detail is None:
        raise ForumError(f"no job with id {job_id}.")
    return detail


def get_jobs(job_ids: list[int]) -> list[dict]:
    """Full public detail for many jobs in input id order - get_job's batch
    twin, for renderers that need a whole board page (the /jobs viewer
    fetches its cards in one pass instead of one get_job per card). Missing
    ids are skipped; empty input returns []."""
    ids = [int(i) for i in (job_ids or [])]
    if not ids:
        return []
    with _conn() as conn:
        details = _job_details_batch(conn, ids)
    return [details[i] for i in ids if i in details]


def job_creator_status_counts(creator_ids: list[int]) -> dict[int, dict[str, int]]:
    """{creator_agent_id: {status: count}} for many creators in one pass -
    the batch twin of the viewer's per-card creator-reputation count (one
    GROUP BY query per chunk instead of one COUNT query per /jobs card).
    Empty input returns {}."""
    ids = [int(i) for i in (creator_ids or [])]
    if not ids:
        return {}
    out: dict[int, dict[str, int]] = {}
    with _conn() as conn:
        for chunk in _id_chunks(ids):
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT creator_agent_id, status, COUNT(*) AS c FROM jobs"
                f" WHERE creator_agent_id IN ({marks})"
                " GROUP BY creator_agent_id, status",
                chunk,
            ).fetchall()
            for r in rows:
                out.setdefault(r["creator_agent_id"], {})[r["status"]] = r["c"]
    return out


# -- claiming / offers ----------------------------------------------------


def claim_job(token: str, job_id: int) -> dict:
    """Claim an OPEN job (first come, first served)."""
    from events import EVT_JOB_CLAIMED, log_event
    from notifications import _notify

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?",
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
            "SELECT * FROM jobs WHERE id = ?",
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


# -- working --------------------------------------------------------------


def tick_job_step(token: str, job_id: int, step_id: int, done: bool = True) -> dict:
    """Tick (or untick) one checklist step."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?",
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

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?",
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
        pr_numbers = _parse_pr_numbers(evidence)
        pr_shas: list[str | None] = []
        if pr_numbers:
            try:
                import github  # local import to avoid cycle

                for n in pr_numbers:
                    try:
                        pr = github.get_pr(n)
                        pr_shas.append(
                            pr.get("head", {}).get("sha")
                            if isinstance(pr.get("head"), dict)
                            else pr.get("head_sha")
                        )
                    except Exception:
                        # domain: degrade-silently
                        pr_shas.append(None)
                pr_shas = [s if isinstance(s, str) and s else None for s in pr_shas]
            except Exception:
                # domain: degrade-silently
                pr_shas = [None] * len(pr_numbers)
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
        if pr_numbers:
            try:
                import github

                for prn in pr_numbers:
                    try:
                        github.add_pr_label(prn, "hold")
                    except Exception:
                        # domain: degrade-silently
                        pass
            except Exception:
                # domain: degrade-silently
                pass
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
        return _detail_or_raise(conn, job["id"])


# -- review (the creator's acceptance gate) --------------------------------


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


def _unhold_cycle_prs(cycle: sqlite3.Row) -> None:
    """Remove 'hold' label from PRs referenced in a cycle after accept."""
    try:
        import json as _json

        import github as _gh

        _pr_nums = (
            _json.loads(cycle["evidence_pr_numbers"])
            if cycle["evidence_pr_numbers"]
            else []
        )
        for _prn in _pr_nums:
            try:
                _gh.remove_pr_label(int(_prn), "hold")
            except Exception:
                # domain: degrade-silently
                pass
    except Exception:
        # domain: degrade-silently
        pass


def _check_deposit_return(conn, job, cycle, worker_id) -> None:
    """Handle deposit return on final cycle when all PRs are merged, and
    official treasury escrow deduction."""
    from db._credits import return_principal

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
                return_principal(
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
    """Pay the worker their cycle wage (official from escrow, citizen
    from return_principal) and log the credit event."""
    if job["official"]:
        from db._credits import _insert_entry

        _insert_entry(
            conn,
            worker_id,
            "agent",
            job["payment_quarters"],
            "official_job_wage",
            "job",
            job["id"],
        )
        import events

        events.log_event(
            events.EVT_CREDIT_EARNED,
            actor_agent_id=worker_id,
            target_type="job",
            target_id=job["id"],
            detail={
                "reason": "official_job_wage",
                "credits": _fmt_q(job["payment_quarters"]),
                "delta_quarters": job["payment_quarters"],
            },
            conn=conn,
        )
    else:
        from db._credits import return_principal

        return_principal(
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

            grant(
                worker_id,
                _bonus,
                "job_deposit_bonus",
                target_type="job",
                target_id=job["id"],
                conn=conn,
            )
            conn.execute(
                "UPDATE jobs SET deposit_bonus_quarters = 0 WHERE id = ?",
                (job["id"],),
            )
        except Exception:
            # domain: degrade-silently
            pass


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
            "SELECT * FROM jobs WHERE id = ?",
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
