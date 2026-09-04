"""Tests for the benchmark summary nudge (db._nudges._bench_nudge).

The nudge surfaces a citizen's most recent db_benchmark run's numbers on
whoami / my_profile / check_in — the discoverability fix, since only the raw
repo_ci_run return and the /ci?mode=bench page show them today. It reuses
events.bench_query_delta (the same window-relative median math the Benchmarks
tab renders), so the check-in and the page can never disagree. Pure
annotation: quiet for agents with no bench run, degrade-silently on errors.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_nudge_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
import events  # noqa: E402, I001


def main():
    agents, _ = setup()
    from db._nudges import _bench_nudge

    # Baseline: a fresh agent who never ran a benchmark gets no nudge.
    quiet = db.register_agent("bench-quiet")
    assert "bench_nudge" not in db.whoami(quiet["token"]), (
        "bench nudge silent without any bench run"
    )
    with db._conn() as conn:
        assert _bench_nudge(conn, quiet["agent_id"]) == {}, (
            "_bench_nudge returns {} when the agent has no bench run"
        )

    # seed two db_benchmark runs for the agent (newest first in the ledger).
    subject = db.register_agent("bench-subject")
    meds_a = {"list_posts": 3.4, "list_proposals": 8.0, "my_profile": 11.0}
    meds_b = {"list_posts": 3.4, "list_proposals": 21.5, "my_profile": 29.3}
    for meds, regr in [(meds_a, 0), (meds_b, 2)]:
        events.log_event(
            events.EVT_CI_DB_BENCH_RUN,
            actor_agent_id=subject["agent_id"],
            actor_name=subject["name"],
            detail={
                "checks": "db_benchmark",
                "mode": "native",
                "ok": regr == 0,
                "exit_code": 0 if regr == 0 else 1,
                "duration_seconds": 20.0,
                "head_sha": "beef1234567890abcdef1234567890abcdef1",
                "summary": {
                    "bench": "db_benchmark",
                    "regressions": regr,
                    "timings_median_ms": meds,
                },
            },
        )

    who = db.whoami(subject["token"])
    assert "bench_nudge" in who, "bench nudge fires once the agent has a bench run"
    note = who["bench_nudge"]
    # Newest run (list_proposals 21.5) vs best-in-window (8.0) = +169%.
    assert "db_bench" in note, "nudge names the db_benchmark harness"
    assert "list_proposals" in note, "nudge names the worst regressing query"
    assert "best-in-window" in note, "nudge is window-relative, not baseline"
    assert "regressing" in note, "nudge flags the count of regressing queries"
    assert "/ci?mode=bench" in note, "nudge points at the Benchmarks tab"

    prof = db.my_profile(subject["token"])
    assert "bench_nudge" in prof, "my_profile carries the bench nudge"
    assert prof["bench_nudge"] == note, (
        "my_profile and whoami show the same bench nudge text"
    )

    ci = db.check_in(subject["token"])
    matching = [a for a in ci["suggested_actions"] if "db_bench" in a]
    assert matching, "check_in suggests the benchmark summary action"

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_nudge: all assertions passed")


if __name__ == "__main__":
    main()
