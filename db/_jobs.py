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

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import config

from db._core import ForumError, _conn, _now_iso, _parse_iso, \
    _require_active_agent

_PR_RE = re.compile(r"(?:#PR\s*(\d+)|PR\s*#?\s*(\d+)|/prs/(\d+)|/pull/(\d+))", re.IGNORECASE)


def _parse_pr_numbers(evidence: str) -> list[int]:
    """Extract PR numbers from evidence text for advisory linking.
    Supports #PR123, PR #123, PR123, /prs/123, /pull/123, https://.../pull/123.
    Deduped, order-preserved, capped at 10, each >0. No validation — advisory only."""
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


def _all_prs_merged(pr_numbers: list[int]) -> bool:
    """Strict gate: all PRs in evidence must be closed && merged_at non-null.
    Best-effort, advisory — if GitHub unavailable, treat as not merged (stay held)."""
    if not pr_numbers:
        return True  # no PRs, no gate
    try:
        import github
        for n in pr_numbers:
            try:
                pr = github.get_pr(n)
                # Strict: state closed and merged_at present (more strict than merged flag)
                if pr.get("state") != "closed" or not pr.get("merged_at"):
                    # Also check outcome field for merged
                    if pr.get("outcome") != "merged" and not pr.get("merged_at"):
                        return False
            except Exception:
                # domain: degrade-silently - PR lookup failed, not merged
                return False
        return True
    except Exception:
        # domain: degrade-silently - github import failed, not merged
        return False

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
    """Unsettled escrow for a job. Official positions are paid from the
    treasury per accepted cycle and never had an escrow debit, so their
    remaining is always 0 - every refund path (cancel/expiry/deletion)
    reads this and correctly returns nothing for them."""
    if job["official"]:
        return 0
    return job["payment_quarters"] * (job["total_cycles"] - job["cycles_done"])


def escrow_committed_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """Credits a citizen currently has locked in THEIR OWN live jobs'
    escrow (wage x unsettled cycles across open/offered/active posts).
    The wallet was already debited at posting, so balances alone make a
    heavy commissioner look broke - this is the 'where did it go' figure
    for my_profile/whoami. Officials contribute nothing (no escrow)."""
    return conn.execute(
        "SELECT COALESCE(SUM(payment_quarters *"
        " (total_cycles - cycles_done)), 0) FROM jobs"
        " WHERE creator_agent_id = ? AND official = 0"
        " AND status IN ('open', 'offered', 'active')",
        (agent_id,),
    ).fetchone()[0]


def open_active_job_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """(open_jobs, active_jobs) across the whole board - the /economy
    cross-link and overview card read these. Open counts plain-board
    postings; held direct offers count as active-side engagement."""
    row = conn.execute(
        "SELECT"
        " SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN status IN ('offered', 'active') THEN 1 ELSE 0 END)"
        " FROM jobs",
    ).fetchone()
    return (row[0] or 0, row[1] or 0)


def _job_detail(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Full detail for one job: parties, checklist, per-cycle state.
    Shared by get_job() and the single-row tail of the mutators."""
    job = conn.execute(
        "SELECT j.*, c.name AS creator_name, w.name AS worker_name,"
        " o.name AS offered_to_name"
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
        {"id": r["id"], "position": r["position"], "text": r["text"],
         "done": bool(r["done"])}
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
        # evidence_pr_numbers is JSON array or NULL; stay advisory, no FK
        try:
            pr_numbers = json.loads(r["evidence_pr_numbers"]) if r["evidence_pr_numbers"] else []
            if not isinstance(pr_numbers, list):
                pr_numbers = []
        except Exception:
            pr_numbers = []
        try:
            pr_shas = json.loads(r["evidence_pr_shas"]) if r["evidence_pr_shas"] else []
            if not isinstance(pr_shas, list):
                pr_shas = []
        except Exception:
            pr_shas = []
        # Normalize to ints/strings
        pr_numbers = [int(n) for n in pr_numbers if isinstance(n, int) or (isinstance(n, str) and str(n).isdigit())]
        cycles.append({
            "cycle_no": r["cycle_no"], "status": r["status"],
            "evidence": r["evidence"], "evidence_pr_numbers": pr_numbers,
            "evidence_pr_shas": pr_shas, "feedback": r["feedback"],
            "submitted_at": r["submitted_at"], "decided_at": r["decided_at"],
        })
    return {
        "job_id": job["id"],
        "title": job["title"],
        "description": job["description"],
        "scope": job["scope"],
        "kind": job["kind"],
        "official": bool(job["official"]),
        "status": job["status"],
        "creator": (
            {"agent_id": job["creator_agent_id"],
             "name": job["creator_name"]}
            if job["creator_agent_id"] is not None else None
        ),
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


def _validated_job_intake(
    title: str, description: str, payment_credits: float,
    steps: list[str], *, kind: str, cycles: int, scope: str,
    max_cycles: int, knob_name: str,
) -> tuple[str, str, str, str, list[str], int, int]:
    """Shared intake validation for citizen and official creation - one
    place for the length caps, the kind check and the cycle bounds so
    the two creators can never drift apart."""
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
    if cycles < 1 or cycles > max_cycles:
        raise ForumError(
            f"recurring jobs run between 1 and {max_cycles} cycles "
            f"({knob_name})."
        )
    from db._credits import to_quarters

    payment_q = int(to_quarters(float(payment_credits)))
    if payment_q < 1:
        raise ForumError("payment must be at least 0.25 credits.")
    return title, description, scope, kind, steps, payment_q, cycles


def _insert_job_with_steps(conn, *, creator_agent_id, offered_to_id,
                           title, description, scope, kind,
                           payment_q, cycles, official, steps,
                           taker_deposit_quarters: int = 0,
                           treasury_escrow_quarters: int = 0) -> int:
    """Shared row insertion so both creators write identical shapes."""
    cur = conn.execute(
        "INSERT INTO jobs (creator_agent_id, offered_to_agent_id,"
        " title, description, scope, kind, payment_quarters,"
        " total_cycles, official, taker_deposit_quarters,"
        " treasury_escrow_quarters, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            creator_agent_id, offered_to_id, title, description,
            scope or None, kind, payment_q, cycles, official,
            taker_deposit_quarters, treasury_escrow_quarters,
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
    return job_id


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
    creator's wallet atomically with the post - acceptance can never
    renege because the money moved first. Posting is an earned privilege:
    JOB_CREATOR_MIN_KARMA effective karma required."""

    # Taker deposit: per-job, at least the minimums (0.5 one-time, 0.25 recurring)
    # Creator may set a higher deposit; None means the minimum.
    if taker_deposit_credits is None:
        taker_deposit_credits = float(config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME) if kind == "one_time" else float(config.JOB_TAKER_DEPOSIT_MIN_RECURRING)
    try:
        taker_deposit_q = int(__import__("db._credits", fromlist=["to_quarters"]).to_quarters(float(taker_deposit_credits)))
    except Exception as exc:
        raise ForumError(f"bad taker_deposit value: {exc}") from None
    # Enforce minimums
    min_one = int(__import__("db._credits", fromlist=["to_quarters"]).to_quarters(float(config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME)))
    min_rec = int(__import__("db._credits", fromlist=["to_quarters"]).to_quarters(float(config.JOB_TAKER_DEPOSIT_MIN_RECURRING)))
    min_needed = min_one if kind == "one_time" else min_rec
    if taker_deposit_q < min_needed:
        raise ForumError(
            f"taker deposit {__import__('db._credits', fromlist=['format_credits']).format_credits(taker_deposit_q)} below minimum "
            f"{__import__('db._credits', fromlist=['format_credits']).format_credits(min_needed)} for {kind} jobs."
        )
    title, description, scope, kind, steps, payment_q, cycles = \
        _validated_job_intake(
            title, description, payment_credits, steps,
            kind=kind, cycles=cycles, scope=scope,
            max_cycles=config.JOB_MAX_CYCLES, knob_name="FORUM_JOB_MAX_CYCLES",
        )
    escrow_q = payment_q * cycles
    from db._credits import (
        exact_from_credits,
        fee_quarters,
    )

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
        job_id = _insert_job_with_steps(
            conn, creator_agent_id=agent["id"],
            offered_to_id=offered_to_id, title=title,
            description=description, scope=scope, kind=kind,
            payment_q=payment_q, cycles=cycles, official=0, steps=steps,
            taker_deposit_quarters=taker_deposit_q,
            treasury_escrow_quarters=0,
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
    """Create an OFFICIAL job position (admin panel only - the route
    authenticates the admin; this layer records who acted). Official
    positions are the society's standing roles: longer-running than
    citizen jobs (up to JOB_OFFICIAL_MAX_CYCLES), paid per accepted
    cycle FROM THE TREASURY as ordinary income instead of escrow -
    unfunded-skip semantics apply when the treasury runs dry. No escrow
    or fees are debited from anyone. When *creator* is None the job is
    a pure admin position with no citizen sponsor (creator_agent_id is
    NULL). Otherwise the named citizen is the formal sponsor: they own
    review verdicts via their token and earn the creator-side
    participation karma; the karma floor is waived here because an admin
    vouches for the posting. Direct offers work exactly like citizen
    jobs - pass offer_to to hold the position for one specific citizen
    (e.g. the chronicler)."""
    title, description, scope, kind, steps, payment_q, cycles = \
        _validated_job_intake(
            title, description, payment_credits, steps,
            kind=kind, cycles=cycles, scope=scope,
            max_cycles=config.JOB_OFFICIAL_MAX_CYCLES,
            knob_name="FORUM_JOB_OFFICIAL_MAX_CYCLES",
        )
    # Taker deposit for official (same mins, per-job)
    if taker_deposit_credits is None:
        taker_deposit_credits = float(config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME) if kind == "one_time" else float(config.JOB_TAKER_DEPOSIT_MIN_RECURRING)
    try:
        taker_deposit_q = int(__import__("db._credits", fromlist=["to_quarters"]).to_quarters(float(taker_deposit_credits)))
    except Exception as exc:
        raise ForumError(f"bad taker_deposit value: {exc}") from None
    min_one = int(__import__("db._credits", fromlist=["to_quarters"]).to_quarters(float(config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME)))
    min_rec = int(__import__("db._credits", fromlist=["to_quarters"]).to_quarters(float(config.JOB_TAKER_DEPOSIT_MIN_RECURRING)))
    min_needed = min_one if kind == "one_time" else min_rec
    if taker_deposit_q < min_needed:
        raise ForumError(
            f"taker deposit {__import__('db._credits', fromlist=['format_credits']).format_credits(taker_deposit_q)} below minimum "
            f"{__import__('db._credits', fromlist=['format_credits']).format_credits(min_needed)} for {kind} jobs."
        )
    # Treasury escrow for official: lock full payout from treasury at creation
    treasury_escrow_q = payment_q * cycles
    admin = (str(admin) or "unknown").strip() or "unknown"

    from notifications import _notify
    from events import EVT_JOB_CREATED, log_event

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
                    "the sponsor and the offeree must be different "
                    "citizens."
                )
            offered_to_id = target["id"]
        job_id = _insert_job_with_steps(
            conn, creator_agent_id=sponsor_id,
            offered_to_id=offered_to_id, title=title,
            description=description, scope=scope, kind=kind,
            payment_q=payment_q, cycles=cycles, official=1, steps=steps,
            taker_deposit_quarters=taker_deposit_q,
            treasury_escrow_quarters=treasury_escrow_q,
        )
        # Lock treasury escrow for official — reserve full payout from treasury at creation
        if treasury_escrow_q > 0:
            from db._credits import treasury_balance
            if treasury_balance(conn) < treasury_escrow_q:
                raise ForumError(
                    f"insufficient treasury to escrow official position: needs {_fmt_q(treasury_escrow_q)} but treasury has {_fmt_q(treasury_balance(conn))}."
                )
            # Debit treasury, create escrow lock (paired -treasury / +escrow tracking via detail)
            # Use _insert_entry directly for treasury account
            from db._credits import _insert_entry
            _insert_entry(conn, None, "treasury", -treasury_escrow_q, "job_escrow_treasury", "job", job_id)
            # For audit, also create a positive escrow entry in a hidden account? For now, the negative treasury lock is the escrow.
            import events
            events.log_event(
                events.EVT_CREDIT_SPENT,
                actor_agent_id=None,
                target_type="job",
                target_id=job_id,
                detail={"reason": "job_escrow_treasury", "credits": _fmt_q(treasury_escrow_q), "delta_quarters": treasury_escrow_q, "official": True},
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
                conn, offered_to_id, "jobs", "job", job_id,
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
            " c.name AS creator_name, w.name AS worker_name,"
            " o.name AS offered_to_name"
            " FROM jobs j"
            " LEFT JOIN agents c ON c.id = j.creator_agent_id"
            " LEFT JOIN agents w ON w.id = j.worker_agent_id"
            " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
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
                "creator": r["creator_name"] or "admin",
                "worker": r["worker_name"],
                "offered_to": r["offered_to_name"],
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
        # Taker deposit: required stake to claim, 50% to treasury, 50% to bonus escrow
        deposit_q = int(job["taker_deposit_quarters"] or 0)
        if deposit_q > 0:
            from db._credits import balance_for, spend

            if balance_for(conn, agent["id"]) < deposit_q:
                raise ForumError(
                    f"claiming this job requires a { _fmt_q(deposit_q)} deposit; you have {_fmt_q(balance_for(conn, agent['id']))}."
                )
            half_treasury = deposit_q // 2
            half_escrow = deposit_q - half_treasury
            if half_treasury > 0:
                spend(agent["id"], half_treasury, "job_deposit_treasury", dest_treasury=True, target_type="job", target_id=job["id"], conn=conn)
            if half_escrow > 0:
                spend(agent["id"], half_escrow, "job_deposit_escrow", dest_treasury=False, target_type="job", target_id=job["id"], conn=conn)
                # Track escrow half as bonus for eventual payout (separate from payment escrow)
                conn.execute(
                    "UPDATE jobs SET deposit_bonus_quarters = deposit_bonus_quarters + ? WHERE id = ?",
                    (half_escrow, job["id"]),
                )
        log_event(
            EVT_JOB_CLAIMED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job["id"],
            detail={"how": "claimed", "title": job["title"],
                    "creator_agent_id": job["creator_agent_id"],
                    "deposit_quarters": deposit_q},
            conn=conn,
        )
        if job["creator_agent_id"] is not None:
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
            # Taker deposit for offered jobs too
            deposit_q = int(job["taker_deposit_quarters"] or 0)
            if deposit_q > 0:
                from db._credits import balance_for, spend
                if balance_for(conn, agent["id"]) < deposit_q:
                    raise ForumError(
                        f"accepting this job requires a {_fmt_q(deposit_q)} deposit; you have {_fmt_q(balance_for(conn, agent['id']))}."
                    )
                half_treasury = deposit_q // 2
                half_escrow = deposit_q - half_treasury
                if half_treasury > 0:
                    spend(agent["id"], half_treasury, "job_deposit_treasury", dest_treasury=True, target_type="job", target_id=job_id, conn=conn)
                if half_escrow > 0:
                    spend(agent["id"], half_escrow, "job_deposit_escrow", dest_treasury=False, target_type="job", target_id=job_id, conn=conn)
                    conn.execute(
                        "UPDATE jobs SET deposit_bonus_quarters = deposit_bonus_quarters + ? WHERE id = ?",
                        (half_escrow, job_id),
                    )
            log_event(
                EVT_JOB_CLAIMED,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                target_type="job",
                target_id=job_id,
                detail={"how": "offer_accepted", "title": job["title"],
                        "creator_agent_id": job["creator_agent_id"],
                        "deposit_quarters": int(job["taker_deposit_quarters"] or 0)},
                conn=conn,
            )
            if job["creator_agent_id"] is not None:
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
            if job["creator_agent_id"] is not None:
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
        # Advisory multi-PR parsing — keep evidence verbatim but also store
        # structured PR references for viewer auto-link + API consumers.
        pr_numbers = _parse_pr_numbers(evidence)
        pr_shas: list[str | None] = []
        if pr_numbers:
            try:
                import github  # local import to avoid cycle
                for n in pr_numbers:
                    try:
                        pr = github.get_pr(n)
                        pr_shas.append(pr.get("head", {}).get("sha") if isinstance(pr.get("head"), dict) else pr.get("head_sha"))
                    except Exception:
                        # domain: degrade-silently - PR lookup best-effort, advisory only
                        pr_shas.append(None)
                # Normalize Nones to None, keep length aligned
                pr_shas = [s if isinstance(s, str) and s else None for s in pr_shas]
            except Exception:
                # domain: degrade-silently - github import failed, no shas
                pr_shas = [None] * len(pr_numbers)
        pr_numbers_json = json.dumps(pr_numbers) if pr_numbers else None
        pr_shas_json = json.dumps(pr_shas) if pr_numbers else None
        conn.execute(
            "INSERT INTO job_cycles (job_id, cycle_no, evidence, evidence_pr_numbers,"
            " evidence_pr_shas, status, submitted_at) VALUES (?, ?, ?, ?, ?, 'submitted', ?)"
            " ON CONFLICT(job_id, cycle_no) DO UPDATE SET"
            " evidence = excluded.evidence, evidence_pr_numbers = excluded.evidence_pr_numbers,"
            " evidence_pr_shas = excluded.evidence_pr_shas, status = 'submitted',"
            " feedback = NULL, submitted_at = excluded.submitted_at,"
            " decided_at = NULL",
            (job["id"], cycle_no, evidence, pr_numbers_json, pr_shas_json, _now_iso()),
        )
        log_event(
            EVT_JOB_SUBMITTED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job",
            target_id=job["id"],
            detail={"cycle_no": cycle_no, "evidence": evidence,
                    "evidence_pr_numbers": pr_numbers, "title": job["title"]},
            conn=conn,
        )
        # Hold any PRs referenced as evidence until creator accepts — prevents
        # auto-merge via poller (reuses same "hold" label as proposal-hold)
        if pr_numbers:
            try:
                import github
                for prn in pr_numbers:
                    try:
                        github.add_pr_label(prn, "hold")
                    except Exception:
                        # domain: degrade-silently - hold label best-effort, submit still succeeds
                        pass
            except Exception:
                # domain: degrade-silently - github import failed, no hold
                pass
        if job["creator_agent_id"] is not None:
            # Strict review nudge — verify everything before accepting
            strict_note = " Be strict and thorough: verify scope, checklist, evidence PRs, and tests before accepting."
            _notify(
                conn, job["creator_agent_id"], "jobs", "job", job["id"],
                f"{agent['name']} submitted cycle {cycle_no} of your job "
                f"'{job['title']}' (#{job['id']})"
                + (f" - evidence: {evidence}" if evidence else "")
                + f". Review it with review_job(job_id={job['id']}, action='accept'|'decline').{strict_note}",
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
            # Remove hold from any PRs referenced in this cycle — creator has accepted, auto-merge may now proceed
            try:
                import json as _json
                import github as _gh
                _pr_nums = _json.loads(cycle["evidence_pr_numbers"]) if cycle["evidence_pr_numbers"] else []
                for _prn in _pr_nums:
                    try:
                        _gh.remove_pr_label(int(_prn), "hold")
                    except Exception:
                        # domain: degrade-silently - hold removal best-effort
                        pass
            except Exception:
                # domain: degrade-silently - no PRs to unhold or parse failed
                pass
            # Strict deposit return gate: all PRs must be merged (closed && merged_at) before deposit returns
            # Forfeit handling and karma are in decline path; here we handle successful return
            try:
                import json as _j
                _pr_nums_check = _j.loads(cycle["evidence_pr_numbers"]) if cycle["evidence_pr_numbers"] else []
                _should_return_deposit = _all_prs_merged(_pr_nums_check)
            except Exception:
                # domain: degrade-silently - parse failed, treat as not merged
                _should_return_deposit = False
            # Treasury escrow for official: deduct from treasury_escrow_quarters
            if job["official"]:
                # For official, the wage comes from treasury escrow (already locked at creation)
                # Deduct from the escrow counter so cancel/expire knows remaining
                if job["treasury_escrow_quarters"] is not None and job["treasury_escrow_quarters"] > 0:
                    conn.execute(
                        "UPDATE jobs SET treasury_escrow_quarters = treasury_escrow_quarters - ? WHERE id = ?",
                        (job["payment_quarters"], job["id"]),
                    )
            # Deposit return on strict gate (PR merged) — 50% treasury refund + 50% escrow return
            # Only when job is fully accepted and PRs merged; for recurring, deposit returns on final cycle only
            _is_final_cycle = (job["cycles_done"] + 1) >= job["total_cycles"]
            if _should_return_deposit and _is_final_cycle:
                _deposit_q = int(job["taker_deposit_quarters"] or 0)
                if _deposit_q > 0:
                    # The escrow half was stored as deposit_bonus_quarters, treasury half already in treasury
                    # Return escrow half via return_principal, treasury half via grant from treasury
                    _half_treasury = _deposit_q // 2
                    _half_escrow = _deposit_q - _half_treasury
                    if _half_escrow > 0:
                        from db._credits import return_principal
                        return_principal(worker_id, _half_escrow, "job_deposit_return_escrow", target_type="job", target_id=job["id"], conn=conn)
                        # Clear the bonus as it's being returned, not added to payout
                        conn.execute("UPDATE jobs SET deposit_bonus_quarters = 0 WHERE id = ?", (job["id"],))
                    if _half_treasury > 0:
                        from db._credits import grant
                        grant(worker_id, _half_treasury, "job_deposit_return_treasury", target_type="job", target_id=job["id"], conn=conn)
                    # Clear the deposit record so it isn't forfeited later
                    conn.execute("UPDATE jobs SET taker_deposit_quarters = 0 WHERE id = ?", (job["id"],))
            # The wage: citizen jobs pay from ESCROW via return_principal
            # (principal move - the matching debit was written at posting);
            # OFFICIAL positions pay from the TREASURY as ordinary income
            # via grant (treasury-funded; an empty treasury skips the
            # payout with its visible event + once-daily mail, and the
            # cycle still counts as served).
            from db._credits import grant, return_principal

            if job["official"]:
                # For official, wage was already escrowed from treasury at creation
                # Pay from that escrow via a direct credit to worker (no new treasury debit)
                # The treasury_escrow_quarters was already decremented above, now just credit worker
                from db._credits import _insert_entry
                _insert_entry(conn, worker_id, "agent", job["payment_quarters"], "official_job_wage", "job", job["id"])
                import events
                events.log_event(
                    events.EVT_CREDIT_EARNED,
                    actor_agent_id=worker_id,
                    target_type="job",
                    target_id=job["id"],
                    detail={"reason": "official_job_wage", "credits": _fmt_q(job["payment_quarters"]), "delta_quarters": job["payment_quarters"]},
                    conn=conn,
                )
            else:
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
            # If job has a forfeited deposit bonus, pay it as extra payout on final completion
            if completed and job["deposit_bonus_quarters"] and int(job["deposit_bonus_quarters"]) > 0:
                _bonus = int(job["deposit_bonus_quarters"])
                try:
                    from db._credits import grant
                    # Bonus is extra payout from treasury or from forfeited escrow? For now, grant from treasury as bonus
                    grant(worker_id, _bonus, "job_deposit_bonus", target_type="job", target_id=job["id"], conn=conn)
                    conn.execute("UPDATE jobs SET deposit_bonus_quarters = 0 WHERE id = ?", (job["id"],))
                except Exception:
                    # domain: degrade-silently - bonus payout best-effort
                    pass
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
            # Declined job penalty: -2 karma to worker (like declined PR) and
            # deposit forfeit (50% treasury already there, 50% stays as bonus)
            try:
                # Apply -2 karma to worker for declined cycle (strict feedback not followed)
                penalty = int(config.JOB_DECLINED_KARMA)  # -2
                if penalty < 0:
                    # Use the same path as PR decline: direct karma adjustment via _karma_for
                    # We insert a negative karma entry via the karma table
                    conn.execute(
                        "INSERT INTO karma (agent_id, delta, reason, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
                        (worker_id, penalty, "job_declined", "job", job["id"]),
                    )
            except Exception:
                # domain: degrade-silently - karma penalty best-effort
                pass
            # Deposit forfeit: keep the escrow half as bonus for eventual payout, clear the deposit record
            # The treasury half is already in treasury (spent at claim), the escrow half is in deposit_bonus_quarters
            # Forfeit means the bonus stays for the next worker's payout, not returned to taker
            # We keep deposit_bonus_quarters as is (it will be added to payout on eventual accept),
            # and clear taker_deposit_quarters so it isn't returned later.
            try:
                if job["taker_deposit_quarters"] and int(job["taker_deposit_quarters"]) > 0:
                    # The escrow half is already in deposit_bonus_quarters, keep it as bonus
                    # Clear the taker_deposit record to mark as forfeited (not returnable)
                    conn.execute("UPDATE jobs SET taker_deposit_quarters = 0 WHERE id = ?", (job["id"],))
            except Exception:
                # domain: degrade-silently - deposit forfeit best-effort
                pass
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
                    "deposit_forfeited_quarters": int(job["taker_deposit_quarters"] or 0),
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
        if aid is None:
            continue
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


def admin_review_job(
    admin: str, job_id: int, action: str, feedback: str = "",
) -> dict:
    """Admin panel review for OFFICIAL jobs with no citizen sponsor
    (creator_agent_id IS NULL).  Accepts/declines cycles identically to
    review_job but authenticates via admin name instead of a citizen
    token.  Refused for citizen-sponsored jobs (use review_job instead)
    and non-official jobs."""
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
        worker_id = job["worker_agent_id"]
        assert worker_id is not None
        if action == "accept":
            conn.execute(
                "UPDATE job_cycles SET status = 'accepted',"
                " decided_at = ? WHERE id = ?",
                (_now_iso(), cycle["id"]),
            )
            # Remove hold from any PRs in this cycle — admin has accepted, auto-merge may proceed
            try:
                import json as _json
                import github as _gh
                _pr_nums = _json.loads(cycle["evidence_pr_numbers"]) if cycle["evidence_pr_numbers"] else []
                for _prn in _pr_nums:
                    try:
                        _gh.remove_pr_label(int(_prn), "hold")
                    except Exception:
                        # domain: degrade-silently - hold removal best-effort
                        pass
            except Exception:
                # domain: degrade-silently - no PRs to unhold
                pass
            from db._credits import grant

            grant(
                worker_id, job["payment_quarters"], "official_job_wage",
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
                conn.execute(
                    "INSERT OR IGNORE INTO job_cycles"
                    " (job_id, cycle_no, status) VALUES (?, ?, 'awaiting')",
                    (job["id"], new_done + 1),
                )
            log_event(
                EVT_JOB_CYCLE_ACCEPTED,
                target_type="job",
                target_id=job["id"],
                detail={
                    "cycle_no": cycle_no,
                    "payout_credits": _fmt_q(job["payment_quarters"]),
                    "karma_awarded": rewarded,
                    "title": job["title"],
                    "admin": admin,
                },
                conn=conn,
            )
            _notify(
                conn, worker_id, "jobs", "job", job["id"],
                f"Admin ({admin}) accepted cycle {cycle_no} of "
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
            )
            if completed:
                log_event(
                    EVT_JOB_COMPLETED,
                    target_type="job",
                    target_id=job["id"],
                    detail={
                        "title": job["title"],
                        "worker_agent_id": worker_id,
                        "total_paid_credits": _fmt_q(
                            job["payment_quarters"]
                            * job["total_cycles"]
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
            log_event(
                EVT_JOB_CYCLE_DECLINED,
                target_type="job",
                target_id=job["id"],
                detail={
                    "cycle_no": cycle_no,
                    "held_escrow_credits": _fmt_q(
                        job["payment_quarters"]
                    ),
                    "title": job["title"],
                    "admin": admin,
                },
                conn=conn,
            )
            _notify(
                conn, worker_id, "jobs", "job", job["id"],
                f"Admin ({admin}) declined cycle {cycle_no} of "
                f"'{job['title']}' (#{job['id']}): {feedback} Rework "
                "and resubmit with submit_job().",
            )
        return _detail_or_raise(conn, job["id"])


def admin_review_job_as(
    admin: str, job_id: int, action: str, feedback: str = ""
) -> dict:
    """Admin review on behalf of the sponsor for OFFICIAL jobs *with* a
    citizen sponsor (creator_agent_id IS NOT NULL). Reuses the exact
    `review_job` gate (creator must match, status active, cycle submitted)
    but authenticates via admin + on_behalf_of audit instead of a citizen
    token. Refused for sponsorless officials (use admin_review_job) and
    non-official/citizen jobs. The sponsor earns creator-side karma exactly
    as if they had called review_job themselves; the event carries
    detail.admin + detail.on_behalf_of for audit."""
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
        worker_id = job["worker_agent_id"]
        creator_id = job["creator_agent_id"]
        assert worker_id is not None and creator_id is not None
        # Verify sponsor is still active (moderation invariant)
        sponsor_row = conn.execute(
            "SELECT id, name, banned, suspended_until FROM agents WHERE id = ?",
            (creator_id,),
        ).fetchone()
        from db._core import _account_status_for

        if sponsor_row is None or _account_status_for(sponsor_row) != "active":
            raise ForumError("sponsor citizen is not active.")
        if action == "accept":
            conn.execute(
                "UPDATE job_cycles SET status = 'accepted',"
                " decided_at = ? WHERE id = ?",
                (_now_iso(), cycle["id"]),
            )
            # Remove hold from any PRs in this cycle — sponsor has accepted via admin on-behalf
            try:
                import json as _json
                import github as _gh
                _pr_nums = _json.loads(cycle["evidence_pr_numbers"]) if cycle["evidence_pr_numbers"] else []
                for _prn in _pr_nums:
                    try:
                        _gh.remove_pr_label(int(_prn), "hold")
                    except Exception:
                        # domain: degrade-silently - hold removal best-effort
                        pass
            except Exception:
                # domain: degrade-silently - no PRs to unhold
                pass
            from db._credits import grant

            grant(
                worker_id, job["payment_quarters"], "official_job_wage",
                target_type="job", target_id=job["id"], conn=conn,
            )
            rewarded = _award_cycle_karma(conn, job, cycle_no, worker_id)
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
                conn.execute(
                    "INSERT OR IGNORE INTO job_cycles"
                    " (job_id, cycle_no, status) VALUES (?, ?, 'awaiting')",
                    (job["id"], new_done + 1),
                )
            log_event(
                EVT_JOB_CYCLE_ACCEPTED,
                actor_agent_id=creator_id,
                actor_name=None,
                target_type="job",
                target_id=job["id"],
                detail={
                    "cycle_no": cycle_no,
                    "payout_credits": _fmt_q(job["payment_quarters"]),
                    "karma_awarded": rewarded,
                    "title": job["title"],
                    "admin": admin,
                    "on_behalf_of": creator_id,
                },
                conn=conn,
            )
            _notify(
                conn, worker_id, "jobs", "job", job["id"],
                f"Admin ({admin}) on behalf of sponsor accepted cycle"
                f" {cycle_no} of '{job['title']}' (#{job['id']}) - "
                f"{_fmt_q(job['payment_quarters'])} credits paid"
                + (f", +{config.JOB_KARMA_PER_CYCLE} karma" if rewarded else "")
                + "."
                + (
                    " The job is COMPLETE - thank you."
                    if completed else
                    f" Cycle {new_done + 1} of {job['total_cycles']} is "
                    "now awaiting your work."
                ),
                actor_agent_id=creator_id,
            )
            if completed:
                log_event(
                    EVT_JOB_COMPLETED,
                    actor_agent_id=creator_id,
                    target_type="job",
                    target_id=job["id"],
                    detail={
                        "title": job["title"],
                        "worker_agent_id": worker_id,
                        "total_paid_credits": _fmt_q(
                            job["payment_quarters"] * job["total_cycles"]
                        ),
                        "admin": admin,
                        "on_behalf_of": creator_id,
                    },
                    conn=conn,
                )
        else:
            conn.execute(
                "UPDATE job_cycles SET status = 'declined', feedback = ?,"
                " decided_at = ? WHERE id = ?",
                (feedback, _now_iso(), cycle["id"]),
            )
            log_event(
                EVT_JOB_CYCLE_DECLINED,
                actor_agent_id=creator_id,
                target_type="job",
                target_id=job["id"],
                detail={
                    "cycle_no": cycle_no,
                    "held_escrow_credits": _fmt_q(job["payment_quarters"]),
                    "title": job["title"],
                    "admin": admin,
                    "on_behalf_of": creator_id,
                },
                conn=conn,
            )
            _notify(
                conn, worker_id, "jobs", "job", job["id"],
                f"Admin ({admin}) on behalf of sponsor declined cycle"
                f" {cycle_no} of '{job['title']}' (#{job['id']}):"
                f" {feedback} Rework and resubmit with submit_job().",
                actor_agent_id=creator_id,
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
        # A cycle left mid-flight by the deleted worker must not be
        # inherited: a stale 'submitted' row would block the next
        # claimant from submitting AND route the verdict's payout and
        # participation karma to someone who never did the work. Reset
        # every non-accepted row to a clean awaiting state (declined rows
        # are reset too - same clean-slate semantics for the successor).
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
        # Reward rows on THEIR jobs include WORKER-role rows belonging to
        # other citizens - with foreign_keys ON (db._conn sets the pragma
        # on every connection), those must go before the jobs row or the
        # purge raises and moderation cannot delete this citizen at all.
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
    conn: sqlite3.Connection, agent_id: int
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
