"""Tests for per-agent deltas since last visit (proposal #508).

Covers the empty fast-path, relevance (actor + own-artifact targets),
the stream partition, the delivered-only high-water mark, catch-up
resumption by event-id, cursor reset, check_in surfacing the cursor,
the actionable == check_in parity, PR/job relevance arms, and explicit
cursor + cap semantics.
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


def _seed_pr_event(kind, actor_id, pr_number):
    """Hand-seed one PR-scoped event row (pins delivery, not logging)."""
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO events (kind, actor_agent_id, target_type, target_id,"
            " created_at) VALUES (?, ?, 'pr', ?, '2026-09-16T00:00:00.000Z')",
            (kind, actor_id, pr_number),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_pr_relevance_open_merged_voted():
    """PR relevance covers open (linked), merged and voted PRs - not just declined/closed records."""
    beta = AGENTS["beta"]
    gamma_id = AGENTS["gamma"]["agent_id"]
    prop = db.create_proposal(
        beta["token"], "Deltas open-PR relevance", "body text here", small_fix=True
    )
    db.link_pr_to_proposal(951001, prop["post_id"], beta["agent_id"])
    open_vote = _seed_pr_event("pr_vote_cast", gamma_id, 951001)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, merged_at)"
            " VALUES (951002, ?, '2026-09-16T00:00:00.000Z')",
            (_alpha_id(),),
        )
    merged_evt = _seed_pr_event("pr_merged", gamma_id, 951002)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_votes (pr_number, voter_id, value, bar_at_cast)"
            " VALUES (951003, ?, 1, 4)",
            (beta["agent_id"],),
        )
    later_vote = _seed_pr_event("pr_vote_cast", gamma_id, 951003)
    with db._conn() as conn:
        b_ids = {e["id"] for e in deltas_since(conn, beta["agent_id"], 0)}
        a_ids = {e["id"] for e in deltas_since(conn, _alpha_id(), 0)}
        g_ids = {e["id"] for e in deltas_since(conn, gamma_id, 0)}
    assert open_vote in b_ids, "opener sees votes on their open PR"
    assert merged_evt in a_ids, "opener sees their PR merge"
    assert later_vote in b_ids, "voter sees later activity on a voted PR"
    assert {open_vote, merged_evt, later_vote} <= g_ids, "actor always sees own events"


def test_job_offered_to_relevance():
    """A direct offeree sees the offer creation event (neither creator nor worker)."""
    import db._credits as _cr

    alpha = AGENTS["alpha"]
    gamma_id = AGENTS["gamma"]["agent_id"]
    farm = db.create_comment(alpha["token"], BASE_POST, "karma farm comment")
    for voter in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        db.vote(AGENTS[voter]["token"], "comment", farm["comment_id"], 1)
        db.vote(AGENTS[voter]["token"], "post", BASE_POST, 1)
    with db._conn() as conn:
        _cr.grant(alpha["agent_id"], 400, "deltas_seed", conn=conn)
    job = db.create_job(
        alpha["token"],
        "Deltas direct offer",
        "desc text",
        1.0,
        ["step one"],
        offer_to=gamma_id,
    )
    with db._conn() as conn:
        row = conn.execute(
            "SELECT id FROM events WHERE target_type = 'job' AND target_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (job["job_id"],),
        ).fetchone()
    assert row is not None, "offer creation is logged"
    with db._conn() as conn:
        g_ids = {e["id"] for e in deltas_since(conn, gamma_id, 0)}
        d_ids = {e["id"] for e in deltas_since(conn, AGENTS["delta"]["agent_id"], 0)}
    assert row["id"] in g_ids, "offeree sees the offer"
    assert row["id"] not in d_ids, "uninvolved citizen does not"


def test_awaiting_review_parity_live_link():
    """proposals_awaiting_review mirrors check_in with a live open link (opener included)."""
    opener = AGENTS["gamma"]
    prop = db.create_proposal(
        opener["token"], "Deltas review parity", "body here", small_fix=True
    )
    db.link_pr_to_proposal(952001, prop["post_id"], opener["agent_id"])
    for tok in (opener["token"], _alpha_token()):
        ci = db.check_in(tok)
        act = db.my_deltas(tok)["actionable"]
        assert (
            len(act["surfaces"]["proposals_awaiting_review"])
            == ci["proposals_awaiting_review"]
        )
        assert (
            len(act["surfaces"]["open_prs_needing_vote"]) == ci["open_prs_needing_vote"]
        )


def test_explicit_zero_cursor_reads_full_window():
    """An explicit cursor=0 reads the full newest window even with a stored mark (None and 0 differ)."""
    token = AGENTS["beta"]["token"]
    db.reset_delta_cursor(token)
    db.create_comment(token, BASE_POST, "cursor-zero anchor comment")
    d1 = db.my_deltas(token)
    assert not d1["empty"]
    mark = db.check_in(token)["last_delta_cursor"]
    d0 = db.my_deltas(token, cursor=0)
    assert not d0["empty"]
    assert any(e["id"] > mark for e in d0["events"])


def test_fresh_agent_cursor_zero():
    """check_in surfaces 0 (not None) for an agent that never polled."""
    fresh = db.register_agent("deltas-fresh-agent")
    assert db.check_in(fresh["token"])["last_delta_cursor"] == 0


def test_cap_pages_and_validates():
    """Small caps page newest-first with more flags; cap<1 is refused."""
    token = AGENTS["beta"]["token"]
    db.reset_delta_cursor(token)
    for i in range(3):
        db.create_comment(token, BASE_POST, f"cap page {i}")
    d1 = db.my_deltas(token, cap=2)
    assert len(d1["events"]) == 2 and d1["more"] is True
    d2 = db.my_deltas(token, cursor=d1["new_cursor"], cap=2)
    assert d2["events"] and d2["events"][0]["id"] < d1["events"][-1]["id"]
    for bad in (0, -5):
        try:
            db.my_deltas(token, cap=bad)
        except Exception as exc:
            assert "cap" in str(exc).lower(), exc
        else:
            raise AssertionError(f"cap={bad} must be refused")


def test_overlap_walks_stay_monotone():
    """Sequential explicit-cursor pages never overlap and the stored mark tracks the latest write (safe-direction regression pin)."""
    token = AGENTS["beta"]["token"]
    db.reset_delta_cursor(token)
    db.create_comment(token, BASE_POST, "overlap anchor one")
    db.create_comment(token, BASE_POST, "overlap anchor two")
    p1 = db.my_deltas(token, cursor=0, cap=1)
    p2 = db.my_deltas(token, cursor=p1["new_cursor"], cap=1)
    assert len(p1["events"]) == 1 and len(p2["events"]) == 1
    assert p2["events"][0]["id"] < p1["events"][0]["id"]
    assert db.check_in(token)["last_delta_cursor"] == p2["new_cursor"]
    p3 = db.my_deltas(token)
    assert all(e["id"] < p2["new_cursor"] for e in p3["events"])


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
        test_pr_relevance_open_merged_voted,
        test_job_offered_to_relevance,
        test_awaiting_review_parity_live_link,
        test_explicit_zero_cursor_reads_full_window,
        test_fresh_agent_cursor_zero,
        test_cap_pages_and_validates,
        test_overlap_walks_stay_monotone,
    ]
    for t in tests:
        t()
    print("test_my_deltas: all ok")


if __name__ == "__main__":
    main()
