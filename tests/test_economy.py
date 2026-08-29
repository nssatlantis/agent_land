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

import db._economy as economy  # noqa: E402
import events  # noqa: E402
from tests._setup import config, db, moderation, reports, setup  # noqa: E402

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
    assert rows[0]["delta_quarters"] == round(config.TREASURY_GENESIS_CREDITS * 4), (
        "genesis size matches the knob"
    )
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
    assert any(h["name"] == "beta" for h in overview["top_holders"]), (
        "the setup karma farm leaves earners holding credits"
    )
    cfg = overview["config"]
    assert set(cfg) == {
        "funds_payouts",
        "runway_enabled",
        "tx_fee_percent",
        "daily_admin_cap_credits",
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
        _bal(AGENTS["alpha"]["agent_id"]),
        _bal(beta),
        _treasury(),
        _supply(),
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
    prop = db.create_proposal(alpha["token"], "Fee stake proposal", "body")
    pid = prop["post_id"]
    _shadow("TX_FEE_PERCENT", 1.0)
    try:
        before_a, before_t = _bal(alpha["agent_id"]), _treasury()
        out = db.stake(alpha["token"], pid, per_pr=2, max_prs=1, currency="credits")
        # Placement charges the fee only: 1q (0.08 -> up); the principal
        # moves later, at lock time.
        assert _bal(alpha["agent_id"]) == before_a - 1
        assert _treasury() == before_t + 1
        locked = db.lock_stakes_for_pr(None, pid, 880001, AGENTS["beta"]["agent_id"])
        assert locked == 1
        assert _bal(alpha["agent_id"]) == before_a - 9, (
            "the lock moves principal only; the fee was paid at placement"
        )
        refunded = db.refund_stake_locks(None, 880001)
        assert refunded == 1
        assert _bal(alpha["agent_id"]) == before_a - 1, (
            "refund returns the principal exactly; the fee stays burned"
        )
        assert _treasury() == before_t + 1
        assert out["new_balance_quarters"] == before_a - 1
    finally:
        _restore()


def test_funded_payout_writes_pair_and_keeps_supply():
    gamma = AGENTS["gamma"]["agent_id"]
    before_t, before_g, before_supply = _treasury(), _bal(gamma), _supply()
    ok = db.award_pr_merge_karma(890001, gamma, "2026-08-26T00:00:00.000Z")
    assert ok is True
    earned = config.PR_MERGE_KARMA * config.KARMA_TO_CREDIT_RATIO * 4
    assert _bal(gamma) == before_g + earned
    assert _treasury() == before_t - earned
    assert _supply() == before_supply, "payout pairs never mint"
    with db._conn() as conn:
        reasons = [
            r["reason"]
            for r in conn.execute(
                "SELECT reason FROM credit_entries WHERE agent_id = ?", (gamma,)
            ).fetchall()
        ]
    assert "stake_paid" not in reasons


def test_unfunded_payout_skips_with_event():
    # Drain the treasury deterministically (test-side ledger surgery, the
    # same license the tamper check uses): earnings must then skip.
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM credit_entries WHERE account = 'treasury'")
    assert _treasury() == 0, "the treasury is empty"
    fresh = db.register_agent("econ-unfunded")
    post = db.create_post(fresh["token"], "unfunded earning", "b")
    before = _bal(fresh["agent_id"])
    db.vote(AGENTS["alpha"]["token"], "post", post["post_id"], 1)
    assert _bal(fresh["agent_id"]) == before, "an empty treasury pays nothing"
    kinds = [e for e in _events("credit_payout_unfunded")]
    assert kinds, "the skip is visible as its own event"


def test_forfeit_split_odd_quarters():
    rich = db.register_agent("econ-forfeit-rich")
    odd = db.register_agent("econ-forfeit-odd")
    _fund(rich["agent_id"], 6)
    _fund(odd["agent_id"], 5)
    out = db.forfeit_agent(rich["agent_id"])
    assert out == {
        "forfeited_quarters": 6,
        "to_treasury_quarters": 3,
        "burned_quarters": 3,
    }
    assert _bal(rich["agent_id"]) == 0
    assert db.forfeit_agent(odd["agent_id"]) == {
        "forfeited_quarters": 5,
        "to_treasury_quarters": 2,
        "burned_quarters": 3,
    }, "floor division biases the odd quarter toward the burn"
    assert db.forfeit_agent(odd["agent_id"]) is None, (
        "a zero-balance citizen is a no-op"
    )
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
    rep = reports.report_content(alpha["token"], "post", vpost["post_id"], "test flag")
    moderation.resolve_report(rep["report_id"], "admin-test", "suspend")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT suspended_until FROM agents WHERE id = ?",
            (victim["agent_id"],),
        ).fetchone()
    assert row["suspended_until"], "the victim is suspended"
    assert _bal(victim["agent_id"]) == 0, "suspension forfeits everything"
    kinds = [e for e in _events("credit_forfeited")]
    assert any(
        e["target_type"] == "agent" and e["target_id"] == victim["agent_id"]
        for e in kinds
    )


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
    assert all(r["agent_id"] is None for r in rows), (
        "the wallet rows survive anonymized"
    )
    kinds = [e for e in _events("credit_forfeited")]
    assert any(e["target_id"] == doomed["agent_id"] for e in kinds)


def test_admin_cap_and_proposal_gate():
    _shadow("ADMIN_MINT_DAILY_CAP_CREDITS", 1.0)
    try:
        out = db.economy_admin_adjust("mint", 0.5, "small mint", admin="tester")
        assert out["minted_quarters"] == 2
        from tests._setup import expect_error

        msg = expect_error(
            db.economy_admin_adjust,
            "mint",
            0.75,
            "over the cap",
            admin="tester",
        )
        assert "daily discretionary budget" in msg
        assert "passed proposal" in msg

        def _fake_check(conn, proposal_id):
            return {"id": proposal_id}

        with patch.object(economy, "_approved_proposal_check", _fake_check):
            out = db.economy_admin_adjust(
                "mint",
                25.0,
                "community-approved mint",
                admin="tester",
                proposal_id=BASE_POST,
            )
        assert out["minted_quarters"] == 100
        assert out["proposal_id"] == BASE_POST
        assert out["reason"] == "proposal_mint"

        msg = expect_error(
            db.economy_admin_adjust,
            "mint",
            1.0,
            "",
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
        db.economy_admin_adjust,
        "burn",
        (held + 4) / 4.0,
        "over-drain",
        admin="tester",
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
    assert overview["checkpoint"]["ok"] is False, "a tampered range must flag DRIFT"


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


# --- review-response regressions (PR #402 round 2) -----------------------


def test_delete_agent_with_placed_stakes_survives():
    """A citizen who ever PLACED a stake must be deletable: the stakes'
    rows survive anonymized (staker NULL) instead of FK-violating the
    agents delete (Agent7 finding #1 / Pickle #4)."""
    import moderation as mod

    staker = db.register_agent("econ-staker-del")
    seed = db.create_post(staker["token"], "karma for staking", "b")
    db.vote(AGENTS["beta"]["token"], "post", seed["post_id"], 1)
    prop = db.create_proposal(AGENTS["alpha"]["token"], "staker deletion target", "b")
    out = db.stake(
        staker["token"], prop["post_id"], per_pr=1, max_prs=1, currency="karma"
    )
    sid = out["stake_id"]
    mod.delete_agent(staker["agent_id"], "t", destroy_content=True)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT staker_agent_id FROM proposal_stakes WHERE id = ?",
            (sid,),
        ).fetchone()
    assert row is not None and row["staker_agent_id"] is None, (
        "the stake row survives with its owner anonymized"
    )


def test_underfunded_stake_abandons_loudly():
    """When the wallet falls below per_pr before a lock, the stake is
    abandoned (status + event + mail) instead of silently zombie-ing
    through later PRs (Agent7 finding #2)."""
    from events import EVT_STAKE_ABANDONED

    staker = db.register_agent("econ-abandon")
    _fund(staker["agent_id"], 20)
    prop = db.create_proposal(AGENTS["alpha"]["token"], "abandon target", "b")
    out = db.stake(
        staker["token"], prop["post_id"], per_pr=1, max_prs=3, currency="credits"
    )
    sid = out["stake_id"]
    # Drain the wallet below one per-PR credit: 20q -> 2q.
    db.transfer_credits(staker["agent_id"], "treasury", 18)
    locked = db.lock_stakes_for_pr(
        None, prop["post_id"], 991001, AGENTS["beta"]["agent_id"]
    )
    assert locked == 0, "an underfunded stake locks nothing"
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status FROM proposal_stakes WHERE id = ?", (sid,)
        ).fetchone()
        mail = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND ref_type = 'proposal_stake' AND ref_id = ?",
            (staker["agent_id"], sid),
        ).fetchone()[0]
    assert row["status"] == "abandoned", "the stake is marked abandoned"
    assert mail >= 1, "the staker is told why their stake died"
    kinds = [e for e in _events(EVT_STAKE_ABANDONED)]
    assert any(e["target_id"] == sid for e in kinds)
    # A later PR must not resurrect it - and must not re-abandon.
    locked2 = db.lock_stakes_for_pr(
        None, prop["post_id"], 991002, AGENTS["beta"]["agent_id"]
    )
    assert locked2 == 0
    kinds2 = [e for e in _events(EVT_STAKE_ABANDONED)]
    assert sum(1 for e in kinds2 if e["target_id"] == sid) == 1


def test_ratio_invalid_degrades_not_poisons():
    """A misconfigured KARMA_TO_CREDIT_RATIO must never take voting down:
    earning degrades to off, votes keep working (Laguna #2 / Pickle #2)."""
    import importlib

    old = os.environ.get("FORUM_KARMA_TO_CREDIT_RATIO")
    os.environ["FORUM_KARMA_TO_CREDIT_RATIO"] = "0.3"
    try:
        importlib.reload(config)
        fresh = db.register_agent("econ-badratio")
        post = db.create_post(fresh["token"], "ratio probe", "b")
        before = _bal(fresh["agent_id"])
        res = db.vote(
            AGENTS["alpha"]["token"], "post", post["post_id"], 1
        )  # must NOT raise
        assert res["new_score"] == 1, "the vote itself lands"
        assert _bal(fresh["agent_id"]) == before, (
            "earning is disabled while the ratio is invalid"
        )
    finally:
        if old is None:
            os.environ.pop("FORUM_KARMA_TO_CREDIT_RATIO", None)
        else:
            os.environ["FORUM_KARMA_TO_CREDIT_RATIO"] = old
        importlib.reload(config)


def test_credits_disabled_refuses_spends_settles_escrow():
    """The kill switch kills both directions for new flows - spends
    refuse loudly - but escrowed principal always settles (Laguna #4 /
    Pickle #6)."""
    from tests._setup import expect_error

    someone = db.register_agent("econ-killswitch")
    _fund(someone["agent_id"], 8)
    _shadow("CREDITS_ENABLED", 0)
    try:
        msg = expect_error(db._credits.spend, someone["agent_id"], 4, "x")
        assert "disabled" in msg
        ok = db._credits.return_principal(
            someone["agent_id"],
            4,
            "escrow_settlement_test",
        )
        assert ok is True, "escrowed principal settles even when disabled"
        assert _bal(someone["agent_id"]) == 12
    finally:
        _restore()


def test_to_quarters_ties_up_exactly():
    """'Nearest quarter, ties up' must be literally true - float round()'s
    half-to-even silently betrayed it on .x125 boundaries (Laguna lower /
    Agent7 #9)."""
    f = db.to_quarters
    assert f(2.125) == 9, "2.125 -> 2.25 (ties UP, not half-to-even)"
    assert f(0.125) == 1, "0.125 -> 0.25"
    assert f(2.4) == 10, "nearest: 2.4 -> 2.5"
    assert f(2.3) == 9, "nearest: 2.3 -> 2.25"
    assert f(2.0) == 8


def test_credit_sub_one_stake_floor():
    """Credit stakes below 1.0 are legal down to one quarter; only the
    conversion-aware floor speaks (Laguna #1 / Pickle #1)."""
    from tests._setup import expect_error

    beta = db.register_agent("econ-subone")
    _fund(beta["agent_id"], 40)
    prop = db.create_proposal(AGENTS["alpha"]["token"], "sub-one stakes", "b")
    out = db.stake(
        beta["token"], prop["post_id"], per_pr=0.5, max_prs=1, currency="credits"
    )
    assert out["per_pr"] == 2 and out["per_pr_credits"] == "0.5", (
        "a half-credit stake converts to 2 quarters"
    )
    msg = expect_error(
        db.stake, beta["token"], prop["post_id"], 0.1, 1, currency="credits"
    )
    assert "at least 0.25 credits" in msg


def test_treasury_name_reserved_and_precedence():
    """The name 'treasury' is reserved at registration, and - for any
    legacy citizen that already owns it - citizen routing wins over the
    treasury account (pre-merge self-audit)."""
    from tests._setup import expect_error

    msg = expect_error(db.register_agent, "treasury")
    assert "reserved" in msg.lower()
    msg = expect_error(db.register_agent, "TREASURY")
    assert "reserved" in msg.lower()
    # Legacy possibility: a citizen already named Treasury exists.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO agents (name, token) VALUES ('Treasury', ?)",
            (f"tok-treas-{id(object())}",),
        )
        aid = conn.execute("SELECT id FROM agents WHERE name = 'Treasury'").fetchone()[
            "id"
        ]
    rich = db.register_agent("econ-name-collide")
    _fund(rich["agent_id"], 8)
    before_t = _treasury()
    out = db.transfer(rich["token"], "treasury", 2.0)
    assert out["to_agent_id"] == aid and not out["to_treasury"], (
        "an existing citizen named treasury receives the transfer"
    )
    assert _treasury() == before_t + (out["fee_quarters"] or 0), (
        "only the fee reaches the account"
    )


def test_transfer_note_escaped_on_events_page():
    """The transfer note is sender-chosen free text: the /events row
    renders it HTML-escaped (self-audit XSS catch)."""
    import viewer._events as ve

    e = {
        "kind": "credit_transferred",
        "actor_name": "sender",
        "actor_agent_id": AGENTS["alpha"]["agent_id"],
        "created_at": "2026-08-26T00:00:00.000Z",
        "target_type": "agent",
        "target_id": AGENTS["beta"]["agent_id"],
        "detail": {
            "credits": "1",
            "to_name": "<b>evil</b>",
            "note": "<script>alert(1)</script>",
            "fee_credits": "0",
            "to_agent_id": AGENTS["beta"]["agent_id"],
        },
    }
    html = ve._event_row(e)
    assert "<script>" not in html, "raw script tags must never render"
    assert "&lt;script&gt;" in html
    assert "<b>evil</b>" not in html
    assert "&lt;b&gt;evil&lt;/b&gt;" in html


def test_checkpoint_replays_the_chain_not_just_sums():
    """A tamper that PRESERVES totals - a rewritten reason - must still
    be caught: verification replays the full hash chain, comparing every
    stored seal boundary (review note N1)."""
    seal = db.write_checkpoint()
    with db._conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT id FROM credit_entries WHERE id <= ? ORDER BY id DESC LIMIT 1",
            (seal["last_entry_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE credit_entries SET reason = reason || '-tampered' WHERE id = ?",
            (row["id"],),
        )
    cp = db.economy_overview()["checkpoint"]
    assert cp["entry_count"] == cp["live_entry_count"], (
        "sums and counts still reconcile..."
    )
    assert cp["sealed_supply_quarters"] == cp["live_supply_quarters"]
    assert cp["chain_ok"] is False, "...but the chain replay catches it"
    assert cp["ok"] is False


def test_negative_admin_cap_clamps_shut():
    """A negative cap knob clamps to 0 - every adjustment then needs a
    proposal. A typo must never unlock unlimited minting (review note
    N3)."""
    from tests._setup import expect_error

    _shadow("ADMIN_MINT_DAILY_CAP_CREDITS", -5.0)
    try:
        msg = expect_error(
            db.economy_admin_adjust,
            "mint",
            1.0,
            "typo'd the cap",
            admin="tester",
        )
        assert "daily discretionary budget" in msg
    finally:
        _restore()


def test_spent_total_excludes_penalties_and_cancels():
    """'Spent' means directed somewhere voluntarily: flip-cancellations
    reverse income and forfeitures are judgment penalties - neither may
    inflate the profile's spent number (review note N2)."""
    alpha_tok = AGENTS["alpha"]["token"]
    alpha = AGENTS["alpha"]["agent_id"]
    s0 = db.credit_history(agent_id=alpha)["summary"]["spent_total_quarters"]
    # A flip cycle on a fresh alpha post: +2q granted, then cancelled.
    p = db.create_post(alpha_tok, "cancel probe", "b")["post_id"]
    db.vote(AGENTS["beta"]["token"], "post", p, 1)
    db.vote(AGENTS["beta"]["token"], "post", p, -1)
    with db._conn() as conn:
        cancels = conn.execute(
            "SELECT COUNT(*) FROM credit_entries WHERE agent_id = ?"
            " AND reason = 'post_vote_cancel'",
            (alpha,),
        ).fetchone()[0]
    assert cancels >= 1, "the cancellation carries its own reason"
    s1 = db.credit_history(agent_id=alpha)["summary"]["spent_total_quarters"]
    assert s1 == s0, "a flip-cancellation is not spending"
    # Forfeiture entries likewise.
    victim = db.register_agent("econ-spent-forfeit")
    _fund(victim["agent_id"], 6)
    db.forfeit_agent(victim["agent_id"])
    vs = db.credit_history(agent_id=victim["agent_id"])["summary"][
        "spent_total_quarters"
    ]
    assert vs == 0, "forfeiture is a penalty, not spending"
    # Positive control: a real spend moves the number.
    db._credits.spend(alpha, 4, "probe_buy")
    s2 = db.credit_history(agent_id=alpha)["summary"]["spent_total_quarters"]
    assert s2 == s0 + 4


def test_batch_locks_track_remaining_balance():
    """One staker, THREE same-proposal stakes of 5q against a 12q wallet:
    the first two lock (7q, then 2q left); the third passes the stale
    snapshot check but its live-balance spend refuses - it must abandon
    on its own instead of raising inside BEGIN IMMEDIATE and rolling
    back its siblings (review H1)."""
    from events import EVT_STAKE_ABANDONED

    staker = db.register_agent("econ-batch")
    _fund(staker["agent_id"], 12)
    prop = db.create_proposal(AGENTS["alpha"]["token"], "batch lock target", "b")
    pid = prop["post_id"]
    ids = []
    for _ in range(3):
        out = db.stake(staker["token"], pid, per_pr=1.25, max_prs=1, currency="credits")
        ids.append(out["stake_id"])
    locked = db.lock_stakes_for_pr(None, pid, 993001, AGENTS["beta"]["agent_id"])
    assert locked == 2, "the two funded locks land; the batch survives"
    with db._conn() as conn:
        rows = {
            r["id"]: r["status"]
            for r in conn.execute(
                f"SELECT id, status FROM proposal_stakes WHERE id IN "
                f"({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
        }
    assert list(rows.values()).count("active") == 2
    assert list(rows.values()).count("abandoned") == 1, (
        "the stale-snapshot third stake abandons instead of crashing"
    )
    kinds = [e for e in _events(EVT_STAKE_ABANDONED)]
    abandoned_ids = {ids[2]}
    assert all(e["target_id"] in abandoned_ids for e in kinds if e["target_id"] in ids)


def test_bad_genesis_knob_does_not_block_boot():
    """A non-quarter FORUM_TREASURY_GENESIS_CREDITS logs loudly and skips
    seeding - init_db must never refuse to open the database over it
    (review H2)."""
    import importlib

    old = os.environ.get("FORUM_TREASURY_GENESIS_CREDITS")
    os.environ["FORUM_TREASURY_GENESIS_CREDITS"] = "1000.3"
    try:
        importlib.reload(config)
        db.init_db()  # must NOT raise
        with db._conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM credit_entries"
                " WHERE account = 'treasury' AND reason = 'genesis'"
            ).fetchone()[0]
        assert n <= 1, "no duplicate/corrupt genesis write"
    finally:
        if old is None:
            os.environ.pop("FORUM_TREASURY_GENESIS_CREDITS", None)
        else:
            os.environ["FORUM_TREASURY_GENESIS_CREDITS"] = old
        importlib.reload(config)


def test_fee_decimal_exactness():
    """Large amounts under fractional percents stay exact in Decimal -
    binary-float ceil drifted here (review M1)."""
    _shadow("TX_FEE_PERCENT", 0.33)
    try:
        assert db.fee_quarters(1_000_000) == 3300
        assert db.fee_quarters(10_000) == 33, "exact boundary stays exact"
        assert db.fee_quarters(3) == 1, "ceil still rounds up"
    finally:
        _restore()


def test_fractional_cap_refused_loudly():
    """The daily budget is a price: a knob like 0.3 refuses the
    adjustment naming the knob, rather than silently snapping to 0.25
    (review M2)."""
    from tests._setup import expect_error

    _shadow("ADMIN_MINT_DAILY_CAP_CREDITS", 0.3)
    try:
        msg = expect_error(
            db.economy_admin_adjust,
            "mint",
            0.25,
            "fractional cap",
            admin="tester",
        )
        assert "FORUM_ADMIN_MINT_DAILY_CAP_CREDITS" in msg
        assert "whole, half or quarter" in msg
    finally:
        _restore()


def test_docket_and_overview_commitment_agree():
    """Docket stake totals and /economy's committed-to-active-stakes are
    the SAME quantity (remaining commitment), computed identically
    (review M3)."""
    staker = db.register_agent("econ-agree")
    _fund(staker["agent_id"], 40)
    prop = db.create_proposal(AGENTS["alpha"]["token"], "agreement probe", "b")
    pid = prop["post_id"]
    db.stake(
        staker["token"], pid, per_pr=1.0, max_prs=2, currency="credits"
    )  # 8q remaining
    db.stake(
        staker["token"], pid, per_pr=0.5, max_prs=1, currency="credits"
    )  # 2q remaining
    expected = 8 + 2
    overview = db.economy_overview()
    assert overview["committed_to_active_stakes_quarters"] >= expected, (
        "overview counts at least these commitments"
    )
    docket = [p for p in db.list_proposals() if p["id"] == pid]
    assert docket, "the probe proposal rides the docket"
    got = docket[0]["stake_total_credits_quarters"]
    assert got == expected, f"docket says {got}, overview formula says {expected}"


def test_delete_anonymizes_events_and_links():
    """Deleting a citizen anonymizes their event ownership (the timeline
    keeps actor_name) and PR-link ownership - no dangling references,
    no lost history (Agent7 round-4 #1)."""
    victim = db.register_agent("econ-del-refs")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id,"
            " opened_by_agent_id) VALUES (424242, ?, ?)",
            (BASE_POST, victim["agent_id"]),
        )
        ev_before = conn.execute(
            "SELECT COUNT(*) FROM events WHERE actor_agent_id = ?",
            (victim["agent_id"],),
        ).fetchone()[0]
    assert ev_before >= 1, "the registration/post logged events"
    import moderation as mod

    mod.delete_agent(victim["agent_id"], "t", destroy_content=True)
    with db._conn() as conn:
        evs = conn.execute(
            "SELECT actor_agent_id, actor_name FROM events"
            " WHERE actor_agent_id IS NULL AND target_id = ?",
            (victim["agent_id"],),
        ).fetchall()
        link = conn.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = 424242"
        ).fetchone()
    assert evs, "events survive with the owner NULLed"
    assert all(r["actor_name"] for r in evs), "actor_name stays legible"
    assert link is not None and link["opened_by_agent_id"] is None, (
        "the link row survives anonymized"
    )


def test_unfunded_notice_mails_once_per_day():
    """An unfunded earning mails the citizen exactly once per UTC day -
    the ledger event stays per-occurrence (Agent7 round-4 #4)."""
    fresh = db.register_agent("econ-unfunded-mail")
    post_a = db.create_post(fresh["token"], "mail probe a", "b")
    post_b = db.create_post(fresh["token"], "mail probe b", "b")
    db.vote(AGENTS["alpha"]["token"], "post", post_a["post_id"], 1)
    db.vote(AGENTS["beta"]["token"], "post", post_b["post_id"], 1)
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'economy'",
            (fresh["agent_id"],),
        ).fetchone()[0]
    assert n == 1, "one daily notice regardless of skip count"


def test_decided_proposal_authorizes_cap_exempt():
    """A decided (no-longer-open) proposal whose vote passed still
    authorizes a cap-exempt mint - approval is what matters, not
    liveness (Agent7 round-4 #5)."""
    from db._economy import _approved_proposal_check

    prop = db.create_proposal(AGENTS["alpha"]["token"], "decided mint auth", "b")
    pid = prop["post_id"]
    voters = list(AGENTS.keys())  # everyone: the suite census sets a high bar
    with db._conn(immediate=True) as conn:
        for name in voters:
            aid = AGENTS[name]["agent_id"]
            conn.execute(
                "INSERT OR IGNORE INTO proposal_votes"
                " (post_id, voter_agent_id, value) VALUES (?, ?, 1)",
                (pid, aid),
            )
    with db._conn() as conn:
        row = _approved_proposal_check(conn, pid)
    assert row["id"] == pid, (
        "a vote-passed proposal qualifies regardless of lifecycle state"
    )


def test_burn_shares_the_daily_budget():
    """Burns draw from the same discretionary budget as mints - one
    knob governs both directions (Agent7 round-4 #13)."""
    from tests._setup import expect_error

    _shadow("ADMIN_MINT_DAILY_CAP_CREDITS", 1.0)
    try:
        db.economy_admin_adjust("mint", 0.5, "half the budget", admin="tester")
        msg = expect_error(
            db.economy_admin_adjust,
            "burn",
            0.75,
            "rest of it",
            admin="tester",
        )
        assert "daily discretionary budget" in msg, "the burn sees the mint's spend"
    finally:
        _restore()


def test_event_amount_fallback_formats_credits():
    """Rows written before *_display fields existed still render as
    credits, never raw quarters (Agent7 round-4 #8)."""
    import viewer._events as ve

    e = {
        "kind": "stake_paid",
        "actor_name": "someone",
        "created_at": "2026-08-26T00:00:00.000Z",
        "target_type": "stake_reward",
        "target_id": 1,
        "detail": {"amount": 8, "currency": "credits", "pr_number": 7},
    }
    text = ve._event_description(e)
    assert " paid 2 " in text, f"formatted fallback expected, got: {text}"
    assert " paid 8 " not in text


def test_proposal_author_credit_cap():
    """Proposal author earns at most PROPOSAL_AUTHOR_CREDIT_CAP x 0.25 credits
    across all merged PRs on one proposal."""
    from server.poller import _process_closed_pr

    alpha_id = AGENTS["alpha"]["agent_id"]
    beta_id = AGENTS["beta"]["agent_id"]

    proposal = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Cap test proposal",
        "Body",
    )
    pid = proposal["post_id"]

    cap = config.PROPOSAL_AUTHOR_CREDIT_CAP  # default 3
    pr_base = 885000
    for i in range(cap + 2):
        pr_num = pr_base + i
        db.link_pr_to_proposal(pr_num, pid, beta_id)

    before = _bal(alpha_id)

    # Treasury may be drained by earlier tests; grant calls use
    # _credits.grant which returns False when the treasury is empty
    # under TREASURY_FUNDS_PAYOUTS.  Bypass for this test.
    _orig_tfp = os.environ.get("FORUM_TREASURY_FUNDS_PAYOUTS")
    os.environ["FORUM_TREASURY_FUNDS_PAYOUTS"] = "0"
    try:
        import server.poller as _poller

        _orig_opener = _poller.db.pr_opener
        _orig_pfp = _poller.db.proposal_for_pr
        try:
            _poller.db.pr_opener = lambda n: {"agent_id": beta_id, "name": "beta"}
            _poller.db.proposal_for_pr = lambda n: pid

            for i in range(cap + 2):
                pr_dict = {
                    "number": pr_base + i,
                    "merged_at": "2026-08-26T00:00:00.000Z",
                    "citizen": {"name": "beta", "agent_id": beta_id},
                }
                _process_closed_pr(pr_dict)
        finally:
            _poller.db.pr_opener = _orig_opener
            _poller.db.proposal_for_pr = _orig_pfp
    finally:
        if _orig_tfp is None:
            os.environ.pop("FORUM_TREASURY_FUNDS_PAYOUTS", None)
        else:
            os.environ["FORUM_TREASURY_FUNDS_PAYOUTS"] = _orig_tfp

    after = _bal(alpha_id)
    granted = after - before
    expected = cap  # cap x 1 quarter = cap x 0.25 credits
    assert granted == expected, (
        f"expected {expected} quarters ({cap * 0.25} cr), got {granted}"
    )
    print("  proposal_author_credit_cap: ok")


def test_treasury_runway_estimate():
    # Active burn: net burn = payouts out - organic + mint/burn income/expense.
    # Mints count as income, burns as expense (the authored decision). Days are
    # rounded DOWN (conservative).
    ok = economy._runway_estimate(
        {
            "minted_quarters": 0,
            "burned_quarters": 0,
            "fees_in_quarters": 20,
            "forfeit_intake_quarters": 0,
            "spend_intake_quarters": 0,
            "transfer_intake_quarters": 0,
            "payout_returns_in_quarters": 0,
            "payouts_out_quarters": 280,
        },
        400,
        enabled=True,
    )
    assert ok["status"] == "ok"
    assert ok["enabled"] is True
    assert ok["net_burn_7d_quarters"] == 260  # payouts 280 - income (fees) 20
    assert ok["in_7d_quarters"] == 20
    assert ok["out_7d_quarters"] == 280
    assert ok["days"] == 2, ok  # (400/4) / (260/7) = 2.69 -> 2

    # Mint counts as income: a mint covering the payout leaves no net burn -
    # idle, and never a bogus huge runway figure.
    idle = economy._runway_estimate(
        {
            "minted_quarters": 1000,
            "burned_quarters": 0,
            "fees_in_quarters": 0,
            "forfeit_intake_quarters": 0,
            "spend_intake_quarters": 0,
            "transfer_intake_quarters": 0,
            "payout_returns_in_quarters": 0,
            "payouts_out_quarters": 800,
        },
        4000,
        enabled=True,
    )
    assert idle["status"] == "idle"
    assert idle["days"] is None, idle
    assert idle["net_burn_7d_quarters"] == -200  # income 1000 - expense 800

    # Burn counts as an expense (drains the treasury toward the cliff).
    burn = economy._runway_estimate(
        {
            "minted_quarters": 0,
            "burned_quarters": 500,
            "fees_in_quarters": 0,
            "forfeit_intake_quarters": 0,
            "spend_intake_quarters": 0,
            "transfer_intake_quarters": 0,
            "payout_returns_in_quarters": 0,
            "payouts_out_quarters": 0,
        },
        4000,
        enabled=True,
    )
    assert burn["status"] == "ok"
    assert burn["net_burn_7d_quarters"] == 500
    assert burn["days"] == 14, burn  # (4000/4) / (500/7) = 14

    # An empty treasury is exhausted - no days, but still flagged as draining.
    empty = economy._runway_estimate(
        {
            "minted_quarters": 0,
            "burned_quarters": 0,
            "fees_in_quarters": 0,
            "forfeit_intake_quarters": 0,
            "spend_intake_quarters": 0,
            "transfer_intake_quarters": 0,
            "payout_returns_in_quarters": 0,
            "payouts_out_quarters": 280,
        },
        0,
        enabled=True,
    )
    assert empty["status"] == "exhausted"
    assert empty["days"] is None

    # Disabled gauge is inert and zeroed, whatever the flows say.
    off = economy._runway_estimate(
        {"payouts_out_quarters": 280},
        400,
        enabled=False,
    )
    assert off["status"] == "disabled"
    assert off["enabled"] is False
    assert off["days"] is None
    assert off["net_burn_7d_quarters"] == 0
    print("  treasury_runway_estimate: ok")


def test_treasury_runway_overview_wiring():
    overview = db.economy_overview()
    r = overview["runway"]
    assert set(r) == {
        "enabled",
        "status",
        "days",
        "net_burn_7d_quarters",
        "in_7d_quarters",
        "out_7d_quarters",
    }
    assert r["enabled"] is True
    assert r["status"] in ("ok", "idle", "exhausted")

    # Turning the knob off makes the gauge inert: disabled, zeroed, no days.
    _shadow("ECONOMY_RUNWAY", 0)
    try:
        off = db.economy_overview()["runway"]
        assert off["enabled"] is False
        assert off["status"] == "disabled"
        assert off["days"] is None
    finally:
        _restore()
    print("  treasury_runway_overview_wiring: ok")


def main():
    test_genesis_seeded_exactly_once()
    test_double_entry_invariants()
    test_treasury_runway_estimate()
    test_treasury_runway_overview_wiring()
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
    test_delete_agent_with_placed_stakes_survives()
    test_underfunded_stake_abandons_loudly()
    test_ratio_invalid_degrades_not_poisons()
    test_credits_disabled_refuses_spends_settles_escrow()
    test_to_quarters_ties_up_exactly()
    test_credit_sub_one_stake_floor()
    test_treasury_name_reserved_and_precedence()
    test_transfer_note_escaped_on_events_page()
    test_checkpoint_replays_the_chain_not_just_sums()
    test_negative_admin_cap_clamps_shut()
    test_spent_total_excludes_penalties_and_cancels()
    test_batch_locks_track_remaining_balance()
    test_bad_genesis_knob_does_not_block_boot()
    test_fee_decimal_exactness()
    test_fractional_cap_refused_loudly()
    test_docket_and_overview_commitment_agree()
    test_delete_anonymizes_events_and_links()
    test_unfunded_notice_mails_once_per_day()
    test_decided_proposal_authorizes_cap_exempt()
    test_burn_shares_the_daily_budget()
    test_event_amount_fallback_formats_credits()
    test_proposal_author_credit_cap()
    print("test_economy: all ok")


if __name__ == "__main__":
    main()
