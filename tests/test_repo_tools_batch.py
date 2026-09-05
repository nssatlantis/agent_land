"""Tests for the batch repo tools vote_on_prs and proposals_ready_to_merge.

vote_on_prs is the consolidated PR-voting tool: its single form
(pr_number + value) is vote_on_pr's drop-in successor, and its batch form
(votes=[...]) votes several PRs in one call, each with its own result/error
kept so one bad or proposal-held PR never blocks its siblings.
proposals_ready_to_merge lists approved proposals with no PR in flight, so
an author (or delegate) knows which branches to open next.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_repo_tools_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github as _github_mod  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

# Voting syncs a cosmetic GitHub label; stub it so the suite never hits the API.
_github_mod.add_pr_label = lambda *a, **k: None
_github_mod.remove_pr_label = lambda *a, **k: None
_github_mod.list_pr_labels = lambda *a, **k: []

AGENTS, _ = setup()

# Load the server package under a private name so server/ stays untouched;
# the tools we assert are re-exported there like the rest of the facade.
_ROOT = Path(__file__).resolve().parent.parent / "server" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "agentland_root_server_repo_tools", _ROOT
)
root_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_server)


_counter = [0]


def _link(proposal_id, pr_number, opener="alpha"):
    db.link_pr_to_proposal(pr_number, proposal_id, AGENTS[opener]["agent_id"])


def _small_fix(opener="alpha"):
    """A small-fix proposal with a linked PR - approved, so voting is open."""
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS[opener]["token"],
        f"Small fix {_counter[0]}",
        "Body",
        small_fix=True,
    )
    pid = prop["post_id"]
    pr = 9000 + _counter[0]
    _link(pid, pr, opener)
    return pid, pr


def _held_regular(opener="alpha"):
    """A regular proposal below threshold - its linked PR is proposal-held."""
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS[opener]["token"],
        f"Regular {_counter[0]}",
        "Body",
    )
    pid = prop["post_id"]
    pr = 9000 + _counter[0]
    _link(pid, pr, opener)
    return pid, pr


# ---- single mode (the vote_on_pr successor) --------------------------------


def test_vote_on_prs_single_happy():
    _pid, pr = _small_fix()
    token = AGENTS["beta"]["token"]
    out = root_server.vote_on_prs(token, pr_number=pr, value=1)
    assert out["pr_number"] == pr, out
    assert out["value"] == 1, out
    assert "results" not in out and "errors" not in out, out
    print("  vote_on_prs single happy: ok")


def test_vote_on_prs_single_hold_raises():
    _pid, held_pr = _held_regular()
    token = AGENTS["beta"]["token"]
    err = expect_error(root_server.vote_on_prs, token, pr_number=held_pr, value=1)
    assert "community vote" in err, err
    print("  vote_on_prs single proposal-hold raises: ok")


def test_vote_on_prs_single_missing_params():
    _pid, pr = _small_fix()
    token = AGENTS["beta"]["token"]
    err = expect_error(root_server.vote_on_prs, token, pr_number=pr)
    assert "pr_number and value" in err, err
    print("  vote_on_prs single missing value: ok")


def test_vote_on_prs_single_mixed_params():
    _pid, pr = _small_fix()
    token = AGENTS["beta"]["token"]
    err = expect_error(
        root_server.vote_on_prs,
        token,
        pr_number=pr,
        value=1,
        votes=[{"pr_number": pr, "value": 1}],
    )
    assert "not both" in err, err
    print("  vote_on_prs single+batch mixed rejected: ok")


# ---- batch mode ------------------------------------------------------------


def test_vote_on_prs_batch_validation():
    token = AGENTS["beta"]["token"]
    err = expect_error(root_server.vote_on_prs, token, votes="nope")
    assert "non-empty list" in err, err
    err = expect_error(root_server.vote_on_prs, token, votes=[])
    assert "non-empty list" in err, err
    too_many = [{"pr_number": i, "value": 1} for i in range(1, 7)]
    err = expect_error(root_server.vote_on_prs, token, votes=too_many)
    assert "at most" in err and str(config.PRS_BATCH_MAX) in err, err
    print("  vote_on_prs batch validation: ok")


def test_vote_on_prs_batch_happy():
    _pid1, pr1 = _small_fix()
    _pid2, pr2 = _small_fix()
    token = AGENTS["beta"]["token"]
    out = root_server.vote_on_prs(
        token,
        votes=[{"pr_number": pr1, "value": 1}, {"pr_number": pr2, "value": -1}],
    )
    assert out["errors"] == [], out
    assert len(out["results"]) == 2, out
    by_pr = {r["pr_number"]: r for r in out["results"]}
    assert by_pr[pr1]["value"] == 1, by_pr
    assert by_pr[pr2]["value"] == -1, by_pr
    print("  vote_on_prs batch two-vote happy path: ok")


def test_vote_on_prs_batch_proposal_hold_isolation():
    _held_pid, held_pr = _held_regular()
    _ok_pid, ok_pr = _small_fix()
    token = AGENTS["beta"]["token"]
    out = root_server.vote_on_prs(
        token,
        votes=[{"pr_number": held_pr, "value": 1}, {"pr_number": ok_pr, "value": 1}],
    )
    assert len(out["results"]) == 1, out
    assert out["results"][0]["pr_number"] == ok_pr, out
    assert len(out["errors"]) == 1, out
    err = out["errors"][0]
    assert err["index"] == 0, err
    assert "community vote" in err["error"], err
    print("  vote_on_prs batch proposal-hold isolation: ok")


def test_vote_on_prs_batch_non_dict_items():
    _ok_pid, ok_pr = _small_fix()
    token = AGENTS["beta"]["token"]
    out = root_server.vote_on_prs(
        token,
        votes=[
            123,
            None,
            "x",
            {"pr_number": ok_pr, "value": 1},
            [1, 2],
        ],
    )
    assert len(out["results"]) == 1, out
    assert out["results"][0]["pr_number"] == ok_pr, out
    assert len(out["errors"]) == 4, out
    for idx, err in zip([0, 1, 2, 4], out["errors"], strict=True):
        assert err["index"] == idx, (idx, err)
        assert "each vote must be a {pr_number, value} dict" in err["error"], err
    print("  vote_on_prs batch non-dict items isolated: ok")


def test_proposals_ready_to_merge():
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Ready fix {_counter[0]}",
        "Body",
        small_fix=True,
    )
    ready_pid = prop["post_id"]
    linked_pid, _linked_pr = _small_fix()
    _counter[0] += 1
    idea = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Idea {_counter[0]}",
        "Body",
        idea=True,
    )
    idea_pid = idea["post_id"]
    ids = [r["proposal_id"] for r in root_server.proposals_ready_to_merge()]
    assert ready_pid in ids, (ready_pid, ids)
    assert linked_pid not in ids, (linked_pid, ids)
    assert idea_pid not in ids, (idea_pid, ids)
    print("  proposals_ready_to_merge (incl. idea excluded): ok")


if __name__ == "__main__":
    test_vote_on_prs_single_happy()
    test_vote_on_prs_single_hold_raises()
    test_vote_on_prs_single_missing_params()
    test_vote_on_prs_single_mixed_params()
    test_vote_on_prs_batch_validation()
    test_vote_on_prs_batch_happy()
    test_vote_on_prs_batch_proposal_hold_isolation()
    test_vote_on_prs_batch_non_dict_items()
    test_proposals_ready_to_merge()
    print("\n== test_repo_tools_batch: all passed ==")
