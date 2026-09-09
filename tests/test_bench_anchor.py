"""Tests for the blessed benchmark anchor store (events.bench_anchor_for /
bench_anchor_aging, single-anchor program #367).

The anchor is the newest well-formed bench_anchor_blessed event (pointer to
the anchor run + denormalized medians + by/reason/at); malformed rows are
skipped, newest well-formed wins. Aging is lazy reader math (no anchor /
older than BENCH_ANCHOR_MAX_AGE_DAYS / trailing native drift >20% on 3+
queries) - nothing here mutates.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_anchor_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
import events  # noqa: E402, I001


def _seed_native_run(subject, meds):
    events.log_event(
        events.EVT_CI_DB_BENCH_RUN,
        actor_agent_id=subject["agent_id"],
        actor_name=subject["name"],
        detail={
            "checks": "db_benchmark",
            "mode": "native",
            "ok": True,
            "exit_code": 0,
            "duration_seconds": 20.0,
            "head_sha": "beef1234567890abcdef1234567890abcdef1",
            "summary": {
                "bench": "db_benchmark",
                "regressions": 0,
                "timings_median_ms": meds,
            },
        },
    )
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=1)
    assert rows, "seeded bench run is queryable"
    return rows[0]["id"]


def _bless(blesser, run_event_id, meds, reason):
    events.log_event(
        events.EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=blesser["agent_id"],
        actor_name=blesser["name"],
        detail={
            "anchor_run_event_id": run_event_id,
            "blessed_by": blesser["agent_id"] if reason == "manual" else None,
            "reason": reason,
            "medians": dict(meds),
        },
    )


def main():
    agents, _ = setup()
    assert events.bench_anchor_for() is None, "no anchor before any bless"
    aging, reason = events.bench_anchor_aging(None, [])
    assert aging and reason == "no anchor blessed", "missing anchor reads aging"

    subject = db.register_agent("anchor-subject")
    blesser = db.register_agent("anchor-blesser")
    flat = {"a": 10.0, "b": 20.0, "c": 30.0}
    run1 = _seed_native_run(subject, flat)
    _bless(blesser, run1, flat, "manual")

    anchor = events.bench_anchor_for()
    assert anchor is not None, "bless lands an anchor"
    assert anchor["medians"] == flat, "anchor carries the blessed medians"
    assert anchor["anchor_run_event_id"] == run1, "anchor points at the run"
    assert anchor["reason"] == "manual", "reason round-trips"
    assert anchor["blessed_by"] == blesser["agent_id"], "blesser round-trips"
    assert isinstance(anchor["bless_event_id"], int), "bless event id carried"

    # A newer malformed bless never shadows a well-formed anchor.
    events.log_event(
        events.EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=blesser["agent_id"],
        actor_name=blesser["name"],
        detail={"anchor_run_event_id": run1},
    )
    assert events.bench_anchor_for()["bless_event_id"] == anchor["bless_event_id"], (
        "malformed bless skipped in favor of older well-formed anchor"
    )

    # Fresh + flat native window reads fresh, not aging.
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=20)
    aging, reason = events.bench_anchor_aging(anchor, rows)
    assert not aging, f"flat fresh anchor is fresh ({reason})"

    # Newer well-formed bless wins (cron/system shape: blessed_by None).
    drifted = {"a": 15.0, "b": 30.0, "c": 45.0}
    run2 = _seed_native_run(subject, drifted)
    _bless(blesser, run2, drifted, "cron")
    anchor2 = events.bench_anchor_for()
    assert anchor2["bless_event_id"] != anchor["bless_event_id"], (
        "newer well-formed bless wins"
    )
    assert anchor2["blessed_by"] is None, "system bless carries no blesser"
    assert anchor2["reason"] == "cron", "cron reason round-trips"

    # Trailing native median drifted +25% on all 3 queries vs the flat
    # anchor: aging with the drift reason.
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=20)
    aging, reason = events.bench_anchor_aging(anchor, rows)
    assert aging and "drifted" in reason, f"drift reads aging ({reason})"

    # Pure unit: a 10-day-old anchor with a flat window ages on age alone.
    old_at = (
        (datetime.now(timezone.utc) - timedelta(days=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    old_anchor = dict(anchor, blessed_at=old_at, medians=dict(flat))
    flat_rows = [r for r in rows if r["id"] == run1]
    aging, reason = events.bench_anchor_aging(old_anchor, flat_rows)
    assert aging and "old" in reason, f"stale anchor reads aging ({reason})"

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_anchor: all assertions passed")


if __name__ == "__main__":
    main()
