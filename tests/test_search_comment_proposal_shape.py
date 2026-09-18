"""Pin the #B58 fix: every search_comments result row carries the `proposal`
key -- None for comments on ordinary posts, a live tally dict for comments on
proposal posts (including zero-vote proposals, which previously fell out of
proposal_tallies -- the GROUP BY only yields voted posts -- and left the key
missing entirely).
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import search  # noqa: E402

_MARKER = "galvanize"


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
