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

    print("\n== test_bounty: all passed ==")


if __name__ == "__main__":
    main()
