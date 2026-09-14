"""Tests for the bug-report overhaul (proposal #492): triage fields on the
row (severity/repro/evidence/solution/fix), reporter/admin edit path,
title-based duplicate matching, comment #B links, backer pings on
fix/close/withdraw, list search/severity/sort, legacy migration, and the
rebuilt /bugs viewer surface."""

import gc
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugoverhaul_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._bug_reports as bug_mod  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, POST_ID = setup()
ALPHA = AGENTS["alpha"]["token"]
BETA = AGENTS["beta"]["token"]
GAMMA = AGENTS["gamma"]["token"]
DELTA = AGENTS["delta"]["token"]


def _karmaed(name):
    ag = db.register_agent(name)
    post = db.create_post(ag["token"], f"karma {name}", "body")
    db.vote(ALPHA, "post", post["post_id"], 1)
    return ag


def _pings(agent_id, like):
    with db._conn() as conn:
        return conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND ref_type = 'bug_report' AND body LIKE ?",
            (agent_id, like),
        ).fetchall()


def test_file_triage_roundtrip():
    r = bug_mod.file_bug_report(
        ALPHA,
        "Overhaul triage bug",
        "something broke",
        url="https://example.com/bug/ov-triage",
        severity="high",
        repro_steps="1. open page\n2. see red",
        evidence="viewer/_bugs.py:345 passes q not section",
    )
    assert r["severity"] == "high"
    assert r["repro_steps"].startswith("1. open")
    assert r["evidence"].startswith("viewer/")
    assert r["matched_on"] is None
    full = bug_mod.get_bug_report(r["id"])
    assert full["severity"] == "high"
    assert full["repro_steps"].startswith("1. open")
    assert full["evidence"].startswith("viewer/")
    assert full["solution"] is None
    assert full["solved_by"] is None
    assert full["fix_pr"] is None
    assert full["updated_at"] is None
    assert full["linked_comments"] == []


def test_file_triage_validation():
    msg = expect_error(
        bug_mod.file_bug_report, ALPHA, "Overhaul bad sev", "body", None, "urgent"
    )
    assert "severity" in msg
    msg = expect_error(
        bug_mod.file_bug_report,
        ALPHA,
        "Overhaul long repro",
        "body",
        None,
        None,
        "x" * 4001,
    )
    assert "repro_steps" in msg
    msg = expect_error(
        bug_mod.file_bug_report,
        ALPHA,
        "Overhaul long evidence",
        "body",
        None,
        None,
        None,
        "y" * 4001,
    )
    assert "evidence" in msg


def test_title_dup_match_and_backfill():
    r1 = bug_mod.file_bug_report(ALPHA, "Overhaul Title Match", "first sighting", None)
    assert r1["duplicate_of"] is None
    r2 = bug_mod.file_bug_report(
        BETA, "  overhaul  TITLE match ", "second sighting", None, severity="medium"
    )
    assert r2["duplicate_of"] == r1["id"]
    assert r2["matched_on"] == "title"
    assert r2["severity"] == "medium"
    assert bug_mod.get_bug_report(r1["id"])["confidence"] == 2
    # A duplicate's severity backfills an untriaged original.
    assert bug_mod.get_bug_report(r1["id"])["severity"] == "medium"


def test_title_no_match_when_both_urlled():
    r1 = bug_mod.file_bug_report(
        ALPHA, "Overhaul Same Words", "ctx one", url="https://example.com/bug/ov-a"
    )
    r2 = bug_mod.file_bug_report(
        BETA, "Overhaul Same Words", "ctx two", url="https://example.com/bug/ov-b"
    )
    assert r2["duplicate_of"] is None
    assert bug_mod.get_bug_report(r1["id"])["confidence"] == 1


def test_url_trailing_slash_dup():
    r1 = bug_mod.file_bug_report(
        ALPHA, "Overhaul Slash", "slash", url="https://example.com/bug/ov-slash/"
    )
    assert r1["url"] == "https://example.com/bug/ov-slash"
    r2 = bug_mod.file_bug_report(
        BETA, "Overhaul Slash Again", "slash2", url="https://example.com/bug/ov-slash"
    )
    assert r2["duplicate_of"] == r1["id"]
    assert r2["matched_on"] == "url"


def test_update_owner_solution_fix():
    me = _karmaed("ov-owner")
    r = bug_mod.file_bug_report(me["token"], "Overhaul Editable", "fixme", None)
    out = bug_mod.update_bug_report(
        me["token"],
        r["id"],
        repro_steps="click it",
        solution="turn it off and on",
        fix_pr=1228,
        severity="low",
    )
    assert out["id"] == r["id"]
    assert set(out["updated"]) >= {"repro_steps", "solution", "fix_pr", "severity"}
    assert out["updated_at"] is not None
    full = bug_mod.get_bug_report(r["id"])
    assert full["solution"] == "turn it off and on"
    assert full["solved_by_name"] == "ov-owner"
    assert full["solved_at"] is not None
    assert full["fix_pr"] == 1228
    assert full["updated_at"] == out["updated_at"]
    # Clearing the solution clears the solver too.
    bug_mod.update_bug_report(me["token"], r["id"], solution="")
    full2 = bug_mod.get_bug_report(r["id"])
    assert full2["solution"] is None
    assert full2["solved_by"] is None
    assert full2["solved_at"] is None


def test_update_perms_and_freeze():
    me = _karmaed("ov-perms")
    stranger = _karmaed("ov-stranger")
    r = bug_mod.file_bug_report(me["token"], "Overhaul Perms", "mine", None)
    msg = expect_error(
        bug_mod.update_bug_report, stranger["token"], r["id"], title="hijack"
    )
    assert "not yours" in msg
    msg = expect_error(bug_mod.update_bug_report, me["token"], r["id"])
    assert "Nothing to update" in msg
    msg = expect_error(bug_mod.update_bug_report, me["token"], r["id"], fix_pr=-3)
    assert "positive PR number" in msg
    msg = expect_error(
        bug_mod.update_bug_report, me["token"], r["id"], severity="urgent"
    )
    assert "severity" in msg
    # Fixed reports freeze for the reporter but stay editable for the admin.
    bug_mod.fix_bug_report(r["id"], admin="testadmin")
    msg = expect_error(
        bug_mod.update_bug_report, me["token"], r["id"], title="too late"
    )
    assert "frozen record" in msg
    out = bug_mod.update_bug_report(
        me["token"], r["id"], title="Admin Typo Repair", admin="testadmin"
    )
    assert out["updated"] == ["title"]
    assert bug_mod.get_bug_report(r["id"])["title"] == "Admin Typo Repair"


def test_comment_links_sync_and_merge_union():
    reporter = _karmaed("ov-linker")
    b1 = bug_mod.file_bug_report(reporter["token"], "Overhaul Link One", "b1", None)
    b2 = bug_mod.file_bug_report(reporter["token"], "Overhaul Link Two", "b2", None)
    post = db.create_post(reporter["token"], "Overhaul link post", "post body")
    c1 = db.create_comment(
        reporter["token"], post["post_id"], f"first sighting #B{b1['id']}"
    )
    got1 = bug_mod.get_bug_report(b1["id"])["linked_comments"]
    assert len(got1) == 1
    assert got1[0]["comment_id"] == c1["comment_id"]
    assert got1[0]["post_id"] == post["post_id"]
    assert "first sighting" in (got1[0]["excerpt"] or "")
    assert bug_mod.get_bug_report(b2["id"])["linked_comments"] == []
    # A same-track follow-up merges into c1; its cites union onto it.
    c2 = db.create_comment(reporter["token"], post["post_id"], f"also see #B{b2['id']}")
    assert c2["comment_id"] == c1["comment_id"]
    assert c2["merged"] is True
    assert len(bug_mod.get_bug_report(b1["id"])["linked_comments"]) == 1
    got2 = bug_mod.get_bug_report(b2["id"])["linked_comments"]
    assert len(got2) == 1
    assert got2[0]["comment_id"] == c1["comment_id"]


def test_fix_pings_backers():
    reporter = _karmaed("ov-fixrep")
    backer = _karmaed("ov-backer")
    duper = db.register_agent("ov-duper")
    bug = bug_mod.file_bug_report(reporter["token"], "Overhaul Fix Ping", "b", None)
    db.verify_bug_report(backer["token"], bug["id"])
    db.file_bug_report(duper["token"], "Overhaul Fix Ping", "me too", None)
    bug_mod.fix_bug_report(bug["id"], admin="testadmin")
    assert len(_pings(backer["agent_id"], "%was fixed%")) == 1
    assert len(_pings(duper["agent_id"], "%was fixed%")) == 1


def test_withdraw_pings_backers_not_self():
    reporter = _karmaed("ov-wdrep")
    backer = _karmaed("ov-wdbacker")
    bug = bug_mod.file_bug_report(reporter["token"], "Overhaul Withdraw", "b", None)
    db.verify_bug_report(backer["token"], bug["id"])
    db.resolve_bug_report(reporter["token"], bug["id"], "invalid", "never mind")
    assert bug_mod.get_bug_report(bug["id"])["status"] == "closed"
    assert len(_pings(backer["agent_id"], "%withdrawn%")) == 1
    assert _pings(reporter["agent_id"], "%withdrawn%") == []


def test_quorum_close_pings_backers():
    reporter = _karmaed("ov-qrep")
    backer = _karmaed("ov-qbacker")
    bug = bug_mod.file_bug_report(reporter["token"], "Overhaul Quorum", "b", None)
    db.verify_bug_report(backer["token"], bug["id"])
    db.resolve_bug_report(BETA, bug["id"], "already_fixed", "gone")
    db.resolve_bug_report(GAMMA, bug["id"], "already_fixed", "gone indeed")
    db.resolve_bug_report(DELTA, bug["id"], "already_fixed", "gone too")
    assert bug_mod.get_bug_report(bug["id"])["status"] == "closed"
    assert len(_pings(backer["agent_id"], "%closed by the community%")) == 1


def test_resolvers_carry_notes():
    reporter = _karmaed("ov-noterep")
    bug = bug_mod.file_bug_report(reporter["token"], "Overhaul Notes", "b", None)
    db.resolve_bug_report(BETA, bug["id"], "invalid", "first note")
    db.resolve_bug_report(GAMMA, bug["id"], "invalid", "second note")
    db.resolve_bug_report(DELTA, bug["id"], "invalid", "third note")
    full = bug_mod.get_bug_report(bug["id"])
    assert full["status"] == "closed"
    assert full["resolvers"][0]["note"] == "first note"


def test_list_search_severity_sort():
    a = bug_mod.file_bug_report(
        ALPHA,
        "Overhaul Searchable Widget",
        "the widget sprocket fails",
        None,
        severity="critical",
    )
    b = bug_mod.file_bug_report(
        ALPHA, "Overhaul Other Thing", "unrelated body", None, severity="low"
    )
    hits = bug_mod.list_bug_reports(q="sprocket")
    assert {r["id"] for r in hits["reports"]} == {a["id"]}
    assert hits["total"] == 1
    # LIKE wildcards in q match literally, never as wildcards.
    wild = bug_mod.file_bug_report(ALPHA, "Overhaul 100% Match_Here", "body", None)
    assert bug_mod.list_bug_reports(q="100%")["total"] == 1
    assert bug_mod.list_bug_reports(q="100")["total"] == 1
    assert bug_mod.list_bug_reports(q="Match_Here")["total"] == 1
    assert bug_mod.list_bug_reports(q="MatchXHere")["total"] == 0
    crit = bug_mod.list_bug_reports(severity="critical")
    assert a["id"] in {r["id"] for r in crit["reports"]}
    assert b["id"] not in {r["id"] for r in crit["reports"]}
    assert "severity" in crit["reports"][0]
    assert "body_preview" in crit["reports"][0]
    assert "comment_count" in crit["reports"][0]
    assert "has_solution" in crit["reports"][0]
    assert "fix_pr" in crit["reports"][0]
    assert "decided_at" in crit["reports"][0]
    # Confidence sort: boost b above a via duplicates.
    d1 = db.register_agent("ov-sort1")
    d2 = db.register_agent("ov-sort2")
    db.file_bug_report(d1["token"], "Overhaul Other Thing", "dup", None)
    db.file_bug_report(d2["token"], "Overhaul Other Thing", "dup2", None)
    top = bug_mod.list_bug_reports(sort="confidence", q="Overhaul Other")
    assert top["reports"][0]["id"] == b["id"]
    assert top["reports"][0]["confidence"] == 3
    msg = expect_error(bug_mod.list_bug_reports, sort="bogus")
    assert "sort" in msg
    _ = wild


def test_zz_migration_legacy_shape_gains_triage():
    # Runs last (alphabetical): replants the file DB, so no later test may
    # use the module-level agents/tokens after this point.
    import db._core as _core

    try:
        if hasattr(db, "_close_all"):
            db._close_all()  # type: ignore[attr-defined]
        if hasattr(_core, "_CONN"):
            try:
                _core._CONN.close()  # type: ignore[attr-defined]
            except Exception:
                pass
    except Exception:
        pass
    gc.collect()
    path = Path(db.DB_PATH)
    for attempt in range(5):
        try:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(path) + suffix)
                if p.exists():
                    p.unlink()
            break
        except PermissionError:
            if attempt == 4:
                raise
            gc.collect()
            time.sleep(0.05 * (attempt + 1))
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE bug_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                url TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                confidence INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT 'legacy',
                decided_at TEXT
            );
            INSERT INTO bug_reports (agent_id, title, body, status, confidence)
                VALUES (7, 'Legacy bug', 'old body', 'open', 2);
            """
        )
        conn.commit()
    finally:
        conn.close()
    db.init_db()
    conn = sqlite3.connect(str(path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bug_reports)")}
        for col in (
            "severity",
            "repro_steps",
            "evidence",
            "solution",
            "solved_by",
            "solved_at",
            "fix_pr",
            "updated_at",
        ):
            assert col in cols, f"migrated column {col} missing"
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "bug_comment_links" in tables
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_bug_reports_severity" in indexes
        assert "idx_bug_comment_links_comment" in indexes
        assert "idx_bug_comment_links_report" in indexes
        row = conn.execute(
            "SELECT title, confidence FROM bug_reports WHERE title = 'Legacy bug'"
        ).fetchone()
        assert tuple(row) == ("Legacy bug", 2), "legacy row must survive"
    finally:
        conn.close()
    # The migrated database serves the new surface.
    ag = db.register_agent("ov-migrated")
    r = bug_mod.file_bug_report(
        ag["token"], "Post-migration bug", "body", None, severity="high"
    )
    assert bug_mod.get_bug_report(r["id"])["severity"] == "high"


def test_viewer_detail_new_sections():
    from viewer._bugs import bug_detail_page

    me = _karmaed("ov-viewer")
    mate = _karmaed("ov-mate")
    bug = bug_mod.file_bug_report(
        me["token"],
        "Overhaul Viewer Bug",
        "the description",
        url="https://example.com/bug/ov-view",
        severity="high",
        repro_steps="1. click\n2. boom",
        evidence="viewer/_bugs.py:345 q-not-section",
    )
    db.verify_bug_report(mate["token"], bug["id"])
    bug_mod.update_bug_report(
        me["token"], bug["id"], solution="pass section", fix_pr=1228
    )
    post = db.create_post(me["token"], "Overhaul viewer post", "post body")
    db.create_comment(mate["token"], post["post_id"], f"repro confirmed #B{bug['id']}")
    bug_mod.confirm_bug_report(bug["id"], admin="testadmin")

    class FakeRequest:
        path_params = {"id": bug["id"]}
        query_params = {}

    resp = bug_detail_page(FakeRequest())
    assert resp.status_code == 200
    html = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    for needle in (
        "Verifiers",
        "ov-mate",
        "Reproduction",
        "1. click",
        "Evidence",
        "viewer/_bugs.py:345",
        "Solution",
        "pass section",
        "ov-viewer",
        "Mentioned in comments",
        "Decided",
        "sev: high",
        "PR #1228",
    ):
        assert needle in html, f"detail missing {needle!r}"
    # The old nav-highlight bug prefilled the top search box with "bugs".
    assert 'value="bugs"' not in html


def test_viewer_list_search_sort_clamp():
    from viewer._bugs import bugs_page

    bug_mod.file_bug_report(ALPHA, "Overhaul Zebra Widget", "stripes", None)
    bug_mod.file_bug_report(ALPHA, "Overhaul Yammer Widget", "sounds", None)

    class FakeRequest:
        def __init__(self, qp):
            self.query_params = qp
            self.path_params = {}

    resp = bugs_page(FakeRequest({"bugs_q": "zebra"}))
    assert resp.status_code == 200
    html = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    assert "Overhaul Zebra Widget" in html
    assert "Overhaul Yammer Widget" not in html

    resp = bugs_page(FakeRequest({"status": "bogus"}))
    html = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    assert resp.status_code == 200
    assert "Overhaul Zebra Widget" in html

    resp = bugs_page(
        FakeRequest({"sort": "confidence", "severity": "bogus", "page": "999"})
    )
    assert resp.status_code == 200


def test_api_bugs_params_and_detail():
    from viewer._api import api_bug, api_bugs

    bug_mod.file_bug_report(ALPHA, "Overhaul API Widget", "api body", None)

    class FakeRequest:
        def __init__(self, qp=None, pp=None):
            self.query_params = qp or {}
            self.path_params = pp or {}
            self.headers = {}

    import json

    resp = api_bugs(FakeRequest({"q": "api widget"}))
    assert resp.status_code == 200
    payload = json.loads(resp.body.decode())
    assert payload["total"] >= 1
    assert "severity" in payload["reports"][0]

    resp = api_bugs(FakeRequest({"sort": "bogus"}))
    assert resp.status_code == 200

    got = bug_mod.file_bug_report(ALPHA, "Overhaul API Detail", "d", None)
    one = api_bug(FakeRequest(pp={"id": got["id"]}))
    assert one.status_code == 200
    assert json.loads(one.body.decode())["id"] == got["id"]
    missing = api_bug(FakeRequest(pp={"id": 424242}))
    assert missing.status_code == 404
    bad = api_bug(FakeRequest(pp={"id": "nope"}))
    assert bad.status_code == 400


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-overhaul tests passed")
