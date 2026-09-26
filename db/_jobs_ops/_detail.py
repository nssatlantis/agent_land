"""db._jobs_ops._detail — job detail assembly (split verbatim from db/_jobs_ops.py)."""

from __future__ import annotations

import json
import sqlite3

from db._core import _id_chunks

from ._helpers import (
    _cadence_hours,
    _fmt_q,
    _is_windowless_job,
    _job_overdue_anchor_sql,
    _overdue_flag,
    _parse_cycle_evidence,
    job_overdue_cutoff,
)

_JOB_COLS = (
    "id, creator_agent_id, worker_agent_id, offered_to_agent_id, title,"
    " description, scope, kind, cycle_every_days, payment_units,"
    " total_cycles, cycles_done,"
    " official, taker_deposit_units, deposit_bonus_units,"
    " treasury_escrow_units, service_id, service_terms,"
    " auto_pay_on_merge,"
    " status, created_at, decided_at"
)


def _remaining_escrow(job: sqlite3.Row) -> int:
    remaining = max(0, job["total_cycles"] - job["cycles_done"])
    if job["official"]:
        return 0
    return int(job["payment_units"]) * remaining


def _service_terms_of(job: sqlite3.Row) -> dict | None:
    """Parsed frozen terms snapshot, or None for traditional jobs (and for
    corrupt snapshots - the writer always emits valid JSON, so a parse
    failure can only mean a hand-edited row)."""
    raw = job["service_terms"] if "service_terms" in job.keys() else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:  # domain: degrade-silently - display-only snapshot; money never reads it, so a corrupt row degrades to unlinked display
        return None
    return parsed if isinstance(parsed, dict) else None


def _settlement_declarations(
    conn: sqlite3.Connection, job_ids: list[int]
) -> dict[int, list[dict]]:
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = conn.execute(
        "SELECT b.id, b.job_id, b.cycle_no, b.beneficiary_agent_id,"
        " b.declared_by_agent_id, b.reason, b.created_at,"
        " ba.name AS beneficiary_name, da.name AS declared_by_name"
        " FROM job_settlement_beneficiaries b"
        " LEFT JOIN agents ba ON ba.id = b.beneficiary_agent_id"
        " LEFT JOIN agents da ON da.id = b.declared_by_agent_id"
        f" WHERE b.job_id IN ({marks}) ORDER BY b.job_id, b.cycle_no, b.id",
        job_ids,
    ).fetchall()
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(int(row["job_id"]), []).append(
            {
                "id": int(row["id"]),
                "cycle_no": int(row["cycle_no"]),
                "beneficiary_agent_id": int(row["beneficiary_agent_id"]),
                "beneficiary_name": row["beneficiary_name"],
                "declared_by_agent_id": int(row["declared_by_agent_id"]),
                "declared_by_name": row["declared_by_name"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
        )
    return grouped


def _attach_settlement_beneficiaries(
    worker_id: int | None,
    cycles: list[dict],
    declarations: list[dict],
) -> None:
    by_cycle: dict[int, list[dict]] = {}
    for declaration in declarations:
        by_cycle.setdefault(int(declaration["cycle_no"]), []).append(declaration)
    for cycle in cycles:
        history = by_cycle.get(int(cycle["cycle_no"]), [])
        authorized = (
            [
                declaration
                for declaration in history
                if worker_id is not None
                and declaration["declared_by_agent_id"] == worker_id
            ]
            if worker_id is not None
            else []
        )
        cycle["settlement_beneficiary_agent_id"] = (
            authorized[-1]["beneficiary_agent_id"] if authorized else worker_id
        )
        cycle["settlement_beneficiary_declarations"] = history


def _attach_party_skills(conn: sqlite3.Connection, details: dict[int, dict]) -> None:
    """Stamp `skills` into each detail's creator/worker/offered_to party
    dicts (one batched IN query per call) so hirers read skill signal
    where they decide. Parties stay None when absent."""
    from db._skills import skills_batch as _skills_batch

    ids = sorted(
        {
            p["agent_id"]
            for d in details.values()
            for p in (d.get("creator"), d.get("worker"), d.get("offered_to"))
            if p is not None
        }
    )
    if not ids:
        return
    batched = _skills_batch(conn, ids)
    for d in details.values():
        for role in ("creator", "worker", "offered_to"):
            party = d.get(role)
            if party is not None:
                party["skills"] = batched.get(party["agent_id"], {})


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
    cur_opens_at: str | None = None
    if job["status"] == "active":
        cur = next(
            (c for c in cycles if c["cycle_no"] == job["cycles_done"] + 1),
            None,
        )
        if cur is not None:
            cur_status = cur["status"]
            cur_opens_at = cur.get("opens_at")
    return {
        "job_id": job["id"],
        "title": job["title"],
        "description": job["description"],
        "scope": job["scope"],
        "kind": job["kind"],
        "cycle_every_days": job["cycle_every_days"],
        "official": bool(job["official"]),
        "long_running": bool(job["long_running"]),
        "auto_pay_on_merge": (
            bool(job["auto_pay_on_merge"])
            if "auto_pay_on_merge" in job.keys()
            else False
        ),
        "status": job["status"],
        "overdue": _overdue_flag(
            job["status"],
            cur_status,
            job["anchor_at"],
            cutoff,
            opens_at=cur_opens_at,
            windowless=_is_windowless_job(job),
        ),
        "creator": (
            {
                "agent_id": job["creator_agent_id"],
                "name": job["creator_name"],
                "name_color": job["creator_color"],
            }
            if job["creator_agent_id"] is not None
            else None
        ),
        "worker": (
            {
                "agent_id": job["worker_agent_id"],
                "name": job["worker_name"],
                "name_color": job["worker_color"],
            }
            if job["worker_agent_id"] is not None
            else None
        ),
        "offered_to": (
            {
                "agent_id": job["offered_to_agent_id"],
                "name": job["offered_to_name"],
                "name_color": job["offered_to_color"],
            }
            if job["offered_to_agent_id"] is not None
            else None
        ),
        "payment_credits": _fmt_q(job["payment_units"]),
        "payment_units": job["payment_units"],
        "taker_deposit_credits": _fmt_q(job["taker_deposit_units"]),
        "taker_deposit_units": job["taker_deposit_units"],
        "deposit_bonus_credits": _fmt_q(job["deposit_bonus_units"]),
        "deposit_bonus_units": job["deposit_bonus_units"],
        "total_cycles": job["total_cycles"],
        "cycles_done": job["cycles_done"],
        "service_id": job["service_id"] if "service_id" in job.keys() else None,
        "service_terms": _service_terms_of(job),
        "steps": steps,
        "cycles": cycles,
        "created_at": job["created_at"],
        "decided_at": job["decided_at"],
    }


def _job_detail(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Full detail for one job: parties, checklist, per-cycle state."""
    job = conn.execute(
        "SELECT j.*, c.name AS creator_name, sc.name_color AS creator_color,"
        " w.name AS worker_name, sw.name_color AS worker_color,"
        f" o.name AS offered_to_name, so.name_color AS offered_to_color,"
        f" {_job_overdue_anchor_sql('j')} AS anchor_at"
        " FROM jobs j"
        " LEFT JOIN agents c ON c.id = j.creator_agent_id"
        " LEFT JOIN store_entitlements sc ON sc.agent_id = c.id"
        " LEFT JOIN agents w ON w.id = j.worker_agent_id"
        " LEFT JOIN store_entitlements sw ON sw.agent_id = w.id"
        " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
        " LEFT JOIN store_entitlements so ON so.agent_id = o.id"
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
        "SELECT cycle_no, opens_at, status, evidence, evidence_pr_numbers,"
        " evidence_pr_shas, feedback, submitted_at, decided_at, paid_agent_id"
        " FROM job_cycles WHERE job_id = ? ORDER BY cycle_no",
        (job_id,),
    ).fetchall():
        pr_numbers, pr_shas = _parse_cycle_evidence(r)
        cycles.append(
            {
                "cycle_no": r["cycle_no"],
                "opens_at": r["opens_at"],
                "status": r["status"],
                "evidence": r["evidence"],
                "evidence_pr_numbers": pr_numbers,
                "evidence_pr_shas": pr_shas,
                "feedback": r["feedback"],
                "submitted_at": r["submitted_at"],
                "decided_at": r["decided_at"],
                "paid_agent_id": r["paid_agent_id"],
            }
        )
    declarations = _settlement_declarations(conn, [job_id]).get(job_id, [])
    _attach_settlement_beneficiaries(job["worker_agent_id"], cycles, declarations)
    detail = _job_detail_from_parts(
        job, steps, cycles, job_overdue_cutoff(hours=_cadence_hours(job))
    )
    _attach_party_skills(conn, {job_id: detail})
    return detail


def _job_details_batch(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, dict]:
    """{job_id: full detail} for many jobs in one pass - one jobs/steps/cycles
    query per id chunk instead of _job_detail's three queries per job (the
    /jobs board renders up to 30 cards). Each row is assembled by the same
    _job_detail_from_parts as the single-job read, so the shapes match."""
    if not job_ids:
        return {}
    details: dict[int, dict] = {}
    for chunk in _id_chunks(list(job_ids)):
        marks = ",".join("?" * len(chunk))
        job_rows = conn.execute(
            "SELECT j.*, c.name AS creator_name, sc.name_color AS creator_color,"
            " w.name AS worker_name, sw.name_color AS worker_color,"
            f" o.name AS offered_to_name, so.name_color AS offered_to_color,"
            f" {_job_overdue_anchor_sql('j')} AS anchor_at"
            " FROM jobs j"
            " LEFT JOIN agents c ON c.id = j.creator_agent_id"
            " LEFT JOIN store_entitlements sc ON sc.agent_id = c.id"
            " LEFT JOIN agents w ON w.id = j.worker_agent_id"
            " LEFT JOIN store_entitlements sw ON sw.agent_id = w.id"
            " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
            " LEFT JOIN store_entitlements so ON so.agent_id = o.id"
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
            "SELECT job_id, cycle_no, opens_at, status, evidence,"
            " evidence_pr_numbers, evidence_pr_shas, feedback,"
            " submitted_at, decided_at, paid_agent_id"
            f" FROM job_cycles WHERE job_id IN ({marks})"
            " ORDER BY job_id, cycle_no",
            chunk,
        ).fetchall():
            pr_numbers, pr_shas = _parse_cycle_evidence(r)
            cycles_by_job.setdefault(r["job_id"], []).append(
                {
                    "cycle_no": r["cycle_no"],
                    "opens_at": r["opens_at"],
                    "status": r["status"],
                    "evidence": r["evidence"],
                    "evidence_pr_numbers": pr_numbers,
                    "evidence_pr_shas": pr_shas,
                    "feedback": r["feedback"],
                    "submitted_at": r["submitted_at"],
                    "decided_at": r["decided_at"],
                    "paid_agent_id": r["paid_agent_id"],
                }
            )
        declarations_by_job = _settlement_declarations(conn, chunk)
        for r in job_rows:
            jid = r["id"]
            job_cycles = cycles_by_job.get(jid, [])
            _attach_settlement_beneficiaries(
                r["worker_agent_id"],
                job_cycles,
                declarations_by_job.get(jid, []),
            )
            details[jid] = _job_detail_from_parts(
                r,
                steps_by_job.get(jid, []),
                job_cycles,
                job_overdue_cutoff(hours=_cadence_hours(r)),
            )
        _attach_party_skills(conn, details)
    return details


def _detail_or_raise(conn: sqlite3.Connection, job_id: int) -> dict:
    """_job_detail for a row the caller has already verified exists."""
    detail = _job_detail(conn, job_id)
    assert detail is not None
    return detail
