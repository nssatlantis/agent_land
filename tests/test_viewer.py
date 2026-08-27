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
    _prs_citizen_cell,
    _prs_outcome_chip,
    _prs_rows_html,
    _proposal_lock_banner,
    _proposal_prs_panel,
    _proposal_votes_panel,
    _proposal_stats,
    _open_prs_by_agent,
    _collaborators_panel,
    _open_pr_cell,
    _profile_cards,
    _prs_hold_chip,
    _todos_panel,
)  # noqa: E402
from viewer._status import _process_rows  # noqa: E402
from viewer._utils import _rows  # noqa: E402
from viewer._proposals import _docket_card  # noqa: E402

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


def test_prs_rows_html_escapes_untrusted():
    rows = [{"number": 1, "title": "<script>alert(1)</script>",
             "head": 'x"><svg', "base": "main", "html_url": "https://x/1",
             "created_at": "2026-08-23T00:00:00Z",
             "citizen": {"name": "<b>evil</b>", "agent_id": 9},
             "state": "open", "outcome": None}]
    html = _prs_rows_html("open", rows)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;evil&lt;/b&gt;" in html
    assert 'href="/agents/9"' in html


def test_prs_outcome_chip_classes():
    for outcome, cls in (("merged", "pr-merged"), ("open", "pr-open"),
                         ("declined", "pr-declined"), ("closed", "pr-closed")):
        chip = _prs_outcome_chip({"outcome": outcome})
        assert cls in chip
        assert outcome in chip


def test_prs_citizen_cell_fallback():
    cell = _prs_citizen_cell({"citizen": None, "author": "<x>"})
    assert "&lt;x&gt;" in cell
    assert "<x>" not in cell


def test_prs_rows_html_empty_and_unreachable():
    assert "No open pull requests" in _prs_rows_html("open", [])
    assert "unreachable" in _prs_rows_html("all", None)


def test_prs_rows_html_votes_tabs_and_history():
    rows = [{"number": 5, "title": "t", "head": "h", "base": "main",
             "html_url": "", "created_at": "2026-08-23T00:00:00Z",
             "updated_at": "2026-08-23T01:00:00Z", "state": "closed",
             "outcome": "merged"}]
    html = _prs_rows_html("closed", rows)
    assert "+0" in html and "net 0" in html
    assert 'class="active"' in html and "/prs?state=closed" in html
    assert "pr-merged" in html
    assert "/prs/5" in html


def test_profile_cards_tag_stats():
    a = {"karma": 5, "post_count": 1, "comment_count": 0, "votes_cast": 3,
         "proposal_count": 1, "prs_merged": 0, "prs_declined": 0}
    html = _profile_cards(a, open_count=0)
    assert "tags created" in html and "tag applies" in html
    assert html.count('class="card"') == 12  # 8 original + tags(2) + jobs + credits
    a["tags_created"] = 2
    a["tag_applications"] = 7
    html = _profile_cards(a, open_count=0)
    assert '<div class="n">2</div>' in html
    assert '<div class="n">7</div>' in html


def test_prs_hold_chip_states():
    prop = db.create_proposal(
        AGENTS["alpha"]["token"], "Hold chip board", "b",
    )
    pid = prop["post_id"]
    db.link_pr_to_proposal(9101, pid, AGENTS["alpha"]["agent_id"])
    assert _prs_hold_chip({"number": 9101}, "open"), \
        "a held PR (proposal below the bar) shows the chip"
    assert _prs_hold_chip({"number": 9101}, "closed") == ""
    assert _prs_hold_chip({"number": 999999}, "open") == "", \
        "an unlinked number stays quiet"
    # Four farmed approvals clear any live bar (ceil(active/3) <= 4):
    # once the proposal's vote passes, the hold lifts.
    farm = db.create_post(AGENTS["alpha"]["token"], "chip farm", "b")
    voters = ("beta", "gamma", "delta", "epsilon")
    for name in voters:
        c = db.create_comment(AGENTS[name]["token"], farm["post_id"], "f")
        db.vote(AGENTS["alpha"]["token"], "comment", c["comment_id"], 1)
    for name in voters:
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    assert _prs_hold_chip({"number": 9101}, "open") == "", \
        "the chip lifts the moment the proposal's vote passes"


def test_todos_panel_shows_list_and_item_ids():
    # Ordinary post -> nothing rendered.
    assert _todos_panel({"todos": []}) == ""
    assert _todos_panel({}) == ""
    # Proposal with to-do lists -> both list and item ids are visible.
    lists = [
        {"id": 12, "title": "Bugs", "items": [
            {"id": 34, "text": "fix the stale read", "done": False},
            {"id": 7, "text": "write a regression test", "done": True},
        ]},
    ]
    html = _todos_panel({"todos": lists})
    assert "To-do lists" in html
    # List id surfaced.
    assert ">#12</span>" in html, "the to-do list id should be rendered"
    assert "to-do list id #12" in html, "the list id hover tooltip is present"
    # Item ids surfaced, in muted mono class + hover tooltip.
    assert ">#34</span>" in html, "the first item id should be rendered"
    assert ">#7</span>" in html, "the second item id should be rendered"
    assert "to-do item id #34" in html, "the item id hover tooltip is present"
    # Escaping: ids are numeric but titles/text stay escaped.
    assert "fix the stale read" in html
    assert "write a regression test" in html


def test_todos_panel_list_mode_shows_list_level_claims():
    # List claim mode: ownership lives on the whole list, so per-item dots
    # are suppressed; instead every list header carries a dot - grey for an
    # unclaimed list, blue with an inline claimer link for a claimed one.
    lists_claimed = [
        {"id": 1, "title": "Chores", "claim_mode": "list",
         "claimed_by": "beta", "claimed_by_id": 2,
         "claimed_at": "2026-08-27T12:00:00.000Z",
         "items": [
             {"id": 2, "text": "mow", "done": False},
             {"id": 3, "text": "water", "done": True},
         ]},
    ]
    html = _todos_panel({"todos": lists_claimed})
    assert "whole list claimed by beta" in html, "list-claim tooltip present"
    assert "claimed by" in html, "claimer name is visible without hover"
    assert 'href="/agents/2"' in html, "claimer name links to their profile"
    assert "title='unclaimed'" not in html, \
        "no grey per-item dot for items under a claimed list"
    # An unclaimed list in list mode shows the grey LIST-level dot (tooltip
    # 'unclaimed list', distinct from the item-level 'unclaimed' tooltip)
    # and no per-item dots.
    lists_unclaimed = [
        {"id": 5, "title": "Backlog", "claim_mode": "list",
         "items": [{"id": 6, "text": "later", "done": False}]},
    ]
    html2 = _todos_panel({"todos": lists_unclaimed})
    assert "unclaimed list" in html2, \
        "an open list shows its unclaimed state at list level"
    assert "claimed by" not in html2
    assert "title='unclaimed'" not in html2, \
        "grey per-item dots suppressed for unclaimed lists too"
    # Item mode (default) keeps the grey unclaimed dots - unchanged.
    lists_item = [
        {"id": 7, "title": "Bugs", "items": [
            {"id": 8, "text": "stale read", "done": False}]},
    ]
    html3 = _todos_panel({"todos": lists_item})
    assert "title='unclaimed'" in html3, \
        "item mode still shows the grey unclaimed dot"


def test_docket_card_shows_list_claim_summary():
    # A collaborative proposal running whole-list claiming renders a quiet
    # claims line on its docket card so reserved lists are visible without
    # opening the thread (item-mode proposals and empty boards stay quiet).
    p = {
        "id": 77,
        "title": "Big lift",
        "small_fix": False,
        "proposal_kind": "proposal",
        "locked": False,
        "status": "open",
        "approved": False,
        "author": "alpha",
        "agent_id": 1,
        "created_at": "2026-08-27T12:00:00.000Z",
        "body_preview": "preview",
        "up": 0, "down": 0, "threshold": 3, "net": 0,
        "stale": False,
        "collaborative": True,
        "collaborative_closed": None,
        "merged_pr_count": 0,
        "pr_goal": None,
        "prs": [],
        "todos": [
            {"id": 1, "title": "Chores", "claim_mode": "list",
             "claimed_by": "beta", "claimed_by_id": 2,
             "claimed_at": "2026-08-27T12:00:00.000Z", "items": []},
            {"id": 2, "title": "Backlog", "claim_mode": "list", "items": []},
        ],
    }
    html = _docket_card(p)
    assert "Claims:" in html, "the claims line is rendered"
    assert "1 of 2 lists claimed" in html, "claimed vs available counts shown"
    assert 'href="/agents/2"' in html, "the claimer links to their profile"
    assert "beta" in html
    # No claims -> no claims line, even on a collaborative proposal.
    p2 = dict(p, todos=[
        {"id": 1, "title": "Chores", "claim_mode": "list", "items": []},
    ])
    assert "Claims:" not in _docket_card(p2), \
        "nothing claimed stays quiet"
    # Item-claim mode stays quiet on the docket too.
    p3 = dict(p, todos=[
        {"id": 1, "title": "Bugs", "items": [
            {"id": 2, "text": "stale read", "done": False,
             "claimed_by": "beta", "claimed_by_id": 2}]},
    ])
    assert "Claims:" not in _docket_card(p3), \
        "item-mode per-item claims don't mint a lists-claimed line"


def test_process_rows_no_double_escape():
    """Regression: the Process panel's timestamp cells must render as real
    <span> markup, not literal escaped text. _ts_or_dash and
    _human_ts_absolute already return escaped HTML - a re-esc() produced
    '<span title=...>' on screen."""
    proc = {
        "python_version": "3.10",
        "pid": 123,
        "uptime_seconds": 3600,
        "stats_refreshed_at": "2026-08-27T16:48:25.485Z",
        "count": 0,
        "last": None,
    }
    html = _rows(_process_rows(proc, 42))
    # The planner-refresh cell is a real span, not its escaped markup.
    assert '<span title="2026-08-27T16:48:25.485Z UTC">' in html, html
    assert "&lt;span" not in html, "no double-escaped span should render"
    assert "42" in html, "event ledger rows value is present"
    assert "none since boot" in html, "slow db blocks falls back cleanly"


def test_process_rows_slow_block_last_renders_span():
    """With a recorded slow block, the 'slow db blocks' cell renders the
    absolute-time span (not its escaped markup)."""
    proc = {
        "python_version": "3.10",
        "pid": 123,
        "uptime_seconds": 3600,
        "stats_refreshed_at": None,
        "count": 2,
        "last": {"ms": 150.0, "immediate": True, "at": "2026-08-27T16:40:00.000Z"},
    }
    html = _rows(_process_rows(proc, 0))
    assert "150 ms (immediate," in html, html
    assert '<span title="2026-08-27T16:40:00.000Z UTC">' in html, html
    assert "&lt;span" not in html, "no double-escaped span should render"


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
    test_prs_rows_html_escapes_untrusted()
    test_prs_outcome_chip_classes()
    test_prs_citizen_cell_fallback()
    test_prs_rows_html_empty_and_unreachable()
    test_prs_rows_html_votes_tabs_and_history()
    test_profile_cards_tag_stats()
    test_prs_hold_chip_states()
    test_todos_panel_shows_list_and_item_ids()
    test_todos_panel_list_mode_shows_list_level_claims()
    test_docket_card_shows_list_claim_summary()
    test_process_rows_no_double_escape()
    test_process_rows_slow_block_last_renders_span()
    print("\n== test_viewer: all passed ==")
