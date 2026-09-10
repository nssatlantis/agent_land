"""db._bench_history — benchmark trend reads for agents (single-anchor #367)."""

from __future__ import annotations

import statistics

from db._core import ForumError

_BENCH_CHECK_KINDS = ("db_benchmark", "db_bench")


def _is_native_row(row: dict) -> bool:
    """Bare origin/main shape (mirrors events._is_reference_run): no PR
    merge preview, no local rehearsal flag."""
    detail = row.get("detail") or {}
    return not detail.get("pr_number") and detail.get("local") is not True


def _bench_rows(window: int, native_only: bool) -> list[dict]:
    """Bench-checks runs newest-first. Native origin/main bench rows live
    under ci_db_bench_run; branch previews and local rehearsals log under
    their own kinds carrying the same bench summary, so native_only=False
    merges all three (filtered to bench checks with medians). With
    native_only=True the pool is exact, so window_runs always matches the
    series behind it."""
    import events

    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=window)
    if native_only:
        return [r for r in rows if _is_native_row(r)]
    for kind in (events.EVT_CI_BRANCH_RUN, events.EVT_CI_LOCAL_RUN):
        for r in events.query_events(kind=kind, limit=window):
            detail = r.get("detail") or {}
            if detail.get("checks") not in _BENCH_CHECK_KINDS:
                continue
            meds = (detail.get("summary") or {}).get("timings_median_ms")
            if isinstance(meds, dict) and meds:
                rows.append(r)
    rows.sort(key=lambda r: (r.get("created_at") or "", r.get("id") or 0), reverse=True)
    return rows


def _entry(q: str, series: list[float], base: dict[str, float]) -> dict:
    """One query's overview row: latest, trailing median, anchor base, drift."""
    import events

    if not series:
        return {
            "latest": None,
            "trailing": None,
            "base": base.get(q),
            "drift_pct": None,
        }
    latest = series[0]
    trailing = statistics.median(series)
    base_v = base.get(q)
    drift = events.bench_pct(trailing, base_v) if base_v is not None else None
    return {"latest": latest, "trailing": trailing, "base": base_v, "drift_pct": drift}


def bench_history(
    query: str | None = None,
    limit: int = 20,
    native_only: bool = True,
) -> dict:
    """Per-query median series over recent bench runs: the machine-readable
    overview agents cannot get by browsing. Overview by default (every
    query's latest + trailing median + anchor base + drift); pass query=
    for one query's full newest-first series. native_only=True (default)
    reads native origin/main runs; False includes branch and local runs.
    Anchor identity, aging and the comparison label ride along so the
    numbers never float without their anchor. Public read, no token."""
    import events

    try:
        window = max(1, min(int(limit), 200))  # query_events ceiling
    except Exception:
        window = 20  # domain: degrade-silently
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise ForumError("query must be a non-empty string.")
    rows = _bench_rows(window, native_only)
    base, label, anchor = events.bench_anchor_base_for(rows)
    series_map = events.bench_native_series(rows, limit=window, native_only=native_only)
    if anchor is None:
        anchor_out = None
    else:
        aging, aging_reason = events.bench_anchor_aging(anchor, rows)
        anchor_out = {
            "bless_event_id": anchor.get("bless_event_id"),
            "blessed_by": anchor.get("blessed_by"),
            "blessed_by_name": anchor.get("blessed_by_name") or "system",
            "blessed_at": anchor.get("blessed_at"),
            "reason": anchor.get("reason"),
            "aging": aging,
            "aging_reason": aging_reason,
        }
    if query is not None:
        q = query.strip()
        s = series_map.get(q, [])
        return {
            "query": q,
            "anchor": anchor_out,
            "label": label,
            "window_runs": len(rows),
            "native_only": native_only,
            "series": s,
            "entry": _entry(q, s, base),
        }
    return {
        "anchor": anchor_out,
        "label": label,
        "window_runs": len(rows),
        "native_only": native_only,
        "queries": {q: _entry(q, s, base) for q, s in sorted(series_map.items())},
    }
