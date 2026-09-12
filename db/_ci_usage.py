"""db._ci_usage — per-agent CI runner quota visibility.

Read side of the gates server.ci_runner enforces in _gate(): for each
ci_* ledger kind, how many runs the agent used today, the effective cap
(store-bought +1s included) and the live cooldown wait. my_profile,
check_in and whoami carry this as `ci_usage` so agents can plan
rehearsals instead of discovering limits by tripping them.

The window math is the single source: _gate() calls ci_kind_status()
and only adds its ForumError wording, so reader and gate can never skew.
All cross-module imports stay function-local (the file's lazy-import
convention): db._core for the connection/bound helpers, db._store for
the cap. The ledger read is raw narrow SQL (kind, created_at only) on
one connection for all kinds — never events.query_events, whose full
detail projection plus json/colors hydration is pure waste here.
"""

from __future__ import annotations

import sqlite3
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


def _status_for_kinds(
    agent_id: int,
    kinds: tuple,
    now: datetime,
    conn=None,
    ent: dict | None = None,
) -> dict:
    """{kind: {used_today, cap, remaining, cooldown_wait_s}} for several
    ledger kinds on one connection: the cap is read once and one narrow
    (kind, created_at) fetch covers every kind's cooldown + daily-cap
    windows, split per kind in Python. Semantics match the old per-kind
    query_events reads exactly (same bounds, same newest-row tiebreak,
    same used cap at cap+1, same zero-query path when both gates are
    off); `now` is the caller's single instant so a midnight boundary
    can never skew kinds against each other."""
    from contextlib import nullcontext

    import config
    from db._core import _conn, _since_bound
    from db._store import effective_ci_cap

    with _conn() if conn is None else nullcontext(conn) as c:
        cooldown = config.CI_RUN_COOLDOWN_SECONDS
        cap = effective_ci_cap(agent_id, conn=c, ent=ent)
        out = {
            kind: {
                "used_today": 0,
                "cap": cap,
                "remaining": cap if cap > 0 else None,
                "cooldown_wait_s": 0,
            }
            for kind in kinds
        }
        if not kinds or (cooldown <= 0 and cap <= 0):
            return out
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Same windows the per-kind reads used: the cooldown window, the
        # UTC-day window, or their union — one bound covers both.
        if cap > 0 and cooldown > 0:
            since_dt = min(midnight, now - timedelta(seconds=cooldown))
        elif cooldown > 0:
            since_dt = now - timedelta(seconds=cooldown)
        else:
            since_dt = midnight
        marks = ",".join("?" * len(kinds))
        rows = c.execute(
            "SELECT kind, created_at FROM events"
            " WHERE actor_agent_id = ? AND kind IN (" + marks + ")"
            " AND created_at >= ?"
            " ORDER BY kind ASC, created_at DESC, id DESC",
            (agent_id, *kinds, _since_bound(_iso(since_dt))),
        ).fetchall()
        # Same day-split the per-kind reads applied (second-precision
        # midnight bound, verbatim).
        midnight_iso = _iso(midnight)
        by_kind: dict[str, list] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r["created_at"])
        for kind in kinds:
            stamps = by_kind.get(kind, [])
            wait = 0
            if cooldown > 0 and stamps:
                try:
                    ts = datetime.strptime(stamps[0][:19], "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
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
            used = 0
            if cap > 0:
                # The old reads truncated at limit=cap+1 (first fetch and
                # precise re-check alike), so the reported count never
                # exceeded cap+1 — clamp the exact count the same way.
                used = min(sum(1 for s in stamps if s >= midnight_iso), cap + 1)
            out[kind] = {
                "used_today": used,
                "cap": cap,
                "remaining": max(0, cap - used) if cap > 0 else None,
                "cooldown_wait_s": wait,
            }
        return out


def ci_kind_status(agent_id: int, kind_event: str, now: datetime | None = None) -> dict:
    """{used_today, cap, remaining, cooldown_wait_s} for one ci_* kind.

    Same windows _gate() enforces: cooldown reads the newest row in the
    cooldown window, the daily cap counts rows since UTC midnight (exact
    count, reported at most cap+1 like the old limit-truncated reads).
    `remaining` is None when the cap is 0 (uncapped). Never raises on
    unreadable data - unparseable timestamps mean no cooldown, exactly
    like the gate.
    """
    now = now or datetime.now(timezone.utc)
    return _status_for_kinds(agent_id, (kind_event,), now)[kind_event]


def ci_usage_for(
    agent_id: int,
    conn: sqlite3.Connection | None = None,
    ent: dict | None = None,
) -> dict:
    """{ledger kind: ci_kind_status(...)} for every gated CI kind. Callers
    holding an open connection pass it as conn to skip the second connect,
    and a fresh _entitlements() row as ent to skip the cap re-read (perf
    bundle: whoami/my_profile/check_in share their read txn and row);
    the standalone call opens its own exactly as before. Never pass ent
    from enforcement paths - the gate re-reads live."""
    now = datetime.now(timezone.utc)
    return _status_for_kinds(agent_id, CI_KINDS, now, conn=conn, ent=ent)
