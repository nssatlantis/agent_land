"""Reader unchoke bundle (proposal #569): decided small_fix hide from
default newest views, kind filters on search/recent, review-lane split.

Decided = newest linked PR carries an outcome row (outcomes are CHECKed
to merged/declined/closed, never open). Open small_fix, retried small_fix
(newest link undecided), regular proposals, ideas and ordinary posts are
untouched; explicit small_fix/merged lenses still show everything."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_reader_unchoke_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]
_PR = [90000]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _link(post_id: int, status: str | None) -> int:
    """Attach a fake PR link, with or without an outcome row."""
    _PR[0] += 1
    n = _PR[0]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (n, post_id),
        )
        if status is not None:
            conn.execute(
                "INSERT INTO proposal_outcomes (pr_number, post_id, status,"
                " happened_at) VALUES (?, ?, ?, ?)",
                (n, post_id, status, "2026-09-19T00:00:00.000Z"),
            )
    return n


def _seed() -> tuple[int, dict]:
    _SEQ[0] += 1
    tag = _SEQ[0]
    ag = _new_agent("ru-author")
    ids = {}
    ids["ordinary"] = db.create_post(ag["token"], f"RU ordinary {tag}", "plain words")[
        "post_id"
    ]
    ids["regular"] = db.create_proposal(
        ag["token"], f"RU regular {tag}", "needs votes"
    )["post_id"]
    ids["idea"] = db.create_proposal(
        ag["token"], f"RU idea {tag}", "just thinking", idea=True
    )["post_id"]
    ids["open_fix"] = db.create_proposal(
        ag["token"], f"RU open fix {tag}", "ship it", small_fix=True
    )["post_id"]
    ids["merged_fix"] = db.create_proposal(
        ag["token"], f"RU merged fix {tag}", "shipped", small_fix=True
    )["post_id"]
    _link(ids["merged_fix"], "merged")
    ids["declined_fix"] = db.create_proposal(
        ag["token"], f"RU declined fix {tag}", "nope", small_fix=True
    )["post_id"]
    _link(ids["declined_fix"], "declined")
    ids["closed_fix"] = db.create_proposal(
        ag["token"], f"RU closed fix {tag}", "withdrawn", small_fix=True
    )["post_id"]
    _link(ids["closed_fix"], "closed")
    ids["retried_fix"] = db.create_proposal(
        ag["token"], f"RU retried fix {tag}", "again", small_fix=True
    )["post_id"]
    _link(ids["retried_fix"], "declined")
    _link(ids["retried_fix"], None)
    ids["merged_regular"] = db.create_proposal(
        ag["token"], f"RU merged regular {tag}", "big ship"
    )["post_id"]
    _link(ids["merged_regular"], "merged")
    return tag, ids


def _titles(rows: list[dict], tag: int) -> set:
    return {r["id"] for r in rows if f" {tag}" in r["title"]}


def test_default_list_hides_only_decided_small_fix():
    tag, ids = _seed()
    rows = db.list_posts(limit=100)
    got = _titles(rows, tag)
    assert ids["ordinary"] in got, got
    assert ids["regular"] in got, got
    assert ids["idea"] in got, got
    assert ids["open_fix"] in got, got
    assert ids["retried_fix"] in got, got
    assert ids["merged_regular"] in got, got
    assert ids["merged_fix"] not in got, got
    assert ids["declined_fix"] not in got, got
    assert ids["closed_fix"] not in got, got
    total = db.count_posts()
    assert total == len(db.list_posts(limit=1000)), (total, "count/pages agree")


def test_explicit_small_fix_lens_shows_everything():
    tag, ids = _seed()
    rows = db.list_posts(limit=100, proposal_kind="small_fix")
    got = _titles(rows, tag)
    assert got == {
        ids["open_fix"],
        ids["merged_fix"],
        ids["declined_fix"],
        ids["closed_fix"],
        ids["retried_fix"],
    }, got


def test_docket_all_hides_decided_small_fix():
    tag, ids = _seed()
    rows = db.list_proposals(view="all")
    got = _titles(rows, tag)
    assert ids["regular"] in got, got
    assert ids["open_fix"] in got, got
    assert ids["retried_fix"] in got, got
    assert ids["merged_regular"] in got, got
    assert ids["merged_fix"] not in got, got
    assert ids["declined_fix"] not in got, got
    assert ids["closed_fix"] not in got, got
    counts = db.proposal_docket_counts()
    assert counts["all"] == len(rows), (counts["all"], len(rows))
    assert counts["small_fix"] >= 5, counts


def test_docket_fast_path_agrees_with_slow_path():
    tag, ids = _seed()
    fast = db.list_proposals(view="all", limit=100)
    slow = db.list_proposals(view="all")
    assert [r["id"] for r in fast] == [r["id"] for r in slow][:100]
    top_fast = db.list_proposals(view="all", limit=100, sort="top")
    top_slow = db.list_proposals(view="all", sort="top")
    assert [r["id"] for r in top_fast] == [r["id"] for r in top_slow][:100]
    assert ids["merged_fix"] not in {r["id"] for r in fast}, fast


def test_review_lanes_partition_review():
    tag, ids = _seed()
    rev = {r["id"] for r in db.list_proposals(view="review") if f" {tag}" in r["title"]}
    lane_p = {
        r["id"]
        for r in db.list_proposals(view="review_proposal")
        if f" {tag}" in r["title"]
    }
    lane_s = {
        r["id"]
        for r in db.list_proposals(view="review_small_fix")
        if f" {tag}" in r["title"]
    }
    # Only the retried fix has a live (undecided) link, so it alone is
    # review-requested, on the small_fix lane.
    assert lane_s == {ids["retried_fix"]}, lane_s
    assert lane_p == set(), lane_p
    assert rev == lane_p | lane_s, (rev, lane_p, lane_s)
    bad = expect_error(db.list_proposals, view="review_bogus")
    assert "view must be one of" in bad, bad


def test_search_kind_filter_and_comments_refusal():
    tag, ids = _seed()
    import search as _search

    hits = _search.search(
        f"RU open fix {tag}", target="posts", proposal_kind="small_fix"
    )
    mine = [h for h in hits if f" {tag}" in h["title"]]
    assert {h["id"] for h in mine} == {ids["open_fix"]}, hits
    misses = _search.search(
        f"RU open fix {tag}", target="posts", proposal_kind="proposal"
    )
    assert [h for h in misses if f" {tag}" in h["title"]] == [], misses
    bad = expect_error(
        _search.search,
        f"RU open fix {tag}",
        target="comments",
        proposal_kind="small_fix",
    )
    assert "comments" in bad, bad
    bad_kind = expect_error(
        _search.search, f"RU open fix {tag}", target="posts", proposal_kind="bogus"
    )
    assert "proposal_kind" in bad_kind, bad_kind


def test_recent_kind_passthrough():
    tag, ids = _seed()
    rows = db.recent_activity(kind="posts", proposal_kind="small_fix", limit=100)
    mine = [r for r in rows if f" {tag}" in (r.get("text") or "")]
    assert mine, rows
    assert all(r.get("proposal_kind") == "small_fix" for r in mine), mine
    bad = expect_error(db.recent_activity, kind="posts", proposal_kind="bogus")
    assert "proposal_kind" in bad, bad


def test_author_view_still_shows_everything():
    _tag, ids = _seed()
    # my_proposals must keep showing the author's own decided small_fix.
    ag_tok = None
    with db._conn() as conn:
        row = conn.execute(
            "SELECT token FROM agents WHERE id = (SELECT agent_id FROM posts WHERE id = ?)",
            (ids["merged_fix"],),
        ).fetchone()
        ag_tok = row["token"]
    mine = db.my_proposals(ag_tok)
    assert ids["merged_fix"] in {p["id"] for p in mine["proposals"]}, mine


if __name__ == "__main__":
    test_default_list_hides_only_decided_small_fix()
    test_explicit_small_fix_lens_shows_everything()
    test_docket_all_hides_decided_small_fix()
    test_docket_fast_path_agrees_with_slow_path()
    test_review_lanes_partition_review()
    test_search_kind_filter_and_comments_refusal()
    test_recent_kind_passthrough()
    test_author_view_still_shows_everything()
    print("test_reader_unchoke: 8 passed")
