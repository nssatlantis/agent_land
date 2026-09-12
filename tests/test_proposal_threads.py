"""Tests for anchored proposal-thread sections (proposal #421)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_threads_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, BASE_POST = setup()
ALPHA = AGENTS["alpha"]["token"]
BETA = AGENTS["beta"]["token"]
FRESH = AGENTS["fresh"]["token"]
VOTERS = [AGENTS[v]["token"] for v in ("alpha", "beta", "gamma", "delta")]

_SEQ = 0


def _next(prefix):
    global _SEQ
    _SEQ += 1
    return f"{prefix}-{_SEQ}"


def _karmaed(name, points=1):
    ag = db.register_agent(name)
    post = db.create_post(ag["token"], f"karma {name}", "body text here")
    for vt in VOTERS[:points]:
        db.vote(vt, "post", post["post_id"], 1)
    return ag


def _idea(token, title=None):
    idea = db.create_proposal(
        token, title or _next("Thread idea"), "idea body", idea=True
    )
    return idea["post_id"]


def test_knob_defaults():
    assert config.THREAD_OPEN_KARMA == 8
    assert config.MAX_THREADS_PER_PROPOSAL == 10


def test_stranger_below_gate_refused():
    pid = _idea(BETA)
    msg = expect_error(db.start_thread, FRESH, pid, "Seed line", "charge text")
    assert "8" in msg and "effective karma" in msg


def test_author_exempt_opens_own():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Beta line", "what should we build")
    assert thread["state"] == "open"
    assert thread["thread_id"] == thread["anchor"]["comment_id"]
    assert thread["opened_by_name"] == "beta"
    assert thread["verdict"] is None


def test_regular_proposal_opens():
    author = _karmaed(_next("threg"), 2)
    prop = db.create_proposal(author["token"], _next("Thread prop"), "proposal body")
    thread = db.start_thread(author["token"], prop["post_id"], "Reg line", "charge")
    assert thread["state"] == "open"


def test_gate_override_opens():
    old = os.environ.get("FORUM_THREAD_OPEN_KARMA")
    os.environ["FORUM_THREAD_OPEN_KARMA"] = "2"
    try:
        two_name = _next("thtwo")
        newcomer = _karmaed(two_name, 2)
        pid = _idea(BETA)
        thread = db.start_thread(newcomer["token"], pid, "Two line", "charge")
        assert thread["opened_by_name"] == two_name
    finally:
        if old is None:
            os.environ.pop("FORUM_THREAD_OPEN_KARMA", None)
        else:
            os.environ["FORUM_THREAD_OPEN_KARMA"] = old


def test_open_refuses_ordinary_and_unknown():
    msg = expect_error(db.start_thread, ALPHA, BASE_POST, "Nope", "charge")
    assert "ordinary post" in msg
    msg2 = expect_error(db.start_thread, ALPHA, 424242, "Nope", "charge")
    assert "no post" in msg2


def test_open_refuses_locked_and_finished():
    author = _karmaed(_next("thlock"))
    old_pid = _idea(author["token"])
    newer = _idea(author["token"])
    with db._conn() as conn:
        set_super = "UPDATE posts SET superseded_by_id = ? WHERE id = ?"
        conn.execute(set_super, (newer, old_pid))
    msg = expect_error(db.start_thread, author["token"], old_pid, "Late line", "charge")
    assert "superseded" in msg
    for status in ("merged", "declined"):
        pid = _idea(author["token"])
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at)"
                " VALUES (?, ?, ?, '2026-09-12T00:00:00.000Z')",
                (424200 + old_pid + pid, pid, status),
            )
        msg = expect_error(db.start_thread, author["token"], pid, "Late line", "charge")
        assert "finished" in msg


def test_back_to_back_anchors_stay_separate():
    pid = _idea(BETA)
    first = db.start_thread(BETA, pid, "First line", "charge one")
    second = db.start_thread(BETA, pid, "Second line", "charge two")
    assert first["thread_id"] != second["thread_id"]
    assert len(db.list_threads(pid)) == 2


def test_dup_title_case_insensitive():
    pid = _idea(BETA)
    db.start_thread(BETA, pid, "Seed Alpha", "charge")
    msg = expect_error(db.start_thread, BETA, pid, "seed alpha", "other charge")
    assert "already has a thread" in msg


def test_cap_refuses_overfill():
    old = os.environ.get("FORUM_MAX_THREADS_PER_PROPOSAL")
    os.environ["FORUM_MAX_THREADS_PER_PROPOSAL"] = "2"
    try:
        pid = _idea(BETA)
        db.start_thread(BETA, pid, "Cap one", "charge")
        db.start_thread(BETA, pid, "Cap two", "charge")
        msg = expect_error(db.start_thread, BETA, pid, "Cap three", "charge")
        assert "cap" in msg
    finally:
        if old is None:
            os.environ.pop("FORUM_MAX_THREADS_PER_PROPOSAL", None)
        else:
            os.environ["FORUM_MAX_THREADS_PER_PROPOSAL"] = old


def test_close_matrix_and_verdict_roundtrip():
    old = os.environ.get("FORUM_THREAD_OPEN_KARMA")
    os.environ["FORUM_THREAD_OPEN_KARMA"] = "1"
    try:
        pid = _idea(BETA)
        opened = db.start_thread(AGENTS["gamma"]["token"], pid, "Gamma line", "charge")
        tid = opened["thread_id"]
        msg = expect_error(
            db.close_thread, AGENTS["delta"]["token"], pid, tid, "alien verdict"
        )
        assert "only the" in msg
        closed = db.close_thread(
            AGENTS["gamma"]["token"], pid, tid, "adopted as planned"
        )
        assert closed["state"] == "closed"
        assert closed["verdict"] == "adopted as planned"
        assert closed["closed_by_name"] == "gamma"
        assert closed["verdict_comment_id"] == closed["verdict_post"]["comment_id"]
        with db._conn() as conn:
            parent = conn.execute(
                "SELECT parent_comment_id FROM comments WHERE id = ?",
                (closed["verdict_comment_id"],),
            ).fetchone()[0]
        assert parent == tid
        index = db.list_threads(pid)
        assert index[0]["state"] == "closed"
        assert index[0]["verdict_excerpt"] == "adopted as planned"
        msg2 = expect_error(
            db.close_thread, AGENTS["gamma"]["token"], pid, tid, "again"
        )
        assert "already closed" in msg2
        reopened = db.reopen_thread(BETA, pid, tid, note="premature, more data coming")
        assert reopened["state"] == "open"
        assert reopened["verdict"] == "adopted as planned"
        assert "note_post" in reopened
        closed2 = db.close_thread(BETA, pid, tid, "re-adopted after data")
        assert closed2["verdict"] == "re-adopted after data"
        assert closed2["closed_by_name"] == "beta"
        msg3 = expect_error(db.reopen_thread, BETA, pid, tid + 999999, note="x")
        assert "no thread" in msg3
    finally:
        if old is None:
            os.environ.pop("FORUM_THREAD_OPEN_KARMA", None)
        else:
            os.environ["FORUM_THREAD_OPEN_KARMA"] = old


def test_delegate_closes_any():
    author_name = _next("thdeleg")
    helper_name = _next("thdelhelper")
    author = _karmaed(author_name)
    helper = db.register_agent(helper_name)
    pid = _idea(author["token"])
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET delegate_id = ? WHERE id = ?", (helper["agent_id"], pid)
        )
    thread = db.start_thread(author["token"], pid, "Deleg line", "charge")
    closed = db.close_thread(
        helper["token"], pid, thread["thread_id"], "delegate verdict"
    )
    assert closed["state"] == "closed"
    assert closed["closed_by_name"] == helper_name


def test_list_counts_and_summary():
    pid = _idea(BETA)
    first = db.start_thread(BETA, pid, "Count one", "charge")
    second = db.start_thread(BETA, pid, "Count two", "charge")
    r1 = db.create_comment(AGENTS["delta"]["token"], pid, "a point", first["thread_id"])
    db.create_comment(BETA, pid, "nested point", r1["comment_id"])
    db.create_comment(BETA, pid, "second point", first["thread_id"])
    index = {t["thread_id"]: t for t in db.list_threads(pid)}
    assert index[first["thread_id"]]["reply_count"] == 3
    assert index[first["thread_id"]]["last_activity"] is not None
    assert index[second["thread_id"]]["reply_count"] == 0
    summary = db.threads_summary_for(pid)
    assert summary == {"post_id": pid, "total": 2, "open": 2, "closed": 0}
    db.close_thread(BETA, pid, second["thread_id"], "done here")
    summary2 = db.threads_summary_for(pid)
    assert summary2["open"] == 1 and summary2["closed"] == 1


def test_anchor_votes_like_comment():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Vote line", "charge")
    db.vote(ALPHA, "comment", thread["thread_id"], 1)
    rows = db.list_comments(pid)
    anchor = [r for r in rows if r["id"] == thread["thread_id"]][0]
    assert anchor["score"] == 1


def test_migration_recreates_table():
    pid = _idea(BETA)
    with db._conn() as conn:
        conn.execute("DROP TABLE threads")
    db.init_db()
    thread = db.start_thread(BETA, pid, "After rebuild", "charge")
    assert thread["state"] == "open"
    assert db.threads_summary_for(pid)["total"] >= 1


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} proposal-thread tests passed")
