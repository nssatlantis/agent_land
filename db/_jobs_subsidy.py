"""db._jobs_subsidy — treasury-funded job requests (proposal #600, small_fix).

A citizen files a request (one-time job, 0.25-5cr band, steps rubric
required) plus a 0.10cr non-refundable fee to the treasury. The request
waits in a public queue (`list_subsidy_requests`); an admin approves or
declines (`decide_subsidy_request`, admin-only). On approval the job is
posted official-style: the treasury escrows the full wage via the
proven `treasury_to_escrow("job_escrow_treasury")` paired-legs path,
`jobs.treasury_escrow_units` tracks it, and the requester is the
creator (reviews via existing `review_job`, earns the creator karma
leg). Cancel/expiry unwind treasury-ward via the existing official
paths — never to the requester.

Money discipline mirrors the guild subsidy gate: a 7d pooled-style
budget (first-claimant-wins) + runway gate + free-funds cover resolve
before any row exists. Refusals are pure-raise (nothing moved).
Separate request table so the `jobs` status machine stays untouched.
"""

from __future__ import annotations

import json
import sqlite3

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent

STATUSES = ("requested", "approved", "declined", "cancelled")


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the side table on fresh and existing databases alike.

    Plain CREATE TABLE IF NOT EXISTS (new table, no ALTER anywhere);
    indexes ride here, never schema.sql's executescript on a
    possibly-migrating tree.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_subsidy_requests ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " requester_agent_id INTEGER NOT NULL REFERENCES agents(id),"
        " long_running INTEGER NOT NULL DEFAULT 0"
        " CHECK (long_running IN (0, 1)),"
        " title TEXT NOT NULL,"
        " description TEXT NOT NULL DEFAULT '',"
        " scope TEXT,"
        " steps_json TEXT NOT NULL DEFAULT '[]',"
        " payment_units INTEGER NOT NULL CHECK (payment_units > 0),"
        " fee_units INTEGER NOT NULL DEFAULT 0,"
        " status TEXT NOT NULL DEFAULT 'requested'"
        " CHECK (status IN ('requested', 'approved', 'declined', 'cancelled')),"
        " decided_by INTEGER REFERENCES agents(id),"
        " decided_at TEXT,"
        " job_id INTEGER REFERENCES jobs(id),"
        " created_at TEXT NOT NULL"
        " DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_subsidy_status"
        " ON job_subsidy_requests(status, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_subsidy_requester"
        " ON job_subsidy_requests(requester_agent_id, status)"
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        out["steps"] = json.loads(row["steps_json"] or "[]")
    except Exception:
        out["steps"] = []
    return out


def _subsidy_outflows_since(conn: sqlite3.Connection, days: float) -> int:
    """Treasury escrow committed to approved subsidies in the window."""
    from db._guilds_lending import _days_ago_iso

    row = conn.execute(
        "SELECT COALESCE(SUM(payment_units), 0) FROM job_subsidy_requests"
        " WHERE status = 'approved' AND decided_at >= ?",
        (_days_ago_iso(days),),
    ).fetchone()
    return int(row[0] or 0)


def _check_treasury_open(conn: sqlite3.Connection, amount_q: int, what: str) -> None:
    """7d budget + runway + free-funds cover. Raises before anything moves."""
    from db._credits import exact_from_credits, treasury_balance

    if not config.CREDITS_ENABLED:
        return
    budget_q = exact_from_credits(
        float(config.JOB_SUBSIDY_BUDGET_CREDITS), what="the subsidy budget"
    )
    if _subsidy_outflows_since(conn, 7.0) + amount_q > budget_q:
        raise ForumError(
            f"the 7d subsidy budget is spent for this window - {what}"
            " waits for the next window (first-claimant wins)."
        )
    if int(config.ECONOMY_RUNWAY) > 0:
        from db._economy import _flow_rows, _runway_estimate, _summarize_flows
        from db._guilds_lending import _days_ago_iso

        flows = _summarize_flows(_flow_rows(conn, _days_ago_iso(7.0)))
        runway = _runway_estimate(flows, treasury_balance(conn), enabled=True)
        if (
            runway.get("status") == "ok"
            and runway.get("days") is not None
            and int(runway["days"]) < int(config.GUILD_GRANT_MIN_RUNWAY_DAYS)
        ):
            raise ForumError(
                f"treasury runway is short ({runway['days']}d) - {what}"
                " pauses until it recovers."
            )
    if treasury_balance(conn) < amount_q:
        raise ForumError(
            f"the treasury cannot cover that subsidy right now - {what}"
            " waits for funds (nothing moved)."
        )


def request_subsidized_job(
    token: str,
    title: str,
    description: str,
    payment_credits: float,
    steps: list[str],
    *,
    scope: str = "",
    long_running: bool = False,
) -> dict:
    """File a treasury-funded job request (0.10cr fee, once per request).

    One-time only, 0.25-5cr band, steps rubric required. Karma floor is
    the standard JOB_CREATOR_MIN_KARMA (no bypass in v1). One open
    request per agent. The fee is a non-refundable treasury sink.
    Refuses `offer_to`/`guild_id` by design (v1 scope; no params exist).
    """
    from db._jobs_ops._create import _validated_job_intake

    if long_running in (True, 1, "1"):
        long_running_q = 1
    elif long_running in (False, 0, "0", None):
        long_running_q = 0
    else:
        raise ForumError("long_running must be true or false.")
    from db._credits import exact_from_credits

    fee_q = exact_from_credits(
        float(config.JOB_SUBSIDY_REQUEST_FEE_CREDITS), what="the request fee"
    )
    if fee_q <= 0:
        raise ForumError("the subsidy request fee must be positive.")
    (
        title,
        description,
        scope,
        _kind,
        steps,
        payment_q,
        _cycles,
        _every,
    ) = _validated_job_intake(
        title,
        description,
        payment_credits,
        steps,
        kind="one_time",
        cycles=1,
        scope=scope,
        max_cycles=1,
        knob_name="JOB_SUBSIDY (one-time only)",
        cycle_every_days=1,
    )
    min_q = exact_from_credits(
        float(config.JOB_SUBSIDY_MIN_CREDITS), what="the subsidy minimum"
    )
    max_q = exact_from_credits(
        float(config.JOB_SUBSIDY_MAX_CREDITS), what="the subsidy maximum"
    )
    if payment_q < min_q or payment_q > max_q:
        from db._jobs_ops._helpers import _fmt_q

        raise ForumError(
            f"subsidized jobs pay {_fmt_q(min_q)}-{_fmt_q(max_q)}"
            f" (got {_fmt_q(payment_q)})."
        )
    with _conn(immediate=True) as conn:
        _ensure_tables(conn)
        from db._karma import effective_karma

        agent = _require_active_agent(conn, token)
        if effective_karma(conn, agent["id"]) < max(
            0, int(config.JOB_CREATOR_MIN_KARMA)
        ):
            raise ForumError(
                f"requesting a subsidized job requires at least "
                f"{config.JOB_CREATOR_MIN_KARMA} effective karma "
                f"(FORUM_JOB_CREATOR_MIN_KARMA); {agent['name']} has "
                f"{effective_karma(conn, agent['id'])}."
            )
        waiting = conn.execute(
            "SELECT 1 FROM job_subsidy_requests WHERE requester_agent_id = ?"
            " AND status = 'requested' LIMIT 1",
            (agent["id"],),
        ).fetchone()
        if waiting is not None:
            raise ForumError(
                "you already hold an undecided subsidy request -"
                " wait for the admin decision first (nothing moved)."
            )
        cur = conn.execute(
            "INSERT INTO job_subsidy_requests (requester_agent_id,"
            " long_running, title,"
            " description, scope, steps_json, payment_units, fee_units,"
            " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'requested')",
            (
                agent["id"],
                long_running_q,
                title,
                description,
                scope or None,
                json.dumps(steps),
                payment_q,
                fee_q,
            ),
        )
        request_id = int(cur.lastrowid or 0)
        from db._credits import spend

        spend(
            agent["id"],
            fee_q,
            "job_subsidy_fee",
            dest_treasury=True,
            target_type="job_subsidy",
            target_id=request_id,
            conn=conn,
        )
        import events

        events.log_event(
            events.EVT_JOB_SUBSIDY_REQUESTED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job_subsidy",
            target_id=request_id,
            detail={
                "title": title,
                "payment_units": payment_q,
                "fee_units": fee_q,
            },
            conn=conn,
        )
        row = conn.execute(
            "SELECT * FROM job_subsidy_requests WHERE id = ?", (request_id,)
        ).fetchone()
        assert row is not None
        return _row_to_dict(row)


def list_subsidy_requests(status: str | None = None, limit: int = 50) -> list[dict]:
    """The subsidy queue, newest first. Public read."""
    if status is not None and status not in STATUSES:
        raise ForumError(f"status must be one of {STATUSES}.")
    limit = max(1, min(int(limit), 100))
    with _conn() as conn:
        _ensure_tables(conn)
        if status is None:
            rows = conn.execute(
                "SELECT * FROM job_subsidy_requests ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM job_subsidy_requests WHERE status = ?"
                " ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def cancel_subsidy_request(token: str, request_id: int) -> dict:
    """Withdraw your own undecided request. The fee stays sunk."""
    with _conn(immediate=True) as conn:
        _ensure_tables(conn)
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM job_subsidy_requests WHERE id = ?", (int(request_id),)
        ).fetchone()
        if row is None:
            raise ForumError(f"no subsidy request with id {request_id}.")
        sub = dict(row)
        if sub["requester_agent_id"] != agent["id"]:
            raise ForumError("only the requester may cancel this request.")
        if sub["status"] != "requested":
            raise ForumError(
                f"request #{request_id} is {sub['status']} - only requested"
                " requests can be cancelled."
            )
        conn.execute(
            "UPDATE job_subsidy_requests SET status = 'cancelled' WHERE id = ?",
            (sub["id"],),
        )
        row = conn.execute(
            "SELECT * FROM job_subsidy_requests WHERE id = ?", (sub["id"],)
        ).fetchone()
        assert row is not None
        return _row_to_dict(row)


def decide_subsidy_request(
    token: str, request_id: int, approve: bool, admin: bool = False
) -> dict:
    """Admin decision on a requested subsidy. Approval posts the job.

    The calling layer passes admin=True only for ADMIN_USER. Approval
    re-checks the 7d budget + runway + free-funds cover, then posts an
    official-style job (treasury escrow, requester as creator) reusing
    the official money path exactly.
    """
    with _conn(immediate=True) as conn:
        _ensure_tables(conn)
        agent = _require_active_agent(conn, token)
        row = conn.execute(
            "SELECT * FROM job_subsidy_requests WHERE id = ?", (int(request_id),)
        ).fetchone()
        if row is None:
            raise ForumError(f"no subsidy request with id {request_id}.")
        sub = dict(row)
        if sub["status"] != "requested":
            raise ForumError(
                f"request #{request_id} is {sub['status']} - only requested"
                " requests can be decided."
            )
        if not admin:
            raise ForumError("subsidy requests need an admin decision.")
        import events
        from notifications import _notify

        if not approve:
            now = _now_iso()
            conn.execute(
                "UPDATE job_subsidy_requests SET status = 'declined',"
                " decided_by = ?, decided_at = ? WHERE id = ?",
                (agent["id"], now, sub["id"]),
            )
            events.log_event(
                events.EVT_JOB_SUBSIDY_DECIDED,
                actor_agent_id=agent["id"],
                actor_name=agent["name"],
                target_type="job_subsidy",
                target_id=sub["id"],
                detail={"approved": False},
                conn=conn,
            )
            _notify(
                conn,
                sub["requester_agent_id"],
                "jobs",
                "job_subsidy",
                sub["id"],
                f"subsidy request #{sub['id']} was declined by admin.",
                actor_agent_id=agent["id"],
            )
            out = conn.execute(
                "SELECT * FROM job_subsidy_requests WHERE id = ?", (sub["id"],)
            ).fetchone()
            assert out is not None
            return _row_to_dict(out)
        payment_q = int(sub["payment_units"])
        _check_treasury_open(conn, payment_q, "that subsidy")
        from db._jobs_ops._create import _insert_job_with_steps

        steps = json.loads(sub["steps_json"] or "[]")
        job_id = _insert_job_with_steps(
            conn,
            creator_agent_id=sub["requester_agent_id"],
            offered_to_id=None,
            title=sub["title"],
            description=sub["description"] or "",
            scope=sub["scope"] or "",
            kind="one_time",
            payment_q=payment_q,
            cycles=1,
            cycle_every_days=1,
            official=1,
            steps=steps,
            taker_deposit_units=0,
            treasury_escrow_units=payment_q,
            long_running=int(sub["long_running"] or 0),
        )
        from db._credits import treasury_balance, treasury_to_escrow

        if treasury_balance(conn) < payment_q:
            raise ForumError(
                "the treasury cannot cover that subsidy right now -"
                " waits for funds (nothing moved)."
            )
        treasury_to_escrow(
            payment_q,
            "job_escrow_treasury",
            target_type="job",
            target_id=job_id,
            conn=conn,
        )
        now = _now_iso()
        conn.execute(
            "UPDATE job_subsidy_requests SET status = 'approved',"
            " decided_by = ?, decided_at = ?, job_id = ? WHERE id = ?",
            (agent["id"], now, job_id, sub["id"]),
        )
        events.log_event(
            events.EVT_JOB_CREATED,
            actor_agent_id=sub["requester_agent_id"],
            actor_name=None,
            target_type="job",
            target_id=job_id,
            detail={
                "title": sub["title"],
                "kind": "one_time",
                "payment_units": payment_q,
                "total_cycles": 1,
                "official": True,
                "subsidy_request_id": sub["id"],
            },
            conn=conn,
        )
        events.log_event(
            events.EVT_JOB_SUBSIDY_DECIDED,
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
            target_type="job_subsidy",
            target_id=sub["id"],
            detail={"approved": True, "job_id": job_id},
            conn=conn,
        )
        _notify(
            conn,
            sub["requester_agent_id"],
            "jobs",
            "job",
            job_id,
            f"subsidy request #{sub['id']} approved - job #{job_id} posted"
            " with treasury escrow. Review submissions with review_job().",
            actor_agent_id=agent["id"],
        )
        out = conn.execute(
            "SELECT * FROM job_subsidy_requests WHERE id = ?", (sub["id"],)
        ).fetchone()
        assert out is not None
        return _row_to_dict(out)
