"""Regression: _recent_row must handle NULL-text event rows."""

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


def _base_event(event_type="vote"):
    return {
        "event_type": event_type,
        "target_type": "post",
        "target_id": 1,
        "actor_agent_id": 1,
        "actor": "test",
        "created_at": "2026-01-01T00:00:00Z",
        "text": None,
    }


def main():
    agents, _base_post = setup()

    def tok(name):
        return agents[name]["token"]

    # Whitelist: recent_activity() preserves 'text' key.
    p1 = db.create_post(tok("alpha"), "Null test", "Body.")
    c1 = db.create_comment(tok("beta"), p1["post_id"], "A comment.")
    db.vote(tok("gamma"), "post", p1["post_id"], 1)
    db.vote(tok("delta"), "comment", c1["comment_id"], 1)

    events = db.recent_activity(limit=50)
    assert events, "need events"
    for e in events:
        assert "text" in e, f"event id={e.get('target_id')} missing text key"
    print(f"  whitelist: {len(events)} events: ok")

    # Downstream: _recent_row handles text=None (no crash).
    for etype in ("post", "comment", "vote"):
        html = _recent_row(_base_event(etype))
        assert isinstance(html, str) and len(html) > 0
    print("  _recent_row text=None: ok")

    # Smoke: real events render.
    for e in events:
        html = _recent_row(e)
        assert isinstance(html, str) and len(html) > 0
    print(f"  smoke: {len(events)} events: ok")


if __name__ == "__main__":
    main()
    print("All recent null-text tests passed.")
