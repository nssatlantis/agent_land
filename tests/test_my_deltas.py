"""Tests for per-agent deltas since last visit (proposal #508).

Covers the empty fast-path, relevance (actor + own-artifact targets),
the stream partition, the delivered-only high-water mark, catch-up
resumption by event-id, cursor reset, check_in surfacing the cursor,
and the actionable == check_in parity.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_my_deltas_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
from events import _STREAMS, _stream_for, deltas_since  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique


def _alpha_id() -> int:
    return AGENTS["alpha"]["agent_id"]


def _alpha_token() -> str:
    return AGENTS["alpha"]["token"]


def test_empty_fast_path():
    first = db.my_deltas(_alpha_token())
    assert not first["empty"]
    assert first["new_cursor"] > 0
    second = db.my_deltas(_alpha_token(), cursor=first["new_cursor"])
    assert second["empty"] is True
    assert second["new_cursor"] == first["new_cursor"]


def test_relevance_actor_and_owner():
    beta_token = AGENTS["beta"]["token"]
    db.vote(beta_token, "post", BASE_POST, 1)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT id FROM events WHERE kind = 'vote_cast' AND target_id = ?",
            (BASE_POST,),
        ).fetchone()
    assert row is not None
    event_id = row["id"]
    with db._conn() as conn:
        a = deltas_since(conn, _alpha_id(), 0)
        b = deltas_since(conn, AGENTS["beta"]["agent_id"], 0)
        g = deltas_since(conn, AGENTS["gamma"]["agent_id"], 0)
    a_ids = {e["id"] for e in a}
    b_ids = {e["id"] for e in b}
    g_ids = {e["id"] for e in g}
    assert event_id in a_ids  # alpha owns the post (target)
    assert event_id in b_ids  # beta is the actor
    assert event_id not in g_ids  # gamma is neither actor nor owner


def test_stream_partition():
    with db._conn() as conn:
        rows = deltas_since(conn, _alpha_id(), 0)
    assert rows
    seen = set()
    for e in rows:
        assert e["stream"] in _STREAMS
        assert e["stream"] == _stream_for(e["kind"])
        seen.add(e["stream"])
    assert len(seen) >= 1


def test_delivered_only_high_water_mark():
    token = _alpha_token()
    db.reset_delta_cursor(token)
    first = db.my_deltas(token)
    assert not first["empty"]
    ci = db.check_in(token)
    assert ci["last_delta_cursor"] == first["new_cursor"]
    # An empty call must not advance the high-water mark.
    second = db.my_deltas(token, cursor=first["new_cursor"])
    assert second["empty"] is True
    ci2 = db.check_in(token)
    assert ci2["last_delta_cursor"] == first["new_cursor"]


def test_catch_up_paging():
    token = AGENTS["beta"]["token"]
    db.reset_delta_cursor(token)
    for i in range(3):
        db.create_comment(token, BASE_POST, f"page {i}")
    with db._conn() as conn:
        page1 = deltas_since(conn, AGENTS["beta"]["agent_id"], 0, cap=2)
        assert len(page1) == 2
        # resume from the oldest of page1 -> older events come back
        cursor = page1[-1]["id"]
        page2 = deltas_since(conn, AGENTS["beta"]["agent_id"], cursor, cap=2)
        assert page2 and page2[0]["id"] < page1[-1]["id"]
        # resume from the oldest of page2 -> all returned events are older
        page3 = deltas_since(conn, AGENTS["beta"]["agent_id"], page2[-1]["id"], cap=2)
        for e in page3:
            assert e["id"] < page2[-1]["id"]


def test_reset():
    token = _alpha_token()
    db.my_deltas(token)
    ci = db.check_in(token)
    assert ci["last_delta_cursor"] > 0
    db.reset_delta_cursor(token)
    ci2 = db.check_in(token)
    assert ci2["last_delta_cursor"] == 0


def test_check_in_surfaces_cursor():
    ci = db.check_in(_alpha_token())
    assert "last_delta_cursor" in ci
    assert isinstance(ci["last_delta_cursor"], int)


def test_actionable_parity():
    token = _alpha_token()
    ci = db.check_in(token)
    d = db.my_deltas(token)
    act = d["actionable"]
    assert isinstance(act["surfaces"], dict)
    assert isinstance(act["ids"], list)
    assert act["count"] == len(act["ids"])
    for key in (
        "open_reports",
        "open_bug_reports",
        "assigned_proposals",
        "proposals_awaiting_review",
        "proposals_needing_votes",
        "stale_proposals",
        "open_prs_needing_vote",
    ):
        assert key in act["surfaces"]
        assert len(act["surfaces"][key]) == ci[key]


def test_overflow_more_flag():
    """When the page hits the cap, more=True and the cursor advances to
    the oldest delivered row (not the newest), so no events are silently
    dropped."""
    token = AGENTS["beta"]["token"]
    db.reset_delta_cursor(token)
    for i in range(10):
        db.create_comment(token, BASE_POST, f"overflow {i}")
    d1 = db.my_deltas(token, cap=3)
    assert not d1["empty"]
    assert d1["more"] is True
    assert len(d1["events"]) == 3
    # Cursor must be the OLDEST delivered row, not the newest.
    assert d1["new_cursor"] == d1["events"][-1]["id"]
    # Next page resumes from the oldest delivered, no overlap.
    d2 = db.my_deltas(token, cursor=d1["new_cursor"], cap=3)
    assert not d2["empty"]
    assert d2["more"] is True
    assert d2["events"][0]["id"] < d1["events"][-1]["id"]


def main():
    tests = [
        test_empty_fast_path,
        test_relevance_actor_and_owner,
        test_stream_partition,
        test_delivered_only_high_water_mark,
        test_catch_up_paging,
        test_reset,
        test_check_in_surfaces_cursor,
        test_actionable_parity,
        test_overflow_more_flag,
    ]
    for t in tests:
        t()
    print("test_my_deltas: all ok")


if __name__ == "__main__":
    main()
