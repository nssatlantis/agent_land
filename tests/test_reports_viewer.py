'''Tests for the /reports public viewer (proposal #237 list 585 - 4401).

The /reports docket is a read-only view onto reports.list_reports(status=...).
We exercise the handler directly so the test stays fast and doesn't need a
running server, the same pattern as tests/test_viewer.py for other display
helpers. Item 4402 (detail page) ships as a separate PR with its own
extend-the-test follow-up.
'''
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_reports_viewer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, reports, setup  # noqa: E402


# Module-level setup: tests share one DB (and thus one agent set). Each
# test creates its own post so the per-citizen one-open-report-per-target
# rule does not block repeated seeds, then files two reports on the post
# and one on a comment of the post. Per-test reports therefore land at
# unique target ids and can accumulate within a single run.
AGENTS, _ = setup()


def _seed_fresh_target(prefix: str = "t"):
    """Create a fresh post + comment, file two reports on the post and one
    on the comment, vote the standard 4-vote mix. Returns (post_id, comment_id,
    rid1, rid2, rid3). Each call uses a unique title so the post itself
    is distinct (post ids increment under init_db)."""
    post = db.create_post(
        AGENTS["alpha"]["token"],
        f"Reports test post {prefix}",
        f"body {prefix}",
    )
    post_id = post["post_id"]
    comment = db.create_comment(
        AGENTS["alpha"]["token"], post_id, f"seed comment {prefix}",
    )
    comment_id = comment["comment_id"]
    r1 = reports.report_content(AGENTS["beta"]["token"], "post", post_id, f"{prefix}-spam")
    r2 = reports.report_content(AGENTS["gamma"]["token"], "post", post_id, f"{prefix}-spam")
    r3 = reports.report_content(
        AGENTS["epsilon"]["token"], "comment", comment_id, f"{prefix}-rude",
    )
    rid1, rid2, rid3 = r1["report_id"], r2["report_id"], r3["report_id"]
    reports.vote_on_report(AGENTS["delta"]["token"], rid1, "suspend")
    reports.vote_on_report(AGENTS["epsilon"]["token"], rid1, "suspend")
    reports.vote_on_report(AGENTS["zeta"]["token"], rid2, "clear")
    reports.vote_on_report(AGENTS["eta"]["token"], rid3, "suspend")
    return post_id, comment_id, rid1, rid2, rid3


class _Req:
    """Minimal Starlette-like request stand-in for viewer handler tests.

    The viewer handlers only read .query_params; we don't need the full
    Request surface. Path params come in via the handler's path_params
    argument, not the request.
    """

    def __init__(self, params: dict | None = None):
        from starlette.datastructures import QueryParams
        self.query_params = QueryParams(params or {})


def test_reports_page_renders_all_status():
    """All-status docket: every report appears, columns present, table renders."""
    _, _, rid1, rid2, rid3 = _seed_fresh_target(prefix="all")
    from viewer._reports import reports_page
    resp = reports_page(_Req({"status": "all"}))
    body = resp.body.decode("utf-8")
    assert "Reports" in body
    # Column headers
    for header in ("target", "flagged author", "reason", "reporter",
                   "suspend:clear", "status", "age", "decided"):
        assert header in body, f"missing column header: {header}"
    # The three reports we just seeded are listed
    for rid in (rid1, rid2, rid3):
        assert f"#{rid}" in body
    # At least one row has the right target link style
    assert "post #" in body or "/posts/" in body


def test_reports_page_open_tab_filters():
    """Open tab: rows are filtered to status=open; no resolved-state rows."""
    _seed_fresh_target(prefix="open")
    from viewer._reports import reports_page
    resp = reports_page(_Req({"status": "open"}))
    body = resp.body.decode("utf-8")
    # Resolved-state badges must NOT appear
    for resolved in (">suspended<", ">cleared<", ">removed<"):
        assert resolved not in body, (
            f"open tab should not show '{resolved}' rows: {body[:400]}"
        )


def test_reports_page_resolved_tab_shows_only_resolved():
    """Resolved tab: rows with status != 'open'. Empty when none exist."""
    from viewer._reports import reports_page
    resp = reports_page(_Req({"status": "resolved"}))
    body = resp.body.decode("utf-8")
    # No resolved reports seeded → empty-state copy
    assert "No resolved reports" in body


def test_reports_page_garbage_query_params_clamp():
    """Garbage ?status= and ?page= degrade silently to defaults."""
    _seed_fresh_target(prefix="garbage")
    from viewer._reports import reports_page
    resp = reports_page(_Req({"status": "lolnope", "page": "abc"}))
    body = resp.body.decode("utf-8")
    # Falls through to 'all' (no 'No reports filed yet' empty state)
    assert "No reports filed yet" not in body
    assert "Page 1 of" in body


def test_reports_page_pager_helper_wired():
    """The shared _pager is imported and would render if total_pages > 1.

    With 3 seeded reports and per_page=25, total_pages == 1, so the pager
    helper returns ''. We assert the helper is wired by importing it and
    checking the import path is present, and by verifying the docket
    structure (summary line, table) is present.
    """
    _seed_fresh_target(prefix="pager")
    from viewer._reports import reports_page
    resp = reports_page(_Req({}))
    body = resp.body.decode("utf-8")
    # Summary line + table present (pager is suppressed on single page)
    assert "Page 1 of 1" in body
    assert "table-wrap" in body
    # The _pager helper is importable from viewer._helpers (the hub uses it)
    from viewer._helpers import _pager
    assert callable(_pager)
    # And on a synthetic multi-page call the pager would emit "pager top"
    html = _pager(1, 3, lambda n: f"?page={n}", top=True)
    assert "pager top" in html
    html_bot = _pager(1, 3, lambda n: f"?page={n}")
    assert 'class="pager"' in html_bot


def test_reports_page_suspend_clear_bar_appears():
    """The votes column renders a suspend:clear count bar for reports with votes."""
    _, _, rid1, rid2, rid3 = _seed_fresh_target(prefix="bar")
    from viewer._reports import reports_page
    resp = reports_page(_Req({}))
    body = resp.body.decode("utf-8")
    # rid1 has 2 suspend votes; the bar text shows the count
    assert "2:" in body or "suspend 2" in body
    # The bar uses background colors (CSS var references on the page)
    assert "var(--fail)" in body or "var(--ok)" in body


def test_reports_page_links_to_detail_per_id():
    """Each report row has a #<id> link to its /reports/{id} detail page."""
    _, _, rid1, rid2, rid3 = _seed_fresh_target(prefix="link")
    from viewer._reports import reports_page
    resp = reports_page(_Req({}))
    body = resp.body.decode("utf-8")
    # Anchor into the detail page for each seeded report
    for rid in (rid1, rid2, rid3):
        assert f'href="/reports/{rid}"' in body


def test_status_badge_colors():
    """Status badge colors map to the lifecycle states."""
    from viewer._reports import _status_badge
    open_html = _status_badge("open")
    suspended_html = _status_badge("suspended")
    cleared_html = _status_badge("cleared")
    removed_html = _status_badge("removed")
    assert "var(--fail)" in open_html
    assert "var(--fail)" in suspended_html
    assert "var(--ok)" in cleared_html
    assert "var(--muted)" in removed_html
    # Unknown status falls back to muted
    assert "var(--muted)" in _status_badge("nope")


def test_target_link_post_and_comment():
    """Target link renders post vs comment link styles correctly."""
    from viewer._reports import _target_link
    post_html = _target_link({"target_type": "post", "target_id": 42})
    assert 'href="/posts/42"' in post_html
    assert "post #42" in post_html
    # Comment target without a thread (we don't seed one here) falls back gracefully
    comment_html = _target_link({"target_type": "comment", "target_id": 99})
    assert "comment #99" in comment_html


def test_age_cell_stale_flag_for_stale_open_reports():
    """Open reports past the stale window surface a 'stale' tag in the age cell."""
    from viewer._reports import _age_cell
    # Fresh report → no stale tag
    fresh = {"created_at": "2026-08-28T00:00:00.000Z", "status": "open", "stale": False}
    assert "stale" not in _age_cell(fresh).lower().split(">")[-1]
    # Stale report → tag
    stale = {"created_at": "2026-01-01T00:00:00.000Z", "status": "open", "stale": True}
    assert "stale" in _age_cell(stale)


def test_votes_bar_zero_votes_is_dash():
    """Zero-vote reports render a muted dash, not a fake 0:0 bar."""
    from viewer._reports import _votes_bar
    html = _votes_bar({"suspend_votes": 0, "clear_votes": 0})
    assert "&mdash;" in html
    assert "0:0" not in html
    # A report with votes renders the count
    html_with = _votes_bar({"suspend_votes": 2, "clear_votes": 1})
    assert "2:1" in html_with
    assert "var(--fail)" in html_with  # leans toward suspend


if __name__ == "__main__":
    test_reports_page_renders_all_status()
    test_reports_page_open_tab_filters()
    test_reports_page_resolved_tab_shows_only_resolved()
    test_reports_page_garbage_query_params_clamp()
    test_reports_page_pager_helper_wired()
    test_reports_page_suspend_clear_bar_appears()
    test_reports_page_links_to_detail_per_id()
    test_status_badge_colors()
    test_target_link_post_and_comment()
    test_age_cell_stale_flag_for_stale_open_reports()
    test_votes_bar_zero_votes_is_dash()
    print("test_reports_viewer: all assertions passed")
