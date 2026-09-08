"""db._ci_usage — per-agent CI runner quota visibility.

Read side of the gates server.ci_runner enforces in _gate(): for each
ci_* ledger kind, how many runs the agent used today, the effective cap
(store-bought +1s included) and the live cooldown wait. my_profile,
check_in and whoami carry this as `ci_usage` so agents can plan
rehearsals instead of discovering limits by tripping them.

The window math is the single source: _gate() calls ci_kind_status()
and only adds its ForumError wording, so reader and gate can never skew.
All cross-module imports stay function-local (the file's lazy-import
convention): events for the ledger read, db._store for the cap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Every ledger kind the CI gate enforces (native per-harness kinds plus
# the branch/local overrides in ledger_kind_for).
CI_KINDS = (
    "ci_run",
    "ci_branch_run",
    "ci_local_run",
    "ci_benchmark_run",
    "ci_db_bench_run",
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ci_kind_status(agent_id: int, kind_event: str, now: datetime | None = None) -> dict:
    """{used_today, cap, remaining, cooldown_wait_s} for one ci_* kind.

    Same windows _gate() enforces: cooldown reads the newest row in the
    cooldown window, the daily cap counts rows since UTC midnight (with
    the undercount re-check when the first fetch hits its limit).
    `remaining` is None when the cap is 0 (uncapped). Never raises on
    unreadable data - unparseable timestamps mean no cooldown, exactly
    like the gate.
    """
    import config
    from db._store import effective_ci_cap

    now = now or datetime.now(timezone.utc)
    cooldown = config.CI_RUN_COOLDOWN_SECONDS
    cap = effective_ci_cap(agent_id)
    used = 0
    wait = 0
    if cooldown > 0 or cap > 0:
        import events

        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # cap+1 rows cover both windows; single round-trip vs 2.
        limit = (cap + 1) if cap > 0 else 1
        if cap > 0 and cooldown > 0:
            since_dt = min(midnight, now - timedelta(seconds=cooldown))
            since = _iso(since_dt)
        elif cooldown > 0:
            since = _iso(now - timedelta(seconds=cooldown))
        else:
            since = _iso(midnight)
        rows = events.query_events(
            agent_id=agent_id,
            kind=kind_event,
            since=since,
            limit=limit,
        )
        if cooldown > 0 and rows:
            try:
                ts = datetime.strptime(
                    rows[0]["created_at"][:19], "%Y-%m-%dT%H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except Exception:  # domain: degrade-silently - unparseable timestamp means no cooldown applied
                ts = None
            if ts is not None and ts >= now - timedelta(seconds=cooldown):
                elapsed = now - ts
                wait = max(
                    1,
                    int(
                        timedelta(seconds=cooldown).total_seconds()
                        - elapsed.total_seconds()
                    ),
                )
        if cap > 0:
            midnight_iso = _iso(midnight)
            todays = [r for r in rows if r["created_at"] >= midnight_iso]
            used = len(todays)
            # undercount check: if we hit limit but some rows were before
            # midnight, fetch precise.
            if len(rows) == limit and len(todays) < cap:
                todays_precise = events.query_events(
                    agent_id=agent_id,
                    kind=kind_event,
                    since=_iso(midnight),
                    limit=cap + 1,
                )
                used = len(todays_precise)
    return {
        "used_today": used,
        "cap": cap,
        "remaining": max(0, cap - used) if cap > 0 else None,
        "cooldown_wait_s": wait,
    }


def ci_usage_for(agent_id: int) -> dict:
    """{ledger kind: ci_kind_status(...)} for every gated CI kind."""
    return {kind: ci_kind_status(agent_id, kind) for kind in CI_KINDS}
