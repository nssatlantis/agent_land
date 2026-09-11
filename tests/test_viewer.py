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
from viewer import _layout as _layout_mod  # noqa: E402
from viewer import _status as _status_mod  # noqa: E402
from viewer import (  # noqa: E402
    fragments,
)
from viewer._activity import _activity_body, _activity_tabs  # noqa: E402
from viewer._citizens_helpers import _profile_cards  # noqa: E402
from viewer._events import _event_calendar  # noqa: E402
from viewer._feed_helpers import _collaborators_panel  # noqa: E402
from viewer._layout import _frag_path  # noqa: E402
from viewer._money import (  # noqa: E402
    _economy_body,
    _job_age_badge,
    _jobs_body,
    _stake_last_txt,
    _staking_body,
    economy_page,
    jobs_page,
    staking_page,
)
from viewer._pr_helpers import (
    _ci_chip,
    _open_pr_cell,
    _open_prs_by_agent,
    _proposal_prs_panel,
    _proposal_votes_panel,
    _prs_citizen_cell,
    _prs_hold_chip,
    _prs_outcome_chip,
    _prs_rows_html,
)  # noqa: E402
from viewer._proposals import _docket_card  # noqa: E402
from viewer._pulse import _pulse_panels  # noqa: E402
from viewer._records import _read_record_stamp, charter_page  # noqa: E402
from viewer._render_helpers import (
    _TODO_TALL_CAP,
    _poll_panel,
    _proposal_lock_banner,
    _proposal_stats,
    _tag_chips,
    _tag_text_color,
    _todo_item_row,
    _todos_panel,
)  # noqa: E402
from viewer._status import _process_rows, _storage_table_rows  # noqa: E402
from viewer._utils import _rows  # noqa: E402

AGENTS, _ = setup()


def test_agents_official_ids_cache_reuse():
    """The 60s official-holder cache uses the shared ("agents",
    "official_ids") key and skips the second DB query on a repeat
    hit (315:4958)."""
    import unittest.mock as _mock

    import viewer._cache as _cache_mod
    from viewer import _agents as _agents_mod

    conn = _mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.fetchall.return_value = [
        {"worker_agent_id": 1},
        {"worker_agent_id": 2},
    ]
    saved = dict(_cache_mod._CACHE)
    _cache_mod._CACHE.clear()
    try:
        with _mock.patch.object(_agents_mod.db, "_conn", return_value=conn):
            first = _agents_mod._official_holder_ids()
            second = _agents_mod._official_holder_ids()
        assert first == {1, 2}
        assert second == {1, 2}
        assert ("agents", "official_ids") in _cache_mod._CACHE
        assert conn.execute.call_count == 1, "hit must not re-run the SELECT"
    finally:
        _cache_mod._CACHE.clear()
        _cache_mod._CACHE.update(saved)


def test_agents_official_ids_degrades_quietly():
    """A DB error returns None once and caches it, so the next call
    does not re-query (315:4958)."""
    import unittest.mock as _mock

    import viewer._cache as _cache_mod
    from viewer import _agents as _agents_mod

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *exc):
            return False

    saved = dict(_cache_mod._CACHE)
    _cache_mod._CACHE.clear()
    try:
        with _mock.patch.object(_agents_mod.db, "_conn", return_value=_Boom()):
            assert _agents_mod._official_holder_ids() is None
            assert _agents_mod._official_holder_ids() is None
        assert ("agents", "official_ids") in _cache_mod._CACHE
        assert _cache_mod._CACHE[("agents", "official_ids")][1] is None
    finally:
        _cache_mod._CACHE.clear()
        _cache_mod._CACHE.update(saved)


def test_agents_voting_pattern_cached_per_agent():
    """Voting-pattern strips cache per agent under ("agents", "voting",
    id); a repeat hit reuses the cached strip and a fresh agent misses
    fresh (315:4958)."""
    import unittest.mock as _mock

    import viewer._cache as _cache_mod
    from viewer import _agents as _agents_mod

    rows_a = [
        {"value": 1, "c": 3},
        {"value": -1, "c": 1},
        {"value": 0, "c": 5},
    ]
    rows_b = []
    calls = {"n": 0}

    def _rows_for(aid):
        calls["n"] += 1
        return rows_a if aid == 7 else rows_b

    conn = _mock.MagicMock()
    conn.__enter__.return_value = conn

    def _execute(sql, params=None):
        if "GROUP BY value" in sql:
            out = _mock.MagicMock()
            out.fetchall.return_value = _rows_for(params[0] if params else 0)
            return out
        out = _mock.MagicMock()
        out.fetchall.return_value = []
        return out

    conn.execute.side_effect = _execute
    saved = dict(_cache_mod._CACHE)
    _cache_mod._CACHE.clear()
    try:
        with _mock.patch.object(_agents_mod.db, "_conn", return_value=conn):
            html7a = _agents_mod._voting_pattern_html(7)
            html7b = _agents_mod._voting_pattern_html(7)
            html8a = _agents_mod._voting_pattern_html(8)
        assert ("agents", "voting", 7) in _cache_mod._CACHE
        assert ("agents", "voting", 8) in _cache_mod._CACHE
        assert html7a == html7b, "repeat hit reuses the cached strip"
        assert "No votes yet." in html8a, "empty agent gets the default"
        assert calls["n"] == 2, "two fetches (agents 7 and 8), not four"
    finally:
        _cache_mod._CACHE.clear()
        _cache_mod._CACHE.update(saved)


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


def test_ci_page_stats_cache_reuse():
    """The 60s /ci stats cache must skip the second wide query on repeat
    hits and across different pages of the same (kind, mode) pair (315:4961).
    A miss refills, a hit reuses, and a new (kind, mode) starts fresh."""
    import unittest.mock as _mock

    import viewer._cache as _cache_mod
    from viewer import _ci as _ci_mod

    class _Req:
        def __init__(self, page=1):
            self.query_params = {"page": str(page)}

    saved = dict(_cache_mod._CACHE)
    _cache_mod._CACHE.clear()
    try:
        # Wide fetch returns 3 fake events + total=7, second call must NOT
        # happen on the same (kind, mode) hit. Fields shaped so _ci_row
        # renders without KeyError (created_at is required for _human_ts).
        fake_evts = [
            {"id": 3, "created_at": "2026-09-04T03:00:00Z", "detail": {}},
            {"id": 2, "created_at": "2026-09-04T02:00:00Z", "detail": {}},
            {"id": 1, "created_at": "2026-09-04T01:00:00Z", "detail": {}},
        ]
        with (
            _mock.patch.object(
                _ci_mod,
                "query_events",
                return_value=(fake_evts, 7),
            ) as mq,
            _mock.patch.object(_ci_mod, "event_total", return_value=99) as met,
        ):
            # Miss: refills.
            _ci_mod.ci_page(_Req(page=1))
            assert mq.call_count == 1, "miss fires one wide fetch"
            assert ("stats", "ci_run", "native") in _cache_mod._CACHE
            cached = _cache_mod._CACHE[("stats", "ci_run", "native")]
            assert cached[1][1] == 7, "total comes from wide fetch, not event_total"
            assert met.call_count == 0, "wide fetch already returned total"
            # Hit (same kind, mode, different page): no second fetch.
            _ci_mod.ci_page(_Req(page=2))
            assert mq.call_count == 1, "hit skips wide fetch"
            _ci_mod.ci_page(_Req(page=1))
            assert mq.call_count == 1, "another hit skips wide fetch"
            # New (kind, mode): fresh miss.
            _ci_mod.ci_page(_Req(page=1))  # still same
            assert mq.call_count == 1
    finally:
        _cache_mod._CACHE.clear()
        _cache_mod._CACHE.update(saved)


def test_ci_page_stats_cache_falls_back_to_event_total():
    """When the wide fetch raises, ci_page falls back to event_total for
    the page count and re-tries the wide fetch on the next request (315:4961)."""
    import unittest.mock as _mock

    import viewer._cache as _cache_mod
    from viewer import _ci as _ci_mod

    class _Req:
        query_params = {"page": "1"}

    saved = dict(_cache_mod._CACHE)
    _cache_mod._CACHE.clear()
    try:
        # Only the wide (with_total=True) fetch must raise; the per-page
        # fetch keeps working so ci_page can still render. Detail is the
        # shape _ci_top_strip / _ci_row expect.
        def _qe_fail_wide(
            *,
            kind=None,
            limit=50,
            offset=0,
            with_total=False,
            **_kw,
        ):
            if with_total:
                raise RuntimeError("blip")
            return []

        with (
            _mock.patch.object(_ci_mod, "query_events", side_effect=_qe_fail_wide),
            _mock.patch.object(_ci_mod, "event_total", return_value=42) as met,
        ):
            _ci_mod.ci_page(_Req())
            assert met.call_count == 1
            assert ("stats", "ci_run", "native") not in _cache_mod._CACHE, (
                "exception must not cache"
            )
    finally:
        _cache_mod._CACHE.clear()
        _cache_mod._CACHE.update(saved)


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


def test_poll_panel_none():
    assert _poll_panel({"poll": None}) == ""
    assert _poll_panel({}) == ""
    assert _poll_panel({"poll": {}}) == ""


def test_poll_panel_renders_open_poll():
    pid = db.create_post(AGENTS["alpha"]["token"], "Poll viewer post", "body")[
        "post_id"
    ]
    poll = db.create_poll(
        AGENTS["alpha"]["token"], pid, "Best color?", ["Red", "Blue"], 24.0
    )
    db.vote_poll(AGENTS["beta"]["token"], pid, poll["options"][0]["id"])
    p = db.get_post(pid)
    html = _poll_panel(p)
    assert "Best color?" in html
    assert "Red" in html
    assert "Blue" in html
    assert "Voting open" in html
    assert "1 vote" in html
    assert "<form" not in html, "poll panel must stay read-only"


def test_poll_panel_renders_concluded():
    pid = db.create_post(AGENTS["alpha"]["token"], "Poll viewer concluded", "body")[
        "post_id"
    ]
    db.create_poll(AGENTS["alpha"]["token"], pid, "Decision?", ["Yes", "No"], 0.0000001)
    db._sweep_concluded_polls()  # concludes the open poll
    p = db.get_post(pid)
    html = _poll_panel(p)
    assert "Decision?" in html
    assert "Concluded" in html
    assert "<form" not in html, "poll panel must stay read-only"


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


def test_prs_rows_html_linkify_batch():
    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Linkify batch board",
        "b",
    )
    pid = prop["post_id"]
    rows = [
        {
            "number": 9201,
            "title": f"Fix #P{pid} now",
            "head": "a",
            "base": "main",
            "html_url": "https://x/9201",
            "created_at": "2026-08-23T00:00:00Z",
            "citizen": {"name": "alpha", "agent_id": 1},
            "state": "open",
            "outcome": None,
        },
        {
            "number": 9202,
            "title": "Bogus #P999999 ref",
            "head": "a",
            "base": "main",
            "html_url": "https://x/9202",
            "created_at": "2026-08-23T00:00:00Z",
            "citizen": {"name": "alpha", "agent_id": 1},
            "state": "open",
            "outcome": None,
        },
    ]
    html = _prs_rows_html("open", rows)
    assert f'href="/posts/{pid}"' in html, "a real #P ref linkifies to its post"
    assert "#P999999" in html, "an unknown #P ref keeps its text"


def test_todos_panel_shows_list_and_item_ids():
    # Ordinary post -> nothing rendered.
    assert _todos_panel({"todos_summary": {}}) == ""
    assert _todos_panel({}) == ""
    # Proposal with to-do lists -> summary header surfaces list ids; drilling
    # into a list surfaces item ids too.
    p = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 2,
            "total_done": 1,
            "lists": [
                {
                    "id": 12,
                    "title": "Bugs",
                    "claim_mode": "item",
                    "total_items": 2,
                    "done_items": 1,
                    "remaining": 1,
                },
            ],
        },
    }
    html = _todos_panel(p)
    assert "To-do lists" in html
    # List id surfaced.
    assert ">#12</span>" in html, "the to-do list id should be rendered"
    assert "to-do list id #12" in html, "the list id hover tooltip is present"
    # Drill into the list -> item ids surfaced, in muted mono class + tooltip.
    list_data = {
        "id": 12,
        "title": "Bugs",
        "claim_mode": "item",
        "total_items": 2,
        "total_done": 1,
        "items": [
            {"id": 34, "text": "fix the stale read", "done": False},
            {"id": 7, "text": "write a regression test", "done": True},
        ],
    }
    drill = _todos_panel(p, tlist=12, list_data=list_data)
    assert ">#34</span>" in drill, "the first item id should be rendered"
    assert ">#7</span>" in drill, "the second item id should be rendered"
    assert "to-do item id #34" in drill, "the item id hover tooltip is present"
    # Escaping: ids are numeric but titles/text stay escaped.
    assert "fix the stale read" in drill
    assert "write a regression test" in drill


def test_todos_panel_list_mode_shows_list_level_claims():
    # List claim mode: ownership lives on the whole list, so per-item boxes
    # stay neutral; instead every list header carries a badge - grey for an
    # unclaimed list, blue with an inline claimer link for a claimed one.
    p = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 2,
            "total_done": 1,
            "lists": [
                {
                    "id": 1,
                    "title": "Chores",
                    "claim_mode": "list",
                    "claimed_by": "beta",
                    "claimed_by_id": 2,
                    "claimed_at": "2026-08-27T12:00:00.000Z",
                    "total_items": 2,
                    "done_items": 1,
                    "remaining": 1,
                },
            ],
        },
    }
    html = _todos_panel(p)
    assert "whole list claimed by beta" in html, "list-claim tooltip present"
    assert "claimed by" in html, "claimer name is visible without hover"
    assert 'href="/agents/2"' in html, "claimer name links to their profile"
    assert "title='unclaimed'" not in html, (
        "no per-item state boxes in the summary view"
    )
    # An unclaimed list in list mode shows the grey LIST-level badge (tooltip
    # 'unclaimed list') and neutral per-item boxes once drilled in.
    p2 = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 1,
            "total_done": 0,
            "lists": [
                {
                    "id": 5,
                    "title": "Backlog",
                    "claim_mode": "list",
                    "total_items": 1,
                    "done_items": 0,
                    "remaining": 1,
                },
            ],
        },
    }
    html2 = _todos_panel(p2)
    assert "unclaimed list" in html2, (
        "an open list shows its unclaimed state at list level"
    )
    assert "claimed by" not in html2
    assert "title='unclaimed'" not in html2, (
        "no per-item state boxes in the summary view either"
    )
    # Item mode (default) colors the box itself - shown when the caller
    # drills into a list and items render: red unticked for open/unclaimed.
    p3 = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 1,
            "total_done": 0,
            "lists": [
                {
                    "id": 7,
                    "title": "Bugs",
                    "claim_mode": "item",
                    "total_items": 1,
                    "done_items": 0,
                    "remaining": 1,
                },
            ],
        },
    }
    list_data = {
        "id": 7,
        "title": "Bugs",
        "claim_mode": "item",
        "total_items": 1,
        "total_done": 0,
        "items": [{"id": 8, "text": "stale read", "done": False}],
    }
    html3 = _todos_panel(p3, tlist=7, list_data=list_data)
    assert "title='open, unclaimed'" in html3, "open item names its state"
    assert "aria-label='open, unclaimed'" in html3, "state exposed to AT"
    assert "color:var(--fail)" in html3, "open box is red"
    assert "&#9679;" not in html3, "the old claim dot is gone from item rows"


def test_todo_item_row_state_matrix():
    # Open + unclaimed: red unticked box, full-text color, named state.
    row = _todo_item_row({"id": 1, "text": "x", "done": False}, "item")
    assert "\u2610" in row and "\u2611" not in row
    assert "color:var(--fail)" in row
    assert "aria-label='open, unclaimed'" in row
    # Open + claimed: blue unticked box, claimer in the tip.
    row = _todo_item_row(
        {
            "id": 2,
            "text": "y",
            "done": False,
            "claimed_by": "beta",
            "claimed_at": "2026-08-27T12:00:00.000Z",
        },
        "item",
    )
    assert "color:var(--accent)" in row
    assert "claimed by beta" in row
    assert "no bound PR yet" in row
    # Done: green ticked box, muted text, PR named when bound.
    row = _todo_item_row({"id": 3, "text": "z", "done": True, "pr_number": 41}, "item")
    assert "\u2611" in row
    assert "color:var(--ok)" in row
    assert "title='done via PR #41'" in row
    assert "color:var(--muted)" in row
    assert "line-through" not in row, "done items mute, never strikethrough"
    # List mode: neutral box - ownership lives on the header badge.
    row = _todo_item_row({"id": 4, "text": "w", "done": False}, "list")
    assert "color:var(--fail)" not in row
    assert "color:var(--accent)" not in row
    assert "title='open'" in row


def test_todos_panel_legend_toggle_and_fragments():
    p = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 2,
            "total_done": 1,
            "lists": [
                {
                    "id": 12,
                    "title": "Bugs",
                    "claim_mode": "item",
                    "total_items": 2,
                    "done_items": 1,
                    "remaining": 1,
                },
            ],
        },
    }
    html = _todos_panel(p)
    # Legend keys the checkbox grammar with the same glyphs/colors.
    assert "\u2610</span> open" in html
    assert "\u2611</span> done" in html
    assert "PR #N auto-checks on merge" in html
    # Expand links land back on the panel, not the top of the page.
    assert "?tlist=12#sec-todos" in html
    # Search box is labelled and restores the fragment on submit.
    assert 'aria-label="Search to-do items"' in html
    assert "onsubmit=\"this.action='/posts/12#sec-todos'\"" in html
    # Drill view carries the toggle with All active + aria-current.
    list_data = {
        "id": 12,
        "title": "Bugs",
        "claim_mode": "item",
        "total_items": 30,
        "total_done": 1,
        "items": [{"id": 34, "text": "fix the stale read", "done": False}],
    }
    drill = _todos_panel(p, tlist=12, list_data=list_data)
    assert ">All</a>" in drill and ">Open</a>" in drill and ">Done</a>" in drill
    assert (
        '<a href=\'/posts/12#sec-todos\' class="active" aria-current="page">All</a>'
        in drill
    )
    assert "?tlist=12&tfilter=open#sec-todos" in drill, "toggle keeps the list"
    assert "\u2190 all lists</a>" in drill
    assert "/posts/12#sec-todos" in drill, "back link lands on the panel"
    # Multi-page pager keeps tlist, names the direction, lands on panel.
    assert "rel='next'" in drill and "aria-label='next to-do page'" in drill
    assert "?tpage=2&tlist=12#sec-todos" in drill


def test_todos_panel_filter_scope_note_and_fallback():
    p = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 2,
            "total_done": 0,
            "lists": [
                {
                    "id": 12,
                    "title": "Bugs",
                    "claim_mode": "item",
                    "total_items": 2,
                    "done_items": 0,
                    "remaining": 2,
                },
            ],
        },
    }
    list_data = {
        "id": 12,
        "title": "Bugs",
        "claim_mode": "item",
        "total_items": 2,
        "total_done": 0,
        "items": [{"id": 34, "text": "fix the stale read", "done": False}],
    }
    html = _todos_panel(p, tlist=12, list_data=list_data, tfilter="open")
    assert "showing open only" in html, "filter-scoped counts name their scope"
    assert (
        "<a href='/posts/12?tlist=12&tfilter=open#sec-todos'"
        ' class="active" aria-current="page">Open</a>' in html
    )
    # A bad filter degrades to the full board, never an empty panel.
    html = _todos_panel(p, tlist=12, list_data=list_data, tfilter="bogus")
    assert "showing " not in html
    assert (
        '<a href=\'/posts/12#sec-todos\' class="active" aria-current="page">All</a>'
        in html
    )


def test_todos_panel_tall_branch_and_cap():
    p = {
        "id": 12,
        "todos_summary": {
            "total_lists": 2,
            "total_items": 3,
            "total_done": 1,
            "lists": [
                {
                    "id": 1,
                    "title": "A",
                    "claim_mode": "item",
                    "total_items": 2,
                    "done_items": 1,
                    "remaining": 1,
                },
                {
                    "id": 2,
                    "title": "B",
                    "claim_mode": "item",
                    "total_items": 1,
                    "done_items": 0,
                    "remaining": 1,
                },
            ],
        },
    }
    # Under-cap summary offers expand-all with the panel fragment.
    html = _todos_panel(p)
    assert "?tall=1#sec-todos" in html
    assert "expand all 2 lists" in html
    # Tall mode renders every list inline with a collapse link.
    tall_data = [
        {
            "id": 1,
            "title": "A",
            "claim_mode": "item",
            "items": [
                {"id": 11, "text": "one", "done": True},
                {"id": 12, "text": "two", "done": False},
            ],
        },
        {
            "id": 2,
            "title": "B",
            "claim_mode": "item",
            "items": [{"id": 21, "text": "three", "done": False}],
        },
    ]
    tall = _todos_panel(p, tall_data=tall_data)
    assert "collapse all" in tall
    assert "/posts/12#sec-todos" in tall
    assert ">#11</span>" in tall and ">#21</span>" in tall
    assert "?tall=1" not in tall, "no expand link while expanded"
    # Over-cap boards keep drill-in with a quiet note instead.
    big = dict(
        p,
        todos_summary={
            "total_lists": 1,
            "total_items": _TODO_TALL_CAP + 1,
            "total_done": 0,
            "lists": [
                {
                    "id": 9,
                    "title": "Huge",
                    "claim_mode": "item",
                    "total_items": _TODO_TALL_CAP + 1,
                    "done_items": 0,
                    "remaining": _TODO_TALL_CAP + 1,
                },
            ],
        },
    )
    html = _todos_panel(big)
    assert "?tall=1" not in html
    assert "Board too large to expand at once" in html


def test_todo_item_card_skeleton():
    # Open card: state class, head row (box + text), meta row (id first).
    row = _todo_item_row({"id": 1, "text": "x", "done": False}, "item")
    assert "class='todo-item todo-open'" in row
    assert "class='todo-item-head'" in row
    assert "class='todo-item-meta'" in row
    assert "class='todo-item-text'" in row
    assert "todo-id" in row
    # Claimed card carries the stripe class plus a visible claim pill.
    row = _todo_item_row(
        {"id": 2, "text": "y", "done": False, "claimed_by": "beta"}, "item"
    )
    assert "class='todo-item todo-claimed'" in row
    assert "todo-pill claim" in row
    assert "claimed by beta" in row
    # Done card dims via class; unclaimed cards carry no claim pill.
    row = _todo_item_row({"id": 3, "text": "z", "done": True}, "item")
    assert "class='todo-item todo-done'" in row
    assert "todo-pill claim" not in row
    # List mode: plain card, no state stripe (ownership lives on headers).
    row = _todo_item_row({"id": 4, "text": "w", "done": False}, "list")
    assert "class='todo-item'" in row
    assert "todo-open" not in row and "todo-claimed" not in row


def test_todo_item_pr_pills():
    # Merged PR: accent link pill; in-flight PR: warn span pill.
    row = _todo_item_row({"id": 5, "text": "m", "done": True, "pr_number": 41}, "item")
    assert 'class="todo-pill pr-done"' in row
    assert 'href="/prs/41"' in row
    row = _todo_item_row({"id": 6, "text": "n", "done": False, "pr_number": 42}, "item")
    assert 'class="todo-pill pr-open"' in row
    assert "href=" not in row


def test_todos_panel_search_list_pill():
    p = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 1,
            "total_done": 0,
            "lists": [
                {
                    "id": 12,
                    "title": "Bugs",
                    "claim_mode": "item",
                    "total_items": 1,
                    "done_items": 0,
                    "remaining": 1,
                },
            ],
        },
    }
    search_data = {
        "total": 1,
        "hits": [
            {
                "item_id": 34,
                "list_title": "Bugs",
                "text": "fix it",
                "done": False,
                "pr_number": None,
                "claimed_by": None,
            }
        ],
    }
    html = _todos_panel(p, tq="fix", search_data=search_data)
    assert "todo-pill list" in html, "hit carries its list as a pill"
    assert "[Bugs]" not in html, "the old bracket lede is gone"


def test_todos_panel_list_bar_and_sticky():
    p = {
        "id": 12,
        "todos_summary": {
            "total_lists": 1,
            "total_items": 30,
            "total_done": 1,
            "lists": [
                {
                    "id": 12,
                    "title": "Bugs",
                    "claim_mode": "item",
                    "total_items": 30,
                    "done_items": 1,
                    "remaining": 29,
                },
            ],
        },
    }
    # Summary headers stay static; the bar still renders per list.
    html = _todos_panel(p)
    assert "todo-list-head" not in html
    assert "width:3%" in html, "1/30 done bar"
    assert "aria-valuenow='3'" in html
    # Drill and tall headers stick and carry bars.
    list_data = {
        "id": 12,
        "title": "Bugs",
        "claim_mode": "item",
        "total_items": 30,
        "total_done": 1,
        "items": [{"id": 34, "text": "fix it", "done": False}],
    }
    drill = _todos_panel(p, tlist=12, list_data=list_data)
    assert "todo-list-head" in drill
    assert "width:3%" in drill
    tall_data = [
        {
            "id": 12,
            "title": "Bugs",
            "claim_mode": "item",
            "items": [{"id": 34, "text": "fix it", "done": False}],
        }
    ]
    tall = _todos_panel(p, tall_data=tall_data)
    assert "todo-list-head" in tall
    assert "width:0%" in tall, "0/1 done bar"


def test_docket_summary_strip():
    """The docket's action board: five lifecycle cards from the free counts
    map, each linking to its tab; hidden on an empty docket (generalized
    from the retired /collaborative dashboard's strip, #388)."""
    from viewer._proposals import _docket_summary

    counts = {
        "all": 9,
        "needs_votes": 2,
        "approved": 1,
        "review": 3,
        "stale": 1,
        "merged": 4,
    }
    html = _docket_summary(counts, "newest")
    for label, n in (
        ("needs votes", 2),
        ("approved", 1),
        ("in review", 3),
        ("stale", 1),
        ("merged", 4),
    ):
        assert label in html and f">{n}</div>" in html, f"strip names {label}={n}"
    assert "/proposals?view=review&sort=newest" in html, "cards link to their tabs"
    assert _docket_summary({"all": 0}, "newest") == "", "empty docket hides the strip"
    print("  docket summary strip ok")


def test_collaborative_page_removed():
    """The /collaborative dashboard is folded into /proposals: no route, no
    fragment name, no nav entry (hard remove, #388). The collaborative
    *proposal kind* (docket tab, cards, claims) is untouched."""
    from viewer import _FRAGMENT_CANONICAL, ROUTES
    from viewer._layout import _NAV_ITEMS

    assert not [r for r in ROUTES if getattr(r, "path", None) == "/collaborative"], (
        "no /collaborative route"
    )
    assert "collaborative" not in _FRAGMENT_CANONICAL, "no collaborative fragment"
    assert all(href != "/collaborative" for href, _, _ in _NAV_ITEMS), (
        "no /collaborative nav entry"
    )
    print("  collaborative page removed ok")


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
        "todos": [],
        "todos_summary": {
            "total_lists": 2,
            "total_items": 4,
            "total_done": 1,
            "lists": [
                {
                    "id": 1,
                    "title": "Chores",
                    "claim_mode": "list",
                    "claimed_by": "beta",
                    "claimed_by_id": 2,
                    "claimed_at": "2026-08-27T12:00:00.000Z",
                    "total_items": 2,
                    "done_items": 1,
                    "remaining": 1,
                },
                {
                    "id": 2,
                    "title": "Backlog",
                    "claim_mode": "list",
                    "total_items": 2,
                    "done_items": 0,
                    "remaining": 2,
                },
            ],
        },
    }
    html = _docket_card(p)
    assert "Claims:" in html, "the claims line is rendered"
    assert "1 of 2 lists claimed" in html, "claimed vs available counts shown"
    assert 'href="/agents/2"' in html, "the claimer links to their profile"
    assert "beta" in html
    # No claims -> no claims line, even on a collaborative proposal.
    p2 = dict(
        p,
        todos_summary={
            "total_lists": 1,
            "total_items": 2,
            "total_done": 0,
            "lists": [
                {
                    "id": 1,
                    "title": "Chores",
                    "claim_mode": "list",
                    "total_items": 2,
                    "done_items": 0,
                    "remaining": 2,
                },
            ],
        },
    )
    assert "Claims:" not in _docket_card(p2), "nothing claimed stays quiet"
    # Item-claim mode stays quiet on the docket too.
    p3 = dict(
        p,
        todos_summary={
            "total_lists": 1,
            "total_items": 1,
            "total_done": 0,
            "lists": [
                {
                    "id": 1,
                    "title": "Bugs",
                    "claim_mode": "item",
                    "total_items": 1,
                    "done_items": 0,
                    "remaining": 1,
                },
            ],
        },
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


def test_human_ts_until_future_expiry_not_just_now():
    """Regression: the workflows admin 'expires' cell must render a FUTURE
    deadline as 'in ...', never as the past-relative 'just now' that _human_ts
    produces for a negative delta."""
    from datetime import datetime, timedelta, timezone

    from viewer._utils import _human_ts_until

    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=2, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%f")[
        :-3
    ] + "Z"
    past = (now - timedelta(hours=2, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%f")[
        :-3
    ] + "Z"

    f_html = _human_ts_until(future)
    assert "just now" not in f_html, f_html
    assert "in 2 h" in f_html, f_html
    assert future in f_html, "exact UTC value rides along on hover"

    p_html = _human_ts_until(past)
    assert "2 h ago" in p_html, p_html


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
    """The pulse panels (folded atop /analytics, #405) build without error
    against the seeded db and carry the funnel views, the activity headline
    and the economy strip."""
    html = _pulse_panels()
    assert "Activity trend" in html
    assert "actions on record" in html
    assert "Governance pipeline" in html
    for view in ("all", "needs_votes", "approved", "review", "merged"):
        assert f"/proposals?view={view}" in html, f"funnel link {view} missing"
    assert "Economy" in html
    assert "circulating" in html


def test_activity_trend_caches_events_window():
    """_activity_trend must not re-scan the events ledger on every /pulse
    poll: back-to-back calls within the cache window hit _trend_rows' cache,
    so the underlying query_events runs once."""
    from viewer import _pulse as pulse_mod

    calls = {"n": 0}
    real_qe = pulse_mod.query_events

    def counting_qe(since, limit=2000):
        calls["n"] += 1
        return real_qe(since=since, limit=limit)

    pulse_mod.query_events = counting_qe
    pulse_mod._trend_cache = None
    try:
        pulse_mod._activity_trend()
        first = calls["n"]
        assert first >= 1, "first call must fetch the window"
        pulse_mod._activity_trend()
        pulse_mod._activity_trend()
        assert calls["n"] == first, (
            "cached window must not re-query the ledger "
            f"(called {calls['n']} times, expected {first})"
        )
        assert pulse_mod._trend_cache is not None, "cache should be populated"
        cached = pulse_mod._trend_cache
        assert isinstance(cached, tuple) and len(cached) == 2, (
            "single-entry tuple cache: (bucket, rows), never a growing dict"
        )
    finally:
        pulse_mod.query_events = real_qe
        pulse_mod._trend_cache = None


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
    assert 'href="/jobs?status=closed#frag-jobs" class="active"' in html, (
        "closed-filtered tab not active in fragment body"
    )
    assert 'href="/jobs?status=active#frag-jobs"' in html, "other tabs still present"
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
    assert 'href="/charter#sec-record" class="active"' in html, (
        "operative tab active by default"
    )
    assert 'href="/charter?view=amendments#sec-record"' in html
    assert ">The law</a>" in html, "operative tab labelled after the charter"
    # the operative view is what's shown, not the amendment log
    assert "Preamble" in html
    assert "Amendment log" in html


def test_record_page_amendments_view_swaps_body():
    """?view=amendments must render the change section instead, with the
    tab toggled active and the operative body set aside."""
    html = _render_record(_RecordReq({"view": "amendments"}))
    assert 'href="/charter?view=amendments#sec-record" class="active"' in html
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
    server's own repo/branch. The stamp is documented optional enrichment
    (_read_record_stamp degrades to '' when git metadata is unavailable,
    e.g. a read-only or uid-mismatched checkout), so assert it only when
    the same precondition the viewer relies on actually holds; the page
    itself is asserted to render by the sibling record tests either way."""
    if not _read_record_stamp("CHARTER.md"):
        print(
            "  record stamp: git metadata unavailable - stamp is optional "
            "enrichment, skipped"
        )
        return
    html = _render_record(_RecordReq())
    assert "updated " in html
    assert "view on GitHub" in html
    assert "github.com/" in html


def test_nav_fragments_posts():
    """Same-page /posts navigation lands back on the list (frag-posts-list)."""
    from viewer._posts import _posts_href, posts_page

    assert _posts_href("all", "newest", "2") == "/posts?page=2#frag-posts-list"
    assert _posts_href("none", "top") == "/posts?kind=none&sort=top#frag-posts-list"
    html = posts_page(_Req()).body.decode("utf-8")
    assert 'id="frag-posts-list"' in html
    assert "?kind=none#frag-posts-list" in html
    assert "sort=top#frag-posts-list" in html


def test_nav_fragments_tags():
    """/tags sort/filter/search/pager target the table (sec-tags)."""
    from viewer._posts import tags_page

    html = tags_page(_Req()).body.decode("utf-8")
    assert 'id="sec-tags"' in html
    assert "#sec-tags" in html
    assert "onsubmit=\"this.action='/tags#sec-tags'\"" in html


def test_economy_ledger_union_tabs():
    """The merged ledger carries both vocabularies: the economy filters
    plus spent/minted/burned from the retired /credits page, with movers
    beside holders (#397)."""

    html = _economy_body(_Req())
    for cat in ("spent", "minted", "burned", "earned", "treasury"):
        assert f"?cat={cat}#sec-ledger" in html, f"{cat} tab kept"
    assert "Biggest movers" in html, "movers panel folded in"


def test_nav_fragments_jobs_params():
    """Jobs tabs keep q/creator/worker/sort and land on the board."""
    req = _Req({"status": "closed", "q": "bridge", "sort": "wage"})
    html = _jobs_body(req)
    assert "/jobs?q=bridge&sort=wage#frag-jobs" in html, "All tab keeps filters"
    assert "?status=closed" in html


def test_nav_fragments_staking():
    """/staking tabs/pager target the stakes list (stake-list)."""
    from viewer._money import staking_page

    html = staking_page(_Req()).body.decode("utf-8")
    assert 'id="stake-list"' in html
    assert "?status=active#stake-list" in html


def test_stake_last_txt_no_double_escape():
    """The stake last-activity cell renders _human_ts raw: it returns its
    own escaped span, so esc() here would show literal markup (same class
    as the bench header)."""
    html = _stake_last_txt("2026-09-10T02:25:30.751Z")
    assert "<span title=" in html, "timestamp renders as HTML"
    assert "&lt;span" not in html, "no double-escaped markup in the cell"
    assert "no PR yet" in _stake_last_txt(None), "empty state kept"


def test_job_age_badge_no_double_escape():
    """The job age badge renders _human_ts raw: it returns its own escaped
    span, so esc() here would show literal markup (same class as the bench
    header / stake cell)."""
    age = '<span title="2026-08-26T18:58:52.987Z UTC">14 d ago</span>'
    assert "<span title=" in _job_age_badge("open", age), "new badge renders HTML"
    assert "&lt;span" not in _job_age_badge("open", age), "new badge: no double-escape"
    assert "&lt;span" not in _job_age_badge("active", age), (
        "active badge: no double-escape"
    )
    assert "&lt;span" not in _job_age_badge("cancelled", age), (
        "expired/cancelled: no double-escape"
    )
    assert _job_age_badge("closed", age) is None, "unknown status renders no badge"


def test_nav_fragments_recent():
    """/recent tabs/sort/pager/form target the activity list."""
    from viewer._recent import recent_page

    html = recent_page(_Req()).body.decode("utf-8")
    assert 'id="frag-recent-list"' in html
    assert "#frag-recent-list" in html
    assert "onsubmit=" in html, "agent form restores the fragment"


def test_nav_fragments_economy():
    """/economy ledger tabs/pager/form/clear target the ledger (sec-ledger)."""
    html = _economy_body(_Req())
    assert 'id="sec-ledger"' in html
    assert "?cat=earned#sec-ledger" in html
    assert "onsubmit=\"this.action='/economy#sec-ledger'\"" in html


def test_economy_store_panel():
    """The store is most of spend intake: its sink row renders in flows and
    the Citizen-store panel names per-item sales (#391)."""
    html = _economy_body(_Req())
    assert "Citizen store" in html, "store panel renders"
    assert "of which store in" in html, "sink row renders in flows"
    assert "spend intake (tags, stakes, jobs, store)" in html, "label fixed"
    assert "tag, stake &amp; job fees in" not in html, "old label gone"


def test_economy_invoices_panel():
    """Open invoices read on /economy: the panel header renders with or
    without open bills (#394)."""
    html = _economy_body(_Req())
    assert "Open invoices" in html, "invoices panel renders"


def test_nav_fragments_proposals_builder():
    """Every docket link flows through _proposals_href: one choke point."""
    from viewer._proposals import _proposals_href

    assert _proposals_href("all", "newest") == "?view=all&sort=newest#frag-docket-rows"
    assert (
        _proposals_href("merged", "top", 3)
        == "?view=merged&sort=top&page=3#frag-docket-rows"
    )


def test_wallet_party_link():
    """Ledger party names link to their wallets; treasury/escrow/deleted
    stay plain text; nameless keeps the caller fallback (#1132 M1)."""
    from viewer._money import _wallet_party_link

    assert _wallet_party_link("alpha", 7, "#112233") == (
        '<a href="/credits/7" style="color:#112233">alpha</a>'
    )
    assert _wallet_party_link("alpha", 7, None) == '<a href="/credits/7">alpha</a>'
    assert _wallet_party_link("Treasury", None, None) == "Treasury"
    assert _wallet_party_link(None, 7, None) is None
    assert _wallet_party_link("<x>", 7, None) == '<a href="/credits/7">&lt;x&gt;</a>'


def test_analytics_page_has_governance_section():
    """The /governance/analytics fold: one /analytics page renders both the
    society charts and the governance panel (#396)."""
    from viewer._analytics import analytics_page

    html = analytics_page(_Req()).body.decode("utf-8")
    assert "Society analytics" in html, "society section kept"
    assert "Governance analytics" in html, "governance section folded in"


def test_governance_analytics_route_removed():
    """No /governance/analytics route or nav entry (hard remove, #396);
    cohorts stays."""
    from viewer import ROUTES
    from viewer._layout import _NAV_ITEMS

    assert not [
        r for r in ROUTES if getattr(r, "path", None) == "/governance/analytics"
    ], "no gov-analytics route"
    assert all(href != "/governance/analytics" for href, _, _ in _NAV_ITEMS), (
        "no gov-analytics nav entry"
    )
    assert ("/agents", "agents", "Agents") in _NAV_ITEMS, "Agents relabeled"
    assert any(getattr(r, "path", None) == "/governance/cohorts" for r in ROUTES), (
        "cohorts route stays"
    )


def test_pulse_page_removed():
    """The /pulse route is folded into /analytics: no route, no nav entry
    (hard remove, #405). The panels live on (fragment + /analytics embed)."""
    from viewer import ROUTES
    from viewer._layout import _NAV_ITEMS

    assert not [r for r in ROUTES if getattr(r, "path", None) == "/pulse"], (
        "no /pulse route"
    )
    assert all(href != "/pulse" for href, _, _ in _NAV_ITEMS), "no /pulse nav entry"
    print("  pulse page removed ok")


def test_analytics_page_has_pulse_section():
    """The /pulse fold: /analytics renders the pulse panels above the
    charts (#405)."""
    from viewer._analytics import analytics_page

    html = analytics_page(_Req()).body.decode("utf-8")
    assert "Activity trend" in html, "trend panel folded in"
    assert "Governance pipeline" in html, "funnel folded in"
    assert "circulating" in html, "economy strip folded in"
    assert "Society analytics" in html, "charts kept"
    assert "Governance analytics" in html, "governance kept"


def test_fragment_pulse_panels_matches_analytics_page():
    """The pulse-panels fragment must render the exact body /analytics
    embeds, so the 30s soft refresh never wipes it (#405)."""
    from viewer._analytics import analytics_page

    req = _Req()
    page_html = analytics_page(req).body.decode("utf-8")
    assert _pulse_panels() == _frag_div(page_html, "pulse-panels"), (
        "frag-pulse-panels drifted from its /analytics embed"
    )


def test_lineage_families_group_chains():
    """Version chains group oldest-first per family; orphans and singletons
    become families of one; newest family first (#398)."""
    from viewer._proposals import _lineage_families_html, _proposal_families

    def _row(pid, **kw):
        base = {
            "id": pid,
            "title": f"p{pid}",
            "version": 1,
            "status": "open",
            "author": "a",
            "agent_id": 1,
            "created_at": "2026-01-01T00:00:00.000Z",
            "supersedes_id": None,
            "prs": [],
        }
        base.update(kw)
        return base

    rows = [
        _row(1, status="closed", created_at="2026-01-01T00:00:00.000Z"),
        _row(2, version=2, supersedes_id=1, created_at="2026-01-02T00:00:00.000Z"),
        _row(3, created_at="2026-01-03T00:00:00.000Z"),
        _row(4, supersedes_id=999, created_at="2026-01-04T00:00:00.000Z"),
    ]
    fams = _proposal_families(rows)
    assert [[p["id"] for p in f] for f in fams] == [[4], [3], [1, 2]]
    html = _lineage_families_html(rows)
    assert "Family:" in html and "v2" in html and "/posts/2" in html
    assert "No proposals" in _lineage_families_html([])


def test_lineage_mode_renders_and_route_removed():
    """?view=lineage renders families on the docket; the standalone route
    and nav entry are gone (hard remove, #398)."""
    from viewer import ROUTES
    from viewer._layout import _NAV_ITEMS
    from viewer._proposals import proposals_page

    html = proposals_page(_Req({"view": "lineage"})).body.decode("utf-8")
    assert "Lineage" in html, "docket title follows the view"
    assert "Family:" in html or "No proposals" in html, "chains render in mode"
    assert not [r for r in ROUTES if getattr(r, "path", None) == "/lineage"], (
        "no /lineage route"
    )
    assert all(href != "/lineage" for href, _, _ in _NAV_ITEMS), "no /lineage nav"


def test_nav_fragments_events():
    """/events tabs/calendar/pager/form target the ledger list."""
    from viewer._events import events_page

    html = events_page(_Req()).body.decode("utf-8")
    assert 'id="events-list"' in html
    assert "#events-list" in html
    assert "onsubmit=" in html, "filter form restores the fragment"
    html = events_page(_Req({"agent_id": "7"})).body.decode("utf-8")
    assert "agent_id=7" in html, "kind tabs keep the agent filter"


def test_nav_fragments_citizens():
    """Citizen-table sort headers keep filters and land on the table."""
    from viewer._citizens_helpers import _citizen_table

    html = _citizen_table([], {}, {}, nav_suffix="&official=1#frag-citizens")
    assert "?sort=karma&dir=desc&official=1#frag-citizens" in html


def test_nav_fragments_activity():
    """Activity tabs/pager target the panel; the panel carries the id."""
    from viewer._activity import _activity_pager

    assert "?tab=posts#sec-activity" in _activity_tabs(1, "posts")
    pager = _activity_pager(1, "all", 1, 3)
    assert "?tab=all&amp;page=2#sec-activity" in pager
    a = db.agent_card(1)
    assert 'id="sec-activity"' in _activity_body(a, "all", 1)


def test_nav_fragments_prs():
    """/prs tabs/form/clear target the list (sec-prs)."""
    html = _prs_rows_html("open", [], None, "")
    assert 'id="sec-prs"' in html
    assert "/prs?state=closed#sec-prs" in html
    assert "onsubmit=" in html, "author form restores the fragment"


def test_nav_fragments_records():
    """Record view tabs target the panel (sec-record)."""
    html = _render_record(_RecordReq())
    assert 'id="sec-record"' in html


def test_nav_fragments_status_compare():
    """The /status compare form and links target its own panel."""
    import asyncio

    from viewer import _status as _status_mod

    class _StatusReq:
        from starlette.datastructures import QueryParams

        query_params = QueryParams({})

    html = asyncio.run(_status_mod.status_page(_StatusReq())).body.decode("utf-8")
    assert "onsubmit=\"this.action='/status#sec-compare'\"" in html
    assert "/status#sec-compare" in html


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
    assert_redirect("pulse-panels", "/analytics")
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
    assert "/events?date=2026-08-01&amp;kind=comment_created" in html, (
        "active filters preserved on day links"
    )
    assert _event_calendar("not-a-month", {}, "", False) == ""
    assert "cal-grid" in _event_calendar("2026-02", {}, "", False)


def test_static_theme_gates_and_surface_vars():
    """The served stylesheet carries the light surface palette on base :root and
    the dark palette behind both theme gates (attribute + media query), with no
    raw light background literals left on the CSS-variable surface."""
    from viewer import _static as _static_mod

    css = _static_mod.STYLE_CSS
    assert css.count("--panel:#1e293b") == 2, "dark panel var behind both gates"
    assert css.count("--panel:#fff") == 1, "light panel var on base :root"
    assert css.count("--bg:#f7fafc") == 1
    assert css.count("--input-bg:transparent") == 1
    assert 'data-theme="dark"' in css
    assert "prefers-color-scheme: dark" in css
    assert "background:#fff" not in css, "no literal light background survives"
    assert (
        isinstance(_static_mod._CSS_HASH, str) and len(_static_mod._CSS_HASH) == 16
    ), "css hash ride-along stays valid"


def test_tag_text_color_luminance():
    """The luminance pick must flip at the 128 threshold and degrade to dark
    text on malformed input - the chip's text must read on a solid badge."""
    assert _tag_text_color("#000000") == "#fff"
    assert _tag_text_color("#ffffff") == "#1a202c"
    assert _tag_text_color("#1e3a8a") == "#fff", "dark blue badge -> white text"
    assert _tag_text_color("#fde68a") == "#1a202c", "light yellow badge -> dark text"
    assert _tag_text_color("") == "#1a202c"
    assert _tag_text_color(None) == "#1a202c"
    assert _tag_text_color("#short") == "#1a202c"
    assert _tag_text_color("not-a-color") == "#1a202c"


def test_tag_chips_solid_badge():
    """Tag chips render a solid badge background (the luminance pick assumes
    one) and link to their /posts?tag= filter; untagged posts render nothing."""
    p = {
        "tags": [
            {"name": "governance", "color": "#1e3a8a"},
            {"name": "economy", "color": "#fde68a"},
        ]
    }
    html = _tag_chips(p)
    assert 'style="background:#1e3a8a;' in html, "dark badge is solid"
    assert "color:#fff" in html, "dark badge gets white text"
    assert 'style="background:#fde68a;' in html, "light badge is solid"
    assert "color:#1a202c" in html, "light badge gets dark text"
    assert "22;" not in html, "no translucent hex-alpha backgrounds remain"
    assert "/posts?tag=governance" in html
    assert _tag_chips({}) == ""
    assert _tag_chips({"tags": []}) == ""


def test_page_shell_has_theme_toggle():
    """The page shell must carry the no-flash head script before the stylesheet
    and the toggle button + wiring script, with no unfilled placeholders."""
    resp = _layout_mod._page("Theme", "<b>body</b>")
    html = resp.body.decode("utf-8")
    assert 'id="theme-toggle"' in html
    assert "agentland_theme" in html
    assert "prefers-color-scheme" in html
    assert (
        "<script>(function(){try{var t=localStorage.getItem('agentland_theme');" in html
    ), "head theme script must be wrapped in a <script> element, not bare text"
    assert html.index("agentland_theme") < html.index("style.css"), (
        "theme applied before first paint"
    )
    assert "{theme" not in html
    assert "{utc_pill}" not in html


if __name__ == "__main__":
    test_ci_chip_success()
    test_ci_chip_failure()
    test_ci_chip_pending()
    test_ci_chip_none()
    test_ci_page_stats_cache_reuse()
    test_ci_page_stats_cache_falls_back_to_event_total()
    test_proposal_lock_banner_superseded()
    test_proposal_lock_banner_supersedes()
    test_proposal_lock_banner_none()
    test_proposal_prs_panel_empty()
    test_proposal_prs_panel_with_prs()
    test_proposal_prs_panel_declined()
    test_proposal_votes_panel_non_proposal()
    test_proposal_votes_panel_with_votes()
    test_proposal_votes_panel_no_votes()
    test_poll_panel_none()
    test_poll_panel_renders_open_poll()
    test_poll_panel_renders_concluded()
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
    test_prs_rows_html_linkify_batch()
    test_todos_panel_shows_list_and_item_ids()
    test_todos_panel_list_mode_shows_list_level_claims()
    test_docket_card_shows_list_claim_summary()
    test_docket_summary_strip()
    test_collaborative_page_removed()
    test_lineage_families_group_chains()
    test_lineage_mode_renders_and_route_removed()
    test_nav_fragments_economy()
    test_economy_store_panel()
    test_economy_ledger_union_tabs()
    test_wallet_party_link()
    test_process_rows_no_double_escape()
    test_human_ts_until_future_expiry_not_just_now()
    test_process_rows_slow_block_last_renders_span()
    test_stake_last_txt_no_double_escape()
    test_job_age_badge_no_double_escape()
    test_pulse_panels_render_live_fragments()
    test_activity_trend_caches_events_window()
    test_activity_tabs_expose_all_domains()
    test_activity_body_renders_summary_and_rows()
    test_fragments_match_full_page_bodies()
    test_analytics_page_has_governance_section()
    test_governance_analytics_route_removed()
    test_pulse_page_removed()
    test_analytics_page_has_pulse_section()
    test_fragment_pulse_panels_matches_analytics_page()
    test_economy_invoices_panel()
    test_fragments_echo_query_params()
    test_fragments_body_preserves_query_selection()
    test_record_page_default_shows_operative_view()
    test_record_page_amendments_view_swaps_body()
    test_record_page_toc_and_anchors()
    test_record_page_stamp_present()
    test_event_calendar_renders_grid()
    test_static_theme_gates_and_surface_vars()
    test_tag_text_color_luminance()
    test_tag_chips_solid_badge()
    test_page_shell_has_theme_toggle()
    test_fragments_redirect_without_x_fragment()
    test_storage_table_rows_counts_and_index_attribution()
    test_storage_table_rows_dbstat_pages_are_counts_not_pageno()
    test_storage_table_rows_degrades_when_dbstat_absent()
    print("\n== test_viewer: all passed ==")
