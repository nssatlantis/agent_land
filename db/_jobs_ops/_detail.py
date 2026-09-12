"""db._jobs_ops._detail — job detail assembly (split verbatim from db/_jobs_ops.py)."""

from __future__ import annotations

import json
import sqlite3

from db._core import _id_chunks

from ._helpers import (
    _cadence_hours,
    _fmt_q,
    _job_overdue_anchor_sql,
    _overdue_flag,
    _parse_cycle_evidence,
    job_overdue_cutoff,
)

_JOB_COLS = (
    "id, creator_agent_id, worker_agent_id, offered_to_agent_id, title,"
    " description, scope, kind, cycle_every_days, payment_quarters,"
    " total_cycles, cycles_done,"
    " official, taker_deposit_quarters, deposit_bonus_quarters,"
    " treasury_escrow_quarters, service_id, service_terms,"
    " status, created_at, decided_at"
)


def _remaining_escrow(job: sqlite3.Row) -> int:
    remaining = max(0, job["total_cycles"] - job["cycles_done"])
    if job["official"]:
        return 0
    return int(job["payment_quarters"]) * remaining


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
        "status": job["status"],
        "overdue": _overdue_flag(
            job["status"],
            cur_status,
            job["anchor_at"],
            cutoff,
            opens_at=cur_opens_at,
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
        "payment_credits": _fmt_q(job["payment_quarters"]),
        "payment_quarters": job["payment_quarters"],
        "taker_deposit_credits": _fmt_q(job["taker_deposit_quarters"]),
        "taker_deposit_quarters": job["taker_deposit_quarters"],
        "deposit_bonus_credits": _fmt_q(job["deposit_bonus_quarters"]),
        "deposit_bonus_quarters": job["deposit_bonus_quarters"],
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
        " evidence_pr_shas, feedback, submitted_at, decided_at"
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
            }
        )
    return _job_detail_from_parts(
        job, steps, cycles, job_overdue_cutoff(hours=_cadence_hours(job))
    )


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
            " submitted_at, decided_at"
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
                }
            )
        for r in job_rows:
            jid = r["id"]
            details[jid] = _job_detail_from_parts(
                r,
                steps_by_job.get(jid, []),
                cycles_by_job.get(jid, []),
                job_overdue_cutoff(hours=_cadence_hours(r)),
            )
    return details


def _detail_or_raise(conn: sqlite3.Connection, job_id: int) -> dict:
    """_job_detail for a row the caller has already verified exists."""
    detail = _job_detail(conn, job_id)
    assert detail is not None
    return detail
