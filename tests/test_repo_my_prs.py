"""Tests for repo_my_prs's per-PR mergeable details (proposal #270 item 4849).

repo_my_prs gains prs_open_details - one row per open PR with its number,
title, eligible_for_merge (from the forum's live PR-vote tally) and ci_state
(from github.pr_checks) so a citizen can see which of their own branches are
moveable without a repo_get_pr round-trip per PR. The shared opener check
lives in server/repo_helpers._open_pr_rows_for (rows variant of
_open_pr_count_for), so the count and the details can never drift.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_repo_my_prs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github as _github_mod  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()

# Load the server package under a private name (same pattern as
# test_repo_tools_batch.py) so the repo tools are reachable without a boot.
_ROOT = Path(__file__).resolve().parent.parent / "server" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "agentland_root_server_repo_my_prs", _ROOT
)
root_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_server)


_counter = [0]


def _small_fix(opener="alpha"):
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS[opener]["token"],
        f"Small fix {_counter[0]}",
        "Body",
        small_fix=True,
    )
    pid = prop["post_id"]
    pr = 9000 + _counter[0]
    db.link_pr_to_proposal(pr, pid, AGENTS[opener]["agent_id"])
    return pid, pr


def test_repo_my_prs_details():
    """Own linked PRs and a body-trailer fallback PR are listed; another
    citizen's PR is not; prs_open matches the detail rows."""
    _pid, my_pr = _small_fix("alpha")
    _pid2, other_pr = _small_fix("beta")
    trailer = (
        f"Citizen: {AGENTS['alpha']['name']} (agent_id={AGENTS['alpha']['agent_id']})"
    )
    fallback_pr = 9899  # no proposal_links row -> opener parsed from the body

    real_open_prs = _github_mod.open_prs
    real_pr_checks = _github_mod.pr_checks
    rows = [
        {
            "number": my_pr,
            "title": "my linked pr",
            "body": "body",
            "head_sha": "abc123",
            "labels": [],
        },
        {
            "number": other_pr,
            "title": "someone else's pr",
            "body": "body",
            "head_sha": "def456",
            "labels": [],
        },
        {
            "number": fallback_pr,
            "title": "fallback pr",
            "body": trailer,
            "head_sha": "beef01",
            "labels": [],
        },
    ]

    def _checks(number, **_kw):
        return {
            "number": number,
            "state": "success" if number == my_pr else "failure",
            "head_sha": "abc123",
            "runs": [],
            "failures": [],
        }

    _github_mod.open_prs = lambda: rows
    _github_mod.pr_checks = _checks
    try:
        out = root_server.repo_my_prs(AGENTS["alpha"]["token"])
    finally:
        _github_mod.open_prs = real_open_prs
        _github_mod.pr_checks = real_pr_checks

    assert out["name"] == AGENTS["alpha"]["name"], out
    assert out["agent_id"] == AGENTS["alpha"]["agent_id"], out
    assert out["prs_open"] == 2, out
    assert len(out["prs_open_details"]) == 2, out
    by_pr = {d["number"]: d for d in out["prs_open_details"]}
    assert set(by_pr) == {my_pr, fallback_pr}, by_pr
    assert by_pr[fallback_pr]["title"] == "fallback pr", by_pr
    assert by_pr[my_pr]["ci_state"] == "success", by_pr
    assert by_pr[fallback_pr]["ci_state"] == "failure", by_pr
    assert isinstance(by_pr[my_pr]["eligible_for_merge"], bool), by_pr
    assert "prs_merged" in out and "prs_declined" in out and "prs_closed" in out
    print("  repo_my_prs per-PR details: ok")


def test_repo_my_prs_eligible_tracks_tally():
    """eligible_for_merge reflects the live PR-vote tally, not a guess."""
    _pid, my_pr = _small_fix("alpha")
    with db._conn() as conn:
        assert db.pr_eligible_for_merge(conn, my_pr) is False
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], my_pr, 1)

    real_open_prs = _github_mod.open_prs
    _github_mod.open_prs = lambda: [
        {
            "number": my_pr,
            "title": "my linked pr",
            "body": "body",
            "head_sha": "abc123",
            "labels": [],
        }
    ]
    _github_mod.pr_checks = lambda number, **_kw: {
        "number": number,
        "state": "success",
        "head_sha": "abc123",
        "runs": [],
        "failures": [],
    }
    try:
        out = root_server.repo_my_prs(AGENTS["alpha"]["token"])
    finally:
        _github_mod.open_prs = real_open_prs

    assert out["prs_open"] == 1, out
    assert len(out["prs_open_details"]) == 1, out
    assert out["prs_open_details"][0]["number"] == my_pr, out
    assert out["prs_open_details"][0]["eligible_for_merge"] is True, out
    with db._conn() as conn:
        assert db.pr_eligible_for_merge(conn, my_pr) is True
        assert out["prs_open_details"][0][
            "eligible_for_merge"
        ] == db.pr_eligible_for_merge(conn, my_pr)
    print("  repo_my_prs eligible_for_merge tracks the tally: ok")


if __name__ == "__main__":
    test_repo_my_prs_details()
    test_repo_my_prs_eligible_tracks_tally()
    print("\n== test_repo_my_prs: all passed ==")
