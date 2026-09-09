"""db._bench_history — benchmark trend reads for agents (single-anchor #367)."""

from __future__ import annotations

import statistics

from db._core import ForumError


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
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=window)
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
