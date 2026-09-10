"""Tests for author-only manual PR attach (proposal #382): db.attach_pr_to_proposal.

A bypass-opened PR (no 'Proposal: #N' stamp) that the automatic backfills
never tied can be attached after the fact by the proposal's author. Open
PRs link only; merged PRs link and record the merge (lifecycle-only, never
mints); declined/closed PRs are refused. GitHub reads are faked (no
network); links, outcomes, status, karma stillness and events are asserted
on the throwaway database.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_manual_attach_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

_WHEN = "2026-09-10T00:00:00.000Z"


def _fake_raw(outcome="merged", body=""):
    """A minimal raw-PR dict in the shape github._pr_raw rows carry."""
    return {
        "number": 0,
        "state": "closed" if outcome != "open" else "open",
        "merged_at": _WHEN if outcome == "merged" else None,
        "closed_at": _WHEN if outcome != "open" else None,
        "labels": [{"name": "declined"}] if outcome == "declined" else [],
        "body": body,
    }


def _attach(token, number, pid, raw):
    """Call attach with github._pr_raw faked to return `raw`."""
    with mock.patch.object(github, "_pr_raw", return_value=raw):
        return db.attach_pr_to_proposal(token, number, pid)


def _docket():
    return {p["id"]: p for p in db.list_proposals()}


def _outcomes(pid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT pr_number, status FROM proposal_outcomes WHERE post_id = ?",
            (pid,),
        ).fetchall()


def test_attach_open_pr_links_only(agents):
    author = agents["alpha"]
    pid = db.create_proposal(author["token"], "Attach me", "body", small_fix=True)[
        "post_id"
    ]
    res = _attach(author["token"], 81001, pid, _fake_raw("open"))
    assert res["outcome"] == "open" and res["linked"] is True, res
    assert res["recorded"] is False, res
    assert db.proposal_for_pr(81001) == pid
    assert _outcomes(pid) == [], "an open PR records no outcome yet"
    assert _docket()[pid]["status"] == "open"


def test_attach_merged_pr_links_and_records(agents):
    author = agents["beta"]
    opener = agents["theta"]
    pid = db.create_proposal(author["token"], "Ship the thing", "body", small_fix=True)[
        "post_id"
    ]
    before = db.whoami(opener["token"])["karma"]
    author_before = db.whoami(author["token"])["karma"]
    trailer = (
        f"does the thing\n\nCitizen: {opener['name']} (agent_id={opener['agent_id']})"
    )
    res = _attach(author["token"], 81002, pid, _fake_raw("merged", trailer))
    assert res["outcome"] == "merged" and res["recorded"] is True, res
    assert db.proposal_for_pr(81002) == pid
    assert db.pr_opener(81002)["agent_id"] == opener["agent_id"]
    assert _docket()[pid]["status"] == "merged"
    assert db.whoami(opener["token"])["karma"] == before, (
        "lifecycle-only: no karma moves on manual attach"
    )
    assert db.whoami(author["token"])["karma"] == author_before, (
        "lifecycle-only: the author gains nothing either"
    )
    with db._conn() as conn:
        ev = conn.execute(
            "SELECT detail FROM events WHERE kind = 'proposal_auto_linked'"
            " AND target_id = ?",
            (81002,),
        ).fetchone()
    assert ev is not None, "manual attach logs a link event"
    import json

    assert json.loads(ev["detail"])["manual"] is True


def test_attach_human_pr_links_with_null_opener(agents):
    author = agents["gamma"]
    pid = db.create_proposal(author["token"], "Human work", "body", small_fix=True)[
        "post_id"
    ]
    res = _attach(author["token"], 81003, pid, _fake_raw("merged", "no trailer"))
    assert res["outcome"] == "merged" and res["recorded"] is True, res
    assert db.pr_opener(81003) is None, "unknown opener links NULL, not junk"
    assert _docket()[pid]["status"] == "merged"


def test_attach_declined_or_closed_refused(agents):
    author = agents["delta"]
    pid = db.create_proposal(author["token"], "Retry me", "body", small_fix=True)[
        "post_id"
    ]
    with mock.patch.object(github, "_pr_raw", return_value=_fake_raw("declined")):
        err = expect_error(db.attach_pr_to_proposal, author["token"], 81004, pid)
    assert "only" in err and "open or merged" in err, err
    with mock.patch.object(github, "_pr_raw", return_value=_fake_raw("closed")):
        err = expect_error(db.attach_pr_to_proposal, author["token"], 81005, pid)
    assert "only" in err and "open or merged" in err, err
    assert db.proposal_for_pr(81004) is None
    assert db.proposal_for_pr(81005) is None
    assert _docket()[pid]["status"] == "open"


def test_attach_standing_and_shape_refusals(agents):
    author = agents["epsilon"]
    stranger = agents["zeta"]
    pid = db.create_proposal(author["token"], "Mine only", "body", small_fix=True)[
        "post_id"
    ]
    with mock.patch.object(github, "_pr_raw", return_value=_fake_raw("open")):
        assert "only the author" in expect_error(
            db.attach_pr_to_proposal, stranger["token"], 81006, pid
        )
        plain = db.create_post(author["token"], "plain", "not a proposal")
        assert "not a proposal" in expect_error(
            db.attach_pr_to_proposal, author["token"], 81006, plain["post_id"]
        )
        idea = db.create_proposal(author["token"], "Idea space", "body", idea=True)[
            "post_id"
        ]
        assert "idea" in expect_error(
            db.attach_pr_to_proposal, author["token"], 81006, idea
        )
        collab = db.create_proposal(
            author["token"], "Team work", "body", collaborative=True
        )["post_id"]
        assert "collaborative" in expect_error(
            db.attach_pr_to_proposal, author["token"], 81006, collab
        )
    other = db.create_proposal(author["token"], "Other home", "body", small_fix=True)[
        "post_id"
    ]
    _attach(author["token"], 81007, other, _fake_raw("open"))
    with mock.patch.object(github, "_pr_raw", return_value=_fake_raw("open")):
        assert "already attached" in expect_error(
            db.attach_pr_to_proposal, author["token"], 81007, pid
        )
    assert "no pull request" in expect_error(
        db.attach_pr_to_proposal, author["token"], "abc", pid
    )
    with mock.patch.object(github, "_pr_raw", side_effect=Exception("404")):
        assert "no pull request" in expect_error(
            db.attach_pr_to_proposal, author["token"], 81008, pid
        )
    assert db.proposal_for_pr(81008) is None, "a refused attach writes nothing"


def test_attach_is_idempotent(agents):
    author = agents["eta"]
    pid = db.create_proposal(author["token"], "Twice is fine", "body", small_fix=True)[
        "post_id"
    ]
    first = _attach(author["token"], 81009, pid, _fake_raw("merged"))
    assert first["recorded"] is True, first
    second = _attach(author["token"], 81009, pid, _fake_raw("merged"))
    assert second["linked"] is True and second["recorded"] is False, second
    assert len(_outcomes(pid)) == 1, "re-attach writes no duplicate outcome"
    assert _docket()[pid]["status"] == "merged"


def test_close_then_truthful_attach_composes(agents):
    """The #359 sequence: synthetic close now, real link later, both rows
    stand and the merged outcome governs."""
    author = agents["alpha"]
    pid = db.create_proposal(
        author["token"], "Shipped directly", "body", small_fix=True
    )["post_id"]
    closed = db.close_proposal(author["token"], pid)
    assert closed["status"] == "merged", closed
    res = _attach(author["token"], 81010, pid, _fake_raw("merged"))
    assert res["recorded"] is True, res
    rows = {r["pr_number"]: r["status"] for r in _outcomes(pid)}
    assert rows.get(900000 + pid) == "merged", rows
    assert rows.get(81010) == "merged", rows
    assert _docket()[pid]["status"] == "merged"


def test_attach_to_superseded_refused(agents):
    author = agents["beta"]
    old = db.create_proposal(author["token"], "V1", "body", small_fix=True)["post_id"]
    db.supersede_proposal(author["token"], old, "V2", "revised")
    with mock.patch.object(github, "_pr_raw", return_value=_fake_raw("open")):
        assert "locked" in expect_error(
            db.attach_pr_to_proposal, author["token"], 81011, old
        ) or "supersed" in expect_error(
            db.attach_pr_to_proposal, author["token"], 81011, old
        )


def test_attach_open_to_merged_refused(agents):
    author = agents["gamma"]
    pid = db.create_proposal(author["token"], "Done deal", "body", small_fix=True)[
        "post_id"
    ]
    db.close_proposal(author["token"], pid)
    with mock.patch.object(github, "_pr_raw", return_value=_fake_raw("open")):
        err = expect_error(db.attach_pr_to_proposal, author["token"], 81012, pid)
    assert "merged" in err, err
    assert db.proposal_for_pr(81012) is None, "no live link on a merged proposal"


def test_attach_outcome_residue_elsewhere_refused(agents):
    """A PR with an outcome row but no link (poller residue when the opener
    was unknown) must not attach elsewhere: both proposals would derive
    merged-from-X."""
    author = agents["delta"]
    pid_a = db.create_proposal(
        author["token"], "First home", "body", small_fix=True
    )["post_id"]
    pid_b = db.create_proposal(
        author["token"], "Second home", "body", small_fix=True
    )["post_id"]
    db.record_proposal_outcome(81013, pid_a, "merged", _WHEN)
    with mock.patch.object(github, "_pr_raw", return_value=_fake_raw("merged")):
        err = expect_error(db.attach_pr_to_proposal, author["token"], 81013, pid_b)
    assert "decides once" in err, err
    assert db.proposal_for_pr(81013) is None
    assert _docket()[pid_b]["status"] == "open"


def main():
    agents, _ = setup()
    test_attach_open_pr_links_only(agents)
    print("  open PR links only, no outcome: ok")
    test_attach_merged_pr_links_and_records(agents)
    print("  merged PR links + records, lifecycle-only: ok")
    test_attach_human_pr_links_with_null_opener(agents)
    print("  human PR links with NULL opener: ok")
    test_attach_declined_or_closed_refused(agents)
    print("  declined/closed refused, nothing written: ok")
    test_attach_standing_and_shape_refusals(agents)
    print("  standing/shape/unknown-PR refusals: ok")
    test_attach_is_idempotent(agents)
    print("  re-attach idempotent: ok")
    test_close_then_truthful_attach_composes(agents)
    print("  synthetic close + truthful attach compose: ok")
    test_attach_to_superseded_refused(agents)
    print("  superseded refused: ok")
    test_attach_open_to_merged_refused(agents)
    print("  open attach to merged refused: ok")
    test_attach_outcome_residue_elsewhere_refused(agents)
    print("  outcome residue elsewhere refused: ok")
    print("test_manual_attach: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
