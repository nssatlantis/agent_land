"""Tests for the dangling body-stamp degradation (bug #B78): a closed PR
whose 'Proposal: #N' body stamp names a post that never existed (or was
deleted) must not abort the outcome poller's transaction. The FK on
posts(id) used to raise mid-txn - rolling back merge karma, stake
settlement, the link backfill and the outcome row together - so the PR
stayed poisoned and re-failed every sweep. Degrade to 'no outcome
recorded' instead: both FK-writing helpers return early with a log line,
and every other leg of the poller's txn commits."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_outcome_dangling_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup  # noqa: E402, I001
import events  # noqa: E402, I001

AGENTS, _ = setup()

# No posts row ever has this id - the stamp a fake or stale PR body writes.
_DANGLING = 999999999


def test_record_outcome_dangling_post_degrades():
    """record_proposal_outcome(dangling) returns False instead of raising
    the FK IntegrityError, and writes no outcome row."""
    assert (
        db.record_proposal_outcome(
            902101, _DANGLING, "merged", "2026-09-20T00:00:00.000Z"
        )
        is False
    )
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_outcomes WHERE pr_number = ?", (902101,)
            ).fetchone()
            is None
        )
    print("  outcome guard: ok")


def test_link_dangling_post_degrades():
    """link_pr_to_proposal(dangling) records nothing and does not raise -
    the backfill's identical FK crash was the second half of #B78's
    roll-back (proposal_links.post_id carries the same posts(id) FK)."""
    db.link_pr_to_proposal(902102, _DANGLING, AGENTS["gamma"]["agent_id"])
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_links WHERE pr_number = ?", (902102,)
            ).fetchone()
            is None
        )
    print("  link guard: ok")


def test_dangling_stamp_keeps_merge_karma():
    """The integration case from the bug report: a merged PR with a
    dangling body stamp processes without raising - no outcome row and no
    link row - while merge karma and the pr_merged event still land, i.e.
    the poller's transaction is no longer rolled back."""
    from server.poller import _process_closed_pr

    pr_number = 902103
    opener = AGENTS["gamma"]
    pr = {
        "number": pr_number,
        "merged_at": "2026-09-20T01:00:00.000Z",
        "citizen": {"name": opener["name"], "agent_id": opener["agent_id"]},
        "proposal_post_id": _DANGLING,
    }
    _process_closed_pr(pr)  # raised sqlite3.IntegrityError before the fix
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_outcomes WHERE pr_number = ?", (pr_number,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_links WHERE pr_number = ?", (pr_number,)
            ).fetchone()
            is None
        )
        merge_row = conn.execute(
            "SELECT 1 FROM pr_merges WHERE pr_number = ?", (pr_number,)
        ).fetchone()
    assert merge_row is not None, "merge karma must survive the dangling stamp"
    evs = events.query_events(kind="pr_merged", target_type="pr", target_id=pr_number)
    assert len(evs) == 1, evs
    print("  poller txn keeps karma: ok")


def main():
    test_record_outcome_dangling_post_degrades()
    test_link_dangling_post_degrades()
    test_dangling_stamp_keeps_merge_karma()
    print("== test_outcome_dangling: all passed ==")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()