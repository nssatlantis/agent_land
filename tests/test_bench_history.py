"""Tests for benchmark trend reads (db.bench_history, single-anchor #367, 5/5).

Overview by default (every query's latest + trailing + base + drift with
anchor identity and label); query= for one query's full newest-first
series; native_only=False includes branch runs. Public read, no token.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_history_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001
import events  # noqa: E402, I001


def _seed_run(subject, meds, extra=None):
    detail = {
        "checks": "db_benchmark",
        "mode": "native",
        "ok": True,
        "exit_code": 0,
        "duration_seconds": 20.0,
        "head_sha": "beef1234567890abcdef1234567890abcdef1",
        "summary": {
            "bench": "db_benchmark",
            "regressions": 0,
            "bench_errors": [],
            "timings_median_ms": meds,
        },
    }
    detail.update(extra or {})
    events.log_event(
        events.EVT_CI_DB_BENCH_RUN,
        actor_agent_id=subject["agent_id"],
        actor_name=subject["name"],
        detail=detail,
    )


def main():
    agents, _ = setup()
    subject = db.register_agent("history-subject")

    # Empty ledger: overview with no anchor, no queries.
    out = db.bench_history()
    assert out["anchor"] is None, "no anchor before any bless"
    assert out["queries"] == {}, "no queries before any runs"

    old = {"a": 10.0, "b": 20.0}
    new = {"a": 12.0, "b": 20.0}
    _seed_run(subject, old)
    _seed_run(subject, new)
    rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=1)
    events.log_event(
        events.EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=subject["agent_id"],
        actor_name=subject["name"],
        detail={
            "anchor_run_event_id": rows[0]["id"],
            "blessed_by": subject["agent_id"],
            "reason": "manual",
            "medians": dict(new),
        },
    )

    out = db.bench_history()
    assert out["label"] == "vs anchor", "anchor label when blessed"
    assert out["anchor"]["reason"] == "manual", "anchor identity rides along"
    assert out["anchor"]["aging"] is False, "flat fresh anchor not aging"
    assert out["window_runs"] == 2, "window counts bench runs"
    qa = out["queries"]["a"]
    assert qa["latest"] == 12.0, "latest is newest-first"
    assert qa["trailing"] == 11.0, "trailing medians the window"
    assert qa["base"] == 12.0, "base is the anchor"
    assert qa["drift_pct"] == -8, "drift trails vs anchor"
    assert out["queries"]["b"]["drift_pct"] == 0, "flat query reads zero"

    one = db.bench_history(query="a")
    assert one["query"] == "a", "query echoed"
    assert one["series"] == [12.0, 10.0], "series newest-first"
    assert one["entry"]["trailing"] == 11.0, "entry matches overview"

    missing = db.bench_history(query="nope")
    assert missing["series"] == [], "unknown query reads empty"
    assert missing["entry"]["latest"] is None, "unknown entry is nulls"

    # Branch runs are excluded by default, included on request.
    _seed_run(subject, {"a": 99.0}, extra={"mode": "branch", "pr_number": 7})
    assert db.bench_history()["queries"]["a"]["latest"] == 12.0, (
        "branch excluded by default"
    )
    assert db.bench_history(native_only=False)["queries"]["a"]["latest"] == 99.0, (
        "branch included on request"
    )

    assert "query must be" in expect_error(db.bench_history, query="  "), (
        "blank query refused"
    )
    assert db.bench_history(limit=0)["window_runs"] == 1, "limit clamps to >=1"

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_history: all assertions passed")


if __name__ == "__main__":
    main()
