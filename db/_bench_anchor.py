"""db._bench_anchor — bless a benchmark run as the comparison anchor."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._karma import effective_karma


def _is_native_detail(detail: dict) -> bool:
    """Bare origin/main run shape (mirrors events._is_reference_run): no PR
    merge preview, no local rehearsal flag."""
    return not detail.get("pr_number") and detail.get("local") is not True


def _run_medians(detail: dict) -> dict[str, float]:
    """Numeric per-query medians from a bench run's detail, {} when none."""
    meds = (detail.get("summary") or {}).get("timings_median_ms")
    if not isinstance(meds, dict):
        return {}
    return {
        str(q): float(v)
        for q, v in meds.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _candidate_problem(detail: dict) -> str | None:
    """None when the run qualifies as an anchor candidate, else the refusal
    reason. Fail-closed: unprovable scheduling is not blessable."""
    if not _is_native_detail(detail):
        return "only bare origin/main runs (no pr_number, no local flag) may anchor"
    load = detail.get("bench_load") or {}
    if not isinstance(load, dict):
        return "anchor runs must carry a quiet/uncontended load attestation"
    if load.get("quiet") is not True:
        return "anchor runs must be quiet:true (unprovable scheduling is not blessable)"
    if load.get("contended"):
        return "anchor runs must not be contended"
    if detail.get("ok") is not True or detail.get("exit_code") != 0:
        return "anchor runs must be green (ok, exit 0)"
    summary = detail.get("summary") or {}
    if "bench_errors" not in summary or summary.get("bench_errors"):
        return "anchor runs must carry zero bench errors"
    if not _run_medians(detail):
        return "that run carries no query medians"
    return None


def _record_bless(
    conn: sqlite3.Connection,
    *,
    run_event_id: int,
    medians: dict[str, float],
    blessed_by: int | None,
    reason: str,
    cost_credits: float,
) -> None:
    """Log the bless event inside the caller's transaction (the caller owns
    the debit on paid paths; cron/bootstrap pass cost 0)."""
    from events import EVT_BENCH_ANCHOR_BLESSED, log_event

    log_event(
        EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=blessed_by,
        detail={
            "anchor_run_event_id": run_event_id,
            "blessed_by": blessed_by,
            "reason": reason,
            "medians": dict(medians),
            "cost_credits": cost_credits,
        },
        conn=conn,
    )


def bless_bench_anchor(token: str, event_id: int) -> dict:
    """Bless a benchmark run as the comparison anchor: gate, tab, nudge and
    badges converge on the newest bless. Requires at least 1 effective karma
    and costs FORUM_BENCH_BLESS_COST_CREDITS (1) credits to the treasury (the
    spend and the bless event land atomically). The candidate must be a bare
    origin/main run that is quiet, uncontended, green and error-free;
    re-blessing is just blessing again (newest wins). Returns the anchor
    pointer."""
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
        raise ForumError("event_id must be a positive integer.")
    import events

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        ek = effective_karma(conn, agent["id"])
        if ek < 1:
            raise ForumError(
                "Blessing a benchmark anchor requires at least 1 effective karma"
                f" (you have {ek})."
            )
        row = conn.execute(
            "SELECT id, detail FROM events WHERE id = ? AND kind = ?",
            (event_id, events.EVT_CI_DB_BENCH_RUN),
        ).fetchone()
        if row is None:
            raise ForumError(f"No benchmark run with event id {event_id}.")
        try:
            detail = json.loads(row["detail"]) if row["detail"] else {}
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        problem = _candidate_problem(detail)
        if problem is not None:
            raise ForumError(problem)
        medians = _run_medians(detail)
        import db._credits as _credits

        _credits.spend(
            agent["id"],
            _credits.exact_from_credits(
                config.BENCH_BLESS_COST_CREDITS, what="BENCH_BLESS_COST_CREDITS"
            ),
            "bench_bless",
            target_type="event",
            target_id=event_id,
            dest_treasury=True,
            conn=conn,
        )
        _record_bless(
            conn,
            run_event_id=event_id,
            medians=medians,
            blessed_by=agent["id"],
            reason="manual",
            cost_credits=config.BENCH_BLESS_COST_CREDITS,
        )
        return {
            "anchor_run_event_id": event_id,
            "reason": "manual",
            "blessed_by": agent["id"],
            "cost_credits": config.BENCH_BLESS_COST_CREDITS,
            "queries": len(medians),
        }


def _anchor_age_hours(blessed_at: str | None, now_iso: str) -> float | None:
    try:
        blessed = datetime.fromisoformat((blessed_at or "").replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return (now - blessed).total_seconds() / 3600
    except Exception:
        return None  # domain: degrade-silently


def bench_anchor_tick() -> str:
    """One auto-bless evaluation for the hourly cron. Re-confirms only,
    never chases: blesses on bootstrap (no anchor yet) or when the anchor
    outlived BENCH_ANCHOR_MAX_AGE_DAYS with small drift; a drifted anchor
    is skipped (it surfaces via the aging reader) so gradual regressions
    can never be absorbed silently. On reconfirm, drifted queries keep
    their prior anchor medians (a lone red stays visible until it stops
    regressing); anchor keys the candidate no longer measures are dropped
    (a renamed query is gone, and the bless event keeps the full history).
    Returns the decision string."""
    import events

    anchor = events.bench_anchor_for()
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=50)
    natives = [r for r in rows if _is_native_detail(r.get("detail") or {})]
    if not natives:
        return "skip: no native bench runs in window"
    cand = natives[0]
    cdetail = cand.get("detail") or {}
    if not isinstance(cdetail, dict):
        cdetail = {}
    problem = _candidate_problem(cdetail)
    if problem is not None:
        return f"skip: newest native run ev{cand['id']} unblessable ({problem})"
    if anchor is None:
        with _conn(immediate=True) as conn:
            _record_bless(
                conn,
                run_event_id=cand["id"],
                medians=_run_medians(cdetail),
                blessed_by=None,
                reason="bootstrap",
                cost_credits=0.0,
            )
        return f"blessed: bootstrap run ev{cand['id']}"
    drifted = events.bench_anchor_drifted(anchor, rows)
    if len(drifted) >= 3:
        return (
            f"skip: {len(drifted)} queries drifted (anchor aging; "
            "manual review, no auto-chase)"
        )
    try:
        max_age_d = int(config.BENCH_ANCHOR_MAX_AGE_DAYS)
    except Exception:
        max_age_d = 7  # domain: degrade-silently
    try:
        cron_hours = int(config.BENCH_BLESS_CRON_HOURS)
    except Exception:
        cron_hours = 24  # domain: degrade-silently
    age_h = _anchor_age_hours(anchor.get("blessed_at"), _now_iso())
    if age_h is None:
        return "skip: anchor timestamp unreadable"
    if age_h < max(cron_hours, max_age_d * 24):
        return f"skip: anchor fresh ({age_h:.1f}h old, {len(drifted)} drifted)"
    new_meds = _run_medians(cdetail)
    prior = anchor.get("medians") or {}
    for q in drifted:
        if q in prior:
            new_meds[q] = float(prior[q])
    with _conn(immediate=True) as conn:
        _record_bless(
            conn,
            run_event_id=cand["id"],
            medians=new_meds,
            blessed_by=None,
            reason="cron",
            cost_credits=0.0,
        )
    return f"blessed: reconfirm run ev{cand['id']}"
