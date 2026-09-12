"""Tests for the anchor heartbeat (db.bench_heartbeat_due /
db.bless_heartbeat_run, heartbeat program #381).

No manual blessing: freshness comes from execution. The hourly tick
dispatches a fresh quiet native bench once HEARTBEAT_DAYS pass since the
last bless (any source) and blesses it when it qualifies with small
drift; newest bless wins. Holds never raise - they return hold strings
so the tick can audit them and (on the store path) refund the buyer.
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

from tests._setup import db, setup  # noqa: E402, I001
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

    # Empty ledger: nothing blessed, heartbeat due (bootstrap).
    assert events.bench_anchor_for() is None, "no anchor on a fresh ledger"
    due, why = db.bench_heartbeat_due()
    assert due and "bootstrap" in why, f"bootstrap due with no anchor ({why})"

    subject = db.register_agent("bless-subject")
    run1 = _seed_run(subject, FLAT)
    due, why = db.bench_heartbeat_due()
    assert due and "bootstrap" in why, "runs never reset the timer, blesses do"

    out = db.bless_heartbeat_run(run1, reason="heartbeat", blessed_by=None)
    assert out == f"blessed: heartbeat run ev{run1}", f"heartbeat blesses ({out})"
    anchor = events.bench_anchor_for()
    assert anchor["anchor_run_event_id"] == run1, "anchor points at the run"
    assert anchor["reason"] == "heartbeat", "heartbeat reason recorded"

    # Fresh anchor: the tick stands down.
    due, why = db.bench_heartbeat_due()
    assert not due and "fresh" in why, f"fresh anchor not due ({why})"

    # Newest bless wins; the store path records its buyer.
    buyer = db.register_agent("bless-buyer")
    run2 = _seed_run(subject, FLAT)
    out = db.bless_heartbeat_run(run2, reason="store", blessed_by=buyer["agent_id"])
    assert out == f"blessed: store run ev{run2}", f"store blesses ({out})"
    anchor = events.bench_anchor_for()
    assert anchor["anchor_run_event_id"] == run2, "newest bless wins"
    assert anchor["blessed_by"] == buyer["agent_id"], "buyer recorded"

    # Holds return strings, cheapest checks first - nothing raises.
    assert "positive integer" in db.bless_heartbeat_run(
        True, reason="heartbeat", blessed_by=None
    ), "bool event id held before any read"
    assert "no benchmark run" in db.bless_heartbeat_run(
        999999999, reason="heartbeat", blessed_by=None
    ), "unknown run held"
    branch = _seed_run(subject, FLAT, mode="branch", extra={"pr_number": 100})
    assert "bare origin/main" in db.bless_heartbeat_run(
        branch, reason="heartbeat", blessed_by=None
    ), "branch runs held"
    contended = _seed_run(
        subject, FLAT, load={"quiet": True, "contended": True, "quiet_wait_s": 0.0}
    )
    assert "contended" in db.bless_heartbeat_run(
        contended, reason="heartbeat", blessed_by=None
    ), "contended runs held"
    red = _seed_run(subject, FLAT, ok=False)
    assert "green" in db.bless_heartbeat_run(
        red, reason="heartbeat", blessed_by=None
    ), "red runs held"
    loud = _seed_run(subject, FLAT, load=None)
    assert "quiet:true" in db.bless_heartbeat_run(
        loud, reason="heartbeat", blessed_by=None
    ), "unprovable scheduling held"
    # Malformed load attestation fails closed at the unit level (a ledger
    # seed here would pollute trailing medians for the drift tests below).
    from db._bench_anchor import _candidate_problem

    assert _candidate_problem({"bench_load": "nope"}) == (
        "anchor runs must carry a quiet/uncontended load attestation"
    ), "truthy non-dict load held, never crashes"
    nometa = _seed_run(subject, None)
    assert "no query medians" in db.bless_heartbeat_run(
        nometa, reason="heartbeat", blessed_by=None
    ), "median-less runs held"

    # Drifted trailing median: six drifted runs balance the six flat seeds
    # (median 12.5 vs 10 = +25% on all 3), so the bless holds.
    drifted = {"a": 15.0, "b": 30.0, "c": 45.0}
    for _ in range(6):
        _seed_run(subject, drifted)
    run3 = _seed_run(subject, FLAT)
    out = db.bless_heartbeat_run(run3, reason="heartbeat", blessed_by=None)
    assert out.startswith("held:") and "drifted" in out, (
        f"drift blocks the bless ({out})"
    )

    # Lone drift carries its prior anchor median through instead of
    # absorbing the red (Pickle's finding, heartbeat form): age the anchor
    # so the timer is due, then bless the lone-drifted run.
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
    due, why = db.bench_heartbeat_due()
    assert due and "old" in why, f"aged anchor due again ({why})"
    run4 = _seed_run(subject, {"a": 15.0, "b": 20.0, "c": 30.0})
    out = db.bless_heartbeat_run(run4, reason="heartbeat", blessed_by=None)
    assert out.startswith("blessed:"), f"lone drift blesses ({out})"
    carried = events.bench_anchor_for()
    assert carried["anchor_run_event_id"] == run4, "bless points at the run"
    assert carried["medians"]["a"] == 10.0, "drifted query keeps prior median"
    assert carried["medians"]["b"] == 20.0, "flat queries take fresh medians"

    # Unblessable candidate: named in the hold string, timer untouched.
    _seed_run(
        subject, FLAT, load={"quiet": True, "contended": True, "quiet_wait_s": 0.0}
    )
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=1)
    out = db.bless_heartbeat_run(rows[0]["id"], reason="heartbeat", blessed_by=None)
    assert "unblessable" in out, f"unblessable candidate named ({out})"

    # Unreadable anchor timestamp: due loudly, never guess an age.
    with db._conn() as _c:
        _c.execute(
            "UPDATE events SET created_at = 'garbage' WHERE kind = ?",
            (events.EVT_BENCH_ANCHOR_BLESSED,),
        )
    due, why = db.bench_heartbeat_due()
    assert due and "unreadable" in why, f"corrupt anchor timestamp due ({why})"

    # NaN on either side of the drift math never crashes: skipped, not flagged.
    nan_rows = [
        {
            "detail": {
                "mode": "native",
                "summary": {"timings_median_ms": {"a": float("nan")}},
            }
        },
    ]
    assert events.bench_anchor_drifted({"medians": {"a": 10.0}}, nan_rows) == [], (
        "NaN native medians skipped"
    )
    flat_rows = [
        {
            "detail": {
                "mode": "native",
                "summary": {"timings_median_ms": {"a": 10.0}},
            }
        },
    ]
    assert (
        events.bench_anchor_drifted({"medians": {"a": float("nan")}}, flat_rows) == []
    ), "NaN anchor medians skipped"

    # Paid judgment overrides drift (#381 restoration): top up a solid
    # drifted trailing window (the flat seeds above outnumber the earlier
    # drifted six), then the same drifted state blesses on reason="store"
    # with the overridden queries ridden loud — while the free path still
    # holds on the identical state.
    for _ in range(6):
        _seed_run(subject, drifted)
    run5 = _seed_run(subject, FLAT)
    out = db.bless_heartbeat_run(run5, reason="store", blessed_by=buyer["agent_id"])
    assert out.startswith("blessed:") and "paid judgment" in out, (
        f"store overrides drift ({out})"
    )
    # The unreadable-timestamp section above poisoned bless-row ordering
    # ('garbage' sorts above real timestamps), so find run5's bless by its
    # recorded run id instead of newest-first.
    brows = events.query_events(kind=events.EVT_BENCH_ANCHOR_BLESSED, limit=50)
    mine = [
        b for b in brows if (b.get("detail") or {}).get("anchor_run_event_id") == run5
    ]
    assert mine, "override bless recorded"
    det = mine[0].get("detail") or {}
    assert sorted(det.get("drift_override") or []) == ["a", "b", "c"], (
        f"drift ridden loud ({det.get('drift_override')})"
    )
    assert det["medians"]["a"] == 10.0, "drifted queries keep prior medians"
    run6 = _seed_run(subject, FLAT)
    out = db.bless_heartbeat_run(run6, reason="heartbeat", blessed_by=None)
    assert out.startswith("held:") and "drifted" in out, (
        f"heartbeat still holds through drift ({out})"
    )

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_bless: all assertions passed")


if __name__ == "__main__":
    main()
