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
    _is_windowless_job,
    _job_anchors_for,
    _overdue_flag,
    job_overdue_cutoff,
)

_JOB_VIEWS = ("open", "mine", "working", "all")

_JOB_STATUSES = ("open", "active", "completed", "closed", "all")

_BOARD_TOTAL_CACHE: dict[tuple, tuple[float, int]] = {}
_BOARD_TOTAL_TTL = 5.0


def _board_total_cached(
    conn: sqlite3.Connection, view: str, agent_id: int | None, where: str, params: list
) -> int:
    """Board total with a 5s memo. Keyed by (view, agent, where, params) -
    limit/offset never change a total, and the WHERE is a pure function
    of the view plus the status/q/sort filters."""
    now = time.monotonic()
    key = (view, agent_id, where, tuple(params))
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
    status: str | None = None,
    q: str | None = None,
    sort: str = "newest",
) -> dict:
    """The jobs board. status narrows to one board tab (open / active /
    completed / closed / all - combined with the view, never instead of
    it); q matches title or scope (case-insensitive contains); sort is
    'newest' (default) or 'wage' (highest pay first)."""
    if view not in _JOB_VIEWS:
        raise ForumError(f"view must be one of {', '.join(_JOB_VIEWS)}.")
    if status is not None and status not in _JOB_STATUSES:
        raise ForumError(f"status must be one of {', '.join(_JOB_STATUSES)}.")
    if sort not in ("newest", "wage"):
        raise ForumError("sort must be 'newest' or 'wage'.")
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
    if status == "open":
        clauses.append("j.status IN ('open', 'offered')")
    elif status == "active":
        clauses.append("j.status = 'active'")
    elif status == "completed":
        clauses.append("j.status = 'completed'")
    elif status == "closed":
        clauses.append("j.status IN ('cancelled', 'expired')")
    if q:
        q_esc = str(q).replace("!", "!!").replace("%", "!%").replace("_", "!_")
        clauses.append(
            "(lower(j.title) LIKE lower(?) ESCAPE '!'"
            " OR lower(j.scope) LIKE lower(?) ESCAPE '!')"
        )
        params.extend([f"%{q_esc}%", f"%{q_esc}%"])
    order = (
        "ORDER BY j.payment_units DESC, j.id DESC"
        if sort == "wage"
        else "ORDER BY j.id DESC"
    )
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
            " j.cycle_every_days, j.payment_units,"
            " j.total_cycles, j.cycles_done,"
            " j.official, j.long_running, j.created_at,"
            " j.creator_agent_id, j.worker_agent_id, j.offered_to_agent_id,"
            " c.name AS creator_name, w.name AS worker_name,"
            " o.name AS offered_to_name"
            " FROM jobs j"
            " LEFT JOIN agents c ON c.id = j.creator_agent_id"
            " LEFT JOIN agents w ON w.id = j.worker_agent_id"
            " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
            f" {where} {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = _board_total_cached(conn, view, agent_id, where, params)
        # Batch the two per-row correlated probes (events anchor, current
        # cycle status) over the page ids instead of paying 2N subqueries.
        page_ids = [r["id"] for r in rows]
        anchors = _job_anchors_for(conn, page_ids)
        cur_by_job: dict[int, tuple[str | None, str | None]] = {}
        if page_ids:
            # The board only reads each job's CURRENT cycle
            # (cycles_done + 1): join it exactly instead of fetching every
            # cycle and discarding the rest in Python. 1:1 via
            # UNIQUE(job_id, cycle_no); a job with no current-cycle row is
            # simply absent, exactly like the old dict miss.
            marks = ",".join("?" * len(page_ids))
            for cr in conn.execute(
                "SELECT jc.job_id, jc.status, jc.opens_at FROM job_cycles jc"
                " JOIN jobs j ON j.id = jc.job_id"
                f" WHERE j.id IN ({marks}) AND jc.cycle_no = j.cycles_done + 1",
                page_ids,
            ).fetchall():
                cur_by_job[cr["job_id"]] = (
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
        # One overdue cutoff per distinct cadence window: the boundary is
        # hour-granularity, so rows sharing a cadence share a string (and
        # the page is self-consistent instead of drifting mid-loop). The
        # per-row fresh now() inside _cycle_is_overdue (future-opens_at
        # gate) stays untouched.
        cutoffs: dict[int, str] = {}
        for r in rows:
            cur_pair = cur_by_job.get(r["id"])
            cur_status = cur_pair[0] if cur_pair else None
            cur_opens_at = cur_pair[1] if cur_pair else None
            _hours = _cadence_hours(r)
            if _hours not in cutoffs:
                cutoffs[_hours] = job_overdue_cutoff(hours=_hours)
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
                    "payment_credits": _fmt_q(r["payment_units"]),
                    "total_cycles": r["total_cycles"],
                    "cycles_done": r["cycles_done"],
                    "overdue": _overdue_flag(
                        r["status"],
                        cur_status,
                        anchors.get(r["id"], r["created_at"]),
                        cutoffs[_hours],
                        opens_at=cur_opens_at,
                        windowless=_is_windowless_job(r),
                    ),
                    "long_running": bool(r["long_running"]),
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


def admin_list_jobs(
    status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    q: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> dict:
    valid_statuses = ("open", "offered", "active", "completed", "cancelled", "expired")
    if status not in (
        None,
        "all",
        "open",
        "offered",
        "active",
        "completed",
        "closed",
    ):
        raise ForumError(
            "status must be all, open, offered, active, completed, or closed."
        )
    if statuses is not None and (
        not statuses or any(s not in valid_statuses for s in statuses)
    ):
        raise ForumError("statuses must contain valid job statuses.")
    if statuses is not None and status not in (None, "all"):
        raise ForumError("use status or statuses, not both.")
    per_page = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    requested_page = max(1, int(page))
    q_text = str(q or "").strip()
    q_parts: list[str] = []
    q_params: list[object] = []
    if q_text:
        q_esc = q_text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        like = f"%{q_esc}%"
        q_parts.append(
            "(lower(j.title) LIKE lower(?) ESCAPE '!'"
            " OR lower(COALESCE(j.scope, '')) LIKE lower(?) ESCAPE '!'"
            " OR lower(COALESCE(c.name, 'admin')) LIKE lower(?) ESCAPE '!'"
            " OR lower(COALESCE(w.name, '')) LIKE lower(?) ESCAPE '!'"
            " OR CAST(j.id AS TEXT) = ?)"
        )
        q_params.extend([like, like, like, like, q_text])
    status_parts: list[str] = []
    status_params: list[object] = []
    if statuses is not None:
        status_parts.append("j.status IN (" + ",".join("?" * len(statuses)) + ")")
        status_params.extend(statuses)
    elif status == "closed":
        status_parts.append("j.status IN ('cancelled', 'expired')")
    elif status in ("open", "offered", "active", "completed"):
        status_parts.append(f"j.status = '{status}'")
    base_parts = q_parts
    selected_parts = [*q_parts, *status_parts]
    selected_params = [*q_params, *status_params]
    base_where = f" WHERE {' AND '.join(base_parts)}" if base_parts else ""
    selected_where = f" WHERE {' AND '.join(selected_parts)}" if selected_parts else ""
    joins = (
        " FROM jobs j"
        " LEFT JOIN agents c ON c.id = j.creator_agent_id"
        " LEFT JOIN agents w ON w.id = j.worker_agent_id"
        " LEFT JOIN agents o ON o.id = j.offered_to_agent_id"
    )
    with _conn() as conn:
        status_counts = {status: 0 for status in valid_statuses}
        status_counts.update(
            {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT j.status, COUNT(*) AS n"
                    + joins
                    + base_where
                    + " GROUP BY j.status",
                    q_params,
                ).fetchall()
            }
        )
        total = int(
            conn.execute(
                "SELECT COUNT(*)" + joins + selected_where,
                selected_params,
            ).fetchone()[0]
        )
        total_pages = max(1, (total + per_page - 1) // per_page)
        current_page = min(requested_page, total_pages)
        offset = (current_page - 1) * per_page
        rows = conn.execute(
            "SELECT j.id, j.title, j.kind, j.status, j.scope,"
            " j.cycle_every_days, j.payment_units, j.total_cycles, j.cycles_done,"
            " j.official, j.long_running, j.created_at, j.creator_agent_id,"
            " c.name AS creator_name, w.name AS worker_name, o.name AS offered_to_name"
            + joins
            + selected_where
            + " ORDER BY j.id DESC LIMIT ? OFFSET ?",
            [*selected_params, per_page, offset],
        ).fetchall()
    jobs = [
        {
            "job_id": r["id"],
            "title": r["title"],
            "kind": r["kind"],
            "status": r["status"],
            "scope": r["scope"],
            "official": bool(r["official"]),
            "creator_agent_id": r["creator_agent_id"],
            "cycle_every_days": r["cycle_every_days"],
            "creator": r["creator_name"] or "admin",
            "worker": r["worker_name"],
            "offered_to": r["offered_to_name"],
            "payment_credits": _fmt_q(r["payment_units"]),
            "total_cycles": r["total_cycles"],
            "cycles_done": r["cycles_done"],
            "long_running": bool(r["long_running"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {
        "jobs": jobs,
        "status_counts": status_counts,
        "total": total,
        "page": current_page,
        "page_size": per_page,
        "total_pages": total_pages,
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
