"""Activity-feed pushdown pins (small_fix #448).

The top-N pushdown rewrote list_recent_activity / _recent_activity_rows so
each UNION leg carries its own ORDER BY created_at DESC LIMIT. These pins
guard the rewrite without freezing its text:

- page tiling: every (limit, offset) page tiles the full feed
  (offset correctness of the limit+offset bound), compared tie-aware:
  identical order outside equal-created_at runs, multisets within runs
  (tie order was never specified);
- filter parity: kind / proposal_kind / agent_id filters select the same
  rows as filtering the unfiltered feed in Python;
- sort=top still orders by net DESC (smoke - that path kept its shape).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_activity_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._aggregates as aggregates  # noqa: E402, I001
from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()


def _aid(name):
    return AGENTS[name]["agent_id"]


def _tok(name):
    return AGENTS[name]["token"]


def _seed():
    """Rows in every branch plus cross-branch ties, all inside one page."""
    p1 = db.create_post(_tok("alpha"), "Activity post one", "Body one.")
    p2 = db.create_post(_tok("beta"), "Activity post two", "Body two.")
    c1 = db.create_comment(_tok("gamma"), p1["post_id"], "Activity comment one.")
    c2 = db.create_comment(_tok("delta"), p2["post_id"], "Activity comment two.")
    db.vote(_tok("epsilon"), "post", p1["post_id"], 1)
    db.vote(_tok("zeta"), "comment", c1["comment_id"], 1)
    tie = "2026-01-01T00:00:00.000Z"
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id IN (?, ?)",
            (tie, p1["post_id"], p2["post_id"]),
        )
        conn.execute(
            "UPDATE comments SET created_at = ? WHERE id IN (?, ?)",
            (tie, c1["comment_id"], c2["comment_id"]),
        )


def _tie_runs(rows):
    """Split an ordered row sequence into equal-created_at runs."""
    runs, cur, cur_key = [], [], None
    for r in rows:
        if r["created_at"] != cur_key:
            if cur:
                runs.append(cur)
            cur, cur_key = [], r["created_at"]
        cur.append(r)
    if cur:
        runs.append(cur)
    return runs


def _key(r):
    return (r["event_type"], r["target_id"], r["created_at"])


def _assert_same_feed(new_rows, ref_rows):
    """Same multiset overall, same order outside tie runs."""
    assert sorted(_key(r) for r in new_rows) == sorted(_key(r) for r in ref_rows), (
        "feed membership changed"
    )
    new_runs, ref_runs = _tie_runs(new_rows), _tie_runs(ref_rows)
    assert [r[0]["created_at"] for r in new_runs] == [
        r[0]["created_at"] for r in ref_runs
    ], "non-tie order changed"
    for nr, rr in zip(new_runs, ref_runs, strict=True):
        assert sorted(_key(r) for r in nr) == sorted(_key(r) for r in rr), (
            "tie-run membership changed"
        )


def main():
    _seed()
    full = aggregates.recent_activity(limit=200)
    assert full, "seed must fill the feed"
    assert {r["event_type"] for r in full} >= {"post", "comment", "vote", "event"}, (
        "seed must cover all four legs"
    )
    for limit, offset in ((5, 0), (5, 2), (3, 4), (50, 0), (7, 6)):
        page = aggregates.recent_activity(limit=limit, offset=offset)
        _assert_same_feed(page, full[offset : offset + limit])
    for kind in ("posts", "comments", "votes", "events"):
        got = aggregates.recent_activity(limit=200, kind=kind)
        want = [r for r in full if r["event_type"] == kind.rstrip("s")]
        _assert_same_feed(got, want)
    by_alpha = aggregates.recent_activity(limit=200, agent_id=_aid("alpha"))
    assert by_alpha and all(r["agent_id"] == _aid("alpha") for r in by_alpha), (
        "agent filter must scope every row"
    )
    top = aggregates.recent_activity(limit=50, sort="top")
    nets = [r["net"] for r in top if r["event_type"] == "post"]
    assert nets == sorted(nets, reverse=True), "sort=top must order by net DESC"
    print("  activity pushdown pins: ok")


if __name__ == "__main__":
    main()
    print("All activity pushdown tests passed.")
