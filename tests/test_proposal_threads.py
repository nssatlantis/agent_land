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


def test_empty_index_returns_none_crash_free():
    pid = _idea(BETA)
    assert db.list_threads(pid) == [], "threads-error@empty-index: must be []"


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


def test_summaries_batch_parity_and_missing():
    pid = _idea(BETA)
    other = _idea(BETA)
    t1 = db.start_thread(BETA, pid, "Batch one", "charge")
    t2 = db.start_thread(BETA, pid, "Batch two", "charge")
    db.close_thread(BETA, pid, t2["thread_id"], "done")
    batch = db.threads_summaries_for([pid, other, pid, 999999999])
    assert set(batch) == {pid, other}, f"threads-error@batch-keys: {sorted(batch)!r}"
    assert batch[pid] == db.threads_summary_for(pid), "threads-error@batch-parity"
    assert batch[pid] == {"post_id": pid, "total": 2, "open": 1, "closed": 1}, (
        f"threads-error@batch-shape: {batch[pid]!r}"
    )
    assert batch[other] == {"post_id": other, "total": 0, "open": 0, "closed": 0}, (
        f"threads-error@batch-empty: {batch[other]!r}"
    )
    assert db.threads_summaries_for([]) == {}, "threads-error@batch-empty-list"
    assert db.threads_summaries_for([pid]) == {pid: batch[pid]}, (
        "threads-error@batch-single"
    )
    with db._conn() as conn:
        assert db.threads_summaries_for([pid], conn=conn) == {pid: batch[pid]}, (
            "threads-error@batch-conn"
        )
        assert db.threads_summary_for(pid, conn=conn) == batch[pid], (
            "threads-error@summary-conn"
        )
    assert t1["thread_id"] != t2["thread_id"]


def test_batch_attach_keeps_get_posts_error_strings():
    pid = _idea(BETA)
    db.start_thread(BETA, pid, "Attach line", "charge")
    res = db.get_posts([pid, 999999999])
    assert isinstance(res[999999999], str) and res[999999999].startswith(
        "error: no post"
    ), f"threads-error@attach-missing: {res[999999999]!r}"
    summaries = db.threads_summaries_for(
        [p for p, r in res.items() if isinstance(r, dict)]
    )
    for p, r in res.items():
        if isinstance(r, dict):
            # Same .get-with-zeroes shape the get_posts tool uses: a post
            # deleted mid-batch keeps zeroes, never a KeyError.
            r["threads_summary"] = summaries.get(
                p, {"post_id": p, "total": 0, "open": 0, "closed": 0}
            )
    assert res[pid]["threads_summary"] == db.threads_summary_for(pid), (
        "threads-error@attach-parity"
    )


def test_get_thread_subtree_beats_single_level():
    pid = _idea(BETA)
    t = db.start_thread(BETA, pid, "Deep line", "charge words")
    tid = t["thread_id"]
    r1 = db.create_comment(BETA, pid, "top reply point", tid)
    r2 = db.create_comment(BETA, pid, "nested reply point", r1["comment_id"])
    r3 = db.create_comment(BETA, pid, "deepest reply point", r2["comment_id"])
    got = db.get_thread(pid, tid)
    assert got["thread_id"] == tid, f"threads-error@one-shape: {got['thread_id']!r}"
    assert got["anchor"]["id"] == tid, "threads-error@one-anchor"
    assert got["reply_count"] == 3, f"threads-error@one-count: {got['reply_count']!r}"
    assert [c["id"] for c in got["comments"]] == [r1["comment_id"]], (
        "threads-error@one-top"
    )
    assert [c["id"] for c in got["comments"][0]["replies"]] == [r2["comment_id"]], (
        "threads-error@one-nested"
    )
    assert [c["id"] for c in got["comments"][0]["replies"][0]["replies"]] == [
        r3["comment_id"]
    ], "threads-error@one-deep"
    assert all("score" in c for c in got["comments"]), "threads-error@one-score"
    # The single-level read this tool replaces sees only the top reply.
    flat = db.list_comments(pid, parent_comment_id=tid)
    assert [c["id"] for c in flat] == [r1["comment_id"]], (
        "threads-error@one-gap: single-level misses the chain"
    )
    assert "no post with id" in expect_error(db.get_thread, 999999999, tid), (
        "threads-error@one-unknown-post"
    )
    assert "no thread #" in expect_error(db.get_thread, pid, 999999999), (
        "threads-error@one-unknown-thread"
    )


def test_list_threads_sort_and_state():
    pid = _idea(BETA)
    a = db.start_thread(BETA, pid, "Alpha line", "charge words")
    b = db.start_thread(BETA, pid, "Beta line", "charge words")
    db.create_comment(ALPHA, pid, "alpha reply one", a["thread_id"])
    db.create_comment(AGENTS["gamma"]["token"], pid, "gamma reply two", a["thread_id"])
    default = [t["thread_id"] for t in db.list_threads(pid)]
    assert default == [a["thread_id"], b["thread_id"]], (
        f"threads-error@sort-anchor: {default!r}"
    )
    assert [t["thread_id"] for t in db.list_threads(pid, sort="anchor")] == default, (
        "threads-error@sort-explicit"
    )
    quiet = [t["thread_id"] for t in db.list_threads(pid, sort="quiet")]
    assert quiet == [b["thread_id"], a["thread_id"]], (
        f"threads-error@sort-quiet: {quiet!r}"
    )
    active = [t["thread_id"] for t in db.list_threads(pid, sort="active")]
    assert active == [a["thread_id"], b["thread_id"]], (
        f"threads-error@sort-active: {active!r}"
    )
    db.close_thread(BETA, pid, b["thread_id"], "done here")
    assert [t["thread_id"] for t in db.list_threads(pid, state="open")] == [
        a["thread_id"]
    ], "threads-error@sort-open"
    assert [t["thread_id"] for t in db.list_threads(pid, state="closed")] == [
        b["thread_id"]
    ], "threads-error@sort-closed"
    assert "sort must be" in expect_error(db.list_threads, pid, sort="bogus"), (
        "threads-error@sort-bad"
    )
    assert "state must be" in expect_error(db.list_threads, pid, state="bogus"), (
        "threads-error@state-bad"
    )


def test_get_thread_explain_uses_indexes():
    pid = _idea(BETA)
    t = db.start_thread(BETA, pid, "Explain line", "charge words")
    tid = t["thread_id"]
    with db._conn() as conn:
        troot = "\n".join(
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM threads"
                " WHERE anchor_comment_id = ? AND post_id = ?",
                (tid, pid),
            ).fetchall()
        )
        assert "SCAN threads" not in troot, f"threads-error@plan-root: {troot!r}"
        sub = "\n".join(
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN WITH RECURSIVE sub(id) AS ("
                " SELECT anchor_comment_id FROM threads"
                " WHERE anchor_comment_id = ? AND post_id = ?"
                " UNION ALL SELECT c.id FROM comments c"
                " JOIN sub s ON c.parent_comment_id = s.id"
                " WHERE c.post_id = ?)"
                " SELECT id FROM sub",
                (tid, pid, pid),
            ).fetchall()
        )
        assert "idx_comments_post_parent_created" in sub, (
            f"threads-error@plan-sub: {sub!r}"
        )
        assert "SCAN comments" not in sub, f"threads-error@plan-scan: {sub!r}"


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} proposal-thread tests passed")
