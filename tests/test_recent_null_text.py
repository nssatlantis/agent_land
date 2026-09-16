"""Regression: _recent_row must handle NULL-text event rows (PR #1243).

The fix adds "text" to the None-stripping whitelist in recent_activity(),
so the key is never removed from event dicts.  This test verifies:

1. recent_activity() preserves the 'text' key even when its value is None.
2. _recent_row() does not crash when fed a dict with text=None.

The vote/event else-branch (viewer/_feed_helpers.py:278) accesses
e["text"] directly — without the whitelist fix the key is stripped and
this raises KeyError.  With the fix the key stays (value None) and
esc(None) renders "None".
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


def _base_event(*, event_type: str = "vote", **overrides) -> dict:
    """Minimal event dict matching the shape _recent_row expects."""
    d = {
        "event_type": event_type,
        "target_type": "post",
        "target_id": 1,
        "actor_agent_id": 1,
        "actor_name": "tester",
        "created_at": "2026-01-01T00:00:00Z",
        "text": None,
    }
    d.update(overrides)
    return d


def main():
    # --- 1. Whitelist: recent_activity() preserves 'text' key ---
    p1 = db.create_post(db._tok("alpha"), "Null-text test", "Body.")
    c1 = db.create_comment(db._tok("beta"), p1["post_id"], "A comment.")
    db.vote(db._tok("gamma"), "post", p1["post_id"], 1)
    db.vote(db._tok("delta"), "comment", c1["comment_id"], 1)

    events = db.recent_activity(limit=50)
    assert events, "need events"
    for e in events:
        assert "text" in e, (
            f"event type={e.get('event_type')} id={e.get('target_id')} "
            f"missing 'text' key — whitelist is broken"
        )
    print(f"  whitelist: all {len(events)} events have 'text' key: ok")

    # --- 2. Downstream: _recent_row handles text=None without crash ---
    #    (simulates NULL from _event_text_sql — the exact scenario the fix
    #    addresses: key present with value None, not stripped)
    for etype in ("post", "comment", "vote"):
        e = _base_event(event_type=etype)
        html = _recent_row(e)
        assert isinstance(html, str) and len(html) > 0, (
            f"_recent_row({etype}) returned empty on text=None"
        )
        assert "None" in html, (
            f"_recent_row({etype}) did not render text=None as 'None'"
        )
    print("  _recent_row handles text=None for post/comment/vote: ok")

    # --- 3. Smoke: real events render cleanly ---
    for e in events:
        html = _recent_row(e)
        assert isinstance(html, str) and len(html) > 0
    print(f"  smoke: {len(events)} real events render: ok")

    print("  recent null-text pins: ok")


if __name__ == "__main__":
    main()
    print("All recent null-text tests passed.")
