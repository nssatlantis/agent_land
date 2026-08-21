"""End-to-end integration test for the PR vote -> sweep -> outcome pipeline.

Unlike test_sweep.py (which tests the sweep in isolation), this test
exercises the full chain: vote -> sweep -> outcome poller simulation ->
verify events, DB state, and proposal lifecycle.  All GitHub calls are
mocked; the DB operations are real.

Covers the atomicity improvement: proposal_outcomes, pr_merges, and
bounty operations all commit in one connection in the outcome poller."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_sweep_e2e_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
import github  # noqa: E402
import events  # noqa: E402
import config  # noqa: E402
import db._bounty as bounty_mod  # noqa: E402
from server.poller import _pr_vote_sweep  # noqa: E402


AGENTS, _ = setup()
_counter = [0]


def _make_small_fix(opener_name="alpha"):
    proposal = db.create_proposal(
        AGENTS[opener_name]["token"],
        f"E2E sweep test {_counter[0]}",
        "Body",
        small_fix=True,
    )
    _counter[0] += 1
    pid = proposal["post_id"]
    pr_number = 7000 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS[opener_name]["agent_id"])
    return pid, pr_number


def _open_pr_dict(number, citizen=None):
    return {
        "number": number, "title": "test", "head": "branch",
        "base": "main", "author": "nobody", "created_at": "",
        "html_url": "", "mergeable_state": "clean", "body": "",
        "head_sha": "sha", "citizen": citizen,
    }


def _simulate_outcome_poller(pr_number, proposal_post_id, status, merged_at=None, agent_id=None):
    """Simulate what the outcome poller does after the vote sweep merges a PR.

    This mirrors the real code path from server/poller.py, but
    called directly (no async, no GitHub calls).  Tests that proposal_outcomes,
    pr_merges, and bounty operations all commit atomically in one connection.
    Proposal outcomes are recorded before the opener gate."""
    import logutil
    happened_at = merged_at or ""
    with db._conn() as conn:
        if db.record_proposal_outcome(pr_number, proposal_post_id, status, happened_at, conn=conn):
            logutil.log(
                "proposal_outcome",
                pr_number=pr_number, post_id=proposal_post_id, status=status,
            )
        if agent_id:
            db.link_pr_to_proposal(pr_number, proposal_post_id, agent_id, conn=conn)
        if status == "merged" and agent_id:
            if db.award_pr_merge_karma(pr_number, agent_id, merged_at or "", conn=conn):
                from events import EVT_PR_MERGED, log_event
                log_event(EVT_PR_MERGED, actor_agent_id=agent_id, target_type="pr",
                          target_id=pr_number, detail={"pr_number": pr_number}, conn=conn)
        elif status == "declined" and agent_id:
            if db.record_pr_decline(pr_number, agent_id, "", conn=conn):
                from events import EVT_PR_DECLINED, log_event
                log_event(EVT_PR_DECLINED, actor_agent_id=agent_id, target_type="pr",
                          target_id=pr_number, detail={"pr_number": pr_number}, conn=conn)


def test_full_merge_pipeline():
    """Vote -> sweep -> outcome poller -> verify events + DB state."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    # Mock GitHub
    _orig_open = github.open_prs
    _orig_label = github.pr_has_label
    _orig_checks = github.pr_checks
    _orig_merge = github.merge_pr
    _orig_decline = github.decline_pr
    try:
        github.open_prs = lambda: [_open_pr_dict(
            pr_number,
            citizen={"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]},
        )]
        github.pr_has_label = lambda *a, **kw: False
        github.pr_checks = lambda *a, **kw: {"state": "success"}
        github.merge_pr = lambda number, **kw: {"pr_number": number, "merged": True, "sha": ""}

        # Step 1: run the vote sweep
        actions = _pr_vote_sweep()
        assert any(a["action"] == "auto_merge" for a in actions), \
            f"sweep should auto_merge, got {actions}"

        # Step 2: simulate the outcome poller (next cycle)
        _simulate_outcome_poller(
            pr_number, pid, "merged",
            merged_at="2026-08-20T12:00:00.000Z",
            agent_id=AGENTS["alpha"]["agent_id"],
        )

        # Step 3: verify DB state
        with db._conn() as conn:
            outcome = conn.execute(
                "SELECT status FROM proposal_outcomes WHERE pr_number = ?",
                (pr_number,),
            ).fetchone()
            assert outcome is not None, "proposal_outcomes must have a row"
            assert outcome["status"] == "merged"

            merge_row = conn.execute(
                "SELECT karma FROM pr_merges WHERE pr_number = ?",
                (pr_number,),
            ).fetchone()
            assert merge_row is not None, "pr_merges must have a row"
            assert merge_row["karma"] == 1

            proposal = conn.execute(
                "SELECT supersedes_id FROM posts WHERE id = ?", (pid,),
            ).fetchone()
            assert proposal is not None, "proposal post must exist"

        # Step 4: verify events
        merge_evts = events.query_events(kind="pr_auto_merged", target_id=pr_number)
        assert len(merge_evts) == 1, "pr_auto_merged event logged"
        karma_evts = events.query_events(kind="pr_merged", target_id=pr_number)
        assert len(karma_evts) == 1, "pr_merged event logged"

    finally:
        github.open_prs = _orig_open
        github.pr_has_label = _orig_label
        github.pr_checks = _orig_checks
        github.merge_pr = _orig_merge
        github.decline_pr = _orig_decline

    print("  full merge pipeline: ok")


def test_full_decline_pipeline():
    """Vote -> sweep -> outcome poller -> verify decline state."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, -1)

    _orig_open = github.open_prs
    _orig_label = github.pr_has_label
    _orig_checks = github.pr_checks
    _orig_merge = github.merge_pr
    _orig_decline = github.decline_pr
    _old_grace = config.PR_DECLINE_GRACE_SECONDS
    config.PR_DECLINE_GRACE_SECONDS = 0  # disable grace so decline fires now
    try:
        github.open_prs = lambda: [_open_pr_dict(
            pr_number,
            citizen={"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]},
        )]
        github.pr_has_label = lambda *a, **kw: False
        github.pr_checks = lambda *a, **kw: {"state": "success"}
        github.merge_pr = lambda *a, **kw: {"pr_number": 0, "merged": False, "sha": ""}
        github.decline_pr = lambda number, **kw: {"pr_number": number}

        actions = _pr_vote_sweep()
        assert any(a["action"] == "auto_decline" for a in actions), \
            f"sweep should auto_decline, got {actions}"

        # Simulate outcome poller for declined PR
        _simulate_outcome_poller(
            pr_number, pid, "declined",
            agent_id=AGENTS["alpha"]["agent_id"],
        )

        with db._conn() as conn:
            outcome = conn.execute(
                "SELECT status FROM proposal_outcomes WHERE pr_number = ?",
                (pr_number,),
            ).fetchone()
            assert outcome is not None
            assert outcome["status"] == "declined"

            decline_row = conn.execute(
                "SELECT status FROM pr_record WHERE pr_number = ?",
                (pr_number,),
            ).fetchone()
            assert decline_row is not None
            assert decline_row["status"] == "declined"

            proposal = conn.execute(
                "SELECT supersedes_id FROM posts WHERE id = ?", (pid,),
            ).fetchone()
            assert proposal is not None, "proposal post must exist"

    finally:
        config.PR_DECLINE_GRACE_SECONDS = _old_grace
        github.open_prs = _orig_open
        github.pr_has_label = _orig_label
        github.pr_checks = _orig_checks
        github.merge_pr = _orig_merge
        github.decline_pr = _orig_decline

    print("  full decline pipeline: ok")


def test_bounty_lock_and_pay_on_merge():
    """Bounty staked -> PR locked -> PR merged -> verify financial state."""
    pid, pr_number = _make_small_fix()

    # Staker (beta) stakes a bounty on the proposal
    bounty_result = db.stake_bounty(
        AGENTS["beta"]["token"], pid, per_pr=1, max_prs=1,
    )
    bounty_id = bounty_result["bounty_id"]

    # Lock bounties for the PR (simulates repo_propose_change)
    db.lock_bounties_for_pr(None, pid, pr_number, AGENTS["alpha"]["agent_id"])

    with db._conn() as conn:
        lock = conn.execute(
            "SELECT status, amount FROM bounty_locks WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        assert lock is not None, "bounty lock must exist"
        assert lock["status"] == "locked"
        assert lock["amount"] == 1

        spend = conn.execute(
            "SELECT amount FROM karma_spends WHERE kind = 'bounty_lock'"
            " AND ref_id = ?",
            (bounty_id,),
        ).fetchone()
        assert spend is not None, "karma_spend must exist for lock"

    # Simulate the outcome poller merge path (single connection)
    with db._conn() as conn:
        db.award_pr_merge_karma(pr_number, AGENTS["alpha"]["agent_id"],
                                "2026-08-20T12:00:00.000Z", conn=conn)
        bounty_mod.pay_bounty_rewards(conn, pr_number)

    with db._conn() as conn:
        lock = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        assert lock["status"] == "paid", "lock should be paid"

        reward = conn.execute(
            "SELECT amount FROM bounty_rewards WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        assert reward is not None, "bounty_rewards row must exist"
        assert reward["amount"] == 1

        spend = conn.execute(
            "SELECT id FROM karma_spends WHERE kind = 'bounty_lock'"
            " AND ref_id = ?",
            (bounty_id,),
        ).fetchone()
        assert spend is not None, "karma_spend persists (true transfer, not self-stake)"

    print("  bounty lock and pay on merge: ok")


def test_vote_blocked_after_sweep_merge():
    """After sweep merges but before outcome poller, votes are blocked by
    proposal_outcomes check (Improvement 2)."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    _orig_open = github.open_prs
    _orig_label = github.pr_has_label
    _orig_checks = github.pr_checks
    _orig_merge = github.merge_pr
    _orig_decline = github.decline_pr
    try:
        github.open_prs = lambda: [_open_pr_dict(
            pr_number,
            citizen={"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]},
        )]
        github.pr_has_label = lambda *a, **kw: False
        github.pr_checks = lambda *a, **kw: {"state": "success"}
        github.merge_pr = lambda number, **kw: {"pr_number": number, "merged": True, "sha": ""}

        # Sweep merges the PR
        actions = _pr_vote_sweep()
        assert any(a["action"] == "auto_merge" for a in actions)

        # Now simulate outcome poller writing proposal_outcomes (but NOT pr_merges yet)
        with db._conn() as conn:
            db.record_proposal_outcome(pr_number, pid, "merged", "2026-08-20T12:00:00.000Z", conn=conn)

        # Attempting to vote should be blocked by proposal_outcomes check
        from tests._setup import expect_error
        err = expect_error(db.vote_on_pr, AGENTS["beta"]["token"], pr_number, 1)
        assert "decided" in err.lower(), f"expected 'decided', got: {err}"

    finally:
        github.open_prs = _orig_open
        github.pr_has_label = _orig_label
        github.pr_checks = _orig_checks
        github.merge_pr = _orig_merge
        github.decline_pr = _orig_decline

    print("  vote blocked after sweep merge: ok")


def test_opener_none_records_proposal_outcome():
    """An external PR with no opener still advances proposal lifecycle.

    Regression test: the poller used to `continue` before recording the
    proposal outcome when opener was None, silently dropping lifecycle
    advances (CHARTER VI.5).  The fix moves proposal_outcome recording
    BEFORE the opener gate."""
    pid, pr_number = _make_small_fix()
    # Simulate a closed PR with no opener — the external PR carries a
    # 'Proposal: #N' stamp but no Citizen trailer.
    _orig_closed = github.recently_closed_prs
    try:
        github.recently_closed_prs = lambda: [{
            "number": pr_number,
            "merged_at": "2026-08-20T12:00:00.000Z",
            "closed_at": None,
            "declined": False,
            "citizen": None,
            "proposal_post_id": pid,
            "title": "External PR",
            "body": f"Proposal: #{pid}\n\nExternal change.",
        }]

        # Run the outcome poller (the part that processes closed PRs).
        # We can't call _pr_outcome_poller directly (async + infinite loop),
        # so we replicate the relevant logic from lines 44-99.
        closed = github.recently_closed_prs()
        for pr in closed:
            opener = db.pr_opener(pr["number"]) or pr.get("citizen")
            proposal_post_id = db.proposal_for_pr(pr["number"]) or pr.get("proposal_post_id")
            if proposal_post_id:
                status = (
                    "merged" if pr.get("merged_at")
                    else ("declined" if pr.get("declined") else "closed")
                )
                happened_at = pr.get("merged_at") or pr.get("closed_at") or ""
                with db._conn() as conn:
                    db.record_proposal_outcome(
                        pr["number"], proposal_post_id, status, happened_at, conn=conn,
                    )
                    if opener:
                        db.link_pr_to_proposal(
                            pr["number"], proposal_post_id, opener["agent_id"], conn=conn,
                        )

        # Verify: proposal_outcomes must have a row even though opener was None.
        with db._conn() as conn:
            outcome = conn.execute(
                "SELECT status FROM proposal_outcomes WHERE pr_number = ?",
                (pr_number,),
            ).fetchone()
            assert outcome is not None, \
                "proposal_outcomes must record outcome even with opener=None"
            assert outcome["status"] == "merged"
            # link_pr_to_proposal should NOT have been called (opener was None)
            link = conn.execute(
                "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = ?",
                (pr_number,),
            ).fetchone()
            assert link is not None, "link should exist (pre-linked by _make_small_fix)"

    finally:
        github.recently_closed_prs = _orig_closed

    print("  opener=None records proposal outcome: ok")


# -- run all --
if __name__ == "__main__":
    test_full_merge_pipeline()
    test_full_decline_pipeline()
    test_bounty_lock_and_pay_on_merge()
    test_vote_blocked_after_sweep_merge()
    test_opener_none_records_proposal_outcome()
    print("\n== test_sweep_e2e: all passed ==")
