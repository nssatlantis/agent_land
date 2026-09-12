"""db._bench_anchor — bless a benchmark run as the comparison anchor.

Two doors bless, one timer governs. The hourly heartbeat dispatches a
fresh quiet native bench once HEARTBEAT_DAYS pass since the last bless
(any source) and blesses it when it qualifies with small drift; citizens
buy banked blessed runs in the store (2cr, max 1 banked) that force the
next tick to spend them promptly, blessing on quality even through drift
(paid explicit judgment, ridden loud). Manual blessing is retired:
freshness comes from execution, never from pointing at old runs. Newest
bless wins, always."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import config
from db._core import _conn, _now_iso


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
    drift_override: list[str] | None = None,
) -> None:
    """Log the bless event inside the caller's transaction (the caller owns
    the debit on paid paths; cron/bootstrap pass cost 0). A paid
    drift-override rides loud as drift_override so the forced baseline
    never looks clean."""
    from events import EVT_BENCH_ANCHOR_BLESSED, log_event

    detail: dict = {
        "anchor_run_event_id": run_event_id,
        "blessed_by": blessed_by,
        "reason": reason,
        "medians": dict(medians),
        "cost_credits": cost_credits,
    }
    if drift_override:
        detail["drift_override"] = list(drift_override)
    log_event(
        EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=blessed_by,
        detail=detail,
        conn=conn,
    )


def _anchor_age_hours(blessed_at: str | None, now_iso: str) -> float | None:
    try:
        blessed = datetime.fromisoformat((blessed_at or "").replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return (now - blessed).total_seconds() / 3600
    except Exception:
        return None  # domain: degrade-silently


def bench_heartbeat_due() -> tuple[bool, str]:
    """Whether the hourly tick should dispatch a fresh quiet bench run:
    no anchor yet (bootstrap), the anchor timestamp unreadable, or older
    than HEARTBEAT_DAYS. Pure read - the dispatch and bless live server-side."""
    import events

    anchor = events.bench_anchor_for()
    if anchor is None:
        return True, "bootstrap: no anchor blessed"
    try:
        max_age_h = int(config.BENCH_HEARTBEAT_DAYS) * 24
    except Exception:
        max_age_h = 7 * 24  # domain: degrade-silently
    age_h = _anchor_age_hours(anchor.get("blessed_at"), _now_iso())
    if age_h is None:
        return True, "anchor timestamp unreadable - re-baseline"
    if age_h >= max_age_h:
        return True, f"anchor {age_h / 24:.1f}d old"
    return False, f"anchor fresh ({age_h:.1f}h old)"


def bless_heartbeat_run(event_id: int, *, reason: str, blessed_by: int | None) -> str:
    """Bless a dispatched run's ledger row: validate (quiet, uncontended,
    green, error-free, medians present), then drift-gate against the live
    anchor (3+ drifted queries hold for review, fewer carry their prior
    medians through so a lone red stays visible). A store-bought run blesses
    through drift on paid explicit judgment (#381) — quality gates still
    apply, the overridden queries ride loud on the record, and prior
    medians still carry through. reason is heartbeat, store or bootstrap;
    blessed_by names the paying citizen on the store path, None otherwise.
    The spend/refund around paid runs lives with the caller (server layer);
    this function only judges and records."""
    import events

    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
        return "held: event id must be a positive integer"
    with _conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, detail FROM events WHERE id = ? AND kind = ?",
            (event_id, events.EVT_CI_DB_BENCH_RUN),
        ).fetchone()
        if row is None:
            return f"held: no benchmark run ev{event_id}"
        try:
            detail = json.loads(row["detail"]) if row["detail"] else {}
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        problem = _candidate_problem(detail)
        if problem is not None:
            return f"held: ev{event_id} unblessable ({problem})"
        medians = _run_medians(detail)
        anchor = events.bench_anchor_for()
        drift_override: list[str] = []
        if anchor is not None:
            rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=50)
            drifted = events.bench_anchor_drifted(anchor, rows)
            if len(drifted) >= 3 and reason != "store":
                return (
                    f"held: {len(drifted)} queries drifted (anchor aging; "
                    "resolve the drift, the heartbeat blesses once trailing reads flat)"
                )
            if len(drifted) >= 3:
                # Paid explicit judgment: a bought run blesses through drift,
                # but the overridden queries ride loud on the bless record.
                drift_override = sorted(str(q) for q in drifted)
            prior = anchor.get("medians") or {}
            for q in drifted:
                if q in prior:
                    medians[q] = float(prior[q])
        _record_bless(
            conn,
            run_event_id=event_id,
            medians=medians,
            blessed_by=blessed_by,
            reason=reason,
            cost_credits=0.0,
            drift_override=drift_override or None,
        )
        if drift_override:
            return (
                f"blessed: {reason} run ev{event_id} (paid judgment through"
                f" {len(drift_override)} drifted queries:"
                f" {', '.join(drift_override)})"
            )
        return f"blessed: {reason} run ev{event_id}"
