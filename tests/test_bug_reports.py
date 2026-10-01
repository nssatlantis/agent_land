"""
tests/test_bug_reports.py — bug report system tests.

file_bug_report, confidence tracking, #B references, viewer helpers.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bug_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db._bug_reports as bug_mod  # noqa: E402
from tests._setup import db, init, setup  # noqa: E402


def test_file_and_get(helpers):
    reporter = helpers["alpha"]
    r = bug_mod.file_bug_report(
        reporter["token"],
        "Login 500",
        "Server crashes on login",
        "https://example.com/login",
    )
    assert r["id"] >= 1
    assert r["title"] == "Login 500"
    assert r["body"] == "Server crashes on login"
    assert r["url"] == "https://example.com/login"
    assert r["confidence"] == 1
    assert r["status"] == "open"
    assert r["duplicate_of"] is None
    assert r["new_confidence"] == 1
    full = bug_mod.get_bug_report(r["id"])
    assert full["id"] == r["id"]
    assert full["reporter_name"] == reporter["name"]
    assert full["duplicates"] == []
    assert full["linked_proposals"] == []
    print("  file_and_get: ok")


def test_duplicate_raises_confidence(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    gamma = helpers["gamma"]
    r1 = bug_mod.file_bug_report(
        alpha["token"], "Login 500", "crash", "https://example.com/login-dup"
    )
    assert r1["confidence"] == 1
    r2 = bug_mod.file_bug_report(
        beta["token"], "Login broken", "500 error", "https://example.com/login-dup"
    )
    assert r2["duplicate_of"] == r1["id"]
    assert r2["confidence"] == 1
    full = bug_mod.get_bug_report(r1["id"])
    assert full["confidence"] == 2
    assert len(full["duplicates"]) == 1
    r3 = bug_mod.file_bug_report(
        gamma["token"], "Login fail", "500", "https://example.com/login-dup"
    )
    assert r3["duplicate_of"] == r1["id"]
    assert r3["confidence"] == 1
    full2 = bug_mod.get_bug_report(r1["id"])
    assert full2["confidence"] == 3
    print("  duplicate raises confidence: ok")


def test_threshold_confirms(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    gamma = helpers["gamma"]
    r = bug_mod.file_bug_report(
        alpha["token"], "DB lock", "sqlite locked", "https://example.com/db-lock"
    )
    bug_mod.file_bug_report(
        beta["token"], "DB lock dup", "locked", "https://example.com/db-lock"
    )
    bug_mod.file_bug_report(
        gamma["token"], "DB lock dup2", "locked", "https://example.com/db-lock"
    )
    full = bug_mod.get_bug_report(r["id"])
    assert full["confidence"] == 3
    assert full["status"] == "confirmed"
    # The auto-confirm crossing stamps decided_at and the confirm event,
    # just like admin confirm_bug_report does (4329).
    assert full["decided_at"] is not None
    with db._conn() as conn:
        ev = conn.execute(
            "SELECT 1 FROM events WHERE kind = ? AND target_type = 'bug_report'"
            " AND target_id = ?",
            (bug_mod.EVT_BUG_CONFIRMED, r["id"]),
        ).fetchone()
    assert ev is not None
    print("  threshold confirms: ok")


def test_different_urls_no_duplicate(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    r1 = bug_mod.file_bug_report(
        alpha["token"], "Login 500", "crash", "https://example.com/login-diff"
    )
    r2 = bug_mod.file_bug_report(
        beta["token"], "Signup 500", "crash", "https://example.com/signup-diff"
    )
    assert r1["id"] != r2["id"]
    assert bug_mod.get_bug_report(r1["id"])["confidence"] == 1
    assert bug_mod.get_bug_report(r2["id"])["confidence"] == 1
    print("  different urls no duplicate: ok")


def test_fixed_bug(helpers):
    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Typo", "fix me", None)
    karma_before = db.whoami(alpha["token"])["karma"]
    assert bug_mod.fix_bug_report(r["id"])["status"] == "fixed"
    assert bug_mod.get_bug_report(r["id"])["status"] == "fixed"
    karma_after = db.whoami(alpha["token"])["karma"]
    assert karma_after == karma_before + 1, (
        f"fixing a bug report credits +1 karma: {karma_before} -> {karma_after}"
    )
    print("  fixed bug: ok")


def test_list_filter(helpers):
    alpha = helpers["alpha"]
    before = bug_mod.list_bug_reports()
    before_count = before["total"]
    bug_mod.file_bug_report(alpha["token"], "Bug A", "body", None)
    bug_mod.file_bug_report(alpha["token"], "Bug B", "body", None)
    all_reports = bug_mod.list_bug_reports()
    assert all_reports["total"] == before_count + 2
    assert all_reports["reports"][0]["title"] == "Bug B"
    assert all_reports["reports"][1]["title"] == "Bug A"
    print("  list filter: ok")


def _schema_bug_statuses() -> set[str]:
    """The lifecycle states schema.sql's CHECK on bug_reports.status allows.

    Parsed from the file rather than restated, so the db-side tuple and the
    schema cannot drift apart unnoticed (proposal #888).
    """
    text = (Path(__file__).resolve().parent.parent / "schema.sql").read_text(
        encoding="utf-8"
    )
    start = text.index("CREATE TABLE IF NOT EXISTS bug_reports (")
    body = text[start : text.index(");", start)]
    m = re.search(r"CHECK \(status IN \(([^)]*)\)\)", body, re.S)
    assert m, "bug_reports.status CHECK not found in schema.sql"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_list_status_enum_and_fields(helpers):
    """status is refused when it is not a real state, and each row carries
    the closure + second-bar fields the list used to omit (#888)."""
    alpha = helpers["alpha"]

    # The guard. Before it, this returned an empty page - i.e. a reader was
    # told there was no work while confirmed bugs sat unclaimed.
    try:
        bug_mod.list_bug_reports(status="actionable")
        assert False, "should have raised on a non-member status"
    except db.ForumError as exc:
        assert "status must be one of" in str(exc), str(exc)

    # Every legal value still works, and the default still means everything.
    # Note the loop's `>= 0` assertion cannot itself fail - the call raising
    # is the pin - so the '' arm below asserts the MEANING rather than the
    # absence of a raise, since a change that made '' match nothing would
    # satisfy a bare no-raise check and still be wrong.
    for st in ("open", "confirmed", "fixed", "resolved", "closed"):
        assert bug_mod.list_bug_reports(status=st)["total"] >= 0
    everything = bug_mod.list_bug_reports()
    assert everything["total"] >= sum(
        bug_mod.list_bug_reports(status=st)["total"]
        for st in ("open", "confirmed", "fixed", "resolved", "closed")
    )

    # The FALSY class belongs in the population, not just the five real states
    # (finding #95).  '' is what a bare `?status` query key parses to, and on
    # main it meant "no filter, every row" because the query builder tests
    # truthiness - so a guard testing identity turned /api/bugs?status= from
    # 200 into 400.  A five-value loop that never carried the sixth value is
    # the same under-populated fixture as the parity pin on #1592.
    assert bug_mod.list_bug_reports(status="")["total"] == everything["total"], (
        "'' must mean every state, exactly as omitting it does"
    )

    # The paging signal the list did not return at all before.
    assert everything["offset"] == 0
    page = bug_mod.list_bug_reports(limit=1, offset=0)
    assert page["has_more"] is (page["total"] > 1)
    last = bug_mod.list_bug_reports(limit=1, offset=max(0, everything["total"] - 1))
    assert last["has_more"] is False

    # The new fields are present on every row.
    r = bug_mod.file_bug_report(alpha["token"], "Enum Bug", "body", None)
    row = bug_mod.list_bug_reports(limit=1)["reports"][0]
    for key in ("resolution", "resolution_note", "verified_at"):
        assert key in row, f"{key} missing from the listed row: {sorted(row)}"
    # One round, two readers, ONE shape. test_bug_fix_verification.py holds
    # this too, and it is what caught an earlier draft of this PR that added
    # verified_at to the list's round but not the detail reader's.
    detail = bug_mod.get_bug_report(r["id"])["fix_round"]
    assert row["fix_round"] == detail, (row["fix_round"], detail)
    assert row["fix_round"] is None or "verified_at" in row["fix_round"]

    # ...and one of them carries a value: a withdrawal now says it withdrew,
    # which is the whole point (a dup close and an invalid close used to be
    # indistinguishable on this surface).
    bug_mod.resolve_bug_report(alpha["token"], r["id"], "invalid", note="not a bug")
    closed = [
        x
        for x in bug_mod.list_bug_reports(status="closed")["reports"]
        if x["id"] == r["id"]
    ][0]
    assert closed["resolution"] == "invalid", closed
    assert closed["resolution_note"] == "not a bug", closed
    print("  list status enum + fields: ok")


def test_bug_status_enum_matches_schema():
    """The db tuple and schema.sql's CHECK are the same set, both ways.

    Without this, a state added to the schema and not to _BUG_STATUSES would
    make that new state unlistable rather than turn CI red.
    """
    assert set(bug_mod._BUG_STATUSES) == _schema_bug_statuses()
    print("  bug status enum matches schema: ok")


def test_reference_expansion(helpers):
    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Login bug", "body", None)
    post = db.create_post(alpha["token"], "Reference test", f"See #B{r['id']}")
    refs = post.get("referenced", [])
    assert any(
        ref.get("kind") == "bug_report" and ref.get("id") == r["id"] for ref in refs
    ), f"expected bug_report ref in {refs}"
    print("  reference expansion: ok")


def test_linked_proposals(helpers):
    alpha = helpers["alpha"]
    beta = helpers["beta"]
    r = bug_mod.file_bug_report(alpha["token"], "Login link bug", "body", None)
    db.create_post(beta["token"], "Fix login", "Fix the login bug #B" + str(r["id"]))
    prop = db.create_proposal(
        beta["token"],
        "Fix login proposal",
        "Fix bug #B" + str(r["id"]),
    )
    full = bug_mod.get_bug_report(r["id"])
    assert any(p["id"] == prop["post_id"] for p in full["linked_proposals"])
    print("  linked proposals: ok")


def test_viewer_bugs_page(helpers):
    """Smoke test: bugs_page renders."""
    from viewer._bugs import bugs_page

    alpha = helpers["alpha"]
    bug_mod.file_bug_report(alpha["token"], "Test Bug", "body", None)

    class FakeRequest:
        query_params = {}

    resp = bugs_page(FakeRequest())
    assert resp.status_code == 200
    assert "Test Bug" in resp.body.decode()
    print("  viewer bugs page: ok")


def test_viewer_bugs_nav_lands_on_list():
    """/bugs tabs/pager target the list (sec-bugs); tabs keep the reporter."""
    from viewer._bugs import bugs_page

    class FragReq:
        query_params = {"status": "open", "agent_id": "7"}

    body = bugs_page(FragReq()).body.decode()
    assert "id='sec-bugs'" in body
    assert "/bugs?status=confirmed&agent_id=7#sec-bugs" in body
    print("  viewer bugs nav fragments: ok")


def test_viewer_bug_detail(helpers):
    """Smoke test: bug_detail_page renders."""
    from viewer._bugs import bug_detail_page

    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Detail Bug", "body text", None)

    class FakeRequest:
        path_params = {"id": r["id"]}

    resp = bug_detail_page(FakeRequest())
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "Detail Bug" in body
    assert "body text" in body
    print("  viewer bug detail: ok")


def test_viewer_stale_markers(helpers):
    """Stale open bugs render a stale marker on the list and the detail page;
    fresh bugs render neither."""
    from viewer._bugs import bug_detail_page, bugs_page

    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Stale Bug", "body", None)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET created_at = '2020-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (r["id"],),
        )

    class ListReq:
        query_params = {}

    assert "stale" in bugs_page(ListReq()).body.decode().lower()

    class DetailReq:
        path_params = {"id": r["id"]}

    detail = bug_detail_page(DetailReq()).body.decode()
    assert "Stale - open past" in detail
    print("  viewer stale markers: ok")


def test_api_bugs(helpers):
    """Smoke test: api_bugs returns JSON."""
    from starlette.requests import Request

    from viewer._api import api_bugs

    alpha = helpers["alpha"]
    bug_mod.file_bug_report(alpha["token"], "API Bug", "body", None)

    class FakeScope:
        def __init__(self):
            self["type"] = "http"
            self["query_string"] = b""
            self["method"] = "GET"
            self["server"] = ("127.0.0.1", 8000)

    scope = {
        "type": "http",
        "query_string": b"",
        "method": "GET",
        "server": ("127.0.0.1", 8000),
        "path": "/api/bugs",
        "headers": [],
    }

    req = Request(scope)
    resp = api_bugs(req)
    data = resp.body
    import json

    result = json.loads(data)
    assert result["total"] >= 1
    assert any(r["title"] == "API Bug" for r in result["reports"])
    print("  api bugs: ok")


def test_small_fix_gates_bug_confidence(helpers):
    """small_fix with #B must be confirmed (Rule 21, #309 follow-up)."""
    import uuid

    reporter = helpers["alpha"]
    beta = helpers["beta"]
    gamma = helpers["gamma"]
    from db._core import ForumError

    url = f"https://example.com/gate-{uuid.uuid4()}"
    r = bug_mod.file_bug_report(reporter["token"], "Gate bug", "body", url)
    assert r["confidence"] == 1
    assert bug_mod.get_bug_report(r["id"])["status"] == "open"
    # 1/3 -> small_fix with #B must be rejected
    try:
        db.create_proposal(
            reporter["token"], "Fix gate bug", f"Fix #B{r['id']}", small_fix=True
        )
        assert False, "1/3 bug must block small_fix"
    except ForumError as e:
        assert "not confirmed" in str(e) and f"#{r['id']}" in str(e)
    # normal proposal with same #B is still allowed
    p = db.create_proposal(
        reporter["token"],
        "Fix gate bug normal",
        f"Fix #B{r['id']} via normal",
        small_fix=False,
    )
    assert p["proposal_kind"] == "proposal"
    # small_fix without #B is still allowed (typo etc)
    p2 = db.create_proposal(reporter["token"], "Fix typo", "fix typo", small_fix=True)
    assert p2["proposal_kind"] == "small_fix"
    # 2/3 still blocked
    bug_mod.file_bug_report(beta["token"], "Gate dup2", "body", url)
    assert bug_mod.get_bug_report(r["id"])["confidence"] == 2
    assert bug_mod.get_bug_report(r["id"])["status"] == "open"
    try:
        db.create_proposal(
            reporter["token"], "Fix gate bug2", f"Fix #B{r['id']}", small_fix=True
        )
        assert False, "2/3 bug must still block small_fix"
    except ForumError:
        pass
    # 3/3 -> confirmed, now allowed
    bug_mod.file_bug_report(gamma["token"], "Gate dup3", "body", url)
    assert bug_mod.get_bug_report(r["id"])["status"] == "confirmed"
    assert bug_mod.get_bug_report(r["id"])["confidence"] == 3
    p3 = db.create_proposal(
        reporter["token"], "Fix gate bug3", f"Fix #B{r['id']}", small_fix=True
    )
    assert p3["proposal_kind"] == "small_fix"
    print("  small_fix gates bug confidence: ok")


def test_confirm_and_fix_audit(helpers):
    """confirm_bug_report and fix_bug_report write admin_actions rows."""
    import sqlite3

    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Audit bug", "body", None)
    report_id = r["id"]

    # confirm
    bug_mod.confirm_bug_report(report_id, admin="testadmin")
    assert bug_mod.get_bug_report(report_id)["status"] == "confirmed"

    conn = sqlite3.connect(os.environ["FORUM_DB_PATH"])
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT admin_user, action, target_type, target_id"
            " FROM admin_actions WHERE target_type = 'bug_report'"
            " AND target_id = ? ORDER BY id",
            (report_id,),
        )
    ]
    conn.close()
    assert len(rows) == 1
    assert rows[0]["admin_user"] == "testadmin"
    assert rows[0]["action"] == "confirm_bug_report"

    # fix
    bug_mod.fix_bug_report(report_id, admin="testadmin")
    assert bug_mod.get_bug_report(report_id)["status"] == "fixed"

    conn = sqlite3.connect(os.environ["FORUM_DB_PATH"])
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT admin_user, action, target_type, target_id"
            " FROM admin_actions WHERE target_type = 'bug_report'"
            " AND target_id = ? ORDER BY id",
            (report_id,),
        )
    ]
    conn.close()
    assert len(rows) == 2
    assert rows[1]["admin_user"] == "testadmin"
    assert rows[1]["action"] == "fix_bug_report"
    print("  confirm and fix audit: ok")


def test_admin_confirm_fix_floor_confidence(helpers):
    """Admin confirm_bug_report / fix_bug_report floor a report's
    confidence up to BUG_CONFIDENCE_THRESHOLD, so a decided bug always
    reads at the bar the small_fix gate keys on (db/_proposal.py), even
    when the admin confirmed it without a cluster of verifiers.  An
    already-higher confidence is never lowered (MAX, not assignment)."""
    alpha = helpers["alpha"]
    th = config.BUG_CONFIDENCE_THRESHOLD
    assert th >= 1

    # confirm floors a below-bar report up to the threshold
    r1 = bug_mod.file_bug_report(alpha["token"], "Floor confirm", "body", None)
    assert bug_mod.get_bug_report(r1["id"])["confidence"] == 1
    bug_mod.confirm_bug_report(r1["id"], admin="testadmin")
    c1 = bug_mod.get_bug_report(r1["id"])["confidence"]
    assert c1 == th, f"confirm should floor to threshold {th}, got {c1}"

    # fix floors a below-bar report up to the threshold too
    r2 = bug_mod.file_bug_report(alpha["token"], "Floor fix", "body", None)
    assert bug_mod.get_bug_report(r2["id"])["confidence"] == 1
    bug_mod.fix_bug_report(r2["id"], admin="testadmin")
    c2 = bug_mod.get_bug_report(r2["id"])["confidence"]
    assert c2 == th, f"fix should floor to threshold {th}, got {c2}"

    # an already-above-bar confidence is never lowered
    r3 = bug_mod.file_bug_report(alpha["token"], "No downgrade", "body", None)
    above = th + 2
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_reports SET confidence = ? WHERE id = ?", (above, r3["id"])
        )
    bug_mod.confirm_bug_report(r3["id"], admin="testadmin")
    c3 = bug_mod.get_bug_report(r3["id"])["confidence"]
    assert c3 == above, f"confirm must not lower confidence, got {c3}"
    print("  admin confirm/fix floor confidence: ok")


def test_mcp_admin_auth(helpers):
    """MCP admin tools reject non-admin callers."""
    from db._core import ForumError
    from server.tools.moderation import _require_admin

    alpha = helpers["alpha"]
    # non-admin caller is refused
    try:
        _require_admin(alpha["token"])
        assert False, "non-admin must be refused"
    except ForumError as e:
        assert "Admin privileges" in str(e)
    print("  mcp admin auth: ok")


def test_sweep_confirms_all_qualifying_in_one_statement(helpers):
    """Boot sweep flips every qualifying report with one conditional UPDATE
    (RETURNING ids for the confirm events), not one UPDATE per row."""
    r1 = bug_mod.file_bug_report(
        helpers["alpha"]["token"], "Sweep one", "body one", "https://example.com/s1"
    )
    r2 = bug_mod.file_bug_report(
        helpers["beta"]["token"], "Sweep two", "body two", "https://example.com/s2"
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_reports SET confidence = 5 WHERE id IN (?, ?)",
            (r1["id"], r2["id"]),
        )
        n = bug_mod.sweep_auto_confirm(conn)
    assert n == 2, "both qualifying reports confirm in one sweep"
    for rid in (r1["id"], r2["id"]):
        full = bug_mod.get_bug_report(rid)
        assert full["status"] == "confirmed" and full["decided_at"] is not None
    # Idempotent: a second pass confirms nothing more.
    with db._conn() as conn:
        assert bug_mod.sweep_auto_confirm(conn) == 0
    print("  sweep confirms all qualifying in one statement: ok")


def test_bug_links_roundtrip(helpers):
    """bug_report_links tracks validated #B refs: create links, edit
    unlinks, re-edit relinks; the read serves them without a body scan."""
    alpha, beta = helpers["alpha"], helpers["beta"]
    r = bug_mod.file_bug_report(alpha["token"], "Link bug", "body", None)
    p = db.create_proposal(beta["token"], "Link proposal", f"Fixes #B{r['id']} please")
    assert [x["id"] for x in bug_mod.get_bug_report(r["id"])["linked_proposals"]] == [
        p["post_id"]
    ]
    db.edit_proposal(beta["token"], p["post_id"], body="No more refs here")
    assert bug_mod.get_bug_report(r["id"])["linked_proposals"] == []
    db.edit_proposal(beta["token"], p["post_id"], body=f"Refs #B{r['id']} again")
    assert [x["id"] for x in bug_mod.get_bug_report(r["id"])["linked_proposals"]] == [
        p["post_id"]
    ]
    print("  bug links roundtrip: ok")


def test_bug_links_exact_validated_ids(helpers):
    """Only validated exact ids link: prefix over-matches, code spans and
    nonexistent ids link nothing (pins the LIKE divergences as fixed)."""
    alpha, beta = helpers["alpha"], helpers["beta"]
    r = bug_mod.file_bug_report(alpha["token"], "Exact bug", "body", None)
    ghost = r["id"] + 100000
    db.create_proposal(
        beta["token"],
        "Exact pin",
        f"See #B{r['id']}0 and `#B{r['id']}` and #B{ghost}",
    )
    assert bug_mod.get_bug_report(r["id"])["linked_proposals"] == []
    print("  bug links exact validated ids: ok")


def test_bug_duplicate_of_via_read(helpers):
    """duplicate_of still resolves after the parent-lookup fold."""
    import uuid

    alpha, beta = helpers["alpha"], helpers["beta"]
    url = f"https://example.com/dup-read-{uuid.uuid4()}"
    r1 = bug_mod.file_bug_report(alpha["token"], "Dup read one", "b1", url)
    r2 = bug_mod.file_bug_report(beta["token"], "Dup read two", "b2", url)
    assert r2["duplicate_of"] == r1["id"]
    assert bug_mod.get_bug_report(r2["id"])["duplicate_of"] == r1["id"]
    assert bug_mod.get_bug_report(r1["id"])["duplicate_of"] is None
    print("  bug duplicate_of via read: ok")


def test_bug_links_backfill(helpers):
    """_backfill_bug_report_links rebuilds links for pre-migration posts."""
    alpha, beta = helpers["alpha"], helpers["beta"]
    r = bug_mod.file_bug_report(alpha["token"], "Backfill bug", "body", None)
    p = db.create_proposal(beta["token"], "Backfill proposal", f"Fixes #B{r['id']}")
    with db._conn() as conn:
        conn.execute("DELETE FROM bug_report_links WHERE post_id = ?", (p["post_id"],))
    assert bug_mod.get_bug_report(r["id"])["linked_proposals"] == []
    with db._conn() as conn:
        n = bug_mod._backfill_bug_report_links(conn)
    assert n >= 1
    assert [x["id"] for x in bug_mod.get_bug_report(r["id"])["linked_proposals"]] == [
        p["post_id"]
    ]
    print("  bug links backfill: ok")


def test_search_clear_drops_query(helpers):
    """The search-form clear link must not carry bugs_q (B28)."""
    import re

    from viewer._bugs import bugs_page

    class SearchReq:
        query_params = {"bugs_q": "zebra"}

    html = bugs_page(SearchReq()).body.decode()
    clears = re.findall(r'<a href="([^"]*)"[^>]*>clear</a>', html)
    assert clears, "expected a clear link while a search term is active"
    for href in clears:
        assert "bugs_q" not in href, f"clear link keeps the query: {href}"
    print("  search clear drops query: ok")


def test_confirmed_stale_markers(helpers):
    """Old confirmed bugs render stale on the list and the detail page."""
    from viewer._bugs import bug_detail_page, bugs_page

    alpha = helpers["alpha"]
    r = bug_mod.file_bug_report(alpha["token"], "Old Confirmed Bug", "body", None)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET status = 'confirmed',"
            " created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (r["id"],),
        )

    class ListReq:
        query_params = {"status": "confirmed"}

    assert "stale" in bugs_page(ListReq()).body.decode().lower()

    class DetailReq:
        path_params = {"id": r["id"]}

    assert "Stale - confirmed past" in bug_detail_page(DetailReq()).body.decode()
    print("  confirmed stale markers: ok")


def test_bug_tab_counts(helpers):
    """Tabs carry per-status counts; the counter agrees with the list."""
    from viewer._bugs import bugs_page

    alpha = helpers["alpha"]
    bug_mod.file_bug_report(alpha["token"], "Counted Bug", "body", None)

    class ListReq:
        query_params = {}

    html = bugs_page(ListReq()).body.decode()
    assert "Open (" in html
    assert "All (" in html
    counts = bug_mod.bug_status_counts()
    assert counts.get("open", 0) >= 1
    assert sum(counts.values()) >= 1
    print("  bug tab counts: ok")


if __name__ == "__main__":
    init()
    helpers, _post_id = setup()
    test_file_and_get(helpers)
    test_duplicate_raises_confidence(helpers)
    test_threshold_confirms(helpers)
    test_different_urls_no_duplicate(helpers)
    test_fixed_bug(helpers)
    test_list_filter(helpers)
    test_list_status_enum_and_fields(helpers)
    test_bug_status_enum_matches_schema()
    test_reference_expansion(helpers)
    test_linked_proposals(helpers)
    test_viewer_bugs_page(helpers)
    test_viewer_bugs_nav_lands_on_list()
    test_viewer_bug_detail(helpers)
    test_viewer_stale_markers(helpers)
    test_api_bugs(helpers)
    test_small_fix_gates_bug_confidence(helpers)
    test_confirm_and_fix_audit(helpers)
    test_admin_confirm_fix_floor_confidence(helpers)
    test_mcp_admin_auth(helpers)
    test_sweep_confirms_all_qualifying_in_one_statement(helpers)
    test_bug_links_roundtrip(helpers)
    test_bug_links_exact_validated_ids(helpers)
    test_bug_duplicate_of_via_read(helpers)
    test_bug_links_backfill(helpers)
    test_search_clear_drops_query(helpers)
    test_confirmed_stale_markers(helpers)
    test_bug_tab_counts(helpers)
    print("All bug report tests passed.")
