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

    # Review-hardening pins: adversarial bless rows never shadow good ones.
    assert (
        events._bench_anchor_valid(
            {"anchor_run_event_id": 5, "medians": {"a": float("nan")}}
        )
        is None
    ), "NaN medians do not validate"
    assert (
        events._bench_anchor_valid(
            {"anchor_run_event_id": 5, "medians": {"a": float("inf")}}
        )
        is None
    ), "inf medians do not validate"
    assert (
        events._bench_anchor_valid({"anchor_run_event_id": True, "medians": {"a": 1.0}})
        is None
    ), "bool run pointer does not validate"
    # A NaN bless logged newest is paged past, not honored (NaN survives
    # the ledger JSON round-trip, so the validator is the only guard).
    events.log_event(
        events.EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=blesser["agent_id"],
        actor_name=blesser["name"],
        detail={
            "anchor_run_event_id": 999,
            "blessed_by": None,
            "reason": "cron",
            "medians": {"a": float("nan")},
        },
    )
    assert events.bench_anchor_for()["bless_event_id"] == anchor2["bless_event_id"], (
        "NaN bless skipped"
    )
    # Eleven newer malformed rows cannot hide the well-formed anchor.
    for _ in range(11):
        events.log_event(
            events.EVT_BENCH_ANCHOR_BLESSED,
            actor_agent_id=blesser["agent_id"],
            actor_name=blesser["name"],
            detail={"anchor_run_event_id": 999},
        )
    assert events.bench_anchor_for()["bless_event_id"] == anchor2["bless_event_id"], (
        "malformed flood paged past"
    )
    assert (
        events.bench_anchor_for(limit=0)["bless_event_id"] == anchor2["bless_event_id"]
    ), "limit clamps to >=1"

    # Boundary pins on crafted rows (no ledger): exact-20% stays fresh
    # under the strict >, 6d23h stays fresh, future stamps never force aging.
    rows12 = [
        {"detail": {"mode": "native", "summary": {"timings_median_ms": {"a": 12.0}}}},
        {"detail": {"mode": "native", "summary": {"timings_median_ms": {"a": 12.0}}}},
    ]
    a10 = {"blessed_at": db._now_iso(), "medians": {"a": 10.0}}
    aging, _ = events.bench_anchor_aging(a10, rows12)
    assert not aging, "exact-20% drift stays fresh"
    rows10 = [
        {"detail": {"mode": "native", "summary": {"timings_median_ms": {"a": 10.0}}}},
    ]
    almost = (
        (datetime.now(timezone.utc) - timedelta(days=6, hours=23))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    aging, _ = events.bench_anchor_aging(
        {"blessed_at": almost, "medians": {"a": 10.0}}, rows10
    )
    assert not aging, "6d23h anchor stays fresh"
    future = (
        (datetime.now(timezone.utc) + timedelta(days=1))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    aging, _ = events.bench_anchor_aging(
        {"blessed_at": future, "medians": {"a": 10.0}}, rows10
    )
    assert not aging, "future stamp never forces aging"

    # Single-source comparison base: blessed anchor wins with its label.
    base, label, got = events.bench_anchor_base_for(rows)
    assert label == "vs anchor", "anchor label when blessed"
    assert got["bless_event_id"] == anchor2["bless_event_id"], "anchor carried"
    assert base["a"] == 15.0, "anchor medians are the base"

    # Native-only series, newest-first, capped.
    series = events.bench_native_series(rows)
    assert series["a"] == [15.0, 10.0], "series newest-first across natives"
    assert events.bench_native_series(rows, limit=1)["a"] == [15.0], "limit caps"

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_anchor: all assertions passed")


if __name__ == "__main__":
    main()
