"""Tests for CI runner quota visibility (db._ci_usage).

my_profile / check_in / whoami carry `ci_usage` (per ci_* kind: used
today, cap, remaining, cooldown wait) so agents can plan rehearsals
instead of discovering limits by tripping the gate. The reader shares
its window math with server.ci_runner._gate, and one test pins their
agreement on both refusal paths.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_usage_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001
import events  # noqa: E402, I001


def _log(agent, kind):
    events.log_event(
        kind,
        actor_agent_id=agent["agent_id"],
        actor_name=agent["name"],
        detail={"checks": "tests", "mode": "local", "ok": True},
    )


def main():
    agents, _ = setup()
    assert config.CI_RUN_COOLDOWN_SECONDS > 0, "cooldown gate must be armed here"
    base_cap = config.CI_RUN_DAILY_CAP
    assert base_cap > 1, "cap must leave headroom here"

    # 1. fresh agent: zeros everywhere, caps at default.
    fresh = db.register_agent("ci-usage-fresh")
    usage = db.ci_usage_for(fresh["agent_id"])
    assert set(usage) == {
        "ci_run",
        "ci_branch_run",
        "ci_local_run",
        "ci_benchmark_run",
        "ci_db_bench_run",
    }, "all five gated kinds reported"
    for kind, st in usage.items():
        assert st == {
            "used_today": 0,
            "cap": base_cap,
            "remaining": base_cap,
            "cooldown_wait_s": 0,
        }, f"fresh {kind} is all zeros"

    # 2. one run: used counts, cooldown live.
    worker = db.register_agent("ci-usage-worker")
    _log(worker, events.EVT_CI_LOCAL_RUN)
    st = db.ci_kind_status(worker["agent_id"], "ci_local_run")
    assert st["used_today"] == 1, "today's run counted"
    assert st["remaining"] == base_cap - 1, "remaining decremented"
    assert st["cooldown_wait_s"] > 0, "cooldown live right after a run"
    other = db.ci_kind_status(worker["agent_id"], "ci_branch_run")
    assert other["used_today"] == 0 and other["cooldown_wait_s"] == 0, (
        "kinds are independent buckets"
    )

    # 3. reader/gate agreement on the cooldown path.
    from server.ci_runner._runs import _gate

    try:
        _gate("ci_local_run", worker["agent_id"])
    except db.ForumError as exc:
        assert "cooldown" in str(exc), f"cooldown message kept: {exc}"
    else:
        raise AssertionError("_gate must refuse inside the cooldown window")

    # 4. reader/gate agreement on the cap path (cap patched to 1,
    # cooldown neutralized so the cap refusal is what fires).
    old_cap = config.CI_RUN_DAILY_CAP
    old_cd = config.CI_RUN_COOLDOWN_SECONDS
    config.CI_RUN_DAILY_CAP = 1
    config.CI_RUN_COOLDOWN_SECONDS = 0
    try:
        capped = db.ci_kind_status(worker["agent_id"], "ci_local_run")
        assert capped["used_today"] == 1 and capped["remaining"] == 0, (
            "cap of 1 with 1 run reads full"
        )
        try:
            _gate("ci_local_run", worker["agent_id"])
        except db.ForumError as exc:
            assert "cap reached (1 per day)" in str(exc), f"cap message kept: {exc}"
        else:
            raise AssertionError("_gate must refuse at the daily cap")
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
        config.CI_RUN_COOLDOWN_SECONDS = old_cd

    # 5. aged rows don't count: move the run to yesterday.
    with db._conn() as conn:
        conn.execute(
            "UPDATE events SET created_at = '2000-01-01T00:00:00.000Z'"
            " WHERE actor_agent_id = ?",
            (worker["agent_id"],),
        )
    aged = db.ci_kind_status(worker["agent_id"], "ci_local_run")
    assert aged["used_today"] == 0 and aged["cooldown_wait_s"] == 0, (
        "yesterday's rows are outside both windows"
    )

    # 6. all three status surfaces carry the identical readout.
    mp = db.my_profile(worker["token"])["ci_usage"]
    ci = db.check_in(worker["token"])["ci_usage"]
    wo = db.whoami(worker["token"])["ci_usage"]
    assert mp == ci == wo, "my_profile/check_in/whoami agree on ci_usage"
    assert set(mp) == set(usage), "same five kinds on the wire"

    print("test_ci_usage: all ok")


if __name__ == "__main__":
    main()
