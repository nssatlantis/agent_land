"""Test the staking system (the Karma Split): dual-currency stake,
lock, pay, refund, admin funding, supersede - plus the table rename
migration from the bounty-era names."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_staking_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db, expect_error, setup,
)


def ek(agent_id: int) -> int:
    """Get effective_karma using a throwaway connection."""
    with db._conn() as conn:
        return db.effective_karma(conn, agent_id)


def main():
    agents, post_id = setup()

    # --- schema migration ------------------------------------------------
    with db._conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()}
        bl_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(stake_locks)"
        ).fetchall()}
        pb_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(proposal_stakes)"
        ).fetchall()}
    assert "proposal_stakes" in tables
    assert "stake_locks" in tables
    assert "stake_rewards" in tables
    assert "karma_spend_id" in bl_cols, "stake_locks must have karma_spend_id"
    assert "currency" in pb_cols, "proposal_stakes must carry the currency column"
    assert "staker_agent_id" in pb_cols
    # staker_agent_id should be nullable (NOT NULL absent)
    with db._conn() as conn2:
        pk_row = [r for r in conn2.execute(
            "PRAGMA table_info(proposal_stakes)"
        ).fetchall() if r[1] == "staker_agent_id"][0]
    assert pk_row[3] == 0, (
        f"staker_agent_id must be nullable, got NOT_NULL={pk_row[3]}"
    )
    print("  staking schema: ok")

    # --- stake: happy path ----------------------------------------
    prop = db.create_proposal(agents["alpha"]["token"], "Bounty Prop", "Body")
    pid = prop["post_id"]
    # Vote it open so it has status 'open'
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], pid, 1)

    # alpha needs effective_karma >= per_pr*max_prs. Give alpha karma by
    # having others upvote alpha's post.
    for name in ("beta", "gamma", "delta", "epsilon", "zeta"):
        db.vote(agents[name]["token"], "post", post_id, 1)
    ek_before = ek(agents["alpha"]["agent_id"])
    assert ek_before >= 4, f"alpha needs >= 4 ek to stake, has {ek_before}"

    result = db.stake(agents["alpha"]["token"], pid, per_pr=2, max_prs=1, currency="karma")
    assert result["stake_id"] >= 1
    assert result["per_pr"] == 2
    assert result["max_prs"] == 1
    assert result["total"] == 2
    ek_after = ek(agents["alpha"]["agent_id"])
    assert ek_after == ek_before, (
        "stake must not deduct karma (deduction happens on lock)"
    )
    print("  stake happy: ok")

    # --- stake: validation errors ----------------------------------
    assert "per_pr must be at least 1" in expect_error(
        db.stake, agents["beta"]["token"], pid, 0, 1,
        currency="karma",
    )
    assert "at least 0.25 credits" in expect_error(
        db.stake, agents["beta"]["token"], pid, 0, 1,
        currency="credits",
    ), "the credit floor speaks in quarter units after conversion"
    assert "max_prs must be at least 1" in expect_error(
        db.stake, agents["beta"]["token"], pid, 1, 0
    )
    assert "balance of 10" in expect_error(
        db.stake, agents["fresh"]["token"], pid, 10, 10,
        currency="karma",
    ), "0-karma agent should be rejected"
    # non-open proposal
    merged = db.create_proposal(agents["alpha"]["token"], "Old Prop", "Body")
    merged_pid = merged["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], merged_pid, 1)
    db.record_proposal_outcome(
        9999, merged_pid, "merged", "2026-08-20T00:00:00.000Z"
    )
    assert "status 'merged'" in expect_error(
        db.stake, agents["beta"]["token"], merged_pid, 1, 1
    )
    print("  stake validation: ok")

    # --- stake: aggregate cap ----------------------------------------
    try:
        # alpha has ~4 ek; cap = int(4*0.33) = 1. Staking total=2 > 1 should fail.
        os.environ["FORUM_STAKE_MAX_FRACTION"] = "0.33"
        assert "aggregate" in expect_error(
            db.stake, agents["alpha"]["token"], pid, 2, 1
        ), "aggregate cap should block over-commitment"
        print("  stake aggregate cap: ok")
    finally:
        os.environ["FORUM_STAKE_MAX_FRACTION"] = "0"

    # --- lock_stakes_for_pr: charges staker, not PR opener --------------
    # gamma will open the PR; alpha is the staker
    ek_alpha_before = ek(agents["alpha"]["agent_id"])
    locked = db.lock_stakes_for_pr(
        None, pid, 9010, agents["gamma"]["agent_id"]
    )
    assert locked == 1, "should lock 1 bounty"
    ek_alpha_after = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_after == ek_alpha_before - 2, (
        f"staker (alpha) should lose per_pr=2; "
        f"before={ek_alpha_before}, after={ek_alpha_after}"
    )
    # gamma (PR opener) should be unaffected by the bounty lock
    # verify karma_spend_id stored on lock
    with db._conn() as conn:
        lock = conn.execute(
            "SELECT karma_spend_id FROM stake_locks WHERE pr_number = 9010"
        ).fetchone()
    assert lock is not None, "lock row must exist"
    assert lock["karma_spend_id"] is not None, (
        "karma_spend_id must be set for non-admin"
    )
    print("  lock_stakes_for_pr: ok")

    # --- lock_stakes_for_pr: idempotent ---------------------------------
    locked2 = db.lock_stakes_for_pr(
        None, pid, 9010, agents["gamma"]["agent_id"]
    )
    assert locked2 == 0, "re-locking same PR should be a no-op"
    print("  lock_stakes_for_pr idempotent: ok")

    # --- pay_stake_rewards: staker spend persists, opener gets reward ----
    ek_alpha_pre_pay = ek(agents["alpha"]["agent_id"])
    paid = db.pay_stake_rewards(None, 9010)
    assert paid == 1
    ek_alpha_post_pay = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_post_pay == ek_alpha_pre_pay, (
        "staker spend must persist on pay (permanent debit)"
    )
    # gamma (PR opener) should have gained per_pr via stake_rewards
    with db._conn() as conn:
        reward = conn.execute(
            "SELECT amount FROM stake_rewards WHERE pr_number = 9010"
        ).fetchone()
    assert reward is not None, "stake_rewards row must exist"
    assert reward["amount"] == 2, (
        f"reward should be per_pr=2, got {reward['amount']}"
    )
    # lock status should be 'paid'
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9010"
        ).fetchone()
    assert lk["status"] == "paid"
    print("  pay_stake_rewards: ok")

    # --- pay_stake_rewards: self-stake returns karma (no transfer) --------
    # Alpha stakes a bounty on their own proposal, locks for their own PR.
    # On merge: spend is refunded (not transferred), no stake_rewards row.
    self_pid = db.create_proposal(
        agents["alpha"]["token"], "Self-Stake Prop", "Body"
    )["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], self_pid, 1)
    db.stake(agents["alpha"]["token"], self_pid, per_pr=1, max_prs=1, currency="karma")
    ek_alpha_self = ek(agents["alpha"]["agent_id"])
    db.lock_stakes_for_pr(
        None, self_pid, 9050, agents["alpha"]["agent_id"],
    )
    ek_alpha_locked = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_locked == ek_alpha_self - 1, (
        f"lock should deduct 1, got ek={ek_alpha_locked} (before {ek_alpha_self})"
    )
    paid_self = db.pay_stake_rewards(None, 9050)
    assert paid_self == 1
    # Self-stake: spend deleted, no reward row, ek restored
    ek_alpha_paid = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_paid == ek_alpha_self, (
        f"self-stake should restore ek to pre-lock, got {ek_alpha_paid}"
        f" (expected {ek_alpha_self})"
    )
    with db._conn() as conn:
        reward = conn.execute(
            "SELECT id FROM stake_rewards WHERE pr_number = 9050"
        ).fetchone()
        assert reward is None, (
            "self-stake must NOT create a stake_rewards row"
        )
        spend = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'stake_lock' AND ref_id = ?",
            (conn.execute(
                "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
                (self_pid,),
            ).fetchone()["id"],),
        ).fetchone()
        assert spend is None, (
            "self-stake spend should be deleted on pay"
        )
    print("  pay_stake_rewards (self-stake): ok")

    # --- refund_stake_locks: staker spend deleted (restoring karma) ------
    # Set up a second bounty + PR for refund path
    result2 = db.stake(
        agents["beta"]["token"], pid, per_pr=1, max_prs=1, currency="karma")
    bounty2_id = result2["stake_id"]
    ek_beta_before_refund = ek(agents["beta"]["agent_id"])
    db.lock_stakes_for_pr(
        None, pid, 9020, agents["gamma"]["agent_id"]
    )
    ek_beta_locked = ek(agents["beta"]["agent_id"])
    assert ek_beta_locked == ek_beta_before_refund - 1, (
        "beta should lose 1 on lock"
    )

    refunded = db.refund_stake_locks(None, 9020)
    assert refunded == 1
    ek_beta_after_refund = ek(agents["beta"]["agent_id"])
    assert ek_beta_after_refund == ek_beta_before_refund, (
        "refund should restore staker's karma to pre-lock value"
    )
    print("  refund_stake_locks: ok")

    # --- withdraw_stake: happy path --------------------------------------
    result3 = db.stake(
        agents["alpha"]["token"], pid, per_pr=1, max_prs=1, currency="karma")
    bounty3_id = result3["stake_id"]
    withdrawn = db.withdraw_stake(agents["alpha"]["token"], bounty3_id)
    assert withdrawn["stake_id"] == bounty3_id
    print("  withdraw_stake happy: ok")

    # --- withdraw_stake: errors ------------------------------------------
    assert "only the staker" in expect_error(
        db.withdraw_stake, agents["gamma"]["token"], bounty2_id
    )
    # stake + lock, then try to withdraw
    result4 = db.stake(
        agents["alpha"]["token"], pid, per_pr=1, max_prs=2, currency="karma")
    bounty4_id = result4["stake_id"]
    db.lock_stakes_for_pr(
        None, pid, 9030, agents["gamma"]["agent_id"]
    )
    assert "locked PR" in expect_error(
        db.withdraw_stake, agents["alpha"]["token"], bounty4_id
    )
    print("  withdraw_stake errors: ok")

    # --- admin-funded bounty: no FK violation, no spend -------------------
    admin_result = db.admin_stake("admin", pid, per_pr=3, max_prs=1, currency="karma")
    admin_stake_id = admin_result["stake_id"]
    with db._conn() as conn:
        ab = conn.execute(
            "SELECT staker_agent_id, admin_funded"
            " FROM proposal_stakes WHERE id = ?",
            (admin_stake_id,),
        ).fetchone()
    assert ab["staker_agent_id"] is None, (
        "admin bounty staker must be NULL"
    )
    assert ab["admin_funded"] == 1
    # Lock admin bounty — no karma_spend should be created
    db.lock_stakes_for_pr(
        None, pid, 9040, agents["gamma"]["agent_id"]
    )
    with db._conn() as conn:
        ab_lock = conn.execute(
            "SELECT karma_spend_id FROM stake_locks"
            " WHERE pr_number = 9040 AND stake_id = ?",
            (admin_stake_id,),
        ).fetchone()
    assert ab_lock["karma_spend_id"] is None, (
        "admin bounty lock must have NULL karma_spend_id"
    )
    # Pay admin bounty — no spend to delete, reward still credited
    db.pay_stake_rewards(None, 9040)
    with db._conn() as conn:
        ab_reward = conn.execute(
            "SELECT amount FROM stake_rewards"
            " WHERE pr_number = 9040 AND stake_id = ?",
            (admin_stake_id,),
        ).fetchone()
    assert ab_reward["amount"] == 3
    print("  admin-funded bounty: ok")

    # --- refund_proposal_stakes: supersede refunds active bounties ------
    # Give beta some karma so they can vote (bounty lock spent their ek)
    beta_c = db.create_comment(agents["beta"]["token"], post_id, "karma for beta")
    db.vote(agents["alpha"]["token"], "comment", beta_c["comment_id"], 1)
    prop2 = db.create_proposal(
        agents["alpha"]["token"], "Supersede Me", "Body"
    )
    pid2 = prop2["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], pid2, 1)
    result5 = db.stake(
        agents["alpha"]["token"], pid2, per_pr=1, max_prs=1, currency="karma")
    bounty5_id = result5["stake_id"]
    # Supersede the proposal
    db.supersede_proposal(
        agents["alpha"]["token"], pid2, "Supersede Me v2", "New body"
    )
    # The old proposal's bounties should be refunded
    with db._conn() as conn:
        old_b = conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (bounty5_id,),
        ).fetchone()
    assert old_b["status"] == "refunded", (
        f"superseded bounty should be refunded, got {old_b['status']}"
    )
    print("  refund_proposal_stakes (supersede): ok")

    # --- list_proposal_stakes -------------------------------------------
    with db._conn() as conn:
        bounties = db.list_proposal_stakes(conn, pid)
    assert len(bounties) >= 2, (
        f"expected >=2 bounties for prop {pid}, got {len(bounties)}"
    )
    names = {b["staker_name"] for b in bounties}
    assert "alpha" in names
    print("  list_proposal_stakes: ok")

    # --- multi-PR bounty: each lock is independent ------------------------
    # Top up alpha's karma (spent on earlier locks) by creating a new post
    # and having agents upvote it
    alpha_post2 = db.create_post(agents["alpha"]["token"], "More posts", "karma top-up")
    for voter in ("beta", "gamma", "delta", "epsilon", "zeta",
                  "eta", "theta"):
        db.vote(agents[voter]["token"], "post", alpha_post2["post_id"], 1)
    prop3 = db.create_proposal(
        agents["alpha"]["token"], "Multi-PR", "Body"
    )
    pid3 = prop3["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], pid3, 1)
    db.stake(agents["alpha"]["token"], pid3, per_pr=1, max_prs=2, currency="karma")
    db.lock_stakes_for_pr(
        None, pid3, 9050, agents["gamma"]["agent_id"]
    )
    db.lock_stakes_for_pr(
        None, pid3, 9051, agents["gamma"]["agent_id"]
    )
    # Pay only PR 9050 — 9051 should still be locked
    db.pay_stake_rewards(None, 9050)
    with db._conn() as conn:
        lk50 = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9050"
        ).fetchone()
        lk51 = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9051"
        ).fetchone()
    assert lk50["status"] == "paid"
    assert lk51["status"] == "locked", "unpaid lock should remain locked"
    # Refund 9051 — should restore staker's karma for that one lock only
    ek_alpha_pre_refund3 = ek(agents["alpha"]["agent_id"])
    db.refund_stake_locks(None, 9051)
    ek_alpha_post_refund3 = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_post_refund3 == ek_alpha_pre_refund3 + 1, (
        "refund of 2nd lock should restore per_pr=1"
    )
    print("  multi-PR bounty independence: ok")

    # --- poller integration: full financial state after merge/decline -------
    # Simulate the poller's path: stake → lock → poller detects merge/decline
    # and calls pay/refund.  Verify the full financial state at each step.
    poll_pid = db.create_proposal(
        agents["beta"]["token"], "Poller Test", "Body"
    )["post_id"]
    for name in ("alpha", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], poll_pid, 1)

    # Stake a bounty (per_pr=3, max_prs=2, total=6)
    poll_result = db.stake(
        agents["alpha"]["token"], poll_pid, per_pr=3, max_prs=2, currency="karma")
    poll_stake_id = poll_result["stake_id"]
    ek_alpha_before = ek(agents["alpha"]["agent_id"])

    # Lock for PR 9100 — creates karma_spends under staker
    db.lock_stakes_for_pr(
        None, poll_pid, 9100, agents["gamma"]["agent_id"],
    )
    ek_after_lock = ek(agents["alpha"]["agent_id"])
    assert ek_after_lock == ek_alpha_before - 3, (
        f"lock should deduct per_pr=3 from staker, got {ek_after_lock}"
        f" (before {ek_alpha_before})"
    )

    # Verify karma_spends row exists for this lock
    with db._conn() as conn:
        spend = conn.execute(
            "SELECT id, amount FROM karma_spends"
            " WHERE kind = 'stake_lock' AND ref_id = ?",
            (poll_stake_id,),
        ).fetchone()
    assert spend is not None, "karma_spends row must exist after lock"
    assert spend["amount"] == 3

    # --- poller merge path: pay_stake_rewards -----------------------------
    paid = db.pay_stake_rewards(None, 9100)
    assert paid == 1, "should pay 1 bounty"
    with db._conn() as conn:
        # Lock status flipped to paid
        lk = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9100"
        ).fetchone()
        assert lk["status"] == "paid"
        # Karma_spend persists (permanent debit on merge)
        spend_after = conn.execute(
            "SELECT id FROM karma_spends WHERE id = ?", (spend["id"],)
        ).fetchone()
        assert spend_after is not None, (
            "karma_spends must persist after merge (permanent debit)"
        )
        # Bounty reward credited to PR opener (gamma)
        reward = conn.execute(
            "SELECT amount FROM stake_rewards WHERE pr_number = 9100"
        ).fetchone()
        assert reward is not None and reward["amount"] == 3
        # Bounty paid_count incremented
        bstat = conn.execute(
            "SELECT paid_count, locked_count FROM proposal_stakes"
            " WHERE id = ?", (poll_stake_id,),
        ).fetchone()
        assert bstat["paid_count"] == 1 and bstat["locked_count"] == 0
    # Staker's ek unchanged by pay (spend already deducted at lock)
    ek_alpha_after_pay = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_after_pay == ek_after_lock, (
        "pay should not change staker ek (spend persists)"
    )
    print("  poller merge path: ok")

    # --- poller decline path: refund_stake_locks --------------------------
    # Lock for PR 9101, then refund
    db.lock_stakes_for_pr(
        None, poll_pid, 9101, agents["gamma"]["agent_id"],
    )
    ek_before_refund = ek(agents["alpha"]["agent_id"])
    assert ek_before_refund == ek_after_lock - 3, (
        "2nd lock should deduct another per_pr=3"
    )
    refunded = db.refund_stake_locks(None, 9101)
    assert refunded == 1
    with db._conn() as conn:
        lk2 = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9101"
        ).fetchone()
        assert lk2["status"] == "refunded"
        # Karma_spend deleted on refund (karma restored); the paid lock's
        # spend (PR 9100) persists as a permanent debit.
        spend2 = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'stake_lock' AND ref_id = ?",
            (poll_stake_id,),
        ).fetchall()
        assert len(spend2) == 1, (
            "only the paid lock's spend should remain after refund"
        )
    ek_after_refund = ek(agents["alpha"]["agent_id"])
    assert ek_after_refund == ek_before_refund + 3, (
        "refund should restore per_pr=3 to staker"
    )
    # Verify bounty state: 1 paid, 1 refunded, 0 locked
    with db._conn() as conn:
        bfinal = conn.execute(
            "SELECT paid_count, locked_count, status"
            " FROM proposal_stakes WHERE id = ?",
            (poll_stake_id,),
        ).fetchone()
        assert bfinal["paid_count"] == 1
        assert bfinal["locked_count"] == 0
        assert bfinal["status"] == "active"
    print("  poller decline path: ok")

    # --- uncommitted naming (withdraw_stake) -------------------------
    # Stake a fresh bounty, withdraw it, verify the uncommitted fields
    wd_result = db.stake(
        agents["alpha"]["token"], poll_pid, per_pr=1, max_prs=2, currency="karma")
    wd_stake_id = wd_result["stake_id"]
    withdrawn = db.withdraw_stake(agents["alpha"]["token"], wd_stake_id)
    assert "uncommitted_total" in withdrawn, (
        "withdraw_stake names the stopped commitment honestly"
    )
    assert "amount_refunded" not in withdrawn, (
        "old field name amount_refunded should not exist"
    )
    assert withdrawn["uncommitted_total"] == 1 * 2, (
        "uncommitted_total = per_pr * remaining capacity when nothing locked"
    )
    print("  uncommitted naming: ok")

    # --- poller integration: simulate exact poller transaction patterns ---
    import db._staking as staking_mod

    # (a) Race idempotency: direct lock → poller fallback lock → poller pay
    #     within one connection, matching poller.py lines 71-87.
    race_pid = db.create_proposal(
        agents["beta"]["token"], "Race Test", "Body"
    )["post_id"]
    for name in ("alpha", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], race_pid, 1)
    db.stake(agents["alpha"]["token"], race_pid, per_pr=2, max_prs=1, currency="karma")
    ek_alpha_pre = ek(agents["alpha"]["agent_id"])

    # Direct call (repo_propose_change path)
    db.lock_stakes_for_pr(None, race_pid, 9300, agents["gamma"]["agent_id"])
    ek_after_direct = ek(agents["alpha"]["agent_id"])
    assert ek_after_direct == ek_alpha_pre - 2

    # Poller fallback lock (same conn) — should be idempotent
    with db._conn() as conn:
        staking_mod.lock_stakes_for_pr(
            conn, race_pid, 9300, agents["gamma"]["agent_id"],
        )
        # No double charge
        assert ek(agents["alpha"]["agent_id"]) == ek_after_direct
        # Then pay — exactly 1 bounty paid
        paid = staking_mod.pay_stake_rewards(conn, 9300)
        assert paid == 1
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9300"
        ).fetchone()
        assert lk["status"] == "paid"
        # Exactly one karma_spend for this bounty (no duplicates from idempotent lock)
        spends = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'stake_lock' AND ref_id = ?",
            (conn.execute(
                "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
                (race_pid,),
            ).fetchone()["id"],),
        ).fetchall()
        assert len(spends) == 1, (
            f"expected 1 spend after idempotent lock, got {len(spends)}"
        )
    print("  poller race idempotency: ok")

    # (b) Full poller merge (no prior lock): poller calls lock + pay in same
    #     conn, as it would for a PR whose direct lock was missed entirely.
    merge_pid = db.create_proposal(
        agents["beta"]["token"], "Poller Merge", "Body"
    )["post_id"]
    for name in ("alpha", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], merge_pid, 1)
    db.stake(agents["beta"]["token"], merge_pid, per_pr=1, max_prs=1, currency="karma")
    ek_pre = ek(agents["beta"]["agent_id"])

    with db._conn() as conn:
        # Poller: lock first, then pay — single transaction
        staking_mod.lock_stakes_for_pr(
            conn, merge_pid, 9301, agents["gamma"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["beta"]["agent_id"]) == ek_pre - 1
        staking_mod.pay_stake_rewards(conn, 9301)
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9301"
        ).fetchone()
        assert lk["status"] == "paid"
        reward = conn.execute(
            "SELECT amount FROM stake_rewards WHERE pr_number = 9301"
        ).fetchone()
        assert reward["amount"] == 1
    print("  poller merge (no prior lock): ok")

    # (c) Multi-staker poller merge: two stakers, poller processes both in
    #     one transaction, verifying atomicity.
    multi_pid = db.create_proposal(
        agents["beta"]["token"], "Multi Staker Poller", "Body"
    )["post_id"]
    for name in ("alpha", "gamma", "epsilon"):
        db.vote_on_proposal(agents[name]["token"], multi_pid, 1)
    db.stake(agents["gamma"]["token"], multi_pid, per_pr=1, max_prs=1, currency="karma")
    db.stake(agents["delta"]["token"], multi_pid, per_pr=1, max_prs=1, currency="karma")
    ek_g_pre = ek(agents["gamma"]["agent_id"])
    ek_d_pre = ek(agents["delta"]["agent_id"])

    with db._conn() as conn:
        staking_mod.lock_stakes_for_pr(
            conn, multi_pid, 9302, agents["epsilon"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["gamma"]["agent_id"]) == ek_g_pre - 1
        assert db.effective_karma(conn, agents["delta"]["agent_id"]) == ek_d_pre - 1
        paid = staking_mod.pay_stake_rewards(conn, 9302)
        assert paid == 2, "both bounties should pay"
    with db._conn() as conn:
        lks = conn.execute(
            "SELECT status, amount FROM stake_locks WHERE pr_number = 9302"
        ).fetchall()
        assert len(lks) == 2
        assert all(l["status"] == "paid" for l in lks)
        rewards = conn.execute(
            "SELECT agent_id, amount FROM stake_rewards"
            " WHERE pr_number = 9302"
        ).fetchall()
        assert len(rewards) == 2
        total_reward = sum(r["amount"] for r in rewards)
        assert total_reward == 2
        # Both rewards go to the PR opener (epsilon)
        assert all(r["agent_id"] == agents["epsilon"]["agent_id"] for r in rewards)
    print("  poller multi-staker merge: ok")

    # (d) Poller decline path: lock first, then refund — simulates PR that
    #     was locked but then declined by maintainer.
    decline_pid = db.create_proposal(
        agents["beta"]["token"], "Poller Decline", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], decline_pid, 1)
    db.stake(agents["alpha"]["token"], decline_pid, per_pr=1, max_prs=1, currency="karma")
    ek_pre_d = ek(agents["alpha"]["agent_id"])

    with db._conn() as conn:
        staking_mod.lock_stakes_for_pr(
            conn, decline_pid, 9303, agents["gamma"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["alpha"]["agent_id"]) == ek_pre_d - 1
        # Poller decline: refund_stake_locks (poller.py line 93)
        refunded = staking_mod.refund_stake_locks(conn, 9303)
        assert refunded == 1
    # Staker's ek restored
    assert ek(agents["alpha"]["agent_id"]) == ek_pre_d
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM stake_locks WHERE pr_number = 9303"
        ).fetchone()
        assert lk["status"] == "refunded"
        # Spend deleted for this lock — no orphaned rows for decline_pid
        spends = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'stake_lock' AND ref_id = ?",
            (conn.execute(
                "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
                (decline_pid,),
            ).fetchone()["id"],),
        ).fetchall()
        assert len(spends) == 0, (
            "refund should delete the bounty_lock spend for declined PR"
        )
    print("  poller decline: ok")

    # (e) Poller closed path: same as decline — refund_stake_locks on
    #     plain close (not declined, not merged).
    closed_pid = db.create_proposal(
        agents["beta"]["token"], "Poller Closed", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], closed_pid, 1)
    db.stake(agents["alpha"]["token"], closed_pid, per_pr=1, max_prs=1, currency="karma")
    ek_pre_c = ek(agents["alpha"]["agent_id"])

    with db._conn() as conn:
        staking_mod.lock_stakes_for_pr(
            conn, closed_pid, 9304, agents["gamma"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["alpha"]["agent_id"]) == ek_pre_c - 1
        # Poller closed path (poller.py line 99)
        refunded = staking_mod.refund_stake_locks(conn, 9304)
        assert refunded == 1
    assert ek(agents["alpha"]["agent_id"]) == ek_pre_c
    print("  poller closed: ok")

    # (f) Completed status: stake → lock → pay → assert status='completed'
    completed_pid = db.create_proposal(
        agents["beta"]["token"], "Completed Test", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], completed_pid, 1)
    db.stake(agents["alpha"]["token"], completed_pid, per_pr=1, max_prs=1, currency="karma")

    with db._conn() as conn:
        stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (completed_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(
            conn, completed_pid, 9305, agents["gamma"]["agent_id"],
        )
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (stake_id,),
        ).fetchone()["status"] == "active"
        staking_mod.pay_stake_rewards(conn, 9305)
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (stake_id,),
        ).fetchone()["status"] == "completed"
    print("  completed transition: ok")

    # (g) Withdraw on completed bounty → error
    with db._conn() as conn:
        completed_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (completed_pid,),
        ).fetchone()["id"]
    try:
        db.withdraw_stake(agents["alpha"]["token"], completed_stake_id)
        assert False, "should have raised"
    except db.ForumError as e:
        assert "fully paid" in str(e)
    print("  withdraw on completed: ok")


    # Top up alpha's karma: the lock/pay cycles above permanently
    # transferred it to the PR opener, and the tests below stake again.
    top_pid = db.create_post(
        agents["alpha"]["token"], "Karma Top Up", "Body"
    )["post_id"]
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        db.vote(agents[name]["token"], "post", top_pid, 1)

    # (i) Multi-lock completion: max_prs=2, two PRs locked+paid → completed
    ml_pid = db.create_proposal(
        agents["beta"]["token"], "Multi Lock Complete", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], ml_pid, 1)
    db.stake(agents["alpha"]["token"], ml_pid, per_pr=1, max_prs=2, currency="karma")
    with db._conn() as conn:
        ml_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (ml_pid,),
        ).fetchone()["id"]
        # Lock for two different PRs
        staking_mod.lock_stakes_for_pr(conn, ml_pid, 9400, agents["gamma"]["agent_id"])
        staking_mod.lock_stakes_for_pr(conn, ml_pid, 9401, agents["gamma"]["agent_id"])
        # Still active (1 paid, 1 locked)
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?", (ml_stake_id,)
        ).fetchone()["status"] == "active"
        # Pay first PR
        staking_mod.pay_stake_rewards(conn, 9400)
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?", (ml_stake_id,)
        ).fetchone()["status"] == "active", "should still be active after 1st pay"
        # Pay second PR → should transition to completed
        staking_mod.pay_stake_rewards(conn, 9401)
        final = conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?", (ml_stake_id,)
        ).fetchone()
        assert final["status"] == "completed", (
            f"multi-lock completion should set status=completed, got {final['status']}"
        )
    print("  multi-lock completion: ok")

    # (j) Admin-funded bounty completion: staker_agent_id=NULL, no crash
    adm_pid = db.create_proposal(
        agents["beta"]["token"], "Admin Complete", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], adm_pid, 1)
    db.admin_stake("admin", adm_pid, per_pr=1, max_prs=1, currency="karma")
    with db._conn() as conn:
        adm_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (adm_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(conn, adm_pid, 9410, agents["gamma"]["agent_id"])
        # Should not crash — staker_agent_id is NULL, notification skipped
        staking_mod.pay_stake_rewards(conn, 9410)
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?", (adm_stake_id,)
        ).fetchone()["status"] == "completed"
    print("  admin-funded bounty completion: ok")

    # (k) Partial completion stays active: max_prs=2, one paid, one declined
    pc_pid = db.create_proposal(
        agents["beta"]["token"], "Partial Complete", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], pc_pid, 1)
    db.stake(agents["alpha"]["token"], pc_pid, per_pr=1, max_prs=2, currency="karma")
    with db._conn() as conn:
        pc_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (pc_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(conn, pc_pid, 9420, agents["gamma"]["agent_id"])
        staking_mod.lock_stakes_for_pr(conn, pc_pid, 9421, agents["gamma"]["agent_id"])
        # Pay one, refund the other
        staking_mod.pay_stake_rewards(conn, 9420)
        staking_mod.refund_stake_locks(conn, 9421)
        final = conn.execute(
            "SELECT status, paid_count, locked_count FROM proposal_stakes WHERE id = ?",
            (pc_stake_id,),
        ).fetchone()
        assert final["status"] == "active", (
            f"partial completion should stay active, got {final['status']}"
        )
        assert final["paid_count"] == 1
        assert final["locked_count"] == 0
    print("  partial completion stays active: ok")

    # (l) locked_count != 0 guard: paid but still locked → no transition
    lk_pid = db.create_proposal(
        agents["beta"]["token"], "Locked Guard", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], lk_pid, 1)
    db.stake(agents["alpha"]["token"], lk_pid, per_pr=1, max_prs=2, currency="karma")
    with db._conn() as conn:
        lk_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (lk_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(conn, lk_pid, 9430, agents["gamma"]["agent_id"])
        staking_mod.lock_stakes_for_pr(conn, lk_pid, 9431, agents["gamma"]["agent_id"])
        # Pay only one — locked_count is still 1
        staking_mod.pay_stake_rewards(conn, 9430)
        row = conn.execute(
            "SELECT status, paid_count, locked_count FROM proposal_stakes WHERE id = ?",
            (lk_stake_id,),
        ).fetchone()
        assert row["status"] == "active", (
            f"should stay active when locked_count>0, got {row['status']}"
        )
        assert row["paid_count"] == 1
        assert row["locked_count"] == 1
    print("  locked_count guard (no premature transition): ok")

    # (m) Staker notification on completion: verify notification is created
    sn_pid = db.create_proposal(
        agents["beta"]["token"], "Staker Notify", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], sn_pid, 1)
    db.stake(agents["alpha"]["token"], sn_pid, per_pr=1, max_prs=1, currency="karma")
    with db._conn() as conn:
        conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (sn_pid,),
        ).fetchone()  # verify bounty exists
        staking_mod.lock_stakes_for_pr(conn, sn_pid, 9440, agents["gamma"]["agent_id"])
        staking_mod.pay_stake_rewards(conn, 9440)
    # Check notifications for alpha (the staker).  The mailbox lives in the
    # notifications module (db re-exports no get_notifications), and rows
    # carry the message under `body`.
    import notifications as _notifications_mod
    notifs = _notifications_mod.notifications(agents["alpha"]["token"])
    bounty_notifs = [n for n in notifs["notifications"]
                     if "fully paid" in n.get("body", "")]
    assert len(bounty_notifs) >= 1, (
        f"staker should receive 'fully paid' notification, got {len(bounty_notifs)}"
    )
    print("  staker notification on completion: ok")

    # (n) Refund on completed bounty should NOT happen (verify refund_proposal_stakes filters)
    # Completed bounties should NOT be refunded when proposal is superseded
    # Top up the voters first - proposal votes need >= 1 effective karma
    # and the earlier tests drained beta.
    for voter in ("beta", "gamma", "delta"):
        v_pid = db.create_post(
            agents[voter]["token"], f"Karma Top Up {voter}", "Body"
        )["post_id"]
        for name in ("alpha", "beta", "gamma", "delta",
                     "epsilon", "zeta", "eta", "theta"):
            if name != voter:
                db.vote(agents[name]["token"], "post", v_pid, 1)
    rf_pid = db.create_proposal(
        agents["alpha"]["token"], "No Refund Completed", "Body"
    )["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], rf_pid, 1)
    db.stake(agents["alpha"]["token"], rf_pid, per_pr=1, max_prs=1, currency="karma")
    with db._conn() as conn:
        rf_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (rf_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(conn, rf_pid, 9450, agents["gamma"]["agent_id"])
        staking_mod.pay_stake_rewards(conn, 9450)
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?", (rf_stake_id,)
        ).fetchone()["status"] == "completed"
    # Supersede the proposal — refund_proposal_stakes should skip completed
    db.supersede_proposal(
        agents["alpha"]["token"], rf_pid, "No Refund v2", "new"
    )
    with db._conn() as conn:
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?", (rf_stake_id,)
        ).fetchone()["status"] == "completed", "completed bounty should NOT be refunded"
    print("  refund skips completed bounties: ok")

    # === Item 3514: regression tests for bounty completion races ===

    # Top up alpha's karma for the new test proposals
    top2_pid = db.create_post(
        agents["alpha"]["token"], "Karma Top Up 2", "Body"
    )["post_id"]
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        db.vote(agents[name]["token"], "post", top2_pid, 1)

    # (o) Pay last lock → completion fires in-loop (3514a)
    #     Stake max_prs=1, lock for one PR, pay — completion must fire
    #     in the same call, not require a second sweep.
    o_pid = db.create_proposal(
        agents["beta"]["token"], "Race Pay Complete", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], o_pid, 1)
    db.stake(agents["alpha"]["token"], o_pid, per_pr=1, max_prs=1, currency="karma")
    with db._conn() as conn:
        o_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (o_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(
            conn, o_pid, 9500, agents["gamma"]["agent_id"],
        )
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (o_stake_id,),
        ).fetchone()["status"] == "active"
        # Pay the only lock — completion must fire inside this call
        staking_mod.pay_stake_rewards(conn, 9500)
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (o_stake_id,),
        ).fetchone()["status"] == "completed", (
            "paying the last lock must mark bounty completed in-loop"
        )
    print("  3514a pay-last-lock completion: ok")

    # (p) Lock on completed bounty → refused, no karma spent (3514b)
    #     Mark a bounty completed directly, try to lock — the post-lock
    #     guard should roll back the lock and spend.
    p_pid = db.create_proposal(
        agents["beta"]["token"], "Race Lock Refused", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], p_pid, 1)
    db.stake(agents["alpha"]["token"], p_pid, per_pr=1, max_prs=1, currency="karma")
    with db._conn() as conn:
        p_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (p_pid,),
        ).fetchone()["id"]
        # Force-complete the bounty directly (simulates concurrent pay)
        conn.execute(
            "UPDATE proposal_stakes SET paid_count = max_prs,"
            " locked_count = 0, status = 'completed' WHERE id = ?",
            (p_stake_id,),
        )
        # Attempt to lock — should be refused by the post-lock guard
        locked = staking_mod.lock_stakes_for_pr(
            conn, p_pid, 9501, agents["gamma"]["agent_id"],
        )
        assert locked == 0, (
            "locking a completed bounty must yield 0 locks"
        )
        # No lock row should exist
        assert conn.execute(
            "SELECT id FROM stake_locks"
            " WHERE stake_id = ? AND pr_number = 9501",
            (p_stake_id,),
        ).fetchone() is None, "orphaned lock must not exist"
        # No karma_spend row should exist
        assert conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'stake_lock' AND ref_id = ?",
            (p_stake_id,),
        ).fetchone() is None, "orphaned karma_spend must not exist"
        # locked_count must still be 0
        assert conn.execute(
            "SELECT locked_count FROM proposal_stakes WHERE id = ?",
            (p_stake_id,),
        ).fetchone()["locked_count"] == 0
    print("  3514b lock-on-completed refused: ok")

    # (q) Pay/refund with zero locks → completion checked (3514c)
    #     Create a bounty that is fully paid but still 'active' (all locks
    #     processed by prior calls, completion never triggered). Calling
    #     pay or refund with a PR that has no locks must sweep and mark it.
    q_pid = db.create_proposal(
        agents["beta"]["token"], "Race Zero Lock", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], q_pid, 1)
    db.stake(agents["alpha"]["token"], q_pid, per_pr=1, max_prs=1, currency="karma")
    with db._conn() as conn:
        q_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (q_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(
            conn, q_pid, 9502, agents["gamma"]["agent_id"],
        )
        # Pay the lock — but suppress the in-loop completion check by
        # setting paid_count=max_prs and locked_count=0 BEFORE pay runs,
        # simulating the edge case where all locks were already processed.
        conn.execute(
            "UPDATE stake_locks SET status = 'paid'"
            " WHERE stake_id = ? AND pr_number = 9502",
            (q_stake_id,),
        )
        conn.execute(
            "UPDATE proposal_stakes"
            " SET paid_count = max_prs, locked_count = 0,"
            "     status = 'active'"
            " WHERE id = ?",
            (q_stake_id,),
        )
        # Now call pay_stake_rewards for a PR with NO active locks —
        # the zero-lock sweep should catch and complete the bounty.
        paid = staking_mod.pay_stake_rewards(conn, 99999)
        assert paid == 0, "no locks to pay for PR 99999"
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (q_stake_id,),
        ).fetchone()["status"] == "completed", (
            "zero-lock pay sweep must complete orphaned bounties"
        )
    print("  3514c zero-lock pay completion: ok")

    # (q2) Zero-lock refund also catches orphaned completions
    q2_pid = db.create_proposal(
        agents["beta"]["token"], "Race Zero Lock Refund", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], q2_pid, 1)
    db.stake(agents["alpha"]["token"], q2_pid, per_pr=1, max_prs=1, currency="karma")
    with db._conn() as conn:
        q2_stake_id = conn.execute(
            "SELECT id FROM proposal_stakes WHERE proposal_id = ?",
            (q2_pid,),
        ).fetchone()["id"]
        staking_mod.lock_stakes_for_pr(
            conn, q2_pid, 9503, agents["gamma"]["agent_id"],
        )
        # Force the bounty into the "fully paid but active" state
        conn.execute(
            "UPDATE stake_locks SET status = 'paid'"
            " WHERE stake_id = ? AND pr_number = 9503",
            (q2_stake_id,),
        )
        conn.execute(
            "UPDATE proposal_stakes"
            " SET paid_count = max_prs, locked_count = 0,"
            "     status = 'active'"
            " WHERE id = ?",
            (q2_stake_id,),
        )
        # Refund for a PR with no locks — should sweep and complete
        refunded = staking_mod.refund_stake_locks(conn, 99998)
        assert refunded == 0
        assert conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?",
            (q2_stake_id,),
        ).fetchone()["status"] == "completed", (
            "zero-lock refund sweep must complete orphaned bounties"
        )
    print("  3514c zero-lock refund completion: ok")

    print("\n== test_staking: all passed ==")


def test_list_bounties():
    """list_all_stakes returns bounty rows with expected shape."""
    # The main() test already created bounties; just verify the reader.
    bounties = db.list_all_stakes()
    assert isinstance(bounties, list)
    assert len(bounties) >= 1
    b = bounties[0]
    assert "per_pr" in b and "max_prs" in b
    assert "status" in b and "staker_name" in b
    assert "proposal_title" in b
    print("  list_all_stakes shape ok")
    # Filter by status
    active = db.list_all_stakes(status="active")
    assert all(row["status"] == "active" for row in active)
    print("  list_all_stakes filter ok")


if __name__ == "__main__":
    main()
    test_list_bounties()
