"""Regression: _recent_row must handle NULL-text event rows (PR #1243).

recent_activity() strips None values from event dicts. The fix adds
"text" to the whitelist so _recent_row() never sees a missing key.

This test verifies:
1. recent_activity() preserves the 'text' key even when its value is None.
2. _recent_row() does not crash on NULL-text vote, post, and comment rows.
"""

import os
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


def main():
    # Create posts, comments, and votes to generate all event types
    p1 = db.create_post(_tok("alpha"), "Null-text test post", "Body.")
    c1 = db.create_comment(_tok("beta"), p1["post_id"], "A comment.")
    db.vote(_tok("gamma"), "post", p1["post_id"], 1)
    db.vote(_tok("delta"), "comment", c1["comment_id"], 1)

    events = db.recent_activity(limit=50)
    assert events, "need events for the test"

    # 1. Verify 'text' key is preserved even when its value is None
    for e in events:
        assert "text" in e, (
            f"event type={e.get('event_type')} id={e.get('target_id')} "
            f"missing 'text' key — whitelist is broken"
        )
    print(f"  all {len(events)} events have 'text' key: ok")

    # 2. _recent_row must not crash on any event type
    for e in events:
        html = _recent_row(e)
        assert isinstance(html, str) and len(html) > 0, (
            f"_recent_row returned empty for event type={e.get('event_type')}"
        )
    print(f"  all {len(events)} events render through _recent_row: ok")

    print("  recent null-text pins: ok")


if __name__ == "__main__":
    main()
    print("All recent null-text tests passed.")
