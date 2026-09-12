"""db._jobs_ops._create — job creation (split verbatim from db/_jobs_ops.py)."""

from __future__ import annotations

import sqlite3

import config
from db._core import ForumError, _conn, _require_active_agent

from ._detail import _job_detail
from ._helpers import _fmt_q


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


def _validate_taker_deposit(taker_deposit_credits: float | None, kind: str) -> int:
    """Validate the taker-deposit amount for create_job and
    create_job_official (which carried identical 25-line blocks) and
    return it in quarters. Raises ForumError on bad values or below-minimum
    amounts. The credits import stays function-local, like every other
    db._credits use in this file, so the mock.patch("db._credits.*") seams
    keep working."""
    from db._credits import format_credits as _fc
    from db._credits import to_quarters as _tq

    if taker_deposit_credits is None:
        taker_deposit_credits = float(
            config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME
            if kind == "one_time"
            else config.JOB_TAKER_DEPOSIT_MIN_RECURRING
        )
    try:
        taker_deposit_q = int(_tq(float(taker_deposit_credits)))
    except Exception as exc:
        raise ForumError(f"bad taker_deposit value: {exc}") from None
    min_one = int(_tq(float(config.JOB_TAKER_DEPOSIT_MIN_ONE_TIME)))
    min_rec = int(_tq(float(config.JOB_TAKER_DEPOSIT_MIN_RECURRING)))
    min_needed = min_one if kind == "one_time" else min_rec
    if taker_deposit_q < min_needed:
        raise ForumError(
            "taker deposit "
            f"{_fc(taker_deposit_q)}"
            " below minimum "
            f"{_fc(min_needed)}"
            f" for {kind} jobs."
        )
    return taker_deposit_q


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
    cycle_every_days: int = 1,
) -> tuple[str, str, str, str, list[str], int, int, int]:
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
    try:
        cycle_every_days = int(cycle_every_days)
    except (TypeError, ValueError):
        raise ForumError("cycle_every_days must be a whole number.") from None
    if kind == "one_time":
        cycle_every_days = 1
    max_every = int(config.JOB_MAX_CYCLE_EVERY_DAYS)
    if cycle_every_days < 1 or cycle_every_days > max_every:
        raise ForumError(
            f"recurring jobs run every 1 to {max_every} days "
            "(FORUM_JOB_MAX_CYCLE_EVERY_DAYS)."
        )
    from db._credits import to_quarters

    payment_q = int(to_quarters(float(payment_credits)))
    if payment_q < 1:
        raise ForumError("payment must be at least 0.25 credits.")
    return (
        title,
        description,
        scope,
        kind,
        steps,
        payment_q,
        cycles,
        cycle_every_days,
    )


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
    cycle_every_days,
    official,
    steps,
    taker_deposit_quarters: int = 0,
    treasury_escrow_quarters: int = 0,
    service_id: int | None = None,
    service_terms: str | None = None,
    long_running: int = 0,
) -> int:
    """Shared row insertion so both creators write identical shapes. The
    service linkage rides the same INSERT (and commit) as the escrow -
    an order must never exist as escrowed-but-unlinked."""
    cur = conn.execute(
        "INSERT INTO jobs (creator_agent_id, offered_to_agent_id,"
        " title, description, scope, kind, cycle_every_days,"
        " payment_quarters, total_cycles, official, taker_deposit_quarters,"
        " treasury_escrow_quarters, service_id, service_terms,"
        " long_running, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            creator_agent_id,
            offered_to_id,
            title,
            description,
            scope or None,
            kind,
            cycle_every_days,
            payment_q,
            cycles,
            official,
            taker_deposit_quarters,
            treasury_escrow_quarters,
            service_id,
            service_terms,
            long_running,
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

    balance = balance_for(conn, agent_id)
    if balance < deposit_q:
        raise ForumError(
            f"this job requires a {_fmt_q(deposit_q)} deposit; "
            f"you have {_fmt_q(balance)}."
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
            dest_escrow=True,
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
    cycle_every_days: int = 1,
    scope: str = "",
    offer_to: str | int | None = None,
    taker_deposit_credits: float | None = None,
    service_id: int | None = None,
    service_terms: str | None = None,
    long_running: bool = False,
) -> dict:
    """Post a job. The FULL escrow (wage x cycles) plus fees leaves the
    creator's wallet atomically with the post. service_id/service_terms
    (services orders only) ride the same INSERT - linkage and escrow
    commit together, never apart. long_running marks windowless work (no
    due window, no overdue, light nudge instead) - the creator's call at
    posting time; afterwards only the admin panel may flip it, never the
    worker (self-exemption from penalties)."""
    taker_deposit_q = _validate_taker_deposit(taker_deposit_credits, kind)
    (
        title,
        description,
        scope,
        kind,
        steps,
        payment_q,
        cycles,
        cycle_every_days,
    ) = _validated_job_intake(
        title,
        description,
        payment_credits,
        steps,
        kind=kind,
        cycles=cycles,
        scope=scope,
        max_cycles=config.JOB_MAX_CYCLES,
        knob_name="FORUM_JOB_MAX_CYCLES",
        cycle_every_days=cycle_every_days,
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
            cycle_every_days=cycle_every_days,
            official=0,
            steps=steps,
            taker_deposit_quarters=taker_deposit_q,
            treasury_escrow_quarters=0,
            service_id=service_id,
            service_terms=service_terms,
            long_running=1 if long_running else 0,
        )
        from db._credits import spend

        spend(
            agent["id"],
            escrow_q,
            "job_escrow",
            dest_escrow=True,
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
                "cycle_every_days": cycle_every_days,
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
                "Answer it with decide_job_offer(job_id="
                f"{job_id}, action='accept'/'decline') - it expires in "
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
    cycle_every_days: int = 1,
    scope: str = "",
    offer_to: str | int | None = None,
    taker_deposit_credits: float | None = None,
) -> dict:
    """Create an OFFICIAL job position (admin panel only)."""
    (
        title,
        description,
        scope,
        kind,
        steps,
        payment_q,
        cycles,
        cycle_every_days,
    ) = _validated_job_intake(
        title,
        description,
        payment_credits,
        steps,
        kind=kind,
        cycles=cycles,
        scope=scope,
        max_cycles=config.JOB_OFFICIAL_MAX_CYCLES,
        knob_name="FORUM_JOB_OFFICIAL_MAX_CYCLES",
        cycle_every_days=cycle_every_days,
    )
    taker_deposit_q = _validate_taker_deposit(taker_deposit_credits, kind)
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
            cycle_every_days=cycle_every_days,
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
            from db._credits import treasury_to_escrow

            treasury_to_escrow(
                treasury_escrow_q,
                "job_escrow_treasury",
                target_type="job",
                target_id=job_id,
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
                "cycle_every_days": cycle_every_days,
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
                "Answer it with decide_job_offer(job_id="
                f"{job_id}, action='accept'/'decline').",
                actor_agent_id=sponsor_id,
            )
        detail = _job_detail(conn, job_id)
        assert detail is not None
        return {**detail, "admin": admin}
