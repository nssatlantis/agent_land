"""Test pins for the /findings union view (proposal #776, leg D3b).

Pins: the route is registered; the page renders an explicit empty state
when no board has an open row; a real open finding renders with its board
and PR scope plus its provenance; and a failed board read degrades to a
visible notice rather than a 500 or a silent blank.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_findings_page_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from tests._setup import setup  # noqa: E402


def main():
    agents, pid = setup()

    from viewer import ROUTES
    from viewer._findings import _findings_body

    # --- the route exists: a page nothing links to is a page nobody finds ---
    paths = [getattr(r, "path", None) for r in ROUTES]
    assert "/findings" in paths, "/findings route is not registered"
    print("  route registered: ok")

    # --- #816: the route is REGISTERED and must also WORK --------------
    # The pin above is the shallowest check available - the route is in a
    # list. It stayed green for a whole merged PR lifetime while the
    # handler raised on every request, because every other pin in this
    # file calls _findings_body() and never the handler. So call it.
    from starlette.requests import Request
    from starlette.responses import HTMLResponse

    from viewer._findings import findings_page

    _scope: dict = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/findings",
        "raw_path": b"/findings",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "app": None,
    }
    resp = findings_page(Request(_scope))
    assert isinstance(resp, HTMLResponse), type(resp)
    assert resp.status_code == 200, resp.status_code
    _page_html = bytes(resp.body).decode("utf-8", "replace")
    assert "Open findings queue" in _page_html, _page_html[:400]
    # The discriminating half: _findings_body() alone carries no nav, so
    # this asserts the PAGE composed rather than that nothing raised.
    # /proposals is in _NAV_ITEMS and the findings table links only to
    # /prs/{n} and /posts/{n}, so this href can only come from _page()'s
    # frame - which is exactly the layer that was broken.
    assert 'href="/proposals"' in _page_html, "the page frame did not render"
    print("  handler returns a real page: ok")

    # --- empty state: an empty board is not an invisible board ---
    html = _findings_body()
    assert "Open findings queue" in html, html
    assert "No open findings" in html, html
    print("  empty state: ok")

    # --- a real open row renders with its scope and its provenance ---
    pid = int(pid)
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO review_findings (post_id, pr_number, finder_agent_id,"
            " category, class, check_text, flip_path, auto_flip, state,"
            " created_at)"
            " VALUES (?, 4242, ?, 'bug', 'wire-shape', 'a check', 'a flip', 1,"
            " 'open', '2026-09-27T00:00:00.000Z')",
            (pid, int(agents["beta"]["agent_id"])),
        )
        fid = int(cur.lastrowid or 0)
    html = _findings_body()
    assert f"finding #{fid}" in html, html
    assert "#4242" in html, html
    assert f"/posts/{pid}" in html, html
    assert f"filed by agent {int(agents['beta']['agent_id'])}" in html, html
    assert "No open findings" not in html, html
    # #1523 review: the Filed column was double-escaped.  _human_ts already
    # returns escaped HTML markup, so wrapping it in esc() printed the span
    # as literal text on every row.  These are the house assertions for that
    # class (see test_process_rows_no_double_escape in tests/test_viewer.py) -
    # without them the defect is invisible to CI.
    assert "<span title=" in html, html
    assert "&lt;span" not in html, "the Filed column double-escaped the markup"
    # and the state badge must be coloured, not bare text
    assert ">open</span>" in html, html
    print("  open row + provenance: ok")

    # --- a failed read is visible, not a 500 and not a silent blank ---
    real_conn = db._conn
    try:

        def _boom(*_a, **_k):
            raise RuntimeError("board unavailable")

        db._conn = _boom  # type: ignore[assignment]
        html = _findings_body()
    finally:
        db._conn = real_conn  # type: ignore[assignment]
    assert "could not be read" in html, html
    # #1523 finding 11: the notice alone was true before AND after the fix,
    # so it could never fail.  The count line is the discriminating half - a
    # degraded read must NOT print a positive count of the very thing it
    # just failed to read.  This is the assertion whose absence let the
    # defect survive three instrumented iterations.
    assert "open finding(s) across every board" not in html, html
    assert "0 open finding" not in html, html
    print("  degraded read: ok")

    # positive control: the SAME page on a working read DOES print the count
    # (real_conn was restored by the finally above and the fixture row is
    # still on the board), so the assertion above discriminates rather than
    # passing vacuously.
    _healthy = _findings_body()
    assert "open finding(s) across every board" in _healthy, _healthy
    print("  count line is discriminating: ok")

    # --- #816: the URL answers all three scopes ------------------------
    from viewer._findings import proposal_findings_panel

    def _req(qs: str = ""):
        return Request({**_scope, "query_string": qs.encode()})

    html = _findings_body(_req(f"proposal={pid}"))
    assert f"finding #{fid}" in html, html
    assert f"Review findings on proposal #{pid}" in html, html

    html = _findings_body(_req("pr=4242"))
    assert "Review findings on PR #4242" in html, html

    # A scope the reader cannot have meant is refused BY NAME, and renders
    # no table - a refusal that still prints rows is a wrong answer.
    html = _findings_body(_req("proposal=abc"))
    assert "proposal must be a whole number" in html, html
    assert "abc" in html, html
    assert "<table>" not in html, "a refused scope must render no table"

    html = _findings_body(_req("proposal=1&pr=2"))
    assert "different scopes" in html, html

    html = _findings_body(_req("state=bogus"))
    assert "state must be open, closed or all" in html, html

    # ?state=closed with no scope SAYS SO rather than answering the open
    # question under a closed-looking URL - the #1534 shape, where a
    # silently-ignored parameter has the reader believing something the
    # server never said.
    html = _findings_body(_req("state=closed"))
    assert "needs a scope" in html, html
    assert "open finding(s) across every board" in html, html
    print("  three scopes + refusals + no-silent-ignore: ok")

    # The capped read discloses the cap. Lowering the module constant is
    # the only way to reach the branch without seeding 200 rows, and it
    # proves the page READS the cap rather than carrying its own copy -
    # the second-copy-of-the-number defect. The second arm is the control:
    # under the cap the note must be absent, or the first arm proves
    # nothing.
    _real_cap = db.FINDINGS_QUEUE_MAX_ROWS
    try:
        db.FINDINGS_QUEUE_MAX_ROWS = 1
        html = _findings_body()
        assert "the cross-board queue is bounded" in html, html
        assert "Showing the oldest 1 open" in html, html
        db.FINDINGS_QUEUE_MAX_ROWS = 99
        html = _findings_body()
        assert "the cross-board queue is bounded" not in html, html
    finally:
        db.FINDINGS_QUEUE_MAX_ROWS = _real_cap
    print("  cap disclosed at the cap, silent below it: ok")

    # --- the proposal panel shares the one table renderer --------------
    rows = [
        {
            "id": fid,
            "post_id": pid,
            "pr_number": 4242,
            "category": "bug",
            "class": "other",
            "state": "open",
            "finder_agent_id": int(agents["beta"]["agent_id"]),
            "created_at": "2026-09-27T00:00:00.000Z",
            "post_title": "a board",
        }
    ]
    panel = proposal_findings_panel({"id": pid, "findings_rows": rows})
    assert "Review findings on this proposal" in panel, panel
    assert f'href="/findings?proposal={pid}&amp;state=all"' in panel, panel
    assert "nothing blocks a merge" in panel, panel
    # An empty board renders nothing at all - the same call the docket
    # chip makes, so the two surfaces agree about silence.
    assert proposal_findings_panel({"id": pid, "findings_rows": []}) == ""
    assert proposal_findings_panel({"id": pid}) == ""
    print("  proposal panel + empty board: ok")

    # --- the SCOPED reader carries the board title (found in review) ----
    # The panel and both scoped URLs render a Board cell from
    # r.get("post_title"). findings_queue joined posts; findings_list did
    # not, so every scoped row rendered "(board proposal not readable)"
    # while linking to /posts/{id} - the page the reader was already on.
    # The pin that should have caught it asserted a HAND-BUILT fixture
    # carrying post_title, which no caller ever supplied: a fixture
    # asserting a shape the reader never produces. So pin the READER,
    # which is where the defect lived and what every renderer depends on.
    with db._conn() as conn:
        scoped = db.findings_list(conn, post_id=pid, board_filter="all")
    assert scoped, "the fixture row vanished"
    assert all("post_title" in r for r in scoped), scoped
    assert any(r.get("post_title") for r in scoped), scoped
    with db._conn() as conn:
        scoped_pr = db.findings_list(conn, pr_number=4242, board_filter="all")
    assert scoped_pr, "the per-PR scope returned nothing for the fixture PR"
    assert all("post_title" in r for r in scoped_pr), scoped_pr
    # And the two readers must agree on shape, or the one shared renderer
    # is being served two different row contracts.
    with db._conn() as conn:
        queued = db.findings_queue(conn)
    assert set(queued[0]) == set(scoped[0]), (sorted(queued[0]), sorted(scoped[0]))
    print("  scoped reader carries the board title: ok")

    # The disclosed cap and the enforced cap are the same number: the page
    # reads the facade alias, the reader's default is the module constant.
    # Asserted rather than assumed - a second home for 200 would let the
    # page disclose a limit the reader does not apply.
    import db._review_findings as _rf

    assert db.FINDINGS_QUEUE_MAX_ROWS == _rf._QUEUE_MAX_ROWS
    print("  the disclosed cap is the enforced cap: ok")

    print("test_findings_page: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
