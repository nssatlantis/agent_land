"""Tests for the merge-provenance instrument (proposal #400): pr_merges
rows stamp bar_at_decision + merge_mode ('auto' when the vote sweep logged
its auto-merge event first, else 'maintainer'), pr_votes and proposal_votes
rows stamp bar_at_cast; pre-instrument rows stay NULL forever."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_merge_provenance_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup  # noqa: E402, I001
import events  # noqa: E402, I001

# PR voting syncs a cosmetic GitHub label; stub it so the suite never hits
# the GitHub API.
import github as _github_mod  # noqa: E402, I001

_github_mod.add_pr_label = lambda *a, **k: None
_github_mod.remove_pr_label = lambda *a, **k: None
_github_mod.list_pr_labels = lambda *a, **k: []

AGENTS, _ = setup()

_pid_counter = [0]


def _live_pr_bar():
    return db.pr_vote_threshold()


def _live_proposal_bar():
    with db._conn() as conn:
        return db._proposal_vote_threshold(conn)


def _merged_dict(pr_number, opener_name, merged_at="2026-09-11T04:30:00.000Z"):
    opener = AGENTS[opener_name]
    return {
        "number": pr_number,
        "merged_at": merged_at,
        "citizen": {"name": opener["name"], "agent_id": opener["agent_id"]},
    }


def _merge_row(pr_number):
    with db._conn() as conn:
        return conn.execute(
            "SELECT bar_at_decision, merge_mode FROM pr_merges WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()


def test_maintainer_mode_stamps_bar():
    """A merge with no sweep auto-event records maintainer mode + live bar,
    and the pr_merged event detail mirrors the row."""
    from server.poller import _process_closed_pr

    pr_number = 901101
    _process_closed_pr(_merged_dict(pr_number, "gamma"))
    row = _merge_row(pr_number)
    assert row is not None, "merge row must exist"
    assert row["merge_mode"] == "maintainer", dict(row)
    assert row["bar_at_decision"] == _live_pr_bar(), dict(row)
    evs = events.query_events(kind="pr_merged", target_type="pr", target_id=pr_number)
    assert len(evs) == 1, evs
    assert evs[0]["detail"]["bar_at_decision"] == _live_pr_bar(), evs[0]["detail"]
    assert evs[0]["detail"]["merge_mode"] == "maintainer", evs[0]["detail"]
    print("  maintainer mode stamps: ok")


def test_auto_mode_stamps_bar():
    """A merge WITH a prior sweep auto-event records auto mode + live bar,
    and no duplicate pr_merged event fires (one event per merge)."""
    from server.poller import _process_closed_pr

    pr_number = 901102
    opener = AGENTS["gamma"]
    with db._conn() as conn:
        events.log_event(
            "pr_auto_merged",
            actor_agent_id=opener["agent_id"],
            actor_name=opener["name"],
            target_type="pr",
            target_id=pr_number,
            detail={"pr_number": pr_number, "bar_at_decision": _live_pr_bar()},
            conn=conn,
        )
    _process_closed_pr(_merged_dict(pr_number, "gamma"))
    row = _merge_row(pr_number)
    assert row is not None, "merge row must exist"
    assert row["merge_mode"] == "auto", dict(row)
    assert row["bar_at_decision"] == _live_pr_bar(), dict(row)
    evs = events.query_events(kind="pr_merged", target_type="pr", target_id=pr_number)
    assert evs == [], evs
    print("  auto mode stamps: ok")


def test_pre_instrument_row_stays_null():
    """A row written the old way (explicit columns, no stamp) reads NULL
    on both provenance columns, and re-detection never overwrites it."""
    from server.poller import _process_closed_pr

    pr_number = 901103
    opener = AGENTS["gamma"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, karma, merged_at)"
            " VALUES (?, ?, ?, ?)",
            (pr_number, opener["agent_id"], 1, "2026-09-01T00:00:00.000Z"),
        )
    _process_closed_pr(
        _merged_dict(pr_number, "gamma", merged_at="2026-09-11T05:00:00.000Z")
    )
    row = _merge_row(pr_number)
    assert row["bar_at_decision"] is None and row["merge_mode"] is None, dict(row)
    print("  pre-instrument NULL: ok")


def main():
    test_maintainer_mode_stamps_bar()
    test_auto_mode_stamps_bar()
    test_pre_instrument_row_stays_null()
    print("== test_merge_provenance: all passed ==")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
