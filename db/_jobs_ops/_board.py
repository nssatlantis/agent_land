"""db._jobs_ops._board — board listing (split verbatim from db/_jobs_ops.py)."""

from __future__ import annotations

import sqlite3
import time

import config
from db._core import ForumError, _conn, _id_chunks, _require_active_agent

from ._detail import _job_detail, _job_details_batch
from ._helpers import (
    _cadence_hours,
    _fmt_q,
    _job_anchors_for,
    _overdue_flag,
    job_overdue_cutoff,
)

_JOB_VIEWS = ("open", "mine", "working", "all")

_BOARD_TOTAL_CACHE: dict[tuple[str, int | None], tuple[float, int]] = {}
_BOARD_TOTAL_TTL = 5.0


def _board_total_cached(
    conn: sqlite3.Connection, view: str, agent_id: int | None, where: str, params: list
) -> int:
    """Board total with a 5s memo. Keyed by (view, agent) — limit/offset
    never change a total, and the WHERE is a pure function of those two."""
    now = time.monotonic()
    key = (view, agent_id)
    hit = _BOARD_TOTAL_CACHE.get(key)
    if hit is not None and now - hit[0] < _BOARD_TOTAL_TTL:
        return hit[1]
    total = conn.execute(
        f"SELECT COUNT(*) FROM jobs j {where}",
        params,
    ).fetchone()[0]
    if len(_BOARD_TOTAL_CACHE) > 256:
        _BOARD_TOTAL_CACHE.clear()
    _BOARD_TOTAL_CACHE[key] = (now, total)
    return total


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
        agent_id: int | None = None
        if view in ("mine", "working"):
            assert token is not None
            agent = _require_active_agent(conn, token)
            agent_id = agent["id"]
            params.append(agent_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            "SELECT j.id, j.title, j.kind, j.status, j.scope,"
            " j.cycle_every_days, j.payment_quarters,"
            " j.total_cycles, j.cycles_done,"
            " j.official, j.created_at,"
            " j.creator_agent_id, j.worker_agent_id, j.offered_to_agent_id,"
            " c.name AS creator_name, w.name AS worker_name,"
            " o.name AS offered_to_name"
            " FROM jobs j"
            " LEFT JOIN agents c ON c.id = j.creator_agent_id"
            " LEFT JOIN agents w ON w.id = j.worker_agent_id"
            " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
            f" {where} ORDER BY j.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = _board_total_cached(conn, view, agent_id, where, params)
        # Batch the two per-row correlated probes (events anchor, current
        # cycle status) over the page ids instead of paying 2N subqueries.
        page_ids = [r["id"] for r in rows]
        anchors = _job_anchors_for(conn, page_ids)
        cur_by_job: dict[int, dict[int, tuple[str | None, str | None]]] = {}
        if page_ids:
            marks = ",".join("?" * len(page_ids))
            for cr in conn.execute(
                "SELECT job_id, cycle_no, status, opens_at FROM job_cycles"
                f" WHERE job_id IN ({marks})",
                page_ids,
            ).fetchall():
                cur_by_job.setdefault(cr["job_id"], {})[cr["cycle_no"]] = (
                    cr["status"],
                    cr["opens_at"],
                )
        # One batched skill lookup for every party on the page - hirers
        # read skill signal on the board, not just the detail page.
        from db._skills import skills_batch as _skills_batch

        _page_ids = sorted(
            {
                aid
                for r in rows
                for aid in (
                    r["creator_agent_id"],
                    r["worker_agent_id"],
                    r["offered_to_agent_id"],
                )
                if aid is not None
            }
        )
        _page_skills = _skills_batch(conn, _page_ids) if _page_ids else {}
        jobs_out = []
        for r in rows:
            cur_pair = cur_by_job.get(r["id"], {}).get(r["cycles_done"] + 1)
            cur_status = cur_pair[0] if cur_pair else None
            cur_opens_at = cur_pair[1] if cur_pair else None
            jobs_out.append(
                {
                    "job_id": r["id"],
                    "title": r["title"],
                    "kind": r["kind"],
                    "status": r["status"],
                    "scope": r["scope"],
                    "official": bool(r["official"]),
                    "cycle_every_days": r["cycle_every_days"],
                    "creator": r["creator_name"] or "admin",
                    "creator_agent_id": r["creator_agent_id"],
                    "creator_skills": _page_skills.get(r["creator_agent_id"], {}),
                    "worker": r["worker_name"],
                    "worker_agent_id": r["worker_agent_id"],
                    "worker_skills": _page_skills.get(r["worker_agent_id"], {}),
                    "offered_to": r["offered_to_name"],
                    "offered_to_agent_id": r["offered_to_agent_id"],
                    "offered_to_skills": _page_skills.get(r["offered_to_agent_id"], {}),
                    # Nested party dicts mirroring get_job's shape, so one
                    # reader serves both (flat names stay for back-compat).
                    "parties": {
                        "creator": (
                            {
                                "agent_id": r["creator_agent_id"],
                                "name": r["creator_name"],
                                "skills": _page_skills.get(r["creator_agent_id"], {}),
                            }
                            if r["creator_agent_id"] is not None
                            else None
                        ),
                        "worker": (
                            {
                                "agent_id": r["worker_agent_id"],
                                "name": r["worker_name"],
                                "skills": _page_skills.get(r["worker_agent_id"], {}),
                            }
                            if r["worker_agent_id"] is not None
                            else None
                        ),
                        "offered_to": (
                            {
                                "agent_id": r["offered_to_agent_id"],
                                "name": r["offered_to_name"],
                                "skills": _page_skills.get(
                                    r["offered_to_agent_id"], {}
                                ),
                            }
                            if r["offered_to_agent_id"] is not None
                            else None
                        ),
                    },
                    "payment_credits": _fmt_q(r["payment_quarters"]),
                    "total_cycles": r["total_cycles"],
                    "cycles_done": r["cycles_done"],
                    "overdue": _overdue_flag(
                        r["status"],
                        cur_status,
                        anchors.get(r["id"], r["created_at"]),
                        job_overdue_cutoff(hours=_cadence_hours(r)),
                        opens_at=cur_opens_at,
                    ),
                    "opens_at": cur_opens_at,
                    "created_at": r["created_at"],
                }
            )
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
