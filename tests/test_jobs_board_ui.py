"""Tests for the jobs-board UI cleanup (proposal #610).

Covers the public /jobs board (strip math, counted tabs, filter form,
card anchors/chips/links, evidence single-render, escrow wording,
bounty collapse, terminal dimming), the /jobs/{id} detail page, and the
list_jobs status/q/sort filters. Card tests call _job_card with plain
dicts (no DB); board tests run against the throwaway DB."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_jobs_board_ui_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402
from viewer._money import (  # noqa: E402
    _bounty_desc_collapsed,
    _job_card,
    _jobs_body,
    job_detail_page,
)
from viewer._utils import _evidence_has_more  # noqa: E402

AGENTS, _ = setup()


class _Req:
    """Minimal Request stand-in - real QueryParams plus path_params."""

    def __init__(self, params=None, path_params=None):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(params or {})
        self.path_params = path_params or {}


def _job(**over):
    base = {
        "job_id": 7,
        "title": "t",
        "description": "",
        "status": "open",
        "kind": "one_time",
        "payment_credits": "0.25",
        "payment_units": 5,
        "cycles_done": 0,
        "total_cycles": 1,
        "cycle_every_days": 1,
        "created_at": None,
        "official": False,
        "long_running": False,
        "auto_pay_on_merge": False,
        "service_id": None,
        "scope": "",
        "steps": [],
        "cycles": [],
        "overdue": False,
        "creator": None,
        "worker": None,
        "offered_to": None,
    }
    base.update(over)
    return base


def _seed_job(title, status="open", payment_units=5, scope=None):
    with db._conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (title, payment_units, total_cycles,"
            " status, scope) VALUES (?, ?, 1, ?, ?)",
            (title, payment_units, status, scope),
        )
        return int(cur.lastrowid or 0)


def test_strip_counts_closed_and_sums():
    """The strip names all four states so it sums to the printed total."""
    import re as _re

    _seed_job("jbui strip probe", status="open")
    html = _jobs_body(_Req())
    m = _re.search(
        r"(\d+) open.*?(\d+) in progress.*?(\d+) completed.*?(\d+) closed", html
    )
    assert m, "strip names open/in-progress/completed/closed"
    t = _re.search(r"Page \d+ of \d+ \u00b7 (\d+) jobs", html)
    assert t, "total line present"
    assert sum(int(g) for g in m.groups()) == int(t.group(1)), (
        "strip states sum to the printed total"
    )


def test_officials_slim_panel_gone():
    """The duplicate unlinked Officials panel no longer renders."""
    html = _jobs_body(_Req())
    assert "Officials</h3>" not in html


def test_tabs_carry_counts_and_form_renders():
    """Tabs show per-tab counts; the visible filter form keeps q/sort."""
    import re as _re

    html = _jobs_body(_Req())
    assert "Open <span" in html
    assert _re.search(r"\(\d+\)</span>", html), "tabs carry counts"
    assert "name='q'" in html
    assert "name='sort'" in html
    assert "top wage" in html


def test_card_anchor_and_no_cycle_fragment():
    """Cards anchor by id; the meta line drops the duplicate cycle x/y."""
    html = _job_card(_job())
    assert "id='job-7'" in html
    assert "cycle 1/1" not in html
    assert "border-left:4px solid" in html


def test_scope_links_to_bug():
    """A bugs/<id> scope links to its bug page; other scopes stay plain."""
    html = _job_card(_job(scope="bugs/12"))
    assert "href='/bugs/12'" in html
    plain = _job_card(_job(scope="HISTORY.md"))
    assert "href='/bugs" not in plain
    assert "HISTORY.md" in plain


def test_service_and_autopay_chips():
    """Service orders name their listing; merge-payout jobs say so."""
    html = _job_card(_job(service_id=3))
    assert "href='/services/3'" in html
    html = _job_card(_job(auto_pay_on_merge=True))
    assert "auto-pay on merge" in html
    html = _job_card(_job())
    assert "auto-pay on merge" not in html


def test_evidence_renders_once():
    """Parsed PR evidence shows the chip alone; raw text only when unparsed."""
    cycle = {
        "cycle_no": 1,
        "status": "accepted",
        "submitted_at": None,
        "decided_at": None,
        "evidence": "#PR99",
        "evidence_pr_numbers": [99],
        "evidence_pr_shas": [None],
        "feedback": None,
        "opens_at": None,
    }
    html = _job_card(_job(cycles=[cycle]))
    assert "/prs/99" in html
    assert html.count("#PR99") == 1, "chip and raw text must not duplicate"
    raw = dict(cycle, evidence_pr_numbers=[])
    html = _job_card(_job(cycles=[raw]))
    assert "evidence #PR99" in html
    prose = dict(cycle, evidence="#PR99, logs at https://example.invalid/x")
    html = _job_card(_job(cycles=[prose]))
    assert html.count("#PR99") == 2, "chip plus residual prose"
    assert "https://example.invalid/x" in html
    assert _evidence_has_more("#PR99") is False
    assert _evidence_has_more("#PR99, logs attached") is True


def test_escrow_wording_follows_funding():
    """Officials read treasury-escrowed; citizen jobs read escrow held."""
    html = _job_card(_job(official=True))
    assert "treasury-escrowed:" in html
    html = _job_card(_job(official=False))
    assert "escrow held:" in html


def test_bounty_desc_collapsed():
    """Auto-generated bounty text collapses; ordinary text does not."""
    job = _job(
        official=True,
        title="Bounty: fix bug #12 - bad predicate",
        description="Confirmed bug #12 (confidence 3): bad predicate. Fix it.",
    )
    assert _bounty_desc_collapsed(job) is True
    assert "full bounty text" in _job_card(job)
    assert (
        _bounty_desc_collapsed(_job(official=False, title="Bounty: fix bug #12"))
        is False
    )
    assert _bounty_desc_collapsed(_job(title="plain title")) is False


def test_completed_card_dims_and_collapses_steps():
    """Terminal cards dim with a collapsed checklist; open cards stay flat."""
    steps = [{"id": 1, "position": 1, "text": "do it", "done": True}]
    html = _job_card(_job(status="completed", cycles_done=1, steps=steps))
    assert "opacity:0.85" in html
    assert "checklist</summary>" in html
    assert "cycle history" not in html
    html = _job_card(_job(status="open", steps=steps))
    assert "opacity:0.85" not in html
    assert "<ol style=" in html


def test_cycle_history_renamed():
    """The dots legend reads cycle history, not health timeline."""
    cycle = {
        "cycle_no": 1,
        "status": "accepted",
        "submitted_at": None,
        "decided_at": None,
        "evidence": "",
        "evidence_pr_numbers": [],
        "evidence_pr_shas": [],
        "feedback": None,
        "opens_at": None,
    }
    html = _job_card(_job(cycles=[cycle]))
    assert "cycle history" in html
    assert "health timeline" not in html


def test_detail_page_404_and_route():
    """Unknown ids 404; the route is registered beside /jobs."""
    from viewer import ROUTES

    assert any(getattr(r, "path", "") == "/jobs/{job_id:int}" for r in ROUTES), (
        "detail route registered"
    )
    resp = job_detail_page(_Req(path_params={"job_id": 999999}))
    assert resp.status_code == 404
    assert "No such job" in resp.body.decode("utf-8")
    resp = job_detail_page(_Req())
    assert resp.status_code == 404


def test_list_jobs_status_filter():
    """status narrows to one tab; unknown statuses refuse loudly."""
    _seed_job("jbui completed one", status="completed")
    rows = db.list_jobs(view="all", status="completed")["jobs"]
    assert rows, "seeded row returned"
    assert all(j["status"] == "completed" for j in rows)
    msg = expect_error(db.list_jobs, view="all", status="bogus")
    assert "status must be one of" in msg


def test_list_jobs_q_and_sort():
    """q matches title/scope case-insensitively; sort=wage pays first."""
    _seed_job("jbui Bridge audit", scope="bugs/91", payment_units=5)
    _seed_job("jbui Chronicle notes", payment_units=5)
    _seed_job("jbui wage low", payment_units=500)
    _seed_job("jbui wage high", payment_units=900)
    rows = db.list_jobs(view="all", q="BRIDGE")["jobs"]
    assert [j["title"] for j in rows] == ["jbui Bridge audit"]
    rows = db.list_jobs(view="all", q="bugs/91")["jobs"]
    assert [j["title"] for j in rows] == ["jbui Bridge audit"]
    rows = db.list_jobs(view="all", q="100%")["jobs"]
    assert rows == [], "wildcard chars are escaped, not patterns"
    rows = db.list_jobs(view="all", q="jbui wage", sort="wage")["jobs"]
    assert [j["title"] for j in rows] == ["jbui wage high", "jbui wage low"]
    rows = db.list_jobs(view="open", status="completed")["jobs"]
    assert rows == [], "view and status intersect"
    msg = expect_error(db.list_jobs, view="all", sort="bogus")
    assert "sort must be" in msg


if __name__ == "__main__":
    test_strip_counts_closed_and_sums()
    test_officials_slim_panel_gone()
    test_tabs_carry_counts_and_form_renders()
    test_card_anchor_and_no_cycle_fragment()
    test_scope_links_to_bug()
    test_service_and_autopay_chips()
    test_evidence_renders_once()
    test_escrow_wording_follows_funding()
    test_bounty_desc_collapsed()
    test_completed_card_dims_and_collapses_steps()
    test_cycle_history_renamed()
    test_detail_page_404_and_route()
    test_list_jobs_status_filter()
    test_list_jobs_q_and_sort()
    print("\n== test_jobs_board_ui: all passed ==")
