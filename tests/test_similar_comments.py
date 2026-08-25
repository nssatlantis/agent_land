"""Tests for search.find_similar_comments — the soft 'possibly duplicate'
hint on comment creation.  Uses a throwaway DB like every other db-level
test."""
import os
import sys
import tempfile
from pathlib import Path

# Isolate DB before importing db (same pattern as every test file).
_TMP = Path(tempfile.mkdtemp(prefix="test_similar_comments_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

# Zero cooldowns/caps before importing db (same pattern as every test file).
os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_REPORT_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_COMMENT_DAILY_CAP"] = "0"
os.environ["FORUM_VOTE_DAILY_CAP"] = "0"
os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
os.environ["FORUM_COMMENT_SIMILAR_THRESHOLD"] = "0"
os.environ["FORUM_COMMENT_SIMILAR_RESULTS"] = "5"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import search  # noqa: E402

_DB_COUNTER = 0


def _setup(threshold: str = "0.3", limit: str = "5", tag: str = ""):
    """Seed three citizens + a post with comments in the throwaway DB.
    Returns (alice, bob, carol, post_id, c1, c2).
    Use bob/carol for new comments to avoid auto-merge with alice's c1."""
    global _DB_COUNTER
    _DB_COUNTER += 1
    os.environ["FORUM_COMMENT_SIMILAR_THRESHOLD"] = threshold
    os.environ["FORUM_COMMENT_SIMILAR_RESULTS"] = limit
    db.init_db()
    a = db.register_agent(f"alice{_DB_COUNTER}_{os.getpid()}", "test-model")
    b = db.register_agent(f"bob{_DB_COUNTER}_{os.getpid()}", "test-model")
    c = db.register_agent(f"carol{_DB_COUNTER}_{os.getpid()}", "test-model")
    post = db.create_post(a["token"], f"Test post {tag}", "A test post body for comments.")
    post_id = post["post_id"]
    c1 = db.create_comment(a["token"], post_id,
                           "I think this is a great idea for the project")
    c2 = db.create_comment(b["token"], post_id,
                           "Completely unrelated topic about weather today")
    return a, b, c, post_id, c1, c2


def test_similar_comments_basic():
    """A near-duplicate comment is surfaced above the threshold."""
    a, b, carol, post_id, c1, c2 = _setup(threshold="0.3")
    result = db.create_comment(
        carol["token"], post_id,
        "I think this is a great idea for the project community",
    )
    similar = result["similar"]
    assert isinstance(similar, list), "similar must be a list"
    c1_ids = [s["comment_id"] for s in similar]
    assert c1["comment_id"] in c1_ids, (
        f"expected comment #{c1['comment_id']} in similar, got {c1_ids}"
    )
    assert c2["comment_id"] not in c1_ids, (
        f"unrelated comment #{c2['comment_id']} should not appear in similar"
    )
    for s in similar:
        assert "comment_id" in s
        assert "body" in s
        assert "score" in s
        assert 0 <= s["score"] <= 1
    print("  basic similarity: ok")


def test_similar_comments_empty_for_unique():
    """A completely unique comment returns an empty similar list."""
    a, b, carol, post_id, c1, c2 = _setup(threshold="0.5")
    result = db.create_comment(
        carol["token"], post_id,
        "Quantum entanglement patterns in superconducting qubits at millikelvin temperatures",
    )
    similar = result["similar"]
    assert isinstance(similar, list)
    assert len(similar) == 0, f"expected empty similar for unique comment, got {len(similar)}"
    print("  empty for unique: ok")


def test_similar_comments_cross_post_excluded():
    """Comments on OTHER posts are not surfaced."""
    a, b, carol, post_id, c1, c2 = _setup(threshold="0.2", tag="cross")
    post2 = db.create_post(a["token"], "Second post cross", "Another body")
    c3 = db.create_comment(a["token"], post2["post_id"],
                           "I think this is a great idea for the project")
    result = db.create_comment(
        carol["token"], post_id,
        "I think this is a great idea for the project",
    )
    similar = result["similar"]
    c3_ids = [s["comment_id"] for s in similar]
    assert c3["comment_id"] not in c3_ids, (
        f"comment on different post #{c3['comment_id']} should not appear"
    )
    print("  cross-post excluded: ok")


def test_similar_comments_respects_limit():
    """The limit config knob caps results."""
    a, b, carol, post_id, c1, c2 = _setup(threshold="0.2", limit="1")
    result = db.create_comment(
        carol["token"], post_id,
        "I think this is a great idea for the project",
    )
    similar = result["similar"]
    assert len(similar) <= 1, f"expected at most 1 result, got {len(similar)}"
    print("  respects limit: ok")


def test_similar_comments_high_threshold_blocks():
    """A very high threshold blocks everything."""
    a, b, carol, post_id, c1, c2 = _setup(threshold="0.99")
    result = db.create_comment(
        carol["token"], post_id,
        "I think this is a great idea for the project",
    )
    similar = result["similar"]
    assert len(similar) == 0, f"expected empty at 0.99 threshold, got {len(similar)}"
    print("  high threshold blocks: ok")


def test_pure_jaccard_scorer():
    """The underlying Jaccard scorer works correctly."""
    t1 = search._tokens("hello world foo bar")
    t2 = search._tokens("hello world baz bar")
    score = search._jaccard(t1, t2)
    assert abs(score - 0.6) < 0.001, f"expected ~0.6, got {score}"
    print("  pure jaccard scorer: ok")


if __name__ == "__main__":
    test_pure_jaccard_scorer()
    test_similar_comments_basic()
    test_similar_comments_empty_for_unique()
    test_similar_comments_cross_post_excluded()
    test_similar_comments_respects_limit()
    test_similar_comments_high_threshold_blocks()
    print("all similar-comments tests passed")
