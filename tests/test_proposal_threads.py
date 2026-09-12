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
    assert config.THREAD_OPEN_KARMA == 8, (
        f"threads-error@knobs: open-gate={config.THREAD_OPEN_KARMA}"
    )
    assert config.MAX_THREADS_PER_PROPOSAL == 10, (
        f"threads-error@knobs: cap={config.MAX_THREADS_PER_PROPOSAL}"
    )


def test_stranger_below_gate_refused():
    pid = _idea(BETA)
    msg = expect_error(db.start_thread, FRESH, pid, "Seed line", "charge text")
    assert "8" in msg and "effective karma" in msg, f"threads-error@gate: {msg!r}"


def test_author_exempt_opens_own():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Beta line", "what should we build")
    assert thread["state"] == "open", f"threads-error@author: {thread['state']!r}"
    assert thread["thread_id"] == thread["anchor"]["comment_id"], (
        "threads-error@author: thread/anchor id mismatch"
    )
    assert thread["opened_by_name"] == "beta", (
        f"threads-error@author: opener={thread['opened_by_name']!r}"
    )
    assert thread["verdict"] is None, f"threads-error@author: {thread['verdict']!r}"


def test_regular_proposal_opens():
    author = _karmaed(_next("threg"), 2)
    prop = db.create_proposal(author["token"], _next("Thread prop"), "proposal body")
    thread = db.start_thread(author["token"], prop["post_id"], "Reg line", "charge")
    assert thread["state"] == "open", f"threads-error@regular: {thread['state']!r}"


def test_gate_override_opens():
    old = os.environ.get("FORUM_THREAD_OPEN_KARMA")
    os.environ["FORUM_THREAD_OPEN_KARMA"] = "2"
    try:
        two_name = _next("thtwo")
        newcomer = _karmaed(two_name, 2)
        pid = _idea(BETA)
        thread = db.start_thread(newcomer["token"], pid, "Two line", "charge")
        assert thread["opened_by_name"] == two_name, (
            f"threads-error@gate2: opener={thread['opened_by_name']!r}"
        )
    finally:
        if old is None:
            os.environ.pop("FORUM_THREAD_OPEN_KARMA", None)
        else:
            os.environ["FORUM_THREAD_OPEN_KARMA"] = old


def test_open_refuses_ordinary_and_unknown():
    msg = expect_error(db.start_thread, ALPHA, BASE_POST, "Nope", "charge")
    assert "ordinary post" in msg, f"threads-error@ordinary: {msg!r}"
    msg2 = expect_error(db.start_thread, ALPHA, 424242, "Nope", "charge")
    assert "no post" in msg2, f"threads-error@unknown: {msg2!r}"


def test_open_refuses_locked_and_finished():
    author = _karmaed(_next("thlock"))
    old_pid = _idea(author["token"])
    newer = _idea(author["token"])
    with db._conn() as conn:
        set_super = "UPDATE posts SET superseded_by_id = ? WHERE id = ?"
        conn.execute(set_super, (newer, old_pid))
    msg = expect_error(db.start_thread, author["token"], old_pid, "Late line", "charge")
    assert "superseded" in msg, f"threads-error@locked: {msg!r}"
    for status in ("merged", "declined"):
        pid = _idea(author["token"])
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at)"
                " VALUES (?, ?, ?, '2026-09-12T00:00:00.000Z')",
                (424200 + old_pid + pid, pid, status),
            )
        msg = expect_error(db.start_thread, author["token"], pid, "Late line", "charge")
        assert "finished" in msg, f"threads-error@finished-{status}: {msg!r}"


def test_back_to_back_anchors_stay_separate():
    pid = _idea(BETA)
    first = db.start_thread(BETA, pid, "First line", "charge one")
    second = db.start_thread(BETA, pid, "Second line", "charge two")
    assert first["thread_id"] != second["thread_id"], "threads-error@nomerge: folded"
    assert len(db.list_threads(pid)) == 2, "threads-error@nomerge: index count"


def test_dup_title_case_insensitive():
    pid = _idea(BETA)
    db.start_thread(BETA, pid, "Seed Alpha", "charge")
    msg = expect_error(db.start_thread, BETA, pid, "seed alpha", "other charge")
    assert "already has a thread" in msg, f"threads-error@dup: {msg!r}"


def test_cap_refuses_overfill():
    old = os.environ.get("FORUM_MAX_THREADS_PER_PROPOSAL")
    os.environ["FORUM_MAX_THREADS_PER_PROPOSAL"] = "2"
    try:
        pid = _idea(BETA)
        db.start_thread(BETA, pid, "Cap one", "charge")
        db.start_thread(BETA, pid, "Cap two", "charge")
        msg = expect_error(db.start_thread, BETA, pid, "Cap three", "charge")
        assert "cap" in msg, f"threads-error@cap: {msg!r}"
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
        assert "only the" in msg, f"threads-error@alien-close: {msg!r}"
        closed = db.close_thread(
            AGENTS["gamma"]["token"], pid, tid, "adopted as planned"
        )
        assert closed["state"] == "closed", f"threads-error@close: {closed['state']!r}"
        assert closed["verdict"] == "adopted as planned", "threads-error@close: text"
        assert closed["closed_by_name"] == "gamma", (
            f"threads-error@close: by={closed['closed_by_name']!r}"
        )
        assert closed["verdict_comment_id"] == closed["verdict_post"]["comment_id"], (
            "threads-error@close: verdict pointer"
        )
        with db._conn() as conn:
            parent = conn.execute(
                "SELECT parent_comment_id FROM comments WHERE id = ?",
                (closed["verdict_comment_id"],),
            ).fetchone()[0]
        assert parent == tid, f"threads-error@close: parent={parent!r} tid={tid!r}"
        index = db.list_threads(pid)
        assert index[0]["state"] == "closed", "threads-error@index: state"
        assert index[0]["verdict_excerpt"] == "adopted as planned", (
            "threads-error@index: excerpt"
        )
        msg2 = expect_error(
            db.close_thread, AGENTS["gamma"]["token"], pid, tid, "again"
        )
        assert "already closed" in msg2, f"threads-error@reclose: {msg2!r}"
        reopened = db.reopen_thread(BETA, pid, tid, note="premature, more data coming")
        assert reopened["state"] == "open", (
            f"threads-error@reopen: {reopened['state']!r}"
        )
        assert reopened["verdict"] == "adopted as planned", "threads-error@reopen: kept"
        assert "note_post" in reopened, "threads-error@reopen: note echo"
        closed2 = db.close_thread(BETA, pid, tid, "re-adopted after data")
        assert closed2["verdict"] == "re-adopted after data", "threads-error@reclose2"
        assert closed2["closed_by_name"] == "beta", (
            f"threads-error@reclose2: by={closed2['closed_by_name']!r}"
        )
        msg3 = expect_error(db.reopen_thread, BETA, pid, tid + 999999, note="x")
        assert "no thread" in msg3, f"threads-error@unknown-thread: {msg3!r}"
    finally:
        if old is None:
            os.environ.pop("FORUM_THREAD_OPEN_KARMA", None)
        else:
            os.environ["FORUM_THREAD_OPEN_KARMA"] = old


def test_close_verdict_cap_checked_before_flip():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Cap verdict", "charge")
    big = "v" * config.MAX_COMMENT_LEN
    msg = expect_error(db.close_thread, BETA, pid, thread["thread_id"], big)
    assert "too long once wrapped" in msg, f"threads-error@verdict-cap: {msg!r}"
    assert db.list_threads(pid)[0]["state"] == "open", "threads-error@verdict-cap: open"


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
    assert closed["state"] == "closed", f"threads-error@delegate: {closed['state']!r}"
    assert closed["closed_by_name"] == helper_name, (
        f"threads-error@delegate: by={closed['closed_by_name']!r}"
    )


def test_list_counts_and_summary():
    pid = _idea(BETA)
    first = db.start_thread(BETA, pid, "Count one", "charge")
    second = db.start_thread(BETA, pid, "Count two", "charge")
    r1 = db.create_comment(AGENTS["delta"]["token"], pid, "a point", first["thread_id"])
    db.create_comment(BETA, pid, "nested point", r1["comment_id"])
    db.create_comment(BETA, pid, "second point", first["thread_id"])
    index = {t["thread_id"]: t for t in db.list_threads(pid)}
    kids = [(r["id"], r["parent_comment_id"]) for r in db.list_comments(pid)]
    assert index[first["thread_id"]]["reply_count"] == 3, (
        f"threads-error@counts: got={index[first['thread_id']]['reply_count']!r}"
        f" anchor={first['thread_id']!r} kids={kids!r}"
    )
    assert index[first["thread_id"]]["last_activity"] is not None, (
        "threads-error@counts: last_activity"
    )
    assert index[second["thread_id"]]["reply_count"] == 0, "threads-error@counts: empty"
    summary = db.threads_summary_for(pid)
    assert summary == {"post_id": pid, "total": 2, "open": 2, "closed": 0}, (
        f"threads-error@summary: {summary!r}"
    )
    db.close_thread(BETA, pid, second["thread_id"], "done here")
    summary2 = db.threads_summary_for(pid)
    assert summary2["open"] == 1 and summary2["closed"] == 1, (
        f"threads-error@summary2: {summary2!r}"
    )


def test_anchor_votes_like_comment():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Vote line", "charge")
    db.vote(ALPHA, "comment", thread["thread_id"], 1)
    rows = db.list_comments(pid)
    anchor = [r for r in rows if r["id"] == thread["thread_id"]][0]
    assert anchor["score"] == 1, f"threads-error@vote: score={anchor['score']!r}"


def test_trailing_ordinary_comment_stands_alone_of_anchor():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Lone line", "charge words here")
    tid = thread["thread_id"]
    posted = db.create_comment(BETA, pid, "main-line follow-up point")
    assert posted["comment_id"] != tid, "threads-error@trailing-fold: folded"
    assert not posted.get("merged"), "threads-error@trailing-fold: merged flag"
    rows = db.list_comments(pid)
    anchor = [r for r in rows if r["id"] == tid][0]
    assert "main-line follow-up point" not in anchor["body"], (
        "threads-error@trailing-fold: anchor body polluted"
    )


def test_reply_after_verdict_stands_alone_of_mirror():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Verdict line", "charge words here")
    tid = thread["thread_id"]
    closed = db.close_thread(BETA, pid, tid, "settled as planned")
    vcid = closed["verdict_comment_id"]
    follow = db.create_comment(BETA, pid, "thread reply after the verdict", tid)
    assert follow["comment_id"] != vcid, "threads-error@verdict-fold: folded"
    assert not follow.get("merged"), "threads-error@verdict-fold: merged flag"


def test_ordinary_back_to_back_still_combine():
    pid = _idea(BETA)
    first = db.create_comment(BETA, pid, "first ordinary point")
    second = db.create_comment(BETA, pid, "second ordinary point")
    assert second["comment_id"] == first["comment_id"], (
        "threads-error@combine-law: split"
    )
    assert second.get("merged"), "threads-error@combine-law: merged flag missing"


def test_reply_after_reopen_note_stands_alone():
    pid = _idea(BETA)
    thread = db.start_thread(BETA, pid, "Reopen line", "charge words here")
    tid = thread["thread_id"]
    db.close_thread(BETA, pid, tid, "done for now")
    reopened = db.reopen_thread(BETA, pid, tid, "second look")
    note_id = reopened["note_post"]["comment_id"]
    assert reopened["note_comment_id"] == note_id, (
        "threads-error@reopen-fold: pointer missing"
    )
    follow = db.create_comment(BETA, pid, "thread reply after the note", tid)
    assert follow["comment_id"] != note_id, "threads-error@reopen-fold: folded"
    assert not follow.get("merged"), "threads-error@reopen-fold: merged flag"


def test_zz_migration_recreates_table():
    pid = _idea(BETA)
    with db._conn() as conn:
        conn.execute("DROP TABLE threads")
    db.init_db()
    thread = db.start_thread(BETA, pid, "After rebuild", "charge")
    assert thread["state"] == "open", f"threads-error@migrate: {thread['state']!r}"
    assert db.threads_summary_for(pid)["total"] >= 1, "threads-error@migrate: summary"


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} proposal-thread tests passed")
