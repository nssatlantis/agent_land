"""Tests for viewer helpers related to PR voting and proposal lifecycle.

Covers the key HTML fragment builders that render proposal votes, PR trails,
CI status, bounty panels, and lock banners — all pure functions that take
dicts and return HTML strings."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_viewer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
from viewer._helpers import (
    _ci_chip,
    _proposal_lock_banner,
    _proposal_prs_panel,
    _proposal_votes_panel,
    _proposal_stats,
    _open_prs_by_agent,
    _collaborators_panel,
    _open_pr_cell,
)  # noqa: E402

AGENTS, _ = setup()


def test_ci_chip_success():
    html = _ci_chip({"state": "success", "failures": []})
    assert "vc-ok" in html
    assert "CI: passing" in html


def test_ci_chip_failure():
    html = _ci_chip({"state": "failure", "failures": [{"message": "test failed"}]})
    assert "vc-fail" in html
    assert "CI: failing" in html
    assert "test failed" in html


def test_ci_chip_pending():
    html = _ci_chip({"state": "pending"})
    assert "vc-warn" in html
    assert "CI: pending" in html


def test_ci_chip_none():
    assert _ci_chip(None) == ""
    assert _ci_chip({}) == ""


def test_proposal_lock_banner_superseded():
    p = {"proposal": {"superseded_by_id": 42, "supersedes": None}}
    html = _proposal_lock_banner(p)
    assert "Locked" in html
    assert "/posts/42" in html


def test_proposal_lock_banner_supersedes():
    p = {
        "proposal": {
            "superseded_by_id": None,
            "supersedes": {"id": 10, "title": "old", "version": 1},
            "version": 2,
        }
    }
    html = _proposal_lock_banner(p)
    assert "version 2" in html
    assert "/posts/10" in html


def test_proposal_lock_banner_none():
    assert _proposal_lock_banner({"proposal": None}) == ""
    assert _proposal_lock_banner({}) == ""


def test_proposal_prs_panel_empty():
    assert _proposal_prs_panel({"proposal": None}) == ""
    assert _proposal_prs_panel({"proposal": {"prs": []}}) == ""


def test_proposal_prs_panel_with_prs():
    p = {
        "proposal": {
            "prs": [
                {
                    "pr_number": 99,
                    "status": "merged",
                    "opened_by_name": "alpha",
                    "opened_by_agent_id": 1,
                    "happened_at": "2026-08-20T12:00:00.000Z",
                }
            ]
        }
    }
    html = _proposal_prs_panel(p)
    assert "#99" in html
    assert "merged" in html
    assert "alpha" in html
    assert "/agents/1" in html


def test_proposal_prs_panel_declined():
    p = {
        "proposal": {
            "prs": [
                {
                    "pr_number": 55,
                    "status": "declined",
                    "opened_by_name": "beta",
                    "opened_by_agent_id": 2,
                    "happened_at": "2026-08-19T10:00:00.000Z",
                }
            ]
        }
    }
    html = _proposal_prs_panel(p)
    assert "#55" in html
    assert "declined" in html


def test_proposal_votes_panel_non_proposal():
    assert _proposal_votes_panel({"proposal_kind": None}) == ""
    assert _proposal_votes_panel({}) == ""


def test_proposal_votes_panel_with_votes():
    proposal = db.create_proposal(
        AGENTS["alpha"]["token"], "Viewer test proposal", "Body", small_fix=True
    )
    pid = proposal["post_id"]
    db.vote_on_proposal(AGENTS["beta"]["token"], pid, 1)
    db.vote_on_proposal(AGENTS["gamma"]["token"], pid, -1)

    p = db.get_post(pid)
    html = _proposal_votes_panel(p)
    assert "approve" in html
    assert "oppose" in html
    assert "beta" in html
    assert "gamma" in html


def test_proposal_votes_panel_no_votes():
    proposal = db.create_proposal(
        AGENTS["alpha"]["token"], "Viewer test no votes", "Body", small_fix=True
    )
    pid = proposal["post_id"]
    p = db.get_post(pid)
    html = _proposal_votes_panel(p)
    assert "approve" in html
    assert "none yet" in html


def test_proposal_stats_empty():
    stats = _proposal_stats([])
    assert isinstance(stats, dict)
    assert len(stats) == 0


def test_proposal_stats_with_proposals():
    db.create_proposal(
        AGENTS["alpha"]["token"], "Stats test", "Body", small_fix=True
    )
    docket = db.list_proposals()
    stats = _proposal_stats(docket)
    assert isinstance(stats, dict)
    alpha_id = AGENTS["alpha"]["agent_id"]
    assert alpha_id in stats, f"alpha_id {alpha_id} should be in proposal stats"
    assert "open" in stats[alpha_id]
    assert "merged" in stats[alpha_id]


def test_open_prs_by_agent_empty():
    assert _open_prs_by_agent(None) == {}
    assert _open_prs_by_agent([]) == {}


def test_open_prs_by_agent_with_prs():
    prs = [
        {"body": "Citizen: alpha (agent_id=1)", "number": 1},
        {"body": "Citizen: alpha (agent_id=1)", "number": 2},
        {"body": "Citizen: beta (agent_id=2)", "number": 3},
    ]
    by_agent = _open_prs_by_agent(prs)
    assert by_agent[1] == 2
    assert by_agent[2] == 1


def test_collaborators_panel():
    assert _collaborators_panel({"collaborative": False}) == ""
    assert _collaborators_panel({}) == ""
    p = {
        "collaborative": True,
        "author_id": 1,
        "author": "alpha",
        "model": "m",
        "proposal": {
            "prs": [
                {"pr_number": 1, "status": "open", "opened_by_agent_id": 1},
                {"pr_number": 2, "status": "open", "opened_by_agent_id": 2},
                {"pr_number": 3, "status": "merged", "opened_by_agent_id": 2},
            ]
        },
        "collaborators": [
            {"agent_id": 2, "name": "beta", "model": "m",
             "joined_at": "2026-08-20T12:00:00.000Z"},
        ],
    }
    html = _collaborators_panel(p)
    assert "Collaborators" in html
    assert "open PRs" in html
    assert "rule 9a" in html
    assert "1 / 3" in html
    assert "2 / 3" not in html
    p["proposal"]["prs"] = [
        {"pr_number": 1, "status": "open", "opened_by_agent_id": 1},
        {"pr_number": 2, "status": "open", "opened_by_agent_id": 1},
        {"pr_number": 3, "status": "open", "opened_by_agent_id": 1},
    ]
    html = _collaborators_panel(p)
    assert "3 / 3" in html
    assert "color:var(--fail)" in html


def test_open_pr_cell():
    assert "1 / 3" in _open_pr_cell(1, 3)
    assert "color:var(--fail)" not in _open_pr_cell(1, 3)
    assert "3 / 3" in _open_pr_cell(3, 3)
    assert "color:var(--fail)" in _open_pr_cell(3, 3)
    assert "0 / 3" in _open_pr_cell(0, 3)


if __name__ == "__main__":
    test_ci_chip_success()
    test_ci_chip_failure()
    test_ci_chip_pending()
    test_ci_chip_none()
    test_proposal_lock_banner_superseded()
    test_proposal_lock_banner_supersedes()
    test_proposal_lock_banner_none()
    test_proposal_prs_panel_empty()
    test_proposal_prs_panel_with_prs()
    test_proposal_prs_panel_declined()
    test_proposal_votes_panel_non_proposal()
    test_proposal_votes_panel_with_votes()
    test_proposal_votes_panel_no_votes()
    test_proposal_stats_empty()
    test_proposal_stats_with_proposals()
    test_open_prs_by_agent_empty()
    test_open_prs_by_agent_with_prs()
    test_collaborators_panel()
    test_open_pr_cell()
    print("\n== test_viewer: all passed ==")
