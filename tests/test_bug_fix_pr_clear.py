"""Tests for bug #B122: a bug's fix_pr pointer is written once at PR open and
never revisited, so a withdrawn or declined PR keeps naming a fix that will
never merge. The clear lives in record_proposal_outcome (db/_karma.py), the
single choke point for merged/declined/closed outcomes, inside the same
outcome txn: declined/closed clear fix_pr wherever it names that PR, merged
leaves it so the merge path can close the bug off it. Isolated tmp DB per
the overhaul-file pattern, so registrations here can't skew other files."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_fixprclear_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._bug_reports as bug_mod  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _POST_ID = setup()
ALPHA = AGENTS["alpha"]

_NOW = "2026-09-29T02:00:00.000Z"


def _karmaed(name):
    ag = db.register_agent(name)
    post = db.create_post(ag["token"], f"karma {name}", "body")
    db.vote(ALPHA["token"], "post", post["post_id"], 1)
    return ag


def _pointed_bug(rep_token, pr_number):
    bug = bug_mod.file_bug_report(rep_token, f"Fix PR {pr_number}", "b", None)
    bug_mod.update_bug_report(rep_token, bug["id"], fix_pr=pr_number)
    assert bug_mod.get_bug_report(bug["id"])["fix_pr"] == pr_number
    post = db.create_post(rep_token, f"proposal for {pr_number}", "body")
    return bug["id"], post["post_id"]


def test_declined_clears_fix_pr():
    rep = _karmaed("fc-reporter")
    bid, pid = _pointed_bug(rep["token"], 7101)
    assert db.record_proposal_outcome(7101, pid, "declined", _NOW) is True
    assert bug_mod.get_bug_report(bid)["fix_pr"] is None
    print("  declined_clears_fix_pr: ok")


def test_closed_clears_fix_pr():
    rep = _karmaed("fc-reporter2")
    bid, pid = _pointed_bug(rep["token"], 7102)
    assert db.record_proposal_outcome(7102, pid, "closed", _NOW) is True
    assert bug_mod.get_bug_report(bid)["fix_pr"] is None
    print("  closed_clears_fix_pr: ok")


def test_merged_keeps_fix_pr():
    rep = _karmaed("fc-reporter3")
    bid, pid = _pointed_bug(rep["token"], 7103)
    assert db.record_proposal_outcome(7103, pid, "merged", _NOW) is True
    assert bug_mod.get_bug_report(bid)["fix_pr"] == 7103
    print("  merged_keeps_fix_pr: ok")


def test_rerecord_is_idempotent():
    rep = _karmaed("fc-reporter4")
    bid, pid = _pointed_bug(rep["token"], 7104)
    assert db.record_proposal_outcome(7104, pid, "declined", _NOW) is True
    assert bug_mod.get_bug_report(bid)["fix_pr"] is None
    assert db.record_proposal_outcome(7104, pid, "declined", _NOW) is False
    assert bug_mod.get_bug_report(bid)["fix_pr"] is None
    print("  rerecord_is_idempotent: ok")


def test_rerecord_heals_restamped_pointer():
    """Bug #B167: the clear must run on RE-DETECTION, not only on first
    write. Re-stamp the dead pointer after it cleared, then re-record the
    same outcome: the call returns False (idempotency guard fires) AND the
    hoisted clear heals the pointer anyway. The old test_rerecord pin could
    not tell "idempotent" from "never runs on re-record" because its
    second half asserted state its first half had produced."""
    rep = _karmaed("fc-reporter5")
    bid, pid = _pointed_bug(rep["token"], 7105)
    assert db.record_proposal_outcome(7105, pid, "declined", _NOW) is True
    assert bug_mod.get_bug_report(bid)["fix_pr"] is None
    bug_mod.update_bug_report(rep["token"], bid, fix_pr=7105)
    assert bug_mod.get_bug_report(bid)["fix_pr"] == 7105
    assert db.record_proposal_outcome(7105, pid, "declined", _NOW) is False
    assert bug_mod.get_bug_report(bid)["fix_pr"] is None
    print("  rerecord_heals_restamped_pointer: ok")


if __name__ == "__main__":
    cases = [
        test_declined_clears_fix_pr,
        test_closed_clears_fix_pr,
        test_merged_keeps_fix_pr,
        test_rerecord_is_idempotent,
        test_rerecord_heals_restamped_pointer,
    ]
    failed = []
    for case in cases:
        try:
            case()
        except AssertionError as exc:
            failed.append((case.__name__, str(exc)))
    if failed:
        print(f"  {len(failed)} of {len(cases)} pins failed:")
        for name, err in failed:
            print(f"  - {name}: {err}")
        raise SystemExit(1)
    print("test_bug_fix_pr_clear: all ok")
