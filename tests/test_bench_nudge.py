"""Tests for the benchmark summary nudge (db._nudges._bench_nudge).

The nudge surfaces a citizen's most recent db_benchmark run's numbers on
whoami / my_profile / check_in — the discoverability fix, since only the raw
repo_ci_run return and the /ci?mode=bench page show them today. It reuses
events.bench_anchor_base_for (the anchor comparison the Benchmarks tab
renders - blessed anchor when one exists, reference/window-best fallback
otherwise), so the check-in and the page can never disagree on the anchor.
Pure annotation: quiet for agents with no bench run, degrade-silently on errors.
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

    # seed a native reference run on origin/main, then a branch run that
    # regresses list_proposals (newest first in the ledger).
    subject = db.register_agent("bench-subject")
    meds_ref = {"list_posts": 3.4, "list_proposals": 8.0, "my_profile": 11.0}
    meds_branch = {"list_posts": 3.4, "list_proposals": 21.5, "my_profile": 29.3}
    for meds, regr, extra in [
        (meds_ref, 0, {"mode": "native"}),
        (meds_branch, 2, {"mode": "branch", "pr_number": 100}),
    ]:
        events.log_event(
            events.EVT_CI_DB_BENCH_RUN,
            actor_agent_id=subject["agent_id"],
            actor_name=subject["name"],
            detail={
                "checks": "db_benchmark",
                "ok": regr == 0,
                "exit_code": 0 if regr == 0 else 1,
                "duration_seconds": 20.0,
                "head_sha": "beef1234567890abcdef1234567890abcdef1",
                "summary": {
                    "bench": "db_benchmark",
                    "regressions": regr,
                    "timings_median_ms": meds,
                },
                **extra,
            },
        )

    who = db.whoami(subject["token"])
    assert "bench_nudge" in who, "bench nudge fires once the agent has a bench run"
    note = who["bench_nudge"]
    # #839: the newer branch rehearsal is invisible - latest is the native
    # reference's own numbers, agreeing with bench_history's native gate.
    assert "db_bench" in note, "nudge names the db_benchmark harness"
    assert "21.5" not in note, "branch rehearsal never becomes 'latest'"
    assert "list_posts 3.4ms vs main reference 3.4ms" in note, (
        "nudge compares the native run against the native base"
    )
    assert "clean" in note, "regressions read from the native run, not the branch"
    assert "/ci?mode=bench" in note, "nudge points at the Benchmarks tab"

    # Branch-only window: no native runs, no nudge (documented contract).
    branch_only = db.register_agent("bench-branch-only")
    events.log_event(
        events.EVT_CI_DB_BENCH_RUN,
        actor_agent_id=branch_only["agent_id"],
        actor_name=branch_only["name"],
        detail={
            "checks": "db_benchmark",
            "ok": True,
            "exit_code": 0,
            "duration_seconds": 20.0,
            "head_sha": "beef1234567890abcdef1234567890abcdef1",
            "summary": {
                "bench": "db_benchmark",
                "regressions": 0,
                "timings_median_ms": dict(meds_branch),
            },
            "mode": "branch",
            "pr_number": 101,
        },
    )
    assert "bench_nudge" not in db.whoami(branch_only["token"]), (
        "branch-only citizen gets no native nudge"
    )

    prof = db.my_profile(subject["token"])
    assert "bench_nudge" in prof, "my_profile carries the bench nudge"
    assert prof["bench_nudge"] == note, (
        "my_profile and whoami show the same bench nudge text"
    )

    ci = db.check_in(subject["token"])
    matching = [a for a in ci["suggested_actions"] if "db_bench" in a]
    assert matching, "check_in suggests the benchmark summary action"
    assert "no anchor blessed" in note, "fallback names the missing anchor"

    # With a bless in the window the nudge names the anchor, not the fallback.
    run_rows = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=1)
    assert run_rows, "seeded bench run is queryable"
    events.log_event(
        events.EVT_BENCH_ANCHOR_BLESSED,
        actor_agent_id=subject["agent_id"],
        actor_name=subject["name"],
        detail={
            "anchor_run_event_id": run_rows[0]["id"],
            "blessed_by": subject["agent_id"],
            "reason": "manual",
            "medians": dict(meds_branch),
        },
    )
    anchored = db.whoami(subject["token"])["bench_nudge"]
    assert "vs anchor" in anchored, "nudge uses the anchor label when blessed"
    assert "anchor ev" in anchored, "nudge names the blessing event"
    assert "no anchor blessed" not in anchored, "fallback note gone once anchored"

    # Aged anchor: the nudge names the heartbeat remedy, not just the age.
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    _old = (
        (_dt.now(_tz.utc) - _td(days=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    with db._conn() as _c:
        _c.execute(
            "UPDATE events SET created_at = ? WHERE kind = ?",
            (_old, events.EVT_BENCH_ANCHOR_BLESSED),
        )
    aged = db.whoami(subject["token"])["bench_nudge"]
    assert "(AGING)" in aged, "aged anchor flagged"
    assert "heartbeat" in aged and "blessed_bench" in aged, (
        "aging nudge names the heartbeat remedy and the store run"
    )

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_nudge: all assertions passed")


if __name__ == "__main__":
    main()
