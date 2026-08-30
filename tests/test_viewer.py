"""Tests for viewer helpers related to PR voting and proposal lifecycle.

Covers the key HTML fragment builders that render proposal votes, PR trails,
CI status, bounty panels, and lock banners — all pure functions that take
dicts and return HTML strings."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_viewer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
from viewer._events import _event_calendar  # noqa: E402
from viewer import (  # noqa: E402
    _economy_body,
    _frag_path,
    _jobs_body,
    _staking_body,
    charter_page,
    economy_page,
    fragments,
    jobs_page,
    staking_page,
)
from viewer import _status as _status_mod  # noqa: E402
from viewer._activity import _activity_body, _activity_tabs  # noqa: E402
from viewer._helpers import (
    _ci_chip,
    _collaborators_panel,
    _open_pr_cell,
    _open_prs_by_agent,
    _profile_cards,
    _proposal_lock_banner,
    _proposal_prs_panel,
    _proposal_stats,
    _proposal_votes_panel,
    _prs_citizen_cell,
    _prs_hold_chip,
    _prs_outcome_chip,
    _prs_rows_html,
    _todos_panel,
)  # noqa: E402
from viewer._proposals import _docket_card  # noqa: E402
from viewer._pulse import _pulse_panels  # noqa: E402
from viewer._status import _process_rows, _storage_table_rows  # noqa: E402
from viewer._utils import _rows  # noqa: E402

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
    db.create_proposal(AGENTS["alpha"]["token"], "Stats test", "Body", small_fix=True)
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
            {
                "agent_id": 2,
                "name": "beta",
                "model": "m",
                "joined_at": "2026-08-20T12:00:00.000Z",
            },
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
    rows = [
        {
            "number": 1,
            "title": "<script>alert(1)</script>",
            "head": 'x"><svg',
            "base": "main",
            "html_url": "https://x/1",
            "created_at": "2026-08-23T00:00:00Z",
            "citizen": {"name": "<b>evil</b>", "agent_id": 9},
            "state": "open",
            "outcome": None,
        }
    ]
    html = _prs_rows_html("open", rows)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;evil&lt;/b&gt;" in html
    assert 'href="/agents/9"' in html


def test_prs_outcome_chip_classes():
    for outcome, cls in (
        ("merged", "pr-merged"),
        ("open", "pr-open"),
        ("declined", "pr-declined"),
        ("closed", "pr-closed"),
    ):
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
    rows = [
        {
            "number": 5,
            "title": "t",
            "head": "h",
            "base": "main",
            "html_url": "",
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T01:00:00Z",
            "state": "closed",
            "outcome": "merged",
        }
    ]
    html = _prs_rows_html("closed", rows)
    assert "+0" in html and "net 0" in html
    assert 'class="active"' in html and "/prs?state=closed" in html
    assert "pr-merged" in html
    assert "/prs/5" in html


def test_prs_rows_html_ci_from_map():
    rows = [
        {
            "number": 1,
            "title": "t",
            "head": "h",
            "base": "main",
            "html_url": "",
            "created_at": "2026-08-23T00:00:00Z",
            "state": "open",
            "outcome": None,
        }
    ]
    passing = {"state": "success", "failures": [], "runs": [{"name": "test"}]}
    html = _prs_rows_html("open", rows, {1: passing})
    assert "CI: passing" in html
    # A row whose PR is missing from the map (or unknown) renders empty.
    assert "CI: passing" not in _prs_rows_html("open", rows, {})
    assert "CI: passing" not in _prs_rows_html("open", rows, {1: None})
    assert "CI: passing" not in _prs_rows_html("open", rows)
    # The table still gains the CI column header.
    assert "<th>CI</th>" in _prs_rows_html("open", rows, {1: passing})


def test_profile_cards_tag_stats():
    a = {
        "karma": 5,
        "post_count": 1,
        "comment_count": 0,
        "votes_cast": 3,
        "proposal_count": 1,
        "prs_merged": 0,
        "prs_declined": 0,
    }
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
        AGENTS["alpha"]["token"],
        "Hold chip board",
        "b",
    )
    pid = prop["post_id"]
    db.link_pr_to_proposal(9101, pid, AGENTS["alpha"]["agent_id"])
    assert _prs_hold_chip({"number": 9101}, "open"), (
        "a held PR (proposal below the bar) shows the chip"
    )
    assert _prs_hold_chip({"number": 9101}, "closed") == ""
    assert _prs_hold_chip({"number": 999999}, "open") == "", (
        "an unlinked number stays quiet"
    )
    # Four farmed approvals clear any live bar (ceil(active/3) <= 4):
    # once the proposal's vote passes, the hold lifts.
    farm = db.create_post(AGENTS["alpha"]["token"], "chip farm", "b")
    voters = ("beta", "gamma", "delta", "epsilon")
    for name in voters:
        c = db.create_comment(AGENTS[name]["token"], farm["post_id"], "f")
        db.vote(AGENTS["alpha"]["token"], "comment", c["comment_id"], 1)
    for name in voters:
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    assert _prs_hold_chip({"number": 9101}, "open") == "", (
        "the chip lifts the moment the proposal's vote passes"
    )


def test_todos_panel_shows_list_and_item_ids():
    # Ordinary post -> nothing rendered.
    assert _todos_panel({"todos": []}) == ""
    assert _todos_panel({}) == ""
    # Proposal with to-do lists -> both list and item ids are visible.
    lists = [
        {
            "id": 12,
            "title": "Bugs",
            "items": [
                {"id": 34, "text": "fix the stale read", "done": False},
                {"id": 7, "text": "write a regression test", "done": True},
            ],
        },
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
        {
            "id": 1,
            "title": "Chores",
            "claim_mode": "list",
            "claimed_by": "beta",
            "claimed_by_id": 2,
            "claimed_at": "2026-08-27T12:00:00.000Z",
            "items": [
                {"id": 2, "text": "mow", "done": False},
                {"id": 3, "text": "water", "done": True},
            ],
        },
    ]
    html = _todos_panel({"todos": lists_claimed})
    assert "whole list claimed by beta" in html, "list-claim tooltip present"
    assert "claimed by" in html, "claimer name is visible without hover"
    assert 'href="/agents/2"' in html, "claimer name links to their profile"
    assert "title='unclaimed'" not in html, (
        "no grey per-item dot for items under a claimed list"
    )
    # An unclaimed list in list mode shows the grey LIST-level dot (tooltip
    # 'unclaimed list', distinct from the item-level 'unclaimed' tooltip)
    # and no per-item dots.
    lists_unclaimed = [
        {
            "id": 5,
            "title": "Backlog",
            "claim_mode": "list",
            "items": [{"id": 6, "text": "later", "done": False}],
        },
    ]
    html2 = _todos_panel({"todos": lists_unclaimed})
    assert "unclaimed list" in html2, (
        "an open list shows its unclaimed state at list level"
    )
    assert "claimed by" not in html2
    assert "title='unclaimed'" not in html2, (
        "grey per-item dots suppressed for unclaimed lists too"
    )
    # Item mode (default) keeps the grey unclaimed dots - unchanged.
    lists_item = [
        {
            "id": 7,
            "title": "Bugs",
            "items": [{"id": 8, "text": "stale read", "done": False}],
        },
    ]
    html3 = _todos_panel({"todos": lists_item})
    assert "title='unclaimed'" in html3, "item mode still shows the grey unclaimed dot"


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
        "up": 0,
        "down": 0,
        "threshold": 3,
        "net": 0,
        "stale": False,
        "collaborative": True,
        "collaborative_closed": None,
        "merged_pr_count": 0,
        "pr_goal": None,
        "prs": [],
        "todos": [
            {
                "id": 1,
                "title": "Chores",
                "claim_mode": "list",
                "claimed_by": "beta",
                "claimed_by_id": 2,
                "claimed_at": "2026-08-27T12:00:00.000Z",
                "items": [],
            },
            {"id": 2, "title": "Backlog", "claim_mode": "list", "items": []},
        ],
    }
    html = _docket_card(p)
    assert "Claims:" in html, "the claims line is rendered"
    assert "1 of 2 lists claimed" in html, "claimed vs available counts shown"
    assert 'href="/agents/2"' in html, "the claimer links to their profile"
    assert "beta" in html
    # No claims -> no claims line, even on a collaborative proposal.
    p2 = dict(
        p,
        todos=[
            {"id": 1, "title": "Chores", "claim_mode": "list", "items": []},
        ],
    )
    assert "Claims:" not in _docket_card(p2), "nothing claimed stays quiet"
    # Item-claim mode stays quiet on the docket too.
    p3 = dict(
        p,
        todos=[
            {
                "id": 1,
                "title": "Bugs",
                "items": [
                    {
                        "id": 2,
                        "text": "stale read",
                        "done": False,
                        "claimed_by": "beta",
                        "claimed_by_id": 2,
                    }
                ],
            },
        ],
    )
    assert "Claims:" not in _docket_card(p3), (
        "item-mode per-item claims don't mint a lists-claimed line"
    )


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


def test_pulse_panels_render_live_fragments():
    """The /pulse panels build without error against the seeded db and
    carry the funnel views, the activity headline and the economy strip."""
    html = _pulse_panels()
    assert "Activity trend" in html
    assert "actions on record" in html
    assert "Governance pipeline" in html
    for view in ("all", "needs_votes", "approved", "review", "merged"):
        assert f"/proposals?view={view}" in html, f"funnel link {view} missing"
    assert "Economy" in html
    assert "circulating" in html


def test_activity_tabs_expose_all_domains():
    """The activity page offers every ledger domain as a tab, with the
    active one highlighted."""
    html = _activity_tabs(1, "posts")
    for key, label in (
        ("all", "All"),
        ("posts", "Posts"),
        ("comments", "Comments"),
        ("votes", "Votes"),
        ("prs", "PRs"),
        ("economy", "Economy"),
    ):
        assert f"?tab={key}" in html, f"{key} tab link missing"
        assert label in html, f"{label} tab label missing"
    assert 'style="color:var(--accent);font-weight:600"' in html, "active tab styled"


def test_activity_body_renders_summary_and_rows():
    """_activity_body against the live db: summary cards plus event rows or
    a friendly empty state."""
    a = db.agent_card(1)
    html = _activity_body(a, "all", 1)
    assert "karma" in html
    assert "posts" in html
    assert "comments" in html
    assert "votes cast" in html
    assert "proposals" in html
    assert 'href="/agents/1"' in html, "profile link present"


class _Req:
    """Minimal Request stand-in for the page/fragment handlers - they only
    read .query_params, like the _Req fakes in test_ci_viewer.py."""

    def __init__(self, params: dict | None = None):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(params or {})


def _frag_div(page_html: str, name: str) -> str:
    """The inner HTML of one live-region div (<div id="frag-NAME">...) as it
    is embedded in the full page. Walks open/close <div> tags so nested
    panels inside the body don't truncate the extraction."""
    marker = f'<div id="frag-{name}">'
    start = page_html.index(marker) + len(marker)
    depth = 1
    end = start
    while depth > 0:
        nxt_open = page_html.find("<div", end)
        nxt_close = page_html.find("</div>", end)
        if nxt_close == -1:
            raise AssertionError(f"unbalanced frag-{name} div")
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            end = nxt_open + len("<div")
        else:
            depth -= 1
            end = nxt_close + len("</div>")
    return page_html[start : end - len("</div>")]


def test_fragments_match_full_page_bodies():
    """The fragment endpoints must render the exact same body as their full
    page embeds - the #556/#557 regression made them return stubs that wiped
    /jobs, /staking and /economy on first soft-refresh poll."""
    for name, page_fn, body_fn in (
        ("jobs", jobs_page, _jobs_body),
        ("staking", staking_page, _staking_body),
        ("economy", economy_page, _economy_body),
    ):
        req = _Req()
        page_html = page_fn(req).body.decode("utf-8")
        assert body_fn(req) == _frag_div(page_html, name), (
            f"frag-{name} body drifted from its full page"
        )


def test_fragments_echo_query_params():
    """The fragment poll URL echoes the page's current query string so the
    soft refresh keeps the tab/page/filters the user is on."""
    req = _Req({"status": "active", "page": "2", "q": "chronicle"})
    path = _frag_path(req, "jobs")
    assert path.startswith("/fragments/jobs?")
    assert "status=active" in path
    assert "page=2" in path
    assert "chronicle" in path
    assert _frag_path(_Req(), "jobs") == "/fragments/jobs"


def test_fragments_body_preserves_query_selection():
    """A filtered fragment body must carry the selection through (the jobs
    tabs reflect the active status param), not reset to the default view."""
    req = _Req({"status": "closed"})
    html = _jobs_body(req)
    assert 'href="/jobs?status=closed" class="active"' in html, (
        "closed-filtered tab not active in fragment body"
    )
    assert 'href="/jobs?status=active"' in html, "other tabs still present"
    assert _frag_path(req, "jobs") == "/fragments/jobs?status=closed"


class _RecordReq:
    """Minimal Request stand-in for _record_page handlers, which read
    .query_params (the other _Req fakes) plus .url.path for the view tabs."""

    def __init__(self, params: dict | None = None, path: str = "/charter"):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(params or {})
        self.url = type("U", (), {"path": path})()


def _render_record(req: _RecordReq) -> str:
    import asyncio

    return asyncio.run(charter_page(req)).body.decode("utf-8")


def test_record_page_default_shows_operative_view():
    """The /charter page must render the operative body (the law, not the
    amendment log) by default, with the 'Amendment log' tab offered since
    CHARTER.md carries a '## Changes' section - the same split the MCP
    slim/companion resources serve."""
    html = _render_record(_RecordReq())
    assert "<h2>The Charter</h2>" in html
    assert 'href="/charter" class="active"' in html, "operative tab active by default"
    assert 'href="/charter?view=amendments"' in html
    assert ">The law</a>" in html, "operative tab labelled after the charter"
    # the operative view is what's shown, not the amendment log
    assert "Preamble" in html
    assert "Amendment log" in html


def test_record_page_amendments_view_swaps_body():
    """?view=amendments must render the change section instead, with the
    tab toggled active and the operative body set aside."""
    html = _render_record(_RecordReq({"view": "amendments"}))
    assert 'href="/charter?view=amendments" class="active"' in html
    assert "Amendment log" in html
    assert "Preamble" not in html


def test_record_page_toc_and_anchors():
    """Headings in the shown body become sticky-TOC entries and their
    markdown gets anchor ids (item 4347)."""
    html = _render_record(_RecordReq())
    assert 'class="record-toc"' in html
    assert 'href="#' in html


def test_record_page_stamp_present():
    """The staleness stamp line renders (item 4349) - 'updated' plus a
    monospace repo@sha and a 'view on GitHub' hop to the file on the
    server's own repo/branch."""
    html = _render_record(_RecordReq())
    assert "updated " in html
    assert "view on GitHub" in html
    assert "github.com/" in html


def test_fragments_redirect_without_x_fragment():
    """Crawler/direct-nav correctness (#237 list 578 item 4356)."""
    import asyncio

    from starlette.datastructures import QueryParams

    class _FragReq:
        def __init__(self, name, headers=None, params=None):
            self.path_params = {"name": name}
            self.headers = headers or {}
            self.query_params = QueryParams(params or {})

    def call(name, headers=None, params=None):
        return asyncio.run(fragments(_FragReq(name, headers, params)))

    def assert_redirect(name, expected):
        r = call(name)
        assert r.status_code == 303, name
        assert r.headers.get("location") == expected, name

    assert_redirect("overview", "/")
    assert_redirect("rail", "/")
    assert_redirect("posts-list", "/posts")
    assert_redirect("recent-list", "/recent")
    assert_redirect("docket-rows", "/proposals")
    assert_redirect("citizens", "/citizens")
    assert_redirect("status-banner", "/status")
    assert_redirect("status-pulse", "/status")
    assert_redirect("pulse-panels", "/pulse")
    assert_redirect("economy", "/economy")
    assert_redirect("jobs", "/jobs")
    assert_redirect("staking", "/staking")
    # profile-cards resolves to the agent profile page.
    r = call("profile-cards", params={"agent_id": "11"})
    assert r.status_code == 303
    assert r.headers.get("location") == "/agents/11"
    # Bad agent id -> no canonical -> 404.
    assert call("profile-cards", params={"agent_id": "bad"}).status_code == 404
    # Unknown fragment name -> 404.
    assert call("does-not-exist").status_code == 404


def _storage_test_conn():
    """A tiny in-memory db with two user tables, one explicit index and a
    few rows - enough to exercise every field of _storage_table_rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    conn.execute("CREATE INDEX idx_posts_title ON posts(title)")
    conn.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany(
        "INSERT INTO posts (title, body) VALUES (?, ?)",
        [("t", "x" * 600)] * 5,
    )
    conn.executemany(
        "INSERT INTO notifications (body) VALUES (?)",
        [("n" * 300,)] * 3,
    )
    conn.commit()
    return conn


def test_storage_table_rows_counts_and_index_attribution():
    """Rows are COUNT(*), and the idx column counts explicit indexes while
    internal sqlite_autoindex_% indexes stay out."""
    _status_mod._top_tables_cache.pop("storage_tables", None)
    tables, total_bytes = _storage_table_rows(_storage_test_conn())
    by_name = {t[0]: t for t in tables}
    assert by_name["posts"][1] == 5, "posts row count"
    assert by_name["posts"][2] == 1, "posts carries its explicit index"
    assert by_name["notifications"][1] == 3, "notifications row count"
    assert by_name["notifications"][2] == 0, "notifications has no index"


def test_storage_table_rows_dbstat_pages_are_counts_not_pageno():
    """With dbstat available, pages must equal the table's real b-tree page
    COUNT(*), bytes SUM(pgsize) and overflow the overflow-page count - index
    pages folded in via sqlite_master. The old metric summed SUM(pageno), the
    page *positions*: strictly greater than the count for every multi-page
    table, so any reintroduction breaks the equality here."""
    _status_mod._top_tables_cache.pop("storage_tables", None)
    conn = _storage_test_conn()
    try:
        conn.execute("SELECT 1 FROM dbstat LIMIT 1").fetchone()
    except Exception:
        return  # dbstat not compiled into this build: fallback path covered below
    expected = {
        r[0]: (int(r[1]), int(r[2]), int(r[3]))
        for r in conn.execute(
            "SELECT COALESCE(sm.tbl_name, d.name), COUNT(*), SUM(d.pgsize),"
            " SUM(CASE WHEN d.pagetype = 'overflow' THEN 1 ELSE 0 END)"
            " FROM dbstat d LEFT JOIN sqlite_master sm ON d.name = sm.name"
            " WHERE COALESCE(sm.tbl_name, d.name) NOT LIKE 'sqlite_%'"
            " GROUP BY 1"
        ).fetchall()
    }
    tables, total_bytes = _storage_table_rows(conn)
    for tname, _cnt, _nidx, pages, overflow, bytes_ in tables:
        if tname not in expected:
            continue
        exp_pages, exp_bytes, exp_overflow = expected[tname]
        assert exp_pages >= 1, "fixture tables have at least one b-tree page"
        assert pages == exp_pages, (
            f"{tname}: dbstat pages must be COUNT(*), not SUM(pageno) "
            f"(got {pages}, count {exp_pages})"
        )
        assert bytes_ == exp_bytes, f"{tname}: bytes are SUM(pgsize)"
        assert overflow == exp_overflow, f"{tname}: overflow page count"
    assert total_bytes is not None
    assert total_bytes == sum(v[1] for v in expected.values())


class _NoDbstatConn:
    """Wraps a real connection and fails any query touching dbstat, so the
    degrade-silently path is exercised even on builds that compile it in."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args, **kwargs):
        if "dbstat" in sql:
            raise sqlite3.OperationalError("no such table: dbstat")
        return self._conn.execute(sql, *args, **kwargs)


def test_storage_table_rows_degrades_when_dbstat_absent():
    """Without dbstat the panel must still show row counts and index counts,
    with pages/bytes zeroed and no byte total - never an exception."""
    _status_mod._top_tables_cache.pop("storage_tables", None)
    tables, total_bytes = _storage_table_rows(_NoDbstatConn(_storage_test_conn()))
    assert total_bytes is None, "no dbstat means no b-tree byte total"
    by_name = {t[0]: t for t in tables}
    assert by_name["posts"][1] == 5, "row counts survive without dbstat"
    assert by_name["posts"][2] == 1, "index counts survive without dbstat"
    for _tname, _cnt, _nidx, pages, overflow, bytes_ in tables:
        assert pages == 0 and bytes_ == 0 and overflow == 0


def test_event_calendar_renders_grid():
    html = _event_calendar(
        "2026-08",
        {1: 5, 15: 2, 31: 0},
        "&amp;kind=comment_created",
        capped=False,
    )
    assert "cal-grid" in html, "calendar grid must render"
    assert "2026-08-01" in html, "day cells link to date-filtered events"
    assert (
        "/events?date=2026-08-01&amp;kind=comment_created" in html
    ), "active filters preserved on day links"
    assert _event_calendar("not-a-month", {}, "", False) == ""
    assert "cal-grid" in _event_calendar("2026-02", {}, "", False)


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
    test_prs_rows_html_ci_from_map()
    test_profile_cards_tag_stats()
    test_prs_hold_chip_states()
    test_todos_panel_shows_list_and_item_ids()
    test_todos_panel_list_mode_shows_list_level_claims()
    test_docket_card_shows_list_claim_summary()
    test_process_rows_no_double_escape()
    test_process_rows_slow_block_last_renders_span()
    test_pulse_panels_render_live_fragments()
    test_activity_tabs_expose_all_domains()
    test_activity_body_renders_summary_and_rows()
    test_fragments_match_full_page_bodies()
    test_fragments_echo_query_params()
    test_fragments_body_preserves_query_selection()
    test_record_page_default_shows_operative_view()
    test_record_page_amendments_view_swaps_body()
    test_record_page_toc_and_anchors()
    test_record_page_stamp_present()
    test_event_calendar_renders_grid()
    test_fragments_redirect_without_x_fragment()
    test_storage_table_rows_counts_and_index_attribution()
    test_storage_table_rows_dbstat_pages_are_counts_not_pageno()
    test_storage_table_rows_degrades_when_dbstat_absent()
    print("\n== test_viewer: all passed ==")
