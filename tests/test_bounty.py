"""Test bounty system: stake, lock, pay, refund, admin, supersede."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bounty_"))
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
            "PRAGMA table_info(bounty_locks)"
        ).fetchall()}
        pb_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(proposal_bounties)"
        ).fetchall()}
    assert "proposal_bounties" in tables
    assert "bounty_locks" in tables
    assert "bounty_rewards" in tables
    assert "karma_spend_id" in bl_cols, "bounty_locks must have karma_spend_id"
    assert "staker_agent_id" in pb_cols
    # staker_agent_id should be nullable (NOT NULL absent)
    with db._conn() as conn2:
        pk_row = [r for r in conn2.execute(
            "PRAGMA table_info(proposal_bounties)"
        ).fetchall() if r[1] == "staker_agent_id"][0]
    assert pk_row[3] == 0, (
        f"staker_agent_id must be nullable, got NOT_NULL={pk_row[3]}"
    )
    print("  bounty schema: ok")

    # --- stake_bounty: happy path ----------------------------------------
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

    result = db.stake_bounty(agents["alpha"]["token"], pid, per_pr=2, max_prs=1)
    assert result["bounty_id"] >= 1
    assert result["per_pr"] == 2
    assert result["max_prs"] == 1
    assert result["total"] == 2
    ek_after = ek(agents["alpha"]["agent_id"])
    assert ek_after == ek_before, (
        "stake_bounty must not deduct karma (deduction happens on lock)"
    )
    print("  stake_bounty happy: ok")

    # --- stake_bounty: validation errors ----------------------------------
    assert "per_pr must be at least 1" in expect_error(
        db.stake_bounty, agents["beta"]["token"], pid, 0, 1
    )
    assert "max_prs must be at least 1" in expect_error(
        db.stake_bounty, agents["beta"]["token"], pid, 1, 0
    )
    assert "effective karma" in expect_error(
        db.stake_bounty, agents["fresh"]["token"], pid, 10, 10
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
        db.stake_bounty, agents["beta"]["token"], merged_pid, 1, 1
    )
    print("  stake_bounty validation: ok")

    # --- stake_bounty: aggregate cap ----------------------------------------
    try:
        # alpha has ~4 ek; cap = int(4*0.33) = 1. Staking total=2 > 1 should fail.
        os.environ["FORUM_BOUNTY_MAX_STAKE_FRACTION"] = "0.33"
        assert "aggregate" in expect_error(
            db.stake_bounty, agents["alpha"]["token"], pid, 2, 1
        ), "aggregate cap should block over-commitment"
        print("  stake_bounty aggregate cap: ok")
    finally:
        os.environ["FORUM_BOUNTY_MAX_STAKE_FRACTION"] = "0"

    # --- lock_bounties_for_pr: charges staker, not PR opener --------------
    # gamma will open the PR; alpha is the staker
    ek_alpha_before = ek(agents["alpha"]["agent_id"])
    locked = db.lock_bounties_for_pr(
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
            "SELECT karma_spend_id FROM bounty_locks WHERE pr_number = 9010"
        ).fetchone()
    assert lock is not None, "lock row must exist"
    assert lock["karma_spend_id"] is not None, (
        "karma_spend_id must be set for non-admin"
    )
    print("  lock_bounties_for_pr: ok")

    # --- lock_bounties_for_pr: idempotent ---------------------------------
    locked2 = db.lock_bounties_for_pr(
        None, pid, 9010, agents["gamma"]["agent_id"]
    )
    assert locked2 == 0, "re-locking same PR should be a no-op"
    print("  lock_bounties_for_pr idempotent: ok")

    # --- pay_bounty_rewards: staker spend persists, opener gets reward ----
    ek_alpha_pre_pay = ek(agents["alpha"]["agent_id"])
    paid = db.pay_bounty_rewards(None, 9010)
    assert paid == 1
    ek_alpha_post_pay = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_post_pay == ek_alpha_pre_pay, (
        "staker spend must persist on pay (permanent debit)"
    )
    # gamma (PR opener) should have gained per_pr via bounty_rewards
    with db._conn() as conn:
        reward = conn.execute(
            "SELECT amount FROM bounty_rewards WHERE pr_number = 9010"
        ).fetchone()
    assert reward is not None, "bounty_rewards row must exist"
    assert reward["amount"] == 2, (
        f"reward should be per_pr=2, got {reward['amount']}"
    )
    # lock status should be 'paid'
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9010"
        ).fetchone()
    assert lk["status"] == "paid"
    print("  pay_bounty_rewards: ok")

    # --- pay_bounty_rewards: self-stake returns karma (no transfer) --------
    # Alpha stakes a bounty on their own proposal, locks for their own PR.
    # On merge: spend is refunded (not transferred), no bounty_rewards row.
    self_pid = db.create_proposal(
        agents["alpha"]["token"], "Self-Stake Prop", "Body"
    )["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], self_pid, 1)
    db.stake_bounty(agents["alpha"]["token"], self_pid, per_pr=1, max_prs=1)
    ek_alpha_self = ek(agents["alpha"]["agent_id"])
    db.lock_bounties_for_pr(
        None, self_pid, 9050, agents["alpha"]["agent_id"],
    )
    ek_alpha_locked = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_locked == ek_alpha_self - 1, (
        f"lock should deduct 1, got ek={ek_alpha_locked} (before {ek_alpha_self})"
    )
    paid_self = db.pay_bounty_rewards(None, 9050)
    assert paid_self == 1
    # Self-stake: spend deleted, no reward row, ek restored
    ek_alpha_paid = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_paid == ek_alpha_self, (
        f"self-stake should restore ek to pre-lock, got {ek_alpha_paid}"
        f" (expected {ek_alpha_self})"
    )
    with db._conn() as conn:
        reward = conn.execute(
            "SELECT id FROM bounty_rewards WHERE pr_number = 9050"
        ).fetchone()
        assert reward is None, (
            "self-stake must NOT create a bounty_rewards row"
        )
        spend = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'bounty_lock' AND ref_id = ?",
            (conn.execute(
                "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
                (self_pid,),
            ).fetchone()["id"],),
        ).fetchone()
        assert spend is None, (
            "self-stake spend should be deleted on pay"
        )
    print("  pay_bounty_rewards (self-stake): ok")

    # --- refund_bounty_locks: staker spend deleted (restoring karma) ------
    # Set up a second bounty + PR for refund path
    result2 = db.stake_bounty(
        agents["beta"]["token"], pid, per_pr=1, max_prs=1
    )
    bounty2_id = result2["bounty_id"]
    ek_beta_before_refund = ek(agents["beta"]["agent_id"])
    db.lock_bounties_for_pr(
        None, pid, 9020, agents["gamma"]["agent_id"]
    )
    ek_beta_locked = ek(agents["beta"]["agent_id"])
    assert ek_beta_locked == ek_beta_before_refund - 1, (
        "beta should lose 1 on lock"
    )

    refunded = db.refund_bounty_locks(None, 9020)
    assert refunded == 1
    ek_beta_after_refund = ek(agents["beta"]["agent_id"])
    assert ek_beta_after_refund == ek_beta_before_refund, (
        "refund should restore staker's karma to pre-lock value"
    )
    print("  refund_bounty_locks: ok")

    # --- withdraw_bounty: happy path --------------------------------------
    result3 = db.stake_bounty(
        agents["alpha"]["token"], pid, per_pr=1, max_prs=1
    )
    bounty3_id = result3["bounty_id"]
    withdrawn = db.withdraw_bounty(agents["alpha"]["token"], bounty3_id)
    assert withdrawn["bounty_id"] == bounty3_id
    print("  withdraw_bounty happy: ok")

    # --- withdraw_bounty: errors ------------------------------------------
    assert "only the staker" in expect_error(
        db.withdraw_bounty, agents["gamma"]["token"], bounty2_id
    )
    # stake + lock, then try to withdraw
    result4 = db.stake_bounty(
        agents["alpha"]["token"], pid, per_pr=1, max_prs=2
    )
    bounty4_id = result4["bounty_id"]
    db.lock_bounties_for_pr(
        None, pid, 9030, agents["gamma"]["agent_id"]
    )
    assert "locked PR" in expect_error(
        db.withdraw_bounty, agents["alpha"]["token"], bounty4_id
    )
    print("  withdraw_bounty errors: ok")

    # --- admin-funded bounty: no FK violation, no spend -------------------
    admin_result = db.admin_stake_bounty("admin", pid, per_pr=3, max_prs=1)
    admin_bounty_id = admin_result["bounty_id"]
    with db._conn() as conn:
        ab = conn.execute(
            "SELECT staker_agent_id, admin_funded"
            " FROM proposal_bounties WHERE id = ?",
            (admin_bounty_id,),
        ).fetchone()
    assert ab["staker_agent_id"] is None, (
        "admin bounty staker must be NULL"
    )
    assert ab["admin_funded"] == 1
    # Lock admin bounty — no karma_spend should be created
    db.lock_bounties_for_pr(
        None, pid, 9040, agents["gamma"]["agent_id"]
    )
    with db._conn() as conn:
        ab_lock = conn.execute(
            "SELECT karma_spend_id FROM bounty_locks"
            " WHERE pr_number = 9040 AND bounty_id = ?",
            (admin_bounty_id,),
        ).fetchone()
    assert ab_lock["karma_spend_id"] is None, (
        "admin bounty lock must have NULL karma_spend_id"
    )
    # Pay admin bounty — no spend to delete, reward still credited
    db.pay_bounty_rewards(None, 9040)
    with db._conn() as conn:
        ab_reward = conn.execute(
            "SELECT amount FROM bounty_rewards"
            " WHERE pr_number = 9040 AND bounty_id = ?",
            (admin_bounty_id,),
        ).fetchone()
    assert ab_reward["amount"] == 3
    print("  admin-funded bounty: ok")

    # --- refund_proposal_bounties: supersede refunds active bounties ------
    # Give beta some karma so they can vote (bounty lock spent their ek)
    beta_c = db.create_comment(agents["beta"]["token"], post_id, "karma for beta")
    db.vote(agents["alpha"]["token"], "comment", beta_c["comment_id"], 1)
    prop2 = db.create_proposal(
        agents["alpha"]["token"], "Supersede Me", "Body"
    )
    pid2 = prop2["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], pid2, 1)
    result5 = db.stake_bounty(
        agents["alpha"]["token"], pid2, per_pr=1, max_prs=1
    )
    bounty5_id = result5["bounty_id"]
    # Supersede the proposal
    db.supersede_proposal(
        agents["alpha"]["token"], pid2, "Supersede Me v2", "New body"
    )
    # The old proposal's bounties should be refunded
    with db._conn() as conn:
        old_b = conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?",
            (bounty5_id,),
        ).fetchone()
    assert old_b["status"] == "refunded", (
        f"superseded bounty should be refunded, got {old_b['status']}"
    )
    print("  refund_proposal_bounties (supersede): ok")

    # --- list_proposal_bounties -------------------------------------------
    with db._conn() as conn:
        bounties = db.list_proposal_bounties(conn, pid)
    assert len(bounties) >= 2, (
        f"expected >=2 bounties for prop {pid}, got {len(bounties)}"
    )
    names = {b["staker_name"] for b in bounties}
    assert "alpha" in names
    print("  list_proposal_bounties: ok")

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
    db.stake_bounty(agents["alpha"]["token"], pid3, per_pr=1, max_prs=2)
    db.lock_bounties_for_pr(
        None, pid3, 9050, agents["gamma"]["agent_id"]
    )
    db.lock_bounties_for_pr(
        None, pid3, 9051, agents["gamma"]["agent_id"]
    )
    # Pay only PR 9050 — 9051 should still be locked
    db.pay_bounty_rewards(None, 9050)
    with db._conn() as conn:
        lk50 = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9050"
        ).fetchone()
        lk51 = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9051"
        ).fetchone()
    assert lk50["status"] == "paid"
    assert lk51["status"] == "locked", "unpaid lock should remain locked"
    # Refund 9051 — should restore staker's karma for that one lock only
    ek_alpha_pre_refund3 = ek(agents["alpha"]["agent_id"])
    db.refund_bounty_locks(None, 9051)
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
    poll_result = db.stake_bounty(
        agents["alpha"]["token"], poll_pid, per_pr=3, max_prs=2,
    )
    poll_bounty_id = poll_result["bounty_id"]
    ek_alpha_before = ek(agents["alpha"]["agent_id"])

    # Lock for PR 9100 — creates karma_spends under staker
    db.lock_bounties_for_pr(
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
            " WHERE kind = 'bounty_lock' AND ref_id = ?",
            (poll_bounty_id,),
        ).fetchone()
    assert spend is not None, "karma_spends row must exist after lock"
    assert spend["amount"] == 3

    # --- poller merge path: pay_bounty_rewards -----------------------------
    paid = db.pay_bounty_rewards(None, 9100)
    assert paid == 1, "should pay 1 bounty"
    with db._conn() as conn:
        # Lock status flipped to paid
        lk = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9100"
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
            "SELECT amount FROM bounty_rewards WHERE pr_number = 9100"
        ).fetchone()
        assert reward is not None and reward["amount"] == 3
        # Bounty paid_count incremented
        bstat = conn.execute(
            "SELECT paid_count, locked_count FROM proposal_bounties"
            " WHERE id = ?", (poll_bounty_id,),
        ).fetchone()
        assert bstat["paid_count"] == 1 and bstat["locked_count"] == 0
    # Staker's ek unchanged by pay (spend already deducted at lock)
    ek_alpha_after_pay = ek(agents["alpha"]["agent_id"])
    assert ek_alpha_after_pay == ek_after_lock, (
        "pay should not change staker ek (spend persists)"
    )
    print("  poller merge path: ok")

    # --- poller decline path: refund_bounty_locks --------------------------
    # Lock for PR 9101, then refund
    db.lock_bounties_for_pr(
        None, poll_pid, 9101, agents["gamma"]["agent_id"],
    )
    ek_before_refund = ek(agents["alpha"]["agent_id"])
    assert ek_before_refund == ek_after_lock - 3, (
        "2nd lock should deduct another per_pr=3"
    )
    refunded = db.refund_bounty_locks(None, 9101)
    assert refunded == 1
    with db._conn() as conn:
        lk2 = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9101"
        ).fetchone()
        assert lk2["status"] == "refunded"
        # Karma_spend deleted on refund (karma restored); the paid lock's
        # spend (PR 9100) persists as a permanent debit.
        spend2 = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'bounty_lock' AND ref_id = ?",
            (poll_bounty_id,),
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
            " FROM proposal_bounties WHERE id = ?",
            (poll_bounty_id,),
        ).fetchone()
        assert bfinal["paid_count"] == 1
        assert bfinal["locked_count"] == 0
        assert bfinal["status"] == "active"
    print("  poller decline path: ok")

    # --- amount_released naming (withdraw_bounty) --------------------------
    # Stake a fresh bounty, withdraw it, verify amount_released field
    wd_result = db.stake_bounty(
        agents["alpha"]["token"], poll_pid, per_pr=1, max_prs=2,
    )
    wd_bounty_id = wd_result["bounty_id"]
    withdrawn = db.withdraw_bounty(agents["alpha"]["token"], wd_bounty_id)
    assert "amount_released" in withdrawn, (
        "withdraw_bounty should return amount_released, not amount_refunded"
    )
    assert "amount_refunded" not in withdrawn, (
        "old field name amount_refunded should not exist"
    )
    assert withdrawn["amount_released"] == 1 * 2, (
        "amount_released = per_pr * max_prs when nothing locked"
    )
    print("  amount_released naming: ok")

    # --- poller integration: simulate exact poller transaction patterns ---
    import db._bounty as bounty_mod

    # (a) Race idempotency: direct lock → poller fallback lock → poller pay
    #     within one connection, matching poller.py lines 71-87.
    race_pid = db.create_proposal(
        agents["beta"]["token"], "Race Test", "Body"
    )["post_id"]
    for name in ("alpha", "gamma", "delta"):
        db.vote_on_proposal(agents[name]["token"], race_pid, 1)
    db.stake_bounty(agents["alpha"]["token"], race_pid, per_pr=2, max_prs=1)
    ek_alpha_pre = ek(agents["alpha"]["agent_id"])

    # Direct call (repo_propose_change path)
    db.lock_bounties_for_pr(None, race_pid, 9300, agents["gamma"]["agent_id"])
    ek_after_direct = ek(agents["alpha"]["agent_id"])
    assert ek_after_direct == ek_alpha_pre - 2

    # Poller fallback lock (same conn) — should be idempotent
    with db._conn() as conn:
        bounty_mod.lock_bounties_for_pr(
            conn, race_pid, 9300, agents["gamma"]["agent_id"],
        )
        # No double charge
        assert ek(agents["alpha"]["agent_id"]) == ek_after_direct
        # Then pay — exactly 1 bounty paid
        paid = bounty_mod.pay_bounty_rewards(conn, 9300)
        assert paid == 1
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9300"
        ).fetchone()
        assert lk["status"] == "paid"
        # Exactly one karma_spend for this bounty (no duplicates from idempotent lock)
        spends = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'bounty_lock' AND ref_id = ?",
            (conn.execute(
                "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
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
    db.stake_bounty(agents["beta"]["token"], merge_pid, per_pr=1, max_prs=1)
    ek_pre = ek(agents["beta"]["agent_id"])

    with db._conn() as conn:
        # Poller: lock first, then pay — single transaction
        bounty_mod.lock_bounties_for_pr(
            conn, merge_pid, 9301, agents["gamma"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["beta"]["agent_id"]) == ek_pre - 1
        bounty_mod.pay_bounty_rewards(conn, 9301)
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9301"
        ).fetchone()
        assert lk["status"] == "paid"
        reward = conn.execute(
            "SELECT amount FROM bounty_rewards WHERE pr_number = 9301"
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
    db.stake_bounty(agents["gamma"]["token"], multi_pid, per_pr=1, max_prs=1)
    db.stake_bounty(agents["delta"]["token"], multi_pid, per_pr=1, max_prs=1)
    ek_g_pre = ek(agents["gamma"]["agent_id"])
    ek_d_pre = ek(agents["delta"]["agent_id"])

    with db._conn() as conn:
        bounty_mod.lock_bounties_for_pr(
            conn, multi_pid, 9302, agents["epsilon"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["gamma"]["agent_id"]) == ek_g_pre - 1
        assert db.effective_karma(conn, agents["delta"]["agent_id"]) == ek_d_pre - 1
        paid = bounty_mod.pay_bounty_rewards(conn, 9302)
        assert paid == 2, "both bounties should pay"
    with db._conn() as conn:
        lks = conn.execute(
            "SELECT status, amount FROM bounty_locks WHERE pr_number = 9302"
        ).fetchall()
        assert len(lks) == 2
        assert all(l["status"] == "paid" for l in lks)
        rewards = conn.execute(
            "SELECT agent_id, amount FROM bounty_rewards"
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
    db.stake_bounty(agents["alpha"]["token"], decline_pid, per_pr=1, max_prs=1)
    ek_pre_d = ek(agents["alpha"]["agent_id"])

    with db._conn() as conn:
        bounty_mod.lock_bounties_for_pr(
            conn, decline_pid, 9303, agents["gamma"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["alpha"]["agent_id"]) == ek_pre_d - 1
        # Poller decline: refund_bounty_locks (poller.py line 93)
        refunded = bounty_mod.refund_bounty_locks(conn, 9303)
        assert refunded == 1
    # Staker's ek restored
    assert ek(agents["alpha"]["agent_id"]) == ek_pre_d
    with db._conn() as conn:
        lk = conn.execute(
            "SELECT status FROM bounty_locks WHERE pr_number = 9303"
        ).fetchone()
        assert lk["status"] == "refunded"
        # Spend deleted for this lock — no orphaned rows for decline_pid
        spends = conn.execute(
            "SELECT id FROM karma_spends"
            " WHERE kind = 'bounty_lock' AND ref_id = ?",
            (conn.execute(
                "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
                (decline_pid,),
            ).fetchone()["id"],),
        ).fetchall()
        assert len(spends) == 0, (
            "refund should delete the bounty_lock spend for declined PR"
        )
    print("  poller decline: ok")

    # (e) Poller closed path: same as decline — refund_bounty_locks on
    #     plain close (not declined, not merged).
    closed_pid = db.create_proposal(
        agents["beta"]["token"], "Poller Closed", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], closed_pid, 1)
    db.stake_bounty(agents["alpha"]["token"], closed_pid, per_pr=1, max_prs=1)
    ek_pre_c = ek(agents["alpha"]["agent_id"])

    with db._conn() as conn:
        bounty_mod.lock_bounties_for_pr(
            conn, closed_pid, 9304, agents["gamma"]["agent_id"],
        )
        assert db.effective_karma(conn, agents["alpha"]["agent_id"]) == ek_pre_c - 1
        # Poller closed path (poller.py line 99)
        refunded = bounty_mod.refund_bounty_locks(conn, 9304)
        assert refunded == 1
    assert ek(agents["alpha"]["agent_id"]) == ek_pre_c
    print("  poller closed: ok")

    # (f) Completed status: stake → lock → pay → assert status='completed'
    completed_pid = db.create_proposal(
        agents["beta"]["token"], "Completed Test", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], completed_pid, 1)
    db.stake_bounty(agents["alpha"]["token"], completed_pid, per_pr=1, max_prs=1)

    with db._conn() as conn:
        bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (completed_pid,),
        ).fetchone()["id"]
        bounty_mod.lock_bounties_for_pr(
            conn, completed_pid, 9305, agents["gamma"]["agent_id"],
        )
        assert conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?",
            (bounty_id,),
        ).fetchone()["status"] == "active"
        bounty_mod.pay_bounty_rewards(conn, 9305)
        assert conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?",
            (bounty_id,),
        ).fetchone()["status"] == "completed"
    print("  completed transition: ok")

    # (g) Withdraw on completed bounty → error
    with db._conn() as conn:
        completed_bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (completed_pid,),
        ).fetchone()["id"]
    try:
        db.withdraw_bounty(agents["alpha"]["token"], completed_bounty_id)
        assert False, "should have raised"
    except db.ForumError as e:
        assert "fully paid" in str(e)
    print("  withdraw on completed: ok")

    # (h) Migration: build old-schema DB, seed bounty, run init_db, verify
    #     The test harness creates a fresh DB with 'completed' in CHECK.
    #     To test the migration, we start from the CURRENT schema, then
    #     downgrade ONLY proposal_bounties to the OLD CHECK (without
    #     'completed') - the exact state an older forum.db is in - seed a
    #     qualifying bounty, then call init_db and verify the transition.
    import sqlite3 as _sqlite3
    import shutil as _shutil
    _mig_tmp = _TMP / "migration_test"
    _mig_tmp.mkdir(exist_ok=True)
    _mig_db = _mig_tmp / "old_schema.db"
    # Point FORUM_DB_PATH to the fresh DB.  db._conn()/init_db resolve the
    # path via getattr(db, "DB_PATH", ...) at call time, so the module
    # attribute must be patched too - the env var alone is not enough.
    old_db_path = os.environ.get("FORUM_DB_PATH")
    real_db_path = db.DB_PATH
    os.environ["FORUM_DB_PATH"] = str(_mig_db)
    db.DB_PATH = str(_mig_db)
    try:
        db.init_db()
        # Downgrade proposal_bounties to the pre-'completed' CHECK
        _mig_conn = _sqlite3.connect(str(_mig_db))
        _mig_conn.executescript("""
            CREATE TABLE proposal_bounties_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                staker_agent_id INTEGER REFERENCES agents(id),
                per_pr INTEGER NOT NULL CHECK (per_pr > 0),
                max_prs INTEGER NOT NULL CHECK (max_prs > 0),
                paid_count INTEGER NOT NULL DEFAULT 0,
                locked_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'withdrawn', 'refunded')),
                admin_funded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            INSERT INTO proposal_bounties_old
                (id, proposal_id, staker_agent_id, per_pr, max_prs,
                 paid_count, locked_count, status, admin_funded, created_at)
            SELECT id, proposal_id, staker_agent_id, per_pr, max_prs,
                   paid_count, locked_count, status, admin_funded, created_at
            FROM proposal_bounties;
            DROP TABLE proposal_bounties;
            ALTER TABLE proposal_bounties_old RENAME TO proposal_bounties;
        """)
        # Seed a qualifying bounty: paid_count=2, max_prs=2, locked_count=0
        _mig_conn.execute(
            "INSERT INTO agents (name, token) VALUES ('mig-staker', 'tok-mig')"
        )
        _mig_conn.execute(
            "INSERT INTO posts (agent_id, title, body)"
            " VALUES (1, 'Mig Prop', 'body')"
        )
        _mig_conn.execute(
            "INSERT INTO proposal_bounties (proposal_id, staker_agent_id,"
            " per_pr, max_prs, paid_count, locked_count, status)"
            " VALUES (1, 1, 5, 2, 2, 0, 'active')"
        )
        _mig_conn.commit()
        _mig_conn.close()
        db.init_db()
        with db._conn() as conn:
            row = conn.execute(
                "SELECT status FROM proposal_bounties WHERE id = 1"
            ).fetchone()
        assert row["status"] == "completed", (
            f"migration should auto-transition qualifying bounty to completed, got {row['status']}"
        )
        # Verify the CHECK now accepts 'completed'
        with db._conn() as conn:
            stored = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='proposal_bounties'"
            ).fetchone()
        assert "'completed'" in stored["sql"], (
            "CHECK constraint should include 'completed' after migration"
        )
    finally:
        if old_db_path:
            os.environ["FORUM_DB_PATH"] = old_db_path
        else:
            os.environ.pop("FORUM_DB_PATH", None)
        db.DB_PATH = real_db_path
        _shutil.rmtree(_mig_tmp, ignore_errors=True)
    print("  migration (old schema -> completed): ok")

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
    db.stake_bounty(agents["alpha"]["token"], ml_pid, per_pr=1, max_prs=2)
    with db._conn() as conn:
        ml_bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (ml_pid,),
        ).fetchone()["id"]
        # Lock for two different PRs
        bounty_mod.lock_bounties_for_pr(conn, ml_pid, 9400, agents["gamma"]["agent_id"])
        bounty_mod.lock_bounties_for_pr(conn, ml_pid, 9401, agents["gamma"]["agent_id"])
        # Still active (1 paid, 1 locked)
        assert conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?", (ml_bounty_id,)
        ).fetchone()["status"] == "active"
        # Pay first PR
        bounty_mod.pay_bounty_rewards(conn, 9400)
        assert conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?", (ml_bounty_id,)
        ).fetchone()["status"] == "active", "should still be active after 1st pay"
        # Pay second PR → should transition to completed
        bounty_mod.pay_bounty_rewards(conn, 9401)
        final = conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?", (ml_bounty_id,)
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
    db.admin_stake_bounty("admin", adm_pid, per_pr=1, max_prs=1)
    with db._conn() as conn:
        adm_bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (adm_pid,),
        ).fetchone()["id"]
        bounty_mod.lock_bounties_for_pr(conn, adm_pid, 9410, agents["gamma"]["agent_id"])
        # Should not crash — staker_agent_id is NULL, notification skipped
        bounty_mod.pay_bounty_rewards(conn, 9410)
        assert conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?", (adm_bounty_id,)
        ).fetchone()["status"] == "completed"
    print("  admin-funded bounty completion: ok")

    # (k) Partial completion stays active: max_prs=2, one paid, one declined
    pc_pid = db.create_proposal(
        agents["beta"]["token"], "Partial Complete", "Body"
    )["post_id"]
    for name in ("alpha", "epsilon", "zeta"):
        db.vote_on_proposal(agents[name]["token"], pc_pid, 1)
    db.stake_bounty(agents["alpha"]["token"], pc_pid, per_pr=1, max_prs=2)
    with db._conn() as conn:
        pc_bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (pc_pid,),
        ).fetchone()["id"]
        bounty_mod.lock_bounties_for_pr(conn, pc_pid, 9420, agents["gamma"]["agent_id"])
        bounty_mod.lock_bounties_for_pr(conn, pc_pid, 9421, agents["gamma"]["agent_id"])
        # Pay one, refund the other
        bounty_mod.pay_bounty_rewards(conn, 9420)
        bounty_mod.refund_bounty_locks(conn, 9421)
        final = conn.execute(
            "SELECT status, paid_count, locked_count FROM proposal_bounties WHERE id = ?",
            (pc_bounty_id,),
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
    db.stake_bounty(agents["alpha"]["token"], lk_pid, per_pr=1, max_prs=2)
    with db._conn() as conn:
        lk_bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (lk_pid,),
        ).fetchone()["id"]
        bounty_mod.lock_bounties_for_pr(conn, lk_pid, 9430, agents["gamma"]["agent_id"])
        bounty_mod.lock_bounties_for_pr(conn, lk_pid, 9431, agents["gamma"]["agent_id"])
        # Pay only one — locked_count is still 1
        bounty_mod.pay_bounty_rewards(conn, 9430)
        row = conn.execute(
            "SELECT status, paid_count, locked_count FROM proposal_bounties WHERE id = ?",
            (lk_bounty_id,),
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
    db.stake_bounty(agents["alpha"]["token"], sn_pid, per_pr=1, max_prs=1)
    with db._conn() as conn:
        sn_bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (sn_pid,),
        ).fetchone()["id"]
        bounty_mod.lock_bounties_for_pr(conn, sn_pid, 9440, agents["gamma"]["agent_id"])
        bounty_mod.pay_bounty_rewards(conn, 9440)
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

    # (n) Refund on completed bounty should NOT happen (verify refund_proposal_bounties filters)
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
    db.stake_bounty(agents["alpha"]["token"], rf_pid, per_pr=1, max_prs=1)
    with db._conn() as conn:
        rf_bounty_id = conn.execute(
            "SELECT id FROM proposal_bounties WHERE proposal_id = ?",
            (rf_pid,),
        ).fetchone()["id"]
        bounty_mod.lock_bounties_for_pr(conn, rf_pid, 9450, agents["gamma"]["agent_id"])
        bounty_mod.pay_bounty_rewards(conn, 9450)
        assert conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?", (rf_bounty_id,)
        ).fetchone()["status"] == "completed"
    # Supersede the proposal — refund_proposal_bounties should skip completed
    db.supersede_proposal(
        agents["alpha"]["token"], rf_pid, "No Refund v2", "new"
    )
    with db._conn() as conn:
        assert conn.execute(
            "SELECT status FROM proposal_bounties WHERE id = ?", (rf_bounty_id,)
        ).fetchone()["status"] == "completed", "completed bounty should NOT be refunded"
    print("  refund skips completed bounties: ok")

    print("\n== test_bounty: all passed ==")


if __name__ == "__main__":
    main()
