"""Regression: _recent_row must handle NULL-text event rows (PR #1243).

recent_activity() strips None values from event dicts, but the vote/event
else-branch in _recent_row accesses e["text"] directly. When
_event_text_sql() produces NULL (via string concat with a missing detail
field), the strip removes the key and _recent_row crashes with KeyError.

This test feeds a NULL-text event row through _recent_row and verifies
no crash occurs and the row renders successfully.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_recent_null_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
from viewer._feed_helpers import _recent_row  # noqa: E402

db.init_db()
AGENTS, BASE_POST = setup()


def _tok(name):
    return AGENTS[name]["token"]


def _aid(name):
    return AGENTS[name]["agent_id"]


def main():
    # Create a post + vote so we have a real event to manipulate
    p1 = db.create_post(_tok("alpha"), "Null-text test post", "Body.")
    c1 = db.create_comment(_tok("beta"), p1["post_id"], "A comment.")
    db.vote(_tok("gamma"), "post", p1["post_id"], 1)
    db.vote(_tok("delta"), "comment", c1["comment_id"], 1)

    # Fetch the raw events — find a vote row
    events = db.recent_activity(limit=50)
    vote_rows = [e for e in events if e["event_type"] == "vote"]
    assert vote_rows, "need at least one vote event for the test"
    row = vote_rows[0]

    # Simulate the NULL-text scenario: set text to None and strip it
    # (mirrors what recent_activity() does after _event_text_sql() returns NULL)
    row["text"] = None
    row = {k: v for k, v in row.items() if v is not None or k in ("score", "comment_id", "post_id", "proposal_kind", "preview", "net")}

    # This must not raise KeyError: 'text'
    html = _recent_row(row)
    assert isinstance(html, str), "_recent_row must return a string"
    assert len(html) > 0, "_recent_row must return non-empty HTML"
    print(f"  NULL-text vote row renders OK: {len(html)} chars")

    # Also test with a post event that has NULL text
    post_rows = [e for e in events if e["event_type"] == "post"]
    if post_rows:
        prow = post_rows[0].copy()
        prow["text"] = None
        prow = {k: v for k, v in prow.items() if v is not None or k in ("score", "comment_id", "post_id", "proposal_kind", "preview", "net")}
        html2 = _recent_row(prow)
        assert isinstance(html2, str) and len(html2) > 0
        print(f"  NULL-text post row renders OK: {len(html2)} chars")

    # Also test with a comment event that has NULL text
    comment_rows = [e for e in events if e["event_type"] == "comment"]
    if comment_rows:
        crow = comment_rows[0].copy()
        crow["text"] = None
        crow = {k: v for k, v in crow.items() if v is not None or k in ("score", "comment_id", "post_id", "proposal_kind", "preview", "net")}
        html3 = _recent_row(crow)
        assert isinstance(html3, str) and len(html3) > 0
        print(f"  NULL-text comment row renders OK: {len(html3)} chars")

    print("  recent null-text pins: ok")


if __name__ == "__main__":
    main()
    print("All recent null-text tests passed.")
