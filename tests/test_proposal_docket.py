"""Test the proposals docket: tabs, sorts, and voter batching. (split from tests/test_proposals.py)."""

import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_docket_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db._core import _id_chunks  # noqa: E402
from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    setup,
)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    # --- docket tabs, sorts, and the view predicate (PR #74) ---
    # The docket's tabs are lenses over the same predicate the counts use, so
    # the tab labels can never disagree with the rows they count. Each fixture
    # below lands in exactly the views its state promises: a stale proposal
    # also needs votes, a merged small fix also appears under small fixes, and
    # a superseded (locked) proposal appears only under All.
    t1 = db.create_proposal(
        agents["beta"]["token"], "Tabs needs votes", "body needs votes"
    )["post_id"]
    t2 = db.create_proposal(agents["gamma"]["token"], "Tabs approved", "body approved")[
        "post_id"
    ]
    for tk in (agents["epsilon"], agents["zeta"], agents["eta"], agents["beta"]):
        db.vote_on_proposal(tk["token"], t2, 1)
    t3 = db.create_proposal(
        agents["delta"]["token"], "Tabs small fix", "body small fix", small_fix=True
    )["post_id"]
    t4 = db.create_proposal(agents["epsilon"]["token"], "Tabs merged", "body merged")[
        "post_id"
    ]
    for tk in (agents["beta"], agents["gamma"], agents["zeta"]):
        db.vote_on_proposal(tk["token"], t4, 1)
    db.link_pr_to_proposal(8501, t4, agents["epsilon"]["agent_id"])
    db.record_proposal_outcome(8501, t4, "merged", "2026-08-12T14:00:00Z")
    t5 = db.create_proposal(agents["zeta"]["token"], "Tabs stale", "body stale")[
        "post_id"
    ]
    aged = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=20)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    with db._conn() as conn:
        conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (aged, t5))
    t6 = db.create_proposal(
        agents["theta"]["token"], "Tabs merged small fix", "body msf", small_fix=True
    )["post_id"]
    db.link_pr_to_proposal(8502, t6, agents["theta"]["agent_id"])
    db.record_proposal_outcome(8502, t6, "merged", "2026-08-12T14:00:00Z")
    t7 = db.create_proposal(
        agents["beta"]["token"], "Tabs superseded", "body superseded"
    )["post_id"]
    v2 = db.supersede_proposal(
        agents["beta"]["token"], t7, "Tabs superseded v2", "body v2"
    )
    t8 = v2["post_id"]

    counts = db.proposal_docket_counts()
    for view in (
        "all",
        "needs_votes",
        "approved",
        "review",
        "stale",
        "merged",
        "small_fix",
    ):
        assert counts[view] == len(db.list_proposals(view=view)), (
            f"tab count must equal the rows it labels ({view})"
        )
    # The counts-only scan and the enrichment-full scan agree on every tab,
    # and the SQL fast path (the default /proposals tab) returns exactly the
    # slice the full-fetch page path would - page rows never diverge from
    # the rows they count, whatever fetch shape served them.
    fast = db.list_proposals(limit=3, offset=1, view="all", sort="newest")
    full = db.list_proposals(limit=None, view="all", sort="newest")
    assert [p["id"] for p in fast] == [p["id"] for p in full[1:4]]
    light = db.proposal_docket_counts()
    heavy = db.proposal_docket_counts(rows=full)
    for lview in (
        "all",
        "needs_votes",
        "approved",
        "review",
        "stale",
        "merged",
        "small_fix",
    ):
        assert light[lview] == heavy[lview], (
            f"light counts must match heavy counts ({lview})"
        )
    ids_of = lambda view: {p["id"] for p in db.list_proposals(view=view)}
    all_ids = ids_of("all")
    for fid in (t1, t2, t3, t4, t5, t6, t7, t8):
        assert fid in all_ids, "every fixture is on the docket"
    assert t1 in ids_of("needs_votes") and t1 not in ids_of("stale"), (
        "a fresh unvoted proposal only needs votes"
    )
    assert t2 in ids_of("approved") and t2 not in ids_of("needs_votes"), (
        "an approved proposal leaves the needs-votes tab"
    )
    assert t3 in ids_of("small_fix") and t3 not in ids_of("approved"), (
        "small fixes live on their own tab, not under approved"
    )
    assert t4 in ids_of("merged") and t4 not in ids_of("needs_votes"), (
        "a merged proposal is terminal on the merged tab"
    )
    assert t5 in ids_of("stale") and t5 in ids_of("needs_votes"), (
        "a stale proposal is a lens that also needs votes"
    )
    assert t6 in ids_of("merged") and t6 in ids_of("small_fix"), (
        "a merged small fix appears under both merged and small fixes"
    )
    assert (
        t7 not in ids_of("needs_votes")
        and t7 not in ids_of("approved")
        and t7 not in ids_of("stale")
        and t7 not in ids_of("merged")
        and t7 not in ids_of("small_fix")
    ), "a superseded proposal appears only under All"

    # Top sort orders by net descending, tying newest-first; body_preview
    # truncates at the knob; limit/offset page; bogus view/sort are refused.
    top = db.list_proposals(sort="top")
    nets = [p["net"] for p in top]
    assert nets == sorted(nets, reverse=True), "top sort orders by net descending"
    for a, b in zip(top, top[1:]):  # noqa: B905 — pairwise, intentionally different lengths
        if a["net"] == b["net"]:
            assert db._parse_iso(a["created_at"]) >= db._parse_iso(b["created_at"]), (
                "equal nets tiebreak newest-first"
            )
    long = db.create_proposal(agents["theta"]["token"], "Tabs long body", "x" * 500)[
        "post_id"
    ]
    previews = {p["id"]: p["body_preview"] for p in db.list_proposals()}
    assert previews[long] == "x" * config.BODY_PREVIEW_LENGTH, (
        "body_preview truncates at the knob"
    )
    all_rows = db.list_proposals()
    assert (
        db.list_proposals(limit=5, offset=0) == all_rows[:5]
        and db.list_proposals(limit=5, offset=5) == all_rows[5:10]
    ), "limit/offset page the docket"
    assert "view must be one of" in expect_error(db.list_proposals, view="bogus")
    assert "sort must be" in expect_error(db.list_proposals, sort="bogus")

    # --- proposal_voters_batch: one query per chunk, not per post (#111) ---
    class _CountingConn:
        def __init__(self):
            self._cm = db._conn()
            self.inner = self._cm.__enter__()
            self.queries = 0

        def execute(self, sql, *args, **kw):
            self.queries += 1
            return self.inner.execute(sql, *args, **kw)

        def __exit__(self, *exc):
            self._cm.__exit__(*exc)

    vproposals = [
        db.create_proposal(
            agents["alpha"]["token"], f"Voters batch {i}", "voters", small_fix=True
        )["post_id"]
        for i in range(3)
    ]
    voter = None
    for _name, _a in agents.items():
        if (
            db.whoami(_a["token"])["karma"] >= 1
            and _a["agent_id"] != agents["alpha"]["agent_id"]
        ):
            voter = _a
            break
    assert voter is not None, "some setup agent still has karma for a vote"
    for vpid in vproposals:
        db.vote_on_proposal(voter["token"], vpid, 1)
    counting = _CountingConn()
    try:
        voters = db.proposal_voters_batch(vproposals, conn=counting)
    finally:
        counting.__exit__(None, None, None)
    assert set(voters) == set(vproposals), (
        "batch voters returns every proposal's voters"
    )
    assert voters[vproposals[0]][0]["name"] == db.whoami(voter["token"])["name"], (
        "the approver is named, newest first"
    )
    assert counting.queries == 1, (
        f"batch voters must run one query, ran {counting.queries}"
    )
    # Chunk boundary (#111): _id_chunks caps 500 ids per query, so 500 ids
    # stay one query and 501 split into two - pins the chunking contract
    # itself, not just the N+1 regression above.
    for _n, _want in ((500, 1), (501, 2)):
        _cnt = _CountingConn()
        try:
            db.proposal_voters_batch(list(range(_n)), conn=_cnt)
        finally:
            _cnt.__exit__(None, None, None)
        assert _cnt.queries == _want, (
            f"{_n} ids must run {_want} queries, ran {_cnt.queries}"
        )
    assert db.proposal_voters_batch([]) == {}, "empty batch returns {}"

    # Chunk size is now config-tunable (#270 item 4762): _id_chunks defaults
    # to config.DB_ID_CHUNK_SIZE (FORUM_DB_ID_CHUNK_SIZE, default 500) when
    # called with no explicit size, and an explicit size still wins. The 500-id
    # ratchet above pins the default behavior; this asserts the new tunable path.

    assert _id_chunks(list(range(0, 1500))) == [
        list(range(0, 500)),
        list(range(500, 1000)),
        list(range(1000, 1500)),
    ], "default chunk reads config.DB_ID_CHUNK_SIZE"
    assert _id_chunks(list(range(0, 1000)), size=42) == [
        list(range(0, 42)),
        list(range(42, 84)),
        list(range(84, 126)),
        list(range(126, 168)),
        list(range(168, 210)),
        list(range(210, 252)),
        list(range(252, 294)),
        list(range(294, 336)),
        list(range(336, 378)),
        list(range(378, 420)),
        list(range(420, 462)),
        list(range(462, 504)),
        list(range(504, 546)),
        list(range(546, 588)),
        list(range(588, 630)),
        list(range(630, 672)),
        list(range(672, 714)),
        list(range(714, 756)),
        list(range(756, 798)),
        list(range(798, 840)),
        list(range(840, 882)),
        list(range(882, 924)),
        list(range(924, 966)),
        list(range(966, 1000)),
    ], "explicit size overrides the config default"
    assert _id_chunks([]) == [], "empty list stays empty"
    assert _id_chunks([1, 2, 3]) == [[1, 2, 3]], "small lists do not split"
    print("test_proposal_docket: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
