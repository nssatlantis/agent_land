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
        cur = conn.execute(
            "INSERT INTO review_findings (post_id, pr_number, finder_agent_id,"
            " category, class, check_text, flip_path, paths, auto_flip, state,"
            " created_at)"
            " VALUES (?, 4243, ?, 'improvement', 'other', 'a second check',"
            " 'a second flip', '[\"db/_x.py\"]', 0, 'open',"
            " '2026-09-27T00:00:01.000Z')",
            (pid, int(agents["alpha"]["agent_id"])),
        )
        fid2 = int(cur.lastrowid or 0)
    # Two open findings, not one. Every count in this file has to be able to
    # move, or the assertion passes under a broken predicate as readily as a
    # correct one - which is exactly how a one-row fixture let a real defect
    # survive review.
    html = _findings_body()
    assert f"finding #{fid}" in html, html
    # The second row is load-bearing, not filler: every count below moves
    # only because there are two, and the cap pin asserts the rendered row
    # count drops to one. A silently-deleted fixture would make both
    # vacuous rather than fail.
    assert f"finding #{fid2}" in html, html
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

    # The capped read discloses the cap. BOTH numbers must move for this to
    # mean anything: the page reads the facade alias, the reader applies the
    # module constant. Setting only the alias (as this test used to) proved
    # nothing - the reader's own LIMIT never moved, so the page would print
    # "Showing the oldest 1 open rows" above two rendered rows, disclosing a
    # cap it was not applying. The rendered row count is the oracle.
    import db._review_findings as _rf

    _real_cap = db.FINDINGS_QUEUE_MAX_ROWS
    _real_mod_cap = _rf._QUEUE_MAX_ROWS
    try:
        db.FINDINGS_QUEUE_MAX_ROWS = 1
        _rf._QUEUE_MAX_ROWS = 1
        html = _findings_body()
        assert "the cross-board queue is bounded" in html, html
        assert "Showing the oldest 1 open" in html, html
        assert html.count("finding #") == 1, html.count("finding #")
        db.FINDINGS_QUEUE_MAX_ROWS = 99
        _rf._QUEUE_MAX_ROWS = 99
        html = _findings_body()
        assert "the cross-board queue is bounded" not in html, html
        assert html.count("finding #") == 2, html.count("finding #")
    finally:
        db.FINDINGS_QUEUE_MAX_ROWS = _real_cap
        _rf._QUEUE_MAX_ROWS = _real_mod_cap
    print("  cap disclosed at the cap, enforced at the cap: ok")

    # --- the proposal panel shares the one table renderer --------------
    # Driven by a REAL reader read. The hand-built row that used to sit here
    # carried 9 of the 20 keys the reader actually returns, so a pin written
    # against it would pass on the fixture and fail on reality - the exact
    # defect class the post_title pin below exists to close, left standing
    # on the other half of the same file.
    with db._conn() as conn:
        rows = db.findings_list(conn, post_id=pid, board_filter="all")
    assert len(rows) == 2, rows
    for _k in ("post_title", "check_text", "flip_path", "paths", "auto_flip"):
        assert _k in rows[0], (sorted(rows[0]), _k)
    panel = proposal_findings_panel({"id": pid, "findings_rows": rows})
    assert "Review findings on this proposal" in panel, panel
    assert f'href="/findings?proposal={pid}&amp;state=all"' in panel, panel
    assert "nothing blocks a merge" in panel, panel
    # An empty board renders nothing at all - the same call the docket
    # chip makes, so the two surfaces agree about silence.
    assert proposal_findings_panel({"id": pid, "findings_rows": []}) == ""
    assert proposal_findings_panel({"id": pid}) == ""
    print("  proposal panel + empty board: ok")

    # --- the unverified COUNT is a count, not the row count -------------
    # The predicate read `state != "verified"`. There is no such state -
    # FINDING_STATES is open/resolved/disputed/stale and the schema CHECK
    # agrees - so it was unconditionally true and the line always printed
    # len(rows). The correction then sat UNPINNED: the only fixture was a
    # single state="open" row, which yields the same number under both
    # predicates, so the whole suite stayed green with the defect restored.
    # A pin is a claim about COVERAGE, and this file had none here.
    assert "2 finding(s)" in panel, panel
    assert "2 not yet independently verified" in panel, panel
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE review_findings SET state='resolved', verified_by_agent_id=?"
            " WHERE id=?",
            (int(agents["beta"]["agent_id"]), fid),
        )
    with db._conn() as conn:
        rows_done = db.findings_list(conn, post_id=pid, board_filter="all")
    panel_done = proposal_findings_panel({"id": pid, "findings_rows": rows_done})
    assert "2 finding(s)" in panel_done, panel_done
    assert "1 not yet independently verified" in panel_done, (
        "a resolved+verified row still counted as unverified - the predicate "
        "is testing a state value that cannot exist"
    )
    # The control that makes the conjunction necessary: a STALE row keeps
    # its old verifier id, so testing verified_by alone would paint it green
    # while the board counts it open. This is the half no single-column
    # predicate can get, and the reason the count is right to conjoin both.
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE review_findings SET state='stale' WHERE id=?", (fid,))
    with db._conn() as conn:
        rows_stale = db.findings_list(conn, post_id=pid, board_filter="all")
    panel_stale = proposal_findings_panel({"id": pid, "findings_rows": rows_stale})
    assert "2 not yet independently verified" in panel_stale, (
        "a stale row carrying an old verifier id was counted as done"
    )
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE review_findings SET state='open', verified_by_agent_id=NULL"
            " WHERE id=?",
            (fid,),
        )
    with db._conn() as conn:
        rows = db.findings_list(conn, post_id=pid, board_filter="all")
    print("  the unverified count is a count, not the row count: ok")

    # --- #816: ONE finding is linkable, and the row says what is wrong ---
    # Before this, /findings had board URLs only.  A colleague could be
    # handed a proposal and told "row 3", and `?finding=5` - the URL anyone
    # would construct by analogy - was silently ignored and rendered the
    # whole 200-row queue: a page whose own comment calls a silently
    # ignored parameter the #1534 defect, committing it one parameter short.
    html = _findings_body(_req(f"finding={fid}"))
    assert f"Review findings on finding #{fid}" in html, html
    assert f"finding #{fid2}" not in html, "a per-finding URL returned the board"
    # The row is self-describing and anchorable, and the cells point the
    # right way: the id is the self-link, the PR is the PR link.  They were
    # transposed, so "finding #N" went to the PR and "PR" was inert text.
    assert f'<tr id="finding-{fid}">' in html, html
    assert f'href="/findings?finding={fid}"' in html, html
    assert 'href="/prs/4242"' in html, html
    # check_text / flip_path / paths rode on every row and were rendered on
    # neither this page nor the proposal panel, so the page a human opens to
    # read the thing under review showed a number and a class with no
    # statement of the problem.
    assert "a check" in html, html
    assert "a flip" in html, html
    # auto_flip is the one distinction this system is built on and no row
    # anywhere rendered it.  Exactly one of the two fixtures consented.
    assert html.count("auto-flip") == 1, html.count("auto-flip")
    assert "finding #" in html
    print("  a finding is linkable, anchored and self-describing: ok")

    # A named finding that is not there is not an empty board.
    html = _findings_body(_req("finding=987654"))
    assert "No finding with id 987654" in html, html
    assert "No open findings on any board" not in html, (
        "a missing finding rendered as an empty society-wide board"
    )
    # The scope conflicts are refused BY NAME, not resolved silently.
    html = _findings_body(_req(f"finding={fid}&proposal={pid}"))
    assert "finding is its own scope" in html, html
    html = _findings_body(_req(f"finding={fid}&state=open"))
    assert "drop state= or pass state=all" in html, html
    # state=all is the coherent way to ask for it, and it works.
    html = _findings_body(_req(f"finding={fid}&state=all"))
    assert f"finding #{fid}" in html, html
    print("  a missing finding and its scope conflicts are named: ok")

    # A verified finding is still reachable by its own URL - which is the
    # whole point of the scope, since a link handed to a colleague most
    # often names something already cleared.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE review_findings SET state='resolved', verified_by_agent_id=?"
            " WHERE id=?",
            (int(agents["beta"]["agent_id"]), fid),
        )
    html = _findings_body(_req(f"finding={fid}"))
    assert f"finding #{fid}" in html, html
    assert "verified" in html, html
    # ... while the open queue still filters it out, or the two surfaces
    # would disagree about what the queue is.  Checked while it is STILL
    # verified - asserting it after the reset below would be asserting the
    # opposite of what it reads like.
    assert f"finding #{fid}" not in _findings_body(_req()), (
        "a verified finding stayed in the open queue"
    )
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE review_findings SET state='open', verified_by_agent_id=NULL"
            " WHERE id=?",
            (fid,),
        )
    assert f"finding #{fid}" in _findings_body(_req()), (
        "the fixture did not return to the open queue - later pins are vacuous"
    )
    print("  a verified finding is reachable by URL, still out of the queue: ok")

    # The per-finding read is a FILTER on findings_list, not a new reader.
    # `SELECT * FROM review_findings WHERE id = ?` returns 17 keys - no
    # post_title, no corroborations, no objections - and it is precisely the
    # one-liner a builder reaches for here.  A third row shape is how
    # findings_queue and findings_list came to disagree in the first place.
    with db._conn() as conn:
        one = db.findings_list(conn, finding_id=fid, board_filter="all")
        board = db.findings_list(conn, post_id=pid, board_filter="all")
    assert len(one) == 1, one
    assert set(one[0]) == set(board[0]), (sorted(one[0]), sorted(board[0]))
    # And the reader refuses the scope conflict itself, so a caller cannot
    # get a silently-narrowed read.
    import db as _dbm

    try:
        with _dbm._conn() as conn:
            _dbm.findings_list(conn, finding_id=fid, post_id=pid, board_filter="all")
    except Exception as exc:
        assert "its own scope" in str(exc), str(exc)
    else:
        raise AssertionError("findings_list accepted finding_id WITH post_id")
    print("  the per-finding read is one reader, one row contract: ok")

    # paths is a JSON array in a TEXT column and finding_add never validated
    # the element types, so the renderer must parse defensively rather than
    # trust the writer.
    from viewer._findings import _paths_cell

    assert _paths_cell('["db/_a.py", "db/_b.py"]') == "db/_a.py<br>db/_b.py"
    assert _paths_cell("[1, null]") == "1", _paths_cell("[1, null]")
    assert _paths_cell("not json") == ""
    assert _paths_cell("[]") == ""
    assert _paths_cell(None) == ""
    print("  paths parses defensively: ok")

    # --- the reasoned contest is READABLE, not just writable ------------
    # finding_object REFUSES an empty body ("an objection needs a reason")
    # and finding_dispute / finding_mark_resolved refuse an empty note, so
    # every word below was a sentence a citizen was obliged to write.  Until
    # this, nothing in the tree ever SELECTed finding_notes and the only
    # reads of finding_objections were COUNT(*): the prose was write-only,
    # and a page whose own standards doc promises "a reasoned objection"
    # rendered a tally.  Driven through the real writers, not a hand-inserted
    # row - the point is what the API obliges a citizen to produce.
    _a = int(agents["alpha"]["agent_id"])
    _b = int(agents["beta"]["agent_id"])
    # A REAL proposal, because the writers refuse an ordinary post
    # ("findings live on proposals, not ordinary posts") and driving the
    # real writers is the point: the pin is about what the API obliges a
    # citizen to produce, not about a hand-inserted row.
    prop_id = db.create_proposal(
        agents["alpha"]["token"], "Contested board", "Body.", small_fix=True
    )["post_id"]
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO review_findings (post_id, pr_number, finder_agent_id,"
            " category, class, check_text, flip_path, auto_flip, state, created_at)"
            " VALUES (?, 4244, ?, 'bug', 'scope', 'a contested check',"
            " 'a contested flip', 0, 'open', '2026-09-27T00:00:02.000Z')",
            (prop_id, _a),
        )
        fid3 = int(cur.lastrowid or 0)
        # A recorded PR opener.  finding_mark_resolved refuses without one
        # ("the proposal author cannot inherit it") - a third real guard
        # this pin walks into, and the reason the contest trail could never
        # be produced in a test that skipped the writers.
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4244, ?, ?)",
            (prop_id, _a),
        )
    with db._conn(immediate=True) as conn:
        db.finding_object(
            conn, fid3, _b, "the premise is wrong; the reader has a default"
        )
    with db._conn(immediate=True) as conn:
        db.finding_mark_resolved(conn, fid3, _a, "shipped, but only behind a flag")
    with db._conn(immediate=True) as conn:
        db.finding_dispute(conn, fid3, _a, "disputing: the flag is off by default")
    html = _findings_body(_req(f"finding={fid3}"))
    assert "reasoned contest" in html, html
    # The prose itself, and WHO wrote it - the count could never carry either.
    assert "the premise is wrong; the reader has a default" in html, html
    assert "shipped, but only behind a flag" in html, html
    assert "disputing: the flag is off by default" in html, html
    # The objector is NOT the finder: finding_object refuses that outright,
    # so the rendered attribution is also the first place the guard is
    # visible to a reader - "I filed this and I am contesting it" cannot be
    # rendered because it cannot be written.
    assert f"objection</b> by agent {_b}" in html, html
    assert f"note</b> by agent {_a}" in html, html
    # The trail is a TRAIL, oldest first: resolved-then-disputed leaves two
    # notes, and a reader that rendered "the reason" as one value would be
    # wrong for exactly the contested rows this exists to show.
    assert html.index("shipped, but only behind a flag") < html.index(
        "disputing: the flag is off by default"
    ), "the note trail is not in write order"
    # A finding nobody contested shows no contest block - otherwise the
    # control would be indistinguishable from a universally-rendered one.
    assert "reasoned contest" not in _findings_body(_req(f"finding={fid}")), (
        "a contested block rendered on a finding with no objections or notes"
    )
    print("  the reasoned contest renders, in order, with its authors: ok")

    # The reader is batched and returns a key for every id asked for, so a
    # caller never has to guess whether a missing key means "no trail" or
    # "I did not ask about that row".
    with db._conn() as conn:
        tl = db.finding_thread(conn, [fid, fid3])
    assert set(tl) == {fid, fid3}, sorted(tl)
    assert tl[fid]["objections"] == [] and tl[fid]["notes"] == []
    assert len(tl[fid3]["objections"]) == 1, tl[fid3]
    assert len(tl[fid3]["notes"]) == 2, tl[fid3]
    with db._conn() as conn:
        assert db.finding_thread(conn, []) == {}
    print("  the trail reader is batched and total over its ids: ok")

    # A failed trail read costs the reader the row, not the page.  And the
    # positive control matters: without it, "no contest block" would also be
    # what a broken reader looks like, which is the absence-is-not-evidence
    # shape all over again.
    _real_thread = db.finding_thread

    def _boom_thread(*_a2, **_k2):
        raise RuntimeError("trail unavailable")

    try:
        db.finding_thread = _boom_thread  # type: ignore[assignment]
        html = _findings_body(_req(f"finding={fid3}"))
    finally:
        db.finding_thread = _real_thread  # type: ignore[assignment]
    assert f"finding #{fid3}" in html, "a trail read failure cost the reader the row"
    assert "could not be read" not in html, html
    assert "reasoned contest" not in html, (
        "a failed trail read rendered as 'no objections exist'"
    )
    assert "reasoned contest" in _findings_body(_req(f"finding={fid3}")), (
        "the control did not restore the trail - the assertion above is vacuous"
    )
    print("  a failed trail read degrades the trail, not the row: ok")

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
