"""Tag-board parity pins (small_fix #449).

list_posts(tag=...) drives from the small tag side (JOIN post_tags on the
covering composite) instead of EXISTS-probing per post row. These pins
guard the rewrite without freezing its text: the tagged board returns
exactly the rows a full-scan filter would (same ids, same order, newest
and top), and unknown tags still fail loudly.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_tagboard_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()


def _tok(name):
    return AGENTS[name]["token"]


def main():
    p1 = db.create_post(_tok("alpha"), "Board tag one", "b1")["post_id"]
    p2 = db.create_post(_tok("alpha"), "Board tag two", "b2")["post_id"]
    p3 = db.create_post(_tok("beta"), "Board tag three", "b3")["post_id"]
    # Karma bootstrap for the tag creator (floor reads karma).
    db.vote(_tok("beta"), "post", p1, 1)
    db.vote(_tok("gamma"), "post", p1, 1)
    db.vote(_tok("delta"), "post", p1, 1)
    db.create_tag(_tok("alpha"), "boardbench")
    db.apply_tag(_tok("alpha"), p1, "boardbench")
    db.apply_tag(_tok("beta"), p3, "boardbench")
    # Newest parity: same rows, same order as the unfiltered scan.
    got = [p["id"] for p in db.list_posts(tag="boardbench")]
    want = [p["id"] for p in db.list_posts() if p["id"] in (p1, p3)]
    assert got == want == [p3, p1], f"newest tag board diverged: {got} vs {want}"
    # Top parity (exercises the score-join composition with the tag join).
    db.vote(_tok("gamma"), "post", p3, 1)
    db.vote(_tok("delta"), "post", p3, 1)
    got_top = [p["id"] for p in db.list_posts(tag="boardbench", sort="top")]
    want_top = [p["id"] for p in db.list_posts(sort="top") if p["id"] in (p1, p3)]
    # p1 outranks p3 here: the karma-bootstrap votes above gave p1 net 3
    # against p3's net 2 - the pin is parity with the untagged board, and
    # the untagged board agrees.
    assert got_top == want_top == [p1, p3], f"top tag board diverged: {got_top}"
    # Unknown tags still fail loudly.
    try:
        db.list_posts(tag="no-such-tag")
        assert False, "unknown tag must raise"
    except Exception as e:
        assert "no tag named" in str(e), f"wrong error: {e}"
    # Untagged board is untouched (p2 absent everywhere above already).
    assert p2 not in got and p2 not in got_top
    print("  tag board parity: ok")


if __name__ == "__main__":
    main()
    print("All tag board tests passed.")
