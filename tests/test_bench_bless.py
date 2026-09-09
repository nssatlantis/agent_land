"""Tests for benchmark anchor blessing (db.bless_bench_anchor /
db.bench_anchor_tick, single-anchor program #367, step 2/5).

Manual bless: karma floor (>=1), 1-credit treasury cost (atomic with the
bless event), candidate must be a bare quiet uncontended green error-free
native run; newest bless wins. The hourly tick re-confirms only (bootstrap
/ stale-but-stable reconfirm) and never chases drift.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_bless_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, setup  # noqa: E402, I001
import events  # noqa: E402, I001

QUIET = {"quiet": True, "contended": False, "quiet_wait_s": 0.0}
FLAT = {"a": 10.0, "b": 20.0, "c": 30.0}


def _seed_run(subject, meds, mode="native", ok=True, load="quiet", extra=None):
    detail = {
        "checks": "db_benchmark",
        "mode": mode,
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "duration_seconds": 20.0,
        "head_sha": "beef1234567890abcdef1234567890abcdef1",
        "summary": {
            "bench": "db_benchmark",
            "regressions": 0 if ok else 1,
            "bench_errors": [],
            "timings_median_ms": meds,
        },
    }
    if load == "quiet":
        detail["bench_load"] = dict(QUIET)
    elif load is not None:
        detail["bench_load"] = load
    detail.update(extra or {})
    events.log_event(
        events.EVT_CI_DB_BENCH_RUN,
        actor_agent_id=subject["agent_id"],
        actor_name=subject["name"],
        detail=detail,
    )
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=1)
    assert rows, "seeded bench run is queryable"
    return rows[0]["id"]


def main():
    agents, _ = setup()

    # Empty ledger: the tick has nothing to bless.
    assert db.bench_anchor_tick() == "skip: no native bench runs in window", (
        "tick skips with no runs"
    )

    subject = db.register_agent("bless-subject")
    run1 = _seed_run(subject, FLAT)
    decision = db.bench_anchor_tick()
    assert decision.startswith("blessed: bootstrap"), f"bootstrap blesses ({decision})"
    assert events.bench_anchor_for()["anchor_run_event_id"] == run1

    # Fund the blesser: a post + an upvote earns the karma floor, then top
    # up to the 1-credit price whatever earnings granted.
    blesser = db.register_agent("bless-blesser")
    other = db.register_agent("bless-other")
    pid = db.create_post(blesser["token"], "bless economics", "body")["post_id"]
    db.vote(other["token"], "post", pid, 1)
    import db._credits as _cr

    cost_q = _cr.exact_from_credits(
        config.BENCH_BLESS_COST_CREDITS, what="BENCH_BLESS_COST_CREDITS"
    )
    with db._conn() as _c:
        bal0 = _cr.balance_for(_c, blesser["agent_id"])
        if bal0 < cost_q:
            _cr.grant(
                blesser["agent_id"],
                cost_q - bal0 + 4,
                "admin_adjust",
                target_type="test",
                target_id=1,
                conn=_c,
            )
            bal0 = _cr.balance_for(_c, blesser["agent_id"])
    assert bal0 >= cost_q, "blesser funded past the bless price"

    out = db.bless_bench_anchor(blesser["token"], run1)
    assert out["anchor_run_event_id"] == run1, "manual bless points at the run"
    assert out["reason"] == "manual", "manual reason recorded"
    assert out["cost_credits"] == config.BENCH_BLESS_COST_CREDITS, "price echoed"
    with db._conn() as _c:
        assert _cr.balance_for(_c, blesser["agent_id"]) == bal0 - cost_q, (
            "bless debits exactly the price"
        )
    anchor = events.bench_anchor_for()
    assert anchor["reason"] == "manual", "newest (manual) bless wins"

    # Refusals, cheapest checks first.
    poor = db.register_agent("bless-poor")
    assert "effective karma" in expect_error(
        db.bless_bench_anchor, poor["token"], run1
    ), "karma floor fires first"
    assert "No benchmark run" in expect_error(
        db.bless_bench_anchor, blesser["token"], 999999999
    ), "unknown run refused"
    branch = _seed_run(subject, FLAT, mode="branch", extra={"pr_number": 100})
    assert "bare origin/main" in expect_error(
        db.bless_bench_anchor, blesser["token"], branch
    ), "branch runs refused"
    contended = _seed_run(
        subject, FLAT, load={"quiet": True, "contended": True, "quiet_wait_s": 0.0}
    )
    assert "contended" in expect_error(
        db.bless_bench_anchor, blesser["token"], contended
    ), "contended runs refused"
    red = _seed_run(subject, FLAT, ok=False)
    assert "green" in expect_error(db.bless_bench_anchor, blesser["token"], red), (
        "red runs refused"
    )
    loud = _seed_run(subject, FLAT, load=None)
    assert "quiet:true" in expect_error(
        db.bless_bench_anchor, blesser["token"], loud
    ), "unprovable scheduling refused"
    nometa = _seed_run(subject, None)
    assert "no query medians" in expect_error(
        db.bless_bench_anchor, blesser["token"], nometa
    ), "median-less runs refused"

    # Credit-poor but karma-rich: drain past the price, keep the floor.
    earner = db.register_agent("bless-earner")
    epid = db.create_post(earner["token"], "drain economics", "body")["post_id"]
    db.vote(other["token"], "post", epid, 1)
    with db._conn() as _c:
        have = _cr.balance_for(_c, earner["agent_id"])
        drain = have - cost_q + 2
        if drain > 0:
            _cr.spend(
                earner["agent_id"],
                drain,
                "admin_adjust",
                target_type="test",
                target_id=1,
                conn=_c,
            )
    assert "insufficient credits" in expect_error(
        db.bless_bench_anchor, earner["token"], run1
    ), "empty wallet refused after the floor passes"

    # Drifted trailing median: four drifted runs outweigh the four flat
    # seeds (median 12.5 vs 10 = +25% on all 3), so the tick skips.
    drifted = {"a": 15.0, "b": 30.0, "c": 45.0}
    for _ in range(4):
        _seed_run(subject, drifted)
    decision = db.bench_anchor_tick()
    assert decision.startswith("skip:") and "drifted" in decision, (
        f"drift blocks auto-bless ({decision})"
    )

    # Stale-but-stable anchor: age the bless rows, add a flat run, reconfirm.
    run3 = _seed_run(subject, FLAT)
    old_at = (
        (datetime.now(timezone.utc) - timedelta(days=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    with db._conn() as _c:
        _c.execute(
            "UPDATE events SET created_at = ? WHERE kind = ?",
            (old_at, events.EVT_BENCH_ANCHOR_BLESSED),
        )
    decision = db.bench_anchor_tick()
    assert decision.startswith("blessed: reconfirm"), (
        f"stale stable anchor reconfirms ({decision})"
    )
    assert events.bench_anchor_for()["anchor_run_event_id"] == run3, (
        "reconfirm points at the newest qualifying run"
    )
    decision = db.bench_anchor_tick()
    assert decision.startswith("skip: anchor fresh"), (
        f"fresh anchor left alone ({decision})"
    )

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_bless: all assertions passed")


if __name__ == "__main__":
    main()
