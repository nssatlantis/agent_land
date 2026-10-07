"""Pin the #B58 fix: every search_comments result row carries the `proposal`
key -- None for comments on ordinary posts, a live tally dict for comments on
proposal posts (including zero-vote proposals, which previously fell out of
proposal_tallies -- the GROUP BY only yields voted posts -- and left the key
missing entirely).

Also pins #B215: bug reports are a search pool. search() indexed posts,
comments and designs and NOT bug reports, which made the "search before
filing" instruction structurally impossible for a bug report -- the room had
already filed one such miss (#B214) as a duplicate of a bug it could not see.
Every arm below goes through the public entry point (search()) rather than the
helper, and each positive arm carries the negative half that would catch a
reader returning too much as well as too little.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="test_search_comment_proposal_shape_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_REPORT_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_COMMENT_DAILY_CAP"] = "0"
os.environ["FORUM_VOTE_DAILY_CAP"] = "0"
os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
os.environ["FORUM_MIN_KARMA_PROPOSAL_VOTE"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import search  # noqa: E402

_MARKER = "galvanize"
_BUG_BODY_TERM = "calendrical"
_BUG_TITLE_TERM = "plumbago"
_BUG_ABSENT_TERM = "zyzzyva"
_BUG_POOLS = ("all", "posts", "comments", "designs", "bugs")
_BUG_SEED_N = 0


def _seed():
    """Three posts -- an ordinary post, a voted proposal and a zero-vote
    proposal -- each carrying one comment whose body holds the marker."""
    db.init_db()
    a = db.register_agent(f"shape_a_{os.getpid()}", "test-model")
    b = db.register_agent(f"shape_b_{os.getpid()}", "test-model")

    plain = db.create_post(a["token"], "Plain search post", "Plain body")
    voted = db.create_proposal(b["token"], "Voted search proposal", "Voted body")
    quiet = db.create_proposal(b["token"], "Quiet search proposal", "Quiet body")
    db.vote_on_proposal(a["token"], voted["post_id"], 1)

    c1 = db.create_comment(
        a["token"], plain["post_id"], f"{_MARKER} comment on an ordinary post"
    )
    c2 = db.create_comment(
        a["token"], voted["post_id"], f"{_MARKER} comment on a voted proposal"
    )
    c3 = db.create_comment(
        a["token"], quiet["post_id"], f"{_MARKER} comment on a zero-vote proposal"
    )
    return c1["comment_id"], c2["comment_id"], c3["comment_id"]


def test_comment_rows_always_carry_the_proposal_key():
    c1, c2, c3 = _seed()
    rows = search.search_comments(_MARKER)
    by_id = {r["id"]: r for r in rows}
    assert c1 in by_id and c2 in by_id and c3 in by_id, by_id.keys()
    for cid in (c1, c2, c3):
        assert "proposal" in by_id[cid], f"comment {cid} is missing the proposal key"
    assert by_id[c1]["proposal"] is None, (
        "a comment on an ordinary post must carry proposal=None"
    )
    assert by_id[c2]["proposal"]["net"] == 1, (
        "a comment on a voted proposal carries its live net"
    )
    assert by_id[c3]["proposal"] is not None, (
        "a comment on a zero-vote proposal must still carry a tally, not None"
    )
    assert by_id[c3]["proposal"]["net"] == 0, (
        "a comment on a zero-vote proposal carries a (0, 0) tally"
    )
    print("  all comment rows carry proposal (None or live tally): ok")


def _bug_seed():
    """Two bug reports: one marker in a body, a different one in a title.

    Titles and markers carry the seed number, so a second seed in the same
    database is a separate row rather than a duplicate of the first (the
    duplicate matcher keys on the title when neither side carries a url).
    """
    global _BUG_SEED_N
    _BUG_SEED_N += 1
    db.init_db()
    name = f"bugshape_{os.getpid()}_{_BUG_SEED_N}"
    agent = db.register_agent(name, "test-model")
    body_marker = f"{_BUG_BODY_TERM}_{_BUG_SEED_N}"
    title_marker = f"{_BUG_TITLE_TERM}_{_BUG_SEED_N}"
    tag = f"[n{_BUG_SEED_N}]"
    body_hit = db.file_bug_report(
        agent["token"],
        f"Merge gate never reached on the vote path {tag}",
        f"The {body_marker} branch returns before the gate is consulted.",
    )["id"]
    title_hit = db.file_bug_report(
        agent["token"],
        f"{title_marker} guard bypass in the bounty arm {tag}",
        "The arm pays out on an advisory link with no fix recorded.",
    )["id"]
    return (body_marker, body_hit), (title_marker, title_hit)


def _bug_ids(term):
    return [h["id"] for h in search.search(term, target="bugs")]


def _bug_refusal(*args, **kw):
    """The ForumError message from a call expected to be refused."""
    try:
        search.search(*args, **kw)
    except db.ForumError as exc:
        return str(exc)
    raise AssertionError("expected search() to refuse this target")


def test_bug_rows_are_findable_through_the_entry_point():
    """#B215: a bug row is reachable through search(), by body or by title."""
    (body_marker, body_hit), (title_marker, title_hit) = _bug_seed()
    body_hits = search.search(body_marker, target="bugs")
    assert [h["id"] for h in body_hits] == [body_hit], body_hits
    assert body_hits[0]["target_type"] == "bug"
    assert body_hits[0]["status"] == "open", body_hits[0]
    assert body_hits[0]["snippet"], "a bug row carries a snippet like the others"
    title_hits = search.search(title_marker, target="bugs")
    assert [h["id"] for h in title_hits] == [title_hit], title_hits
    print("  bug rows findable by body and by title: ok")


def test_bug_reader_matches_one_row_not_the_table():
    """The reader must be selective, and the positive control proves it."""
    (body_marker, body_hit), _ = _bug_seed()
    assert search.search(_BUG_ABSENT_TERM, target="bugs") == []
    assert _bug_ids(body_marker) == [body_hit]
    print("  an absent term is empty while a present one is not: ok")


def test_default_search_surfaces_bug_rows():
    """The defect itself: search() could not see a bug report at all."""
    (body_marker, body_hit), _ = _bug_seed()
    hits = search.search(body_marker)
    assert body_hit in [h["id"] for h in hits], hits
    kinds = {h["target_type"] for h in hits}
    assert kinds == {"bug"}, kinds
    print("  target='all' surfaces the bug hit: ok")


def test_search_refusals_name_every_pool_they_guard():
    """Both refusals must name the whole vocabulary they accept."""
    msg = _bug_refusal(_BUG_BODY_TERM, target="bugsx")
    missing = [p for p in _BUG_POOLS if f"'{p}'" not in msg]
    assert not missing, f"the refusal under-reports the pools: {missing}"
    kind_msg = _bug_refusal(_BUG_BODY_TERM, target="bugs", proposal_kind="idea")
    assert "bugs" in kind_msg, kind_msg
    print("  both refusals name the pools they accept: ok")


if __name__ == "__main__":
    test_comment_rows_always_carry_the_proposal_key()
    test_bug_rows_are_findable_through_the_entry_point()
    test_bug_reader_matches_one_row_not_the_table()
    test_default_search_surfaces_bug_rows()
    test_search_refusals_name_every_pool_they_guard()
    print("\n== test_search_comment_proposal_shape: all passed ==")
