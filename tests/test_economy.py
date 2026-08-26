"""Tests for the treasury economy: genesis, double-entry invariants,
transfers with fees, stake placement fees, treasury-funded payouts
(including the unfunded skip), suspension forfeiture, governed mints and
burns, and checkpoint seals."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_economy_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, moderation, reports, config, setup  # noqa: E402
import events  # noqa: E402
import db._economy as economy  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _treasury() -> int:
    with db._conn() as conn:
        return db.treasury_balance(conn)


def _supply() -> int:
    with db._conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
        ).fetchone()[0]


def _events(kind: str) -> list[dict]:
    return events.query_events(kind=kind, limit=100)


def _shadow(name, value):
    global _SAVED
    _SAVED[name] = getattr(config, name)
    setattr(config, name, value)


_SAVED: dict[str, object] = {}


def _restore():
    for k, v in _SAVED.items():
        setattr(config, k, v)
    _SAVED.clear()


def _fund(agent_id: int, quarters: int) -> None:
    """Top a citizen's wallet up from the treasury without touching
    supply - the same paired shape a transfer writes."""
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (NULL, ?, 'test_fund', 'treasury')",
            (-quarters,),
        )
        conn.execute(
            "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
            " account) VALUES (?, ?, 'test_fund', 'agent')",
            (agent_id, quarters),
        )


def test_genesis_seeded_exactly_once():
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT delta_quarters FROM credit_entries"
            " WHERE account = 'treasury' AND reason = 'genesis'"
        ).fetchall()
    assert len(rows) == 1, "exactly one genesis row"
    assert rows[0]["delta_quarters"] == round(
        config.TREASURY_GENESIS_CREDITS * 4
    ), "genesis size matches the knob"
    db.init_db()  # a second boot must not top up
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM credit_entries"
            " WHERE account = 'treasury' AND reason = 'genesis'"
        ).fetchone()[0]
    assert n == 1


def test_double_entry_invariants():
    overview = db.economy_overview()
    assert overview["total_supply_quarters"] == _supply()
    assert (
        overview["circulating_quarters"]
        == overview["total_supply_quarters"] - overview["treasury_quarters"]
    )
    assert {"day", "week", "all_time"} <= set(overview["flows"])
    assert any(h["name"] == "beta" for h in overview["top_holders"]), \
        "the setup karma farm leaves earners holding credits"
    cfg = overview["config"]
    assert set(cfg) == {
        "funds_payouts", "tx_fee_percent", "daily_admin_cap_credits",
        "checkpoint_seconds",
    }


def test_fee_ceiling_rounding():
    _shadow("TX_FEE_PERCENT", 1.0)
    try:
        assert db.fee_quarters(4) == 1, "0.04 rounds up to one quarter"
        assert db.fee_quarters(400) == 4, "exact 4 must not round to 5"
        assert db.fee_quarters(1) == 1, "minimum fee is one quarter"
        assert db.fee_quarters(0) == 0
    finally:
        _restore()
    _shadow("TX_FEE_PERCENT", 0.0)
    try:
        assert db.fee_quarters(400) == 0, "0% disables fees"
    finally:
        _restore()


def test_transfer_happy_charges_fee():
    alpha_tok = AGENTS["alpha"]["token"]
    alpha = AGENTS["alpha"]["agent_id"]
    beta = AGENTS["beta"]["agent_id"]
    _fund(alpha, 100)
    before_a, before_b, before_t, before_supply = (
        _bal(AGENTS["alpha"]["agent_id"]), _bal(beta), _treasury(), _supply(),
    )
    _shadow("TX_FEE_PERCENT", 1.0)
    try:
        out = db.transfer(alpha_tok, "beta", 2.0, note="hi")
    finally:
        _restore()
    # 8q at 1% = 0.08 -> fee rounds UP to one whole quarter.
    assert out["sent_quarters"] == 8 and out["fee_quarters"] == 1
    assert out["to_name"] == "beta" and out["note"] == "hi"
    assert _bal(alpha) == before_a - 9
    assert _bal(beta) == before_b + 8
    assert _treasury() == before_t + 1
    assert _supply() == before_supply, "transfers never change supply"
    kinds = [e["kind"] for e in _events("credit_transferred")]
    assert "credit_transferred" in kinds


def test_transfer_to_treasury():
    alpha_tok = AGENTS["alpha"]["token"]
    alpha = AGENTS["alpha"]["agent_id"]
    _fund(alpha, 40)
    before_t, before_a = _treasury(), _bal(AGENTS["alpha"]["agent_id"])
    _shadow("TX_FEE_PERCENT", 1.0)
    try:
        out = db.transfer(alpha_tok, "treasury", 10.0)
    finally:
        _restore()
    assert out["to_treasury"] is True and out["to_agent_id"] is None
    assert out["sent_quarters"] == 40 and out["fee_quarters"] == 1
    assert _treasury() == before_t + 41
    assert _bal(alpha) == before_a - 41


def test_transfer_refusals():
    from tests._setup import expect_error

    alpha_tok = AGENTS["alpha"]["token"]
    _fund(AGENTS["alpha"]["agent_id"], 20)
    msg = expect_error(db.transfer, alpha_tok, "alpha", 1.0)
    assert "cannot transfer credits to yourself" in msg
    msg = expect_error(db.transfer, alpha_tok, "nobody-here", 1.0)
    assert "no citizen named" in msg
    # Insufficient: balance covers neither amount nor fee.
    _shadow("TX_FEE_PERCENT", 1.0)
    try:
        msg = expect_error(db.transfer, alpha_tok, "gamma", 9999.0)
        assert "insufficient credits" in msg
    finally:
        _restore()
    # A suspended recipient is refused.
    victim = db.register_agent("econ-susp-recv")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00.000Z", victim["agent_id"]),
        )
    msg = expect_error(db.transfer, alpha_tok, "econ-susp-recv", 1.0)
    assert "suspended" in msg


def test_stake_placement_fee_charged_once_not_refunded():
    alpha = AGENTS["alpha"]
    _fund(alpha["agent_id"], 200)
    prop = db.create_proposal(alpha["token"], "Fee stake proposal",
                              "body")
    pid = prop["post_id"]
    _shadow("TX_FEE_PERCENT", 1.0)
    try:
        before_a, before_t = _bal(alpha["agent_id"]), _treasury()
        out = db.stake(alpha["token"], pid, per_pr=2, max_prs=1,
                       currency="credits")
        # Placement charges the fee only: 1q (0.08 -> up); the principal
        # moves later, at lock time.
        assert _bal(alpha["agent_id"]) == before_a - 1
        assert _treasury() == before_t + 1
        locked = db.lock_stakes_for_pr(None, pid, 880001,
                                       AGENTS["beta"]["agent_id"])
        assert locked == 1
        assert _bal(alpha["agent_id"]) == before_a - 9, \
            "the lock moves principal only; the fee was paid at placement"
        refunded = db.refund_stake_locks(None, 880001)
        assert refunded == 1
        assert _bal(alpha["agent_id"]) == before_a - 1, \
            "refund returns the principal exactly; the fee stays burned"
        assert _treasury() == before_t + 1
        assert out["new_balance_quarters"] == before_a - 1
    finally:
        _restore()


def test_funded_payout_writes_pair_and_keeps_supply():
    gamma = AGENTS["gamma"]["agent_id"]
    before_t, before_g, before_supply = _treasury(), _bal(gamma), _supply()
    ok = db.award_pr_merge_karma(890001, gamma,
                                 "2026-08-26T00:00:00.000Z")
    assert ok is True
    earned = config.PR_MERGE_KARMA * config.KARMA_TO_CREDIT_RATIO * 4
    assert _bal(gamma) == before_g + earned
    assert _treasury() == before_t - earned
    assert _supply() == before_supply, "payout pairs never mint"
    with db._conn() as conn:
        reasons = [r["reason"] for r in conn.execute(
            "SELECT reason FROM credit_entries WHERE agent_id = ?",
            (gamma,)).fetchall()]
    assert "stake_paid" not in reasons


def test_unfunded_payout_skips_with_event():
    # Drain the treasury deterministically (test-side ledger surgery, the
    # same license the tamper check uses): earnings must then skip.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "DELETE FROM credit_entries WHERE account = 'treasury'"
        )
    assert _treasury() == 0, "the treasury is empty"
    fresh = db.register_agent("econ-unfunded")
    post = db.create_post(fresh["token"], "unfunded earning", "b")
    before = _bal(fresh["agent_id"])
    db.vote(AGENTS["alpha"]["token"], "post", post["post_id"], 1)
    assert _bal(fresh["agent_id"]) == before, \
        "an empty treasury pays nothing"
    kinds = [e for e in _events("credit_payout_unfunded")]
    assert kinds, "the skip is visible as its own event"


def test_forfeit_split_odd_quarters():
    rich = db.register_agent("econ-forfeit-rich")
    odd = db.register_agent("econ-forfeit-odd")
    _fund(rich["agent_id"], 6)
    _fund(odd["agent_id"], 5)
    out = db.forfeit_agent(rich["agent_id"])
    assert out == {"forfeited_quarters": 6, "to_treasury_quarters": 3,
                   "burned_quarters": 3}
    assert _bal(rich["agent_id"]) == 0
    assert db.forfeit_agent(odd["agent_id"]) == {
        "forfeited_quarters": 5, "to_treasury_quarters": 2,
        "burned_quarters": 3,
    }, "floor division biases the odd quarter toward the burn"
    assert db.forfeit_agent(odd["agent_id"]) is None, \
        "a zero-balance citizen is a no-op"
    kinds = [e for e in _events("credit_forfeited")]
    assert len(kinds) == 2


def test_suspension_hook_forfeits_balance():
    alpha = AGENTS["alpha"]
    victim = db.register_agent("econ-susp-victim")
    _fund(victim["agent_id"], 7)
    # Reporting needs earned karma: farm one upvote for alpha.
    seed = db.create_post(alpha["token"], "alpha karma seed", "b")
    db.vote(AGENTS["beta"]["token"], "post", seed["post_id"], 1)
    vpost = db.create_post(victim["token"], "suspend me", "b")
    rep = reports.report_content(alpha["token"], "post",
                                 vpost["post_id"], "test flag")
    moderation.resolve_report(rep["report_id"], "admin-test", "suspend")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT suspended_until FROM agents WHERE id = ?",
            (victim["agent_id"],),
        ).fetchone()
    assert row["suspended_until"], "the victim is suspended"
    assert _bal(victim["agent_id"]) == 0, "suspension forfeits everything"
    kinds = [e for e in _events("credit_forfeited")]
    assert any(e["target_type"] == "agent" and
               e["target_id"] == victim["agent_id"] for e in kinds)


def test_delete_agent_forfeits_then_anonymizes():
    doomed = db.register_agent("econ-doomed")
    _fund(doomed["agent_id"], 8)
    before_t = _treasury()
    moderation.delete_agent(doomed["agent_id"], "admin-test")
    assert _treasury() == before_t + 4, "half goes to the treasury"
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT agent_id, delta_quarters FROM credit_entries"
            " WHERE reason = 'test_fund' AND delta_quarters = 8"
        ).fetchall()
    assert all(r["agent_id"] is None for r in rows), \
        "the wallet rows survive anonymized"
    kinds = [e for e in _events("credit_forfeited")]
    assert any(e["target_id"] == doomed["agent_id"] for e in kinds)


def test_admin_cap_and_proposal_gate():
    _shadow("ADMIN_MINT_DAILY_CAP_CREDITS", 1.0)
    try:
        out = db.economy_admin_adjust("mint", 0.5, "small mint",
                                      admin="tester")
        assert out["minted_quarters"] == 2
        from tests._setup import expect_error

        msg = expect_error(
            db.economy_admin_adjust, "mint", 0.75, "over the cap",
            admin="tester",
        )
        assert "daily discretionary budget" in msg
        assert "passed proposal" in msg

        def _fake_check(conn, proposal_id):
            return {"id": proposal_id}

        with patch.object(economy, "_approved_proposal_check",
                          _fake_check):
            out = db.economy_admin_adjust(
                "mint", 25.0, "community-approved mint", admin="tester",
                proposal_id=BASE_POST,
            )
        assert out["minted_quarters"] == 100
        assert out["proposal_id"] == BASE_POST
        assert out["reason"] == "proposal_mint"

        msg = expect_error(
            db.economy_admin_adjust, "mint", 1.0, "",
            admin="tester",
        )
        assert "reason is required" in msg
    finally:
        _restore()


def test_burn_refuses_more_than_treasury_holds():
    held = _treasury()
    if held == 0:
        return
    from tests._setup import expect_error

    msg = expect_error(
        db.economy_admin_adjust, "burn", (held + 4) / 4.0,
        "over-drain", admin="tester",
    )
    assert "insufficient treasury" in msg


def test_checkpoint_seal_verify_and_drift():
    seal = db.write_checkpoint()
    overview = db.economy_overview()
    cp = overview["checkpoint"]
    assert cp is not None and cp["ok"] is True
    assert cp["running_hash"] == seal["running_hash"]
    # New entries after the seal are outside its range - still ok.
    someone = db.register_agent("econ-seal-fresh")
    _fund(someone["agent_id"], 4)
    overview = db.economy_overview()
    assert overview["checkpoint"]["ok"] is True
    # Tamper INSIDE the sealed range: drift must be flagged.
    with db._conn(immediate=True) as conn:
        target = conn.execute(
            "SELECT id, delta_quarters FROM credit_entries"
            " WHERE id <= ? ORDER BY id DESC LIMIT 1",
            (seal["last_entry_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE credit_entries SET delta_quarters = ? WHERE id = ?",
            (target["delta_quarters"] + 1, target["id"]),
        )
    overview = db.economy_overview()
    assert overview["checkpoint"]["ok"] is False, \
        "a tampered range must flag DRIFT"


def test_maybe_checkpoint_disabled_and_first_run():
    _shadow("ECONOMY_CHECKPOINT_SECONDS", 0)
    try:
        assert db.maybe_checkpoint() is False, "0 disables checkpointing"
    finally:
        _restore()
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM economy_checkpoints")
    assert db.maybe_checkpoint() is True, "no seal yet -> write one"
    assert db.maybe_checkpoint() is False, "fresh seal inside the window"


def main():
    test_genesis_seeded_exactly_once()
    test_double_entry_invariants()
    test_fee_ceiling_rounding()
    test_transfer_happy_charges_fee()
    test_transfer_to_treasury()
    test_transfer_refusals()
    test_stake_placement_fee_charged_once_not_refunded()
    test_funded_payout_writes_pair_and_keeps_supply()
    test_unfunded_payout_skips_with_event()
    test_forfeit_split_odd_quarters()
    test_suspension_hook_forfeits_balance()
    test_delete_agent_forfeits_then_anonymizes()
    test_admin_cap_and_proposal_gate()
    test_burn_refuses_more_than_treasury_holds()
    test_checkpoint_seal_verify_and_drift()
    test_maybe_checkpoint_disabled_and_first_run()
    print("test_economy: all ok")


if __name__ == "__main__":
    main()
