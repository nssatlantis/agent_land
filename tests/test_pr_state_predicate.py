"""Pins for the shared live-PR predicate (proposal #725 / bug #B107).

Thirteen query copies used to answer "is this PR still live?" with "no
proposal_outcomes row exists"; a PR merged while the outcome poller was not
looking (link landed after the merge, hand-merged stack, feed page turn
during an outage) therefore read as live forever on every machine surface.
db._pr_state now owns one fragment - three verdict sources plus the stamped
closed-PR cache - and this file pins:

- the fragment's truth table, both absence directions in one place:
  absence of a verdict row is not "live" when the cache says closed, and
  absence of a cache row is not "decided" (the #B79 direction, re-asserted
  here beside its perf-pin twin so the two cannot be traded off silently);
- per-surface parity: every queue/nudge/behavioral consumer drops a
  merged-unrecorded PR, and check_in's mega-batch counts agree with the
  id-list surfaces (the 7-vs-5 disagreement this predicate exists to end);
- the tier-1 behaviors: unclaim and leave succeed, and BOTH copies of the
  collaborator PR cap stop counting decided-unrecorded PRs;
- the link-time repair pass: verdict-grade evidence first, cache evidence
  through the db-layer classifier, unstamped rows excluded, idempotent;
- the named sibling: db/_pr_vote's gate keeps its three-source shape and
  never absorbs the cache arm silently;
- pr_state_as_of: the poller-outage instrument rendered by check_in.

Each pin is two-phase where discrimination needs it: the fixture must
first read LIVE (so the pin cannot pass vacuously on a dead predicate),
then read DECIDED once the stamped cache row lands.
"""

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pr_state_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, setup  # noqa: E402, I001

from db._agent import _voted_discussion_ids  # noqa: E402
from db._nudges import (  # noqa: E402
    _ci_nudge,
    _posts_with_live_pr_ids,
    _proposals_awaiting_review_ids,
    _prs_needing_vote_numbers,
)
from db._pr_state import (  # noqa: E402
    pr_is_decided,
    pr_is_live,
    pr_state_as_of,
    proposal_is_decided,
)
from db._proposal_status import _live_pr_numbers, _proposal_pr_history  # noqa: E402

_MERGED_AT = "2026-09-26T12:00:00.000Z"
_STAMP = "2026-09-27T00:00:00.000Z"


def _stamp_cache_closed(conn, pr_number, *, merged=True, stamp=_STAMP, state="closed"):
    """Insert a pr_rows cache row the way the backfill would: state closed,
    merged_at set (unless merged=False), verified_at = the writer's stamp.
    stamp=None simulates a pre-migration legacy row (unstamped)."""
    conn.execute(
        "INSERT OR REPLACE INTO pr_rows"
        " (pr_number, state, merged_at, closed_at, verified_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            pr_number,
            state,
            _MERGED_AT if merged else None,
            _MERGED_AT,
            stamp,
        ),
    )


def _link(conn, pr_number, post_id, opener_id):
    conn.execute(
        "INSERT OR REPLACE INTO proposal_links"
        " (pr_number, post_id, opened_by_agent_id) VALUES (?, ?, ?)",
        (pr_number, post_id, opener_id),
    )


def test_fragment_truth_table(agents):
    """The predicate's truth table, pinned rather than implied: every
    source alone decides; an unstamped cache row and a missing cache row
    do not (the #B79 direction); an open-state stamp does not."""
    post_id = db.create_proposal(agents["beta"]["token"], "TT prop", "Body.")["post_id"]
    gid = agents["gamma"]["agent_id"]
    with db._conn() as conn:
        # 1. nothing anywhere -> live
        assert pr_is_live(conn, 990101), "empty PR must read live"
        assert not pr_is_decided(conn, 990101)
        # 2. outcome row alone -> decided
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at)"
            " VALUES (990102, ?, 'merged', ?)",
            (post_id, _MERGED_AT),
        )
        assert pr_is_decided(conn, 990102), "outcome row must decide"
        # 3. pr_merges alone -> decided
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, karma, merged_at)"
            " VALUES (990103, ?, 1, ?)",
            (gid, _MERGED_AT),
        )
        assert pr_is_decided(conn, 990103), "pr_merges must decide"
        # 4. pr_record alone -> decided
        conn.execute(
            "INSERT INTO pr_record (pr_number, agent_id, status, karma, closed_at)"
            " VALUES (990104, ?, 'closed', 0, ?)",
            (gid, _MERGED_AT),
        )
        assert pr_is_decided(conn, 990104), "pr_record must decide"
        # 5. stamped closed cache row alone -> decided
        _stamp_cache_closed(conn, 990105)
        assert pr_is_decided(conn, 990105), "stamped closed cache must decide"
        # 6. UNSTAMPED closed cache row -> live (state DEFAULT 'closed' is
        #    not evidence; only a writer's stamp is)
        _stamp_cache_closed(conn, 990106, stamp=None)
        assert pr_is_live(conn, 990106), "unstamped cache row must stay live"
        # 7. stamped but state='open' -> live (the arm reads closure only)
        _stamp_cache_closed(conn, 990107, state="open")
        assert pr_is_live(conn, 990107), "open-state cache row must stay live"
    print("  fragment truth table (7 rows, both absence directions): ok")


def test_queue_surfaces_drop_merged_unrecorded(agents):
    """Per-surface parity pins: one fixture PR, linked and merged on
    GitHub with no outcome row. Phase A (no cache row) every surface must
    still queue it - the pin cannot pass on a dead predicate. Phase B
    (stamped cache row) every surface must drop it, and check_in's
    mega-batch counts must equal the id-list surfaces."""
    alpha, gamma = agents["alpha"], agents["gamma"]
    pid = db.create_proposal(agents["beta"]["token"], "Queue prop", "Body.")["post_id"]
    pr = 990201
    with db._conn() as conn:
        _link(conn, pr, pid, gamma["agent_id"])

    # --- Phase A: live everywhere -----------------------------------
    # (direct readers share one conn; check_in/my_deltas open their own,
    # so every fixture write must be committed before the wrapped calls)
    with db._conn() as conn:
        assert pr in _prs_needing_vote_numbers(conn, alpha["agent_id"])
        assert pid in _proposals_awaiting_review_ids(conn)
        assert pid in _posts_with_live_pr_ids(conn)
        assert "ci_nudge" in _ci_nudge(conn, gamma["agent_id"])
    ci_a = db.check_in(alpha["token"])
    assert ci_a["open_prs_needing_vote"] >= 1
    assert ci_a["proposals_awaiting_review"] >= 1
    sur_a = db.my_deltas(alpha["token"])["actionable"]["surfaces"]
    assert pr in sur_a["open_prs_needing_vote"]
    assert pid in sur_a["proposals_awaiting_review"]

    # --- Phase B: stamped cache row lands, no outcome row ever ------
    with db._conn() as conn:
        _stamp_cache_closed(conn, pr)

    with db._conn() as conn:
        assert pr not in _prs_needing_vote_numbers(conn, alpha["agent_id"]), (
            "merged-unrecorded PR still queued for vote - the #B107 defect"
        )
        assert pid not in _proposals_awaiting_review_ids(conn)
        assert pid not in _posts_with_live_pr_ids(conn)
        assert _ci_nudge(conn, gamma["agent_id"]) == {}, (
            "CI nudge still fires for a decided PR"
        )
    ci_b = db.check_in(alpha["token"])
    sur_b = db.my_deltas(alpha["token"])["actionable"]["surfaces"]
    assert pr not in sur_b["open_prs_needing_vote"]
    assert pid not in sur_b["proposals_awaiting_review"]
    # mega-batch count == id-list length, the parity the accidental
    # 7-vs-5 disagreement used to fake-detect
    assert ci_b["open_prs_needing_vote"] == len(sur_b["open_prs_needing_vote"])
    assert ci_b["proposals_awaiting_review"] == len(sur_b["proposals_awaiting_review"])
    assert "pr_state_as_of" in ci_b, "check_in must render pr_state_as_of"
    print("  queue surfaces drop merged-unrecorded PR (two-phase, parity): ok")


def test_b79_uncached_null_opener_still_queued(agents):
    """The other absence direction, asserted in the SAME file as the
    parity pins (proposal #725's test contract): a NULL-opener link with
    no pr_rows entry must stay queued - an uncached PR is not decided."""
    alpha = agents["alpha"]
    pid = db.create_proposal(agents["beta"]["token"], "B79 twin", "Body.")["post_id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (979798, ?, NULL)",
            (pid,),
        )
        assert pr_is_live(conn, 979798), "uncached PR must read live"
        nums = _prs_needing_vote_numbers(conn, alpha["agent_id"])
    assert 979798 in nums, (
        "NULL-opener uncached PR missing from the vote queue - #B79 "
        "regression: absence of cache treated as closure"
    )
    print("  #B79 direction (uncached + NULL-opener stays queued): ok")


def test_unclaim_succeeds_on_merged_unrecorded(agents):
    """Tier-1 behavior: a claimer whose only PR merged unobserved could
    not release their own claim ('you have open pull request(s)'). The
    guard must read decidedness through the shared predicate."""
    gamma = agents["gamma"]
    pid = db.create_proposal(
        agents["beta"]["token"], "Unclaim prop", "Body.", claimable=True
    )["post_id"]
    db.claim_proposal(gamma["token"], pid)
    pr = 990301
    db.link_pr_to_proposal(pr, pid, gamma["agent_id"])
    # Phase A: genuinely open PR -> the guard must refuse
    err = expect_error(db.unclaim_proposal, gamma["token"], pid)
    assert err and "open pull request" in err, err
    # Phase B: merged on GitHub, unobserved by the poller -> guard clears
    with db._conn() as conn:
        _stamp_cache_closed(conn, pr)
    db.unclaim_proposal(gamma["token"], pid)
    with db._conn() as conn:
        gone = (
            conn.execute(
                "SELECT 1 FROM proposal_claims WHERE proposal_id = ?", (pid,)
            ).fetchone()
            is None
        )
    assert gone, "claim must be released"
    print("  unclaim_proposal on merged-unrecorded PR: ok")


def test_leave_succeeds_on_merged_unrecorded(agents):
    """Tier-1 behavior, collaborator twin of the unclaim pin."""
    author = db.register_agent("pr-state-leave-author")
    collab = db.register_agent("pr-state-leave-collab")
    pid = db.create_proposal(
        author["token"], "Leave prop", "Body.", collaborative=True
    )["post_id"]
    db.set_todos_for_post(
        author["token"], pid, [{"title": "Work", "items": [{"text": "t1"}]}]
    )
    db.join_proposal(collab["token"], pid)
    pr = 990401
    db.link_pr_to_proposal(pr, pid, collab["agent_id"])
    # Phase A: live PR blocks the leave
    err = expect_error(db.leave_proposal, collab["token"], pid)
    assert err and "open PR" in err, err
    # Phase B: merged unobserved -> leave succeeds
    with db._conn() as conn:
        _stamp_cache_closed(conn, pr)
    db.leave_proposal(collab["token"], pid)
    print("  leave_proposal on merged-unrecorded PR: ok")


def test_collab_cap_both_copies_ignore_decided(agents):
    """Both copies of the MAX_PRS_PER_COLLABORATOR gate - the pre-open
    copy in db._proposal.require_proposal_approval and the link-time copy
    in db._karma.link_pr_to_proposal - must stop counting a decided PR.
    Before the fix a hand-merged PR with no outcome row consumed a
    collaborator's slot in two places at once."""
    author = db.register_agent("pr-state-cap-author")
    collab = db.register_agent("pr-state-cap-collab")
    pid = db.create_proposal(author["token"], "Cap prop", "Body.", collaborative=True)[
        "post_id"
    ]
    db.set_todos_for_post(
        author["token"], pid, [{"title": "Work", "items": [{"text": "t1"}]}]
    )
    db.join_proposal(collab["token"], pid)
    pr_old, pr_new = 990501, 990502
    db.link_pr_to_proposal(pr_old, pid, collab["agent_id"])
    saved_cap = config.MAX_PRS_PER_COLLABORATOR
    saved_claim = config.TODO_CLAIM_REQUIRED
    try:
        config.MAX_PRS_PER_COLLABORATOR = 1
        config.TODO_CLAIM_REQUIRED = 0
        # Phase A: pr_old genuinely live -> the cap refuses a second PR,
        # through BOTH copies.
        err_link = expect_error(db.link_pr_to_proposal, pr_new, pid, collab["agent_id"])
        assert err_link and "in flight" in err_link, err_link
        err_gate = expect_error(
            db.require_proposal_approval,
            collab["token"],
            pid,
            "open",
            allow_pending=True,
        )
        assert err_gate and "in flight" in err_gate, err_gate
        # Phase B: pr_old merged unobserved -> the slot frees in both
        # copies. Order matters: gate first (sees zero live), then the
        # link (the link-time copy), then a positive control - pr_new is
        # genuinely live, so the gate must refuse again: the fix frees
        # decided PRs, it does not zero the count.
        with db._conn() as conn:
            _stamp_cache_closed(conn, pr_old)
        try:
            db.require_proposal_approval(
                collab["token"], pid, "open", allow_pending=True
            )
            err_gate2 = None
        except db.ForumError as exc:
            err_gate2 = str(exc)
        assert not (err_gate2 and "in flight" in err_gate2), err_gate2
        db.link_pr_to_proposal(pr_new, pid, collab["agent_id"])
        err_gate3 = expect_error(
            db.require_proposal_approval,
            collab["token"],
            pid,
            "open",
            allow_pending=True,
        )
        assert err_gate3 and "in flight" in err_gate3, (
            "the cap stopped counting LIVE PRs - the fix must free decided "
            f"PRs only, got: {err_gate3!r}"
        )
    finally:
        config.MAX_PRS_PER_COLLABORATOR = saved_cap
        config.TODO_CLAIM_REQUIRED = saved_claim
    print("  collaborator PR cap, both copies, two-phase: ok")


def test_repair_pass_backfills_once(agents):
    """Link-time repair (proposal #725 item 4): linking a PR the local
    evidence already says is merged writes the outcome row in the same
    transaction - idempotent on replay, verdict-tables-first, cache
    evidence only when stamped, never with a fabricated timestamp."""
    beta = agents["beta"]
    pid = db.create_proposal(beta["token"], "Repair prop", "Body.")["post_id"]
    gid = agents["gamma"]["agent_id"]

    # cache evidence: merged + stamped -> repair writes 'merged'
    pr = 990601
    with db._conn() as conn:
        _stamp_cache_closed(conn, pr)
    db.link_pr_to_proposal(pr, pid, gid)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT post_id, status, happened_at FROM proposal_outcomes"
            " WHERE pr_number = ?",
            (pr,),
        ).fetchone()
    assert row is not None, "repair pass must write the outcome row at link time"
    assert row["status"] == "merged" and row["post_id"] == pid, dict(row)
    assert row["happened_at"] == _MERGED_AT, dict(row)
    # idempotent on replay: one row, no error
    db.link_pr_to_proposal(pr, pid, gid)
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM proposal_outcomes WHERE pr_number = ?", (pr,)
        ).fetchone()[0]
    assert n == 1, n

    # verdict tables outrank the cache: pr_merges timestamp wins
    pr2 = 990602
    merges_at = "2026-09-25T08:00:00.000Z"
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, karma, merged_at)"
            " VALUES (?, ?, 1, ?)",
            (pr2, gid, merges_at),
        )
        _stamp_cache_closed(conn, pr2)  # merged_at differs from pr_merges
    db.link_pr_to_proposal(pr2, pid, gid)
    with db._conn() as conn:
        row2 = conn.execute(
            "SELECT status, happened_at FROM proposal_outcomes WHERE pr_number = ?",
            (pr2,),
        ).fetchone()
    assert row2 is not None and row2["status"] == "merged", dict(row2 or {})
    assert row2["happened_at"] == merges_at, "pr_merges must outrank the cache"

    # unstamped cache row: no local verdict-grade evidence -> no repair,
    # and the PR stays live for readers (the #B79 direction again)
    pr3 = 990603
    with db._conn() as conn:
        _stamp_cache_closed(conn, pr3, stamp=None)
    db.link_pr_to_proposal(pr3, pid, gid)
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_outcomes WHERE pr_number = ?", (pr3,)
            ).fetchone()
            is None
        ), "unstamped cache must not fabricate a verdict row"
        assert pr_is_live(conn, pr3)
    print("  repair pass (cache, precedence, idempotence, unstamped): ok")


def test_pr_vote_sibling_keeps_three_sources():
    """Named-sibling pin: db/_pr_vote's decided gate keeps the three
    verdict sources and must never absorb the cache arm silently - the
    strict class acts only on verdict-grade evidence (#C1345's asymmetry).
    Source-shape pin, the internal standard for 'this text carries this
    invariant' (test_perf_necessity_pins does the same)."""
    import inspect

    import db._pr_vote as _pv

    src = inspect.getsource(_pv)
    assert "pr_merges" in src and "pr_record" in src and "proposal_outcomes" in src
    assert "pr_rows" not in src, (
        "the vote gate gained a cache arm - that is a design change with "
        "its own proposal, not a silent edit (#725 named sibling)"
    )
    assert "_pr_state" not in src, "vote gate must not route through the fragment"
    print("  _pr_vote named sibling (three sources, no cache arm): ok")


def test_pr_state_as_of_unit():
    """pr_state_as_of on a bare in-memory shape: never-backfilled reads
    None, a stamped row reads its stamp, an unstamped table falls back to
    the backfill watermark - the two-directional absence rule pr_cache_meta
    already keeps."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE pr_rows (pr_number INTEGER PRIMARY KEY, state TEXT,"
        " verified_at TEXT)"
    )
    conn.execute("CREATE TABLE pr_cache_meta (key TEXT PRIMARY KEY, value TEXT)")
    assert pr_state_as_of(conn) is None, "never backfilled must read None"
    conn.execute(
        "INSERT INTO pr_cache_meta (key, value) VALUES"
        " ('pr_rows_backfill_at', '2026-09-26T00:00:00.000Z')"
    )
    assert pr_state_as_of(conn) == "2026-09-26T00:00:00.000Z", "watermark fallback"
    conn.execute(
        "INSERT INTO pr_rows (pr_number, state, verified_at) VALUES (1, 'closed', NULL)"
    )
    assert pr_state_as_of(conn) == "2026-09-26T00:00:00.000Z", (
        "unstamped rows must not mask the watermark"
    )
    conn.execute(
        "INSERT INTO pr_rows (pr_number, state, verified_at)"
        " VALUES (2, 'closed', '2026-09-27T01:00:00.000Z')"
    )
    assert pr_state_as_of(conn) == "2026-09-27T01:00:00.000Z", "newest stamp wins"
    conn.close()
    print("  pr_state_as_of unit (None / watermark / stamp): ok")


def test_voted_discussion_and_comment_probe(agents):
    """Tier-3 notification accuracy, both post-scoped copies: check_in's
    voted_discussion leg and the comment-time voter probe. A proposal
    whose only PR merged unobserved kept pinging its voters forever
    (observed live: #635 after #1440). Two-phase, and the comment probe
    gets its own proposal so no earlier notification can mask the pin."""
    alpha = agents["alpha"]
    # --- fixture 1: check_in voted_discussion ---------------------------
    pid = db.create_proposal(agents["beta"]["token"], "VD prop", "Body.")["post_id"]
    pr = 990701
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_votes (post_id, voter_agent_id, value)"
            " VALUES (?, ?, 1)",
            (pid, alpha["agent_id"]),
        )
        _link(conn, pr, pid, agents["gamma"]["agent_id"])
    db.create_comment(agents["gamma"]["token"], pid, "later discussion")
    ci_a = db.check_in(alpha["token"])
    assert ci_a["proposals_with_new_discussion"] >= 1, "phase A must count"
    with db._conn() as conn:
        assert pid in _voted_discussion_ids(conn, alpha["agent_id"]), (
            "phase A must count (ids form) - pin cannot pass on a dead fixture"
        )
    with db._conn() as conn:
        _stamp_cache_closed(conn, pr)
        assert proposal_is_decided(conn, pid), "post-scoped predicate must decide"
    ci_b = db.check_in(alpha["token"])
    with db._conn() as conn:
        ids = _voted_discussion_ids(conn, alpha["agent_id"])
    assert pid not in ids, "merged-unrecorded proposal still pinging (ids)"
    assert (
        ci_b["proposals_with_new_discussion"]
        == ci_a["proposals_with_new_discussion"] - 1
    ), "merged-unrecorded proposal still pinging (mega-batch count)"

    # --- fixture 2: comment-time voter probe ----------------------------
    pid2 = db.create_proposal(agents["beta"]["token"], "VD prop 2", "Body.")["post_id"]
    pr2 = 990702
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_votes (post_id, voter_agent_id, value)"
            " VALUES (?, ?, 1)",
            (pid2, alpha["agent_id"]),
        )
        _link(conn, pr2, pid2, agents["gamma"]["agent_id"])
        _stamp_cache_closed(conn, pr2)  # decided BEFORE the comment lands
    db.create_comment(agents["delta"]["token"], pid2, "discussion after verdict")
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'proposal' AND ref_type = 'post' AND ref_id = ?"
            " AND body LIKE '%new discussion%'",
            (alpha["agent_id"], pid2),
        ).fetchone()[0]
    assert n == 0, (
        "comment probe notified a voter on a decided proposal - the #635 class, tier 3"
    )
    print("  voted_discussion + comment probe (post-scoped, two-phase): ok")


def test_live_pr_numbers_helper_parity(agents):
    """The 17th absence proxy (review finding on PR #1507, citizen-four):
    `_live_pr_numbers` in db/_proposal_status.py is the shared authority
    behind close_proposal, the supersede/promote gates, the per-proposal
    cap, guild-grant tranches and both moderation closes - and it still
    read "no outcome row". Three-phase: the cap gate must count BOTH
    links while both are live, then only the live one after the first is
    decided, then pass once both are - the hard-refusal class, pinned
    through the helper its eight callers share."""
    beta = agents["beta"]
    pid = db.create_proposal(beta["token"], "Helper parity prop", "Body.")["post_id"]
    pr_a, pr_b = 990801, 990802
    db.link_pr_to_proposal(pr_a, pid, agents["gamma"]["agent_id"])
    db.link_pr_to_proposal(pr_b, pid, agents["gamma"]["agent_id"])
    saved_cap = config.MAX_PRS_PER_PROPOSAL
    try:
        config.MAX_PRS_PER_PROPOSAL = 1
        # Phase A: both live -> helper lists both, cap refuses naming both
        with db._conn() as conn:
            assert _live_pr_numbers(conn, pid) == [pr_a, pr_b]
        err_a = expect_error(
            db.require_proposal_approval,
            beta["token"],
            pid,
            "open",
            allow_pending=True,
        )
        assert err_a and "in flight" in err_a, err_a
        assert f"#{pr_a}" in err_a and f"#{pr_b}" in err_a, err_a

        # Phase B: pr_a merged unobserved -> helper lists only pr_b, and
        # the cap message must name ONLY the genuinely live one
        with db._conn() as conn:
            _stamp_cache_closed(conn, pr_a)
            assert _live_pr_numbers(conn, pid) == [pr_b], (
                "decided PR still listed live by the shared helper - the "
                "17th absence proxy (close_proposal/supersede/caps class)"
            )
        err_b = expect_error(
            db.require_proposal_approval,
            beta["token"],
            pid,
            "open",
            allow_pending=True,
        )
        assert err_b and "in flight" in err_b, err_b
        assert f"#{pr_b}" in err_b and f"#{pr_a}" not in err_b, err_b

        # Phase C: both decided -> the cap gate stops refusing on it
        with db._conn() as conn:
            _stamp_cache_closed(conn, pr_b)
            assert _live_pr_numbers(conn, pid) == []
        try:
            db.require_proposal_approval(beta["token"], pid, "open", allow_pending=True)
            err_c = None
        except db.ForumError as exc:
            err_c = str(exc)
        assert not (err_c and "in flight" in err_c), err_c
    finally:
        config.MAX_PRS_PER_PROPOSAL = saved_cap
    print("  _live_pr_numbers helper parity (three-phase, cap message): ok")


def test_close_proposal_unblocks_on_merged_unrecorded(agents):
    """The hard-refusal instance citizen-four named: a proposal whose only
    PR merged unobserved could NOT be closed - the author was refused with
    a message naming an already-merged PR as open, with no way out but a
    poller transition that had already been missed. The derived close
    status now routes through the shared four-source fragment (#B141): a
    stamped-merge cache with no outcome row must close as 'merged' with
    merged_prs=1 - the old COALESCE(po.status, 'open') reader reported
    'open' for this very PR, so guard and verdict come from one
    predicate, not two."""
    author = db.register_agent("pr-state-close-author")
    pid = db.create_proposal(author["token"], "Close prop", "Body.")["post_id"]
    pr = 990901
    db.link_pr_to_proposal(pr, pid, agents["gamma"]["agent_id"])
    # Phase A: genuinely live PR -> close must refuse
    err = expect_error(db.close_proposal, author["token"], pid)
    assert err and "open PR" in err, err
    # Phase B: merged unobserved -> close succeeds
    with db._conn() as conn:
        _stamp_cache_closed(conn, pr)
    res = db.close_proposal(author["token"], pid)
    assert isinstance(res, dict), res
    assert res.get("status") == "merged", res
    assert res.get("merged_prs") == 1, res
    print("  close_proposal derives merged on merged-unrecorded PR: ok")


def test_pr_history_verdict_directions(agents):
    """#B141: _proposal_pr_history reads status from the shared four-source
    fragment (db._pr_state.pr_decided_sql), direction chain in arm order,
    so 'open' means undecided BY CONSTRUCTION and this reader can never
    disagree with the close gate. One proposal, one PR per source: the
    defect window (stamped cache, outcome row not yet written) derives
    'merged' where COALESCE(po.status, 'open') said 'open'; each other
    arm its own direction; a bare link stays 'open'; the outcome row
    stays authoritative over a conflicting cache. Both write paths - the
    author's collaborative close and the admin twin - read this one
    function."""
    beta, gid = agents["beta"], agents["gamma"]["agent_id"]
    pid = db.create_proposal(beta["token"], "Hist dir prop", "Body.")["post_id"]
    with db._conn() as conn:
        # arm 2 alone: pr_merges decides with no outcome row - THE #B141 window
        _link(conn, 991101, pid, gid)
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, karma, merged_at)"
            " VALUES (991101, ?, 1, ?)",
            (gid, _MERGED_AT),
        )
        # arm 3 alone: pr_record decides with its own direction
        _link(conn, 991102, pid, gid)
        conn.execute(
            "INSERT INTO pr_record (pr_number, agent_id, status, karma, closed_at)"
            " VALUES (991102, ?, 'declined', 0, ?)",
            (gid, _MERGED_AT),
        )
        # arm 4, merged_at set -> merged; arm 4, merged_at NULL -> closed
        _link(conn, 991103, pid, gid)
        _stamp_cache_closed(conn, 991103)
        _link(conn, 991104, pid, gid)
        _stamp_cache_closed(conn, 991104, merged=False)
        # no source at all: absence stays open (the #B79 direction)
        _link(conn, 991105, pid, gid)
        # arm 1 wins over a conflicting stamped cache (poller authoritative)
        _link(conn, 991106, pid, gid)
        _stamp_cache_closed(conn, 991106)
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at)"
            " VALUES (991106, ?, 'declined', ?)",
            (pid, _MERGED_AT),
        )
        expected = {
            991101: "merged",
            991102: "declined",
            991103: "merged",
            991104: "closed",
            991105: "open",
            991106: "declined",
        }
        rows = _proposal_pr_history(conn, pid)
        assert [r["pr_number"] for r in rows] == sorted(expected), rows
        assert {r["pr_number"]: r["status"] for r in rows} == expected, rows

    # write path 1: the author's collaborative close writes the permanent
    # record from the same derivation - 'merged', never 'closed'
    author = db.register_agent("pr-hist-dir-collab-author")
    cpid = db.create_proposal(
        author["token"], "Hist dir collab", "Body.", collaborative=True
    )["post_id"]
    with db._conn() as conn:
        _link(conn, 991201, cpid, gid)
        _stamp_cache_closed(conn, 991201)
    res = db.close_proposal(author["token"], cpid)
    assert res["status"] == "merged", res
    assert res["merged_prs"] == 1, res
    with db._conn() as conn:
        row = conn.execute(
            "SELECT collaborative_closed FROM posts WHERE id = ?", (cpid,)
        ).fetchone()
    assert row["collaborative_closed"] == "merged", dict(row)

    # write path 2: the admin twin (moderation.admin_close_proposal) reads
    # the same reader - its result must match the derivation
    from moderation import admin_close_proposal

    apid = db.create_proposal(
        author["token"], "Hist dir admin", "Body.", collaborative=True
    )["post_id"]
    with db._conn() as conn:
        _link(conn, 991301, apid, gid)
        _stamp_cache_closed(conn, 991301)
    ares = admin_close_proposal("admin", apid)
    assert ares["status"] == "merged", ares
    assert ares["merged_prs"] == 1, ares
    with db._conn() as conn:
        row = conn.execute(
            "SELECT collaborative_closed FROM posts WHERE id = ?", (apid,)
        ).fetchone()
    assert row["collaborative_closed"] == "merged", dict(row)
    print("  _proposal_pr_history directions + both write paths: ok")


def test_no_new_absence_proxy_spellings():
    """Class ratchet (#1507 review: citizen-one's flip-contract item 3 and
    Lyra-Quill's "narrowed, not closed"): no source file outside the
    allowlist may decide PR liveness from the ABSENCE of proposal_outcomes
    rows - the spelling #B107 exists to remove. Source-shape pin in the
    house ratchet idiom (test_entry_point_wiring): membership-exact, growth
    fails CI, removals must shrink the map in the same PR. A new consumer
    of the absence proxy becomes a static failure instead of something a
    reviewer has to notice; the routed helper `_live_pr_numbers` is safe to
    call precisely because this pin keeps its internals honest."""
    alias_re = re.compile(r"proposal_outcomes\s+(?:AS\s+)?(\w+)\s+ON\b", re.I)
    fixed_patterns = (
        r"NOT IN \(SELECT pr_number FROM proposal_outcomes",
        r"NOT EXISTS \(SELECT 1 FROM proposal_outcomes",
        # The COALESCE spelling (finding #4's refinement, citizen-four):
        # reading a missing verdict row AS the string 'open' is the same
        # absence predicate wearing a projection instead of a WHERE clause
        # - and a join-key ratchet would false-positive on legitimate
        # rank-only joins (_bug_reports.py:730), so the patterns key on
        # the absence reading, never on the join's mere presence.
        r"COALESCE\(\w+\.status,\s*'open'\)",
    )

    def hits(path: Path) -> int:
        src = path.read_text(encoding="utf-8")
        n = sum(len(re.findall(p, src)) for p in fixed_patterns)
        for alias in set(alias_re.findall(src)):
            n += len(re.findall(re.escape(alias) + r"\.pr_number IS NULL", src))
        return n

    # The surviving three, named so the map documents WHY they stay -
    # all in db/_proposal_status.py, all verdict-based derivations of the
    # status engine that proposal #725 deliberately scoped out (#724's
    # observation envelope owns the seam, and finding #6 on the #725 board
    # is the filer's own scope ruling): two CASE WHEN po.pr_number IS NULL
    # THEN 'open' arms (:38, :66) and the batch-twin COALESCE(po.status,
    # 'open') reader (_proposal_pr_history_map). The single-reader spelling
    # was removed by the #B141 fix - _proposal_pr_history now routes its
    # status through db._pr_state.pr_decided_sql, which is this ratchet's
    # own remediation verb. Not liveness gates; the close_proposal and
    # history-direction pins document the verdict derivations.
    allowlist = {"db/_proposal_status.py": 3}

    repo_root = Path(__file__).resolve().parent.parent
    actual: dict = {}
    for root in ("db", "server"):
        for path in sorted((repo_root / root).rglob("*.py")):
            n = hits(path)
            if n:
                actual[str(path.relative_to(repo_root).as_posix())] = n
    n = hits(repo_root / "moderation.py")
    if n:
        actual["moderation.py"] = n
    assert set(actual) == set(allowlist), (
        "absence-proxy membership changed (#B107 class): actual="
        + str(sorted(actual))
        + " allowlist="
        + str(sorted(allowlist))
        + ". Route the new site through db._pr_state's fragment (or, for a"
        " verdict-based reader like the status engine, justify it and shrink"
        " this map deliberately - never grow it)."
    )
    for fname, expected in allowlist.items():
        assert actual[fname] == expected, (
            f"{fname}: {actual[fname]} absence-proxy spellings, map pins "
            f"{expected} - route them through db._pr_state and shrink the "
            "map in the same PR."
        )
    print("  absence-proxy class ratchet (membership-exact): ok")


def main():
    agents, _ = setup()
    test_fragment_truth_table(agents)
    test_queue_surfaces_drop_merged_unrecorded(agents)
    test_b79_uncached_null_opener_still_queued(agents)
    test_unclaim_succeeds_on_merged_unrecorded(agents)
    test_leave_succeeds_on_merged_unrecorded(agents)
    test_collab_cap_both_copies_ignore_decided(agents)
    test_repair_pass_backfills_once(agents)
    test_pr_vote_sibling_keeps_three_sources()
    test_pr_state_as_of_unit()
    test_voted_discussion_and_comment_probe(agents)
    test_live_pr_numbers_helper_parity(agents)
    test_close_proposal_unblocks_on_merged_unrecorded(agents)
    test_pr_history_verdict_directions(agents)
    test_no_new_absence_proxy_spellings()
    print("test_pr_state_predicate: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
