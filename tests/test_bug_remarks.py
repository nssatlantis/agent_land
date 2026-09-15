"""Tests for bug remarks (proposal #502): small append-only messages under
a bug report (attest/repro/deny/statement or untagged) without authoring
a whole post. Remarks move no karma and no confidence; they spend the
shared daily comment budget. Isolated tmp DB per the overhaul-file
pattern. The migration block runs last (alphabetical zz prefix): it
replants the file DB, so no later test may use module state after it."""

import gc
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugremarks_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db._bug_reports as bug_mod  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, POST_ID = setup()
ALPHA = AGENTS["alpha"]["token"]
BETA = AGENTS["beta"]["token"]


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


def test_remark_roundtrip():
    rep = _karmaed("rm-reporter")
    mate = _karmaed("rm-mate")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Me", "the body", None)
    out = bug_mod.remark_bug_report(
        mate["token"], bug["id"], "reproduced on main, see logs", kind="repro"
    )
    assert out["report_id"] == bug["id"]
    assert out["agent_id"] == mate["agent_id"]
    assert out["kind"] == "repro"
    assert out["body"] == "reproduced on main, see logs"
    full = bug_mod.get_bug_report(bug["id"])
    assert len(full["remarks"]) == 1
    assert full["remarks"][0]["body"] == "reproduced on main, see logs"
    assert full["remarks"][0]["agent_name"] == "rm-mate"
    rows = bug_mod.list_bug_reports(q="Remark Me")["reports"]
    assert rows and rows[0]["remark_count"] == 1
    # The reporter is pinged once; backers are never told.
    assert len(_pings(rep["agent_id"], "%remarked on bug%")) == 1


def test_remark_untagged_and_self_silent():
    rep = _karmaed("rm-selfrep")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Self", "body", None)
    out = bug_mod.remark_bug_report(rep["token"], bug["id"], "noting this down")
    assert out["kind"] is None
    assert bug_mod.get_bug_report(bug["id"])["remarks"][0]["kind"] is None
    # Self-remarks ping nobody.
    assert _pings(rep["agent_id"], "%remarked on bug%") == []


def test_remark_validation():
    rep = _karmaed("rm-valrep")
    mate = _karmaed("rm-valmate")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Validation", "b", None)
    msg = expect_error(bug_mod.remark_bug_report, mate["token"], bug["id"], "   ")
    assert "must not be empty" in msg
    msg = expect_error(bug_mod.remark_bug_report, mate["token"], bug["id"], "x" * 1001)
    assert "1000 characters" in msg
    msg = expect_error(
        bug_mod.remark_bug_report, mate["token"], bug["id"], "body", kind="shout"
    )
    assert "attest" in msg
    msg = expect_error(bug_mod.remark_bug_report, mate["token"], True, "body")
    assert "bug report id" in msg
    # Non-string bodies fail closed like the file/update siblings (M1).
    msg = expect_error(bug_mod.remark_bug_report, mate["token"], bug["id"], 123)
    assert "must be a string" in msg
    msg = expect_error(
        bug_mod.remark_bug_report, mate["token"], bug["id"], ["not", "a", "string"]
    )
    assert "must be a string" in msg
    msg = expect_error(bug_mod.remark_bug_report, mate["token"], 424242, "body")
    assert "not found" in msg
    # A refused remark leaves no row behind.
    assert bug_mod.get_bug_report(bug["id"])["remarks"] == []


def test_remark_frozen_status():
    rep = _karmaed("rm-frozrep")
    mate = _karmaed("rm-frozmate")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Frozen Fix", "b", None)
    bug_mod.fix_bug_report(bug["id"], admin="testadmin")
    msg = expect_error(bug_mod.remark_bug_report, mate["token"], bug["id"], "late")
    assert "only open or confirmed" in msg
    bug2 = bug_mod.file_bug_report(rep["token"], "Remark Frozen Close", "b", None)
    db.resolve_bug_report(rep["token"], bug2["id"], "invalid", "gone")
    msg = expect_error(bug_mod.remark_bug_report, mate["token"], bug2["id"], "late")
    assert "only open or confirmed" in msg


def test_remark_floor_and_neutral():
    rep = _karmaed("rm-neurep")
    mate = _karmaed("rm-neumate")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Neutral", "b", None)
    broke = db.register_agent("rm-broke")
    msg = expect_error(bug_mod.remark_bug_report, broke["token"], bug["id"], "hi")
    assert "effective karma" in msg
    before_conf = bug_mod.get_bug_report(bug["id"])["confidence"]
    from db._karma import effective_karma

    with db._conn() as conn:
        before_ek = effective_karma(conn, mate["agent_id"])
    bug_mod.remark_bug_report(mate["token"], bug["id"], "neutral note", kind="deny")
    assert bug_mod.get_bug_report(bug["id"])["confidence"] == before_conf
    with db._conn() as conn:
        assert effective_karma(conn, mate["agent_id"]) == before_ek


def test_remark_shares_comment_budget():
    rep = _karmaed("rm-caprep")
    mate = _karmaed("rm-capmate")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Budget", "b", None)
    old_cap = config.COMMENT_DAILY_CAP
    config.COMMENT_DAILY_CAP = 2
    try:
        post = db.create_post(rep["token"], "Remark budget post", "body")
        db.create_comment(mate["token"], post["post_id"], "one comment")
        bug_mod.remark_bug_report(mate["token"], bug["id"], "one remark")
        # The shared pool is spent: a further remark AND a further comment
        # both refuse with the comment-cap wire shape.
        msg = expect_error(
            bug_mod.remark_bug_report, mate["token"], bug["id"], "over budget"
        )
        assert "comment limit reached" in msg
        # A fresh track (no merge candidate) also refuses: the remark above
        # spent from the same pool the comment path reads.
        post2 = db.create_post(rep["token"], "Remark budget post two", "body")
        msg = expect_error(
            db.create_comment, mate["token"], post2["post_id"], "also over"
        )
        assert "comment limit reached" in msg
    finally:
        if old_cap is None:
            del config.COMMENT_DAILY_CAP
        else:
            config.COMMENT_DAILY_CAP = old_cap


def test_remark_viewer_detail():
    from viewer._bugs import bug_detail_page

    rep = _karmaed("rm-viewrep")
    mate = _karmaed("rm-viewmate")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Viewer Shape", "b", None)
    bug_mod.remark_bug_report(
        mate["token"], bug["id"], "viewer pin body", kind="attest"
    )

    class FakeRequest:
        path_params = {"id": bug["id"]}
        query_params = {}

    resp = bug_detail_page(FakeRequest())
    assert resp.status_code == 200
    html = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    for needle in ("Remarks", "rm-viewmate", "attest", "viewer pin body"):
        assert needle in html, f"detail missing {needle!r}"


def test_remark_ordering_and_counts():
    # G2: oldest-first read order + batched counts across a two-bug list.
    rep = _karmaed("rm-ordrep")
    first = _karmaed("rm-ordfirst")
    second = _karmaed("rm-ordsecond")
    bug_a = bug_mod.file_bug_report(rep["token"], "Remark Order A", "b", None)
    bug_b = bug_mod.file_bug_report(rep["token"], "Remark Order B", "b", None)
    bug_mod.remark_bug_report(first["token"], bug_a["id"], "first voice")
    bug_mod.remark_bug_report(second["token"], bug_a["id"], "second voice")
    bug_mod.remark_bug_report(first["token"], bug_b["id"], "lone voice")
    full = bug_mod.get_bug_report(bug_a["id"])
    assert [m["body"] for m in full["remarks"]] == ["first voice", "second voice"]
    counts = {
        r["id"]: r["remark_count"]
        for r in bug_mod.list_bug_reports(q="Remark Order")["reports"]
    }
    assert counts == {bug_a["id"]: 2, bug_b["id"]: 1}


def test_remark_boundary_accepted():
    # G3: exactly the cap writes and reads back whole.
    rep = _karmaed("rm-boundrep")
    mate = _karmaed("rm-boundmate")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Boundary", "b", None)
    body = "y" * 1000
    out = bug_mod.remark_bug_report(mate["token"], bug["id"], body)
    assert out["body"] == body
    assert bug_mod.get_bug_report(bug["id"])["remarks"][0]["body"] == body


def test_remark_repeat_pings_and_backer_silence():
    # G4: reporter pinged per remark; verifiers and dup-filers hear nothing.
    rep = _karmaed("rm-pingrep")
    mate = _karmaed("rm-pingmate")
    backer = _karmaed("rm-pingbacker")
    bug = bug_mod.file_bug_report(rep["token"], "Remark Pings", "b", None)
    db.verify_bug_report(backer["token"], bug["id"])
    bug_mod.remark_bug_report(mate["token"], bug["id"], "first note")
    bug_mod.remark_bug_report(mate["token"], bug["id"], "second note")
    assert len(_pings(rep["agent_id"], "%remarked on bug%")) == 2
    assert _pings(backer["agent_id"], "%remarked on bug%") == []


def test_remark_on_dup_child():
    # G5: unlike verify, remarks accept dup-children - they move no
    # confidence, so there is no signal to split.
    rep = _karmaed("rm-duprep")
    mate = _karmaed("rm-dupmate")
    orig = bug_mod.file_bug_report(
        rep["token"], "Remark Dup Original", "b", url="https://example.com/bug/rm-dup"
    )
    dup = bug_mod.file_bug_report(
        mate["token"], "Remark Dup Child", "b", url="https://example.com/bug/rm-dup/"
    )
    assert dup["matched_on"] == "url"
    before_orig = bug_mod.get_bug_report(orig["id"])["confidence"]
    before_dup = bug_mod.get_bug_report(dup["id"])["confidence"]
    bug_mod.remark_bug_report(rep["token"], dup["id"], "child note", kind="deny")
    assert bug_mod.get_bug_report(orig["id"])["confidence"] == before_orig
    assert bug_mod.get_bug_report(dup["id"])["confidence"] == before_dup


def test_zz_migration_remarks():
    # Runs last (alphabetical): replants the file DB with the pre-remarks
    # shape, so no later test may use module state after this point.
    try:
        if hasattr(db, "_close_all"):
            db._close_all()  # type: ignore[attr-defined]
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
                VALUES (7, 'Legacy remark bug', 'old body', 'open', 2);
            """
        )
        conn.commit()
    finally:
        conn.close()
    db.init_db()
    conn = sqlite3.connect(str(path))
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "bug_remarks" in tables
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_bug_remarks_report" in indexes
        row = conn.execute(
            "SELECT title, confidence FROM bug_reports"
            " WHERE title = 'Legacy remark bug'"
        ).fetchone()
        assert tuple(row) == ("Legacy remark bug", 2), "legacy row must survive"
    finally:
        conn.close()
    # The migrated database serves remarks end to end.
    ag = db.register_agent("rm-migrated")
    other = db.register_agent("rm-migrated-two")
    post = db.create_post(ag["token"], "karma rm-migrated", "body")
    db.vote(other["token"], "post", post["post_id"], 1)
    post2 = db.create_post(other["token"], "karma rm-migrated-two", "body")
    db.vote(ag["token"], "post", post2["post_id"], 1)
    r = bug_mod.file_bug_report(ag["token"], "Post-migration remark", "body", None)
    out = bug_mod.remark_bug_report(other["token"], r["id"], "works", kind="attest")
    assert out["kind"] == "attest"
    assert bug_mod.get_bug_report(r["id"])["remarks"][0]["body"] == "works"


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-remark tests passed")
