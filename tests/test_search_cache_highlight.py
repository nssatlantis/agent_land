"""Test the search module's result-level cache key (#B56) and the
all-column FTS highlight (#B57)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_search_cache_highlight_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db,
    search,
    setup,
)


def main():
    agents, _ = setup()
    alpha = agents["alpha"]["token"]

    # ---- #B56 ------------------------------------------------------------
    # The result-level LRU cache key must cover exclude_post_id and limit:
    # the SQL (search.py:114-148) binds both, so a key of (title, body,
    # kind) alone serves one call's list to another call that needs a
    # different exclusion or page size. Distinctive tokens keep the
    # seeded base post out of the FTS pool.
    pa = db.create_post(alpha, "zxqwvplok alpha", "zxqwvplok shared seam marker")
    pb = db.create_post(alpha, "zxqwvplok beta", "zxqwvplok shared seam marker")
    q_title = "zxqwvplok"
    q_body = "zxqwvplok shared seam marker"

    search._SIMILAR_POSTS_CACHE.clear()
    first = search.find_similar_posts(
        q_title, q_body, "post", exclude_post_id=pa["post_id"]
    )
    first_ids = {s["post_id"] for s in first}
    assert (first_ids & {pa["post_id"], pb["post_id"]}) == {pb["post_id"]}, (
        "the first call excludes its own post but keeps the sibling"
    )
    second = search.find_similar_posts(
        q_title, q_body, "post", exclude_post_id=pb["post_id"]
    )
    second_ids = {s["post_id"] for s in second}
    assert pa["post_id"] in second_ids, (
        "#B56: swapping exclude_post_id recomputes instead of serving A's cache"
    )
    assert pb["post_id"] not in second_ids, (
        "#B56: the second call's own exclusion is honored, not the first's"
    )

    # limit is part of the key too: a wider limit must recompute instead
    # of echoing the limit=1 cache.
    search._SIMILAR_POSTS_CACHE.clear()
    lim1 = search.find_similar_posts(
        q_title, q_body, "post", exclude_post_id=0, limit=1
    )
    assert len(lim1) <= 1, "limit=1 caps the result"
    lim5 = search.find_similar_posts(
        q_title, q_body, "post", exclude_post_id=0, limit=5
    )
    assert len(lim5) == 2, (
        "#B56: a wider limit recomputes instead of echoing the limit=1 cache"
    )

    # ---- #B57 ------------------------------------------------------------
    # highlight() column indices are 0-based; posts_fts is title(0), body(1)
    # (schema.sql:491-496). Highlighting column 1 alone leaves a title-only
    # match unmarked, so the snippet must carry the [[ ]] markers from the
    # title column's highlight too (both columns concatenated).
    tagged = db.create_post(
        alpha,
        "zxqwv unicorn seam",
        "this body never mentions the marker token",
    )
    hits = search.search_posts("zxqwv")
    hit = next((r for r in hits if r["id"] == tagged["post_id"]), None)
    assert hit is not None, "the title-only token still matches the post"
    assert "[[" in hit["snippet"] and "]]" in hit["snippet"], (
        "#B57: a title-only match is highlighted in the snippet"
    )

    print("test_search_cache_highlight: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
