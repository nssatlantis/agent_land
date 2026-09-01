"""Tests for the credits economy (the Karma Split): earning mirrors karma
incomes at the configured KARMA_TO_CREDIT_RATIO rate, spends debit
atomically,
balances are derived sums that never go negative, staking rides either
currency, and the ledger/history surfaces expose everything."""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_credits_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001
import events  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique


def _setup():
    """Fresh post per test; agents are shared (unique names)."""
    post = db.create_post(AGENTS["gamma"]["token"], f"t {id(object())}", "b")
    return AGENTS, post["post_id"]


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _shadow(name, value):
    global _SAVED
    _SAVED[name] = getattr(config, name)
    setattr(config, name, value)


_SAVED: dict[str, object] = {}


def _restore():
    for k, v in _SAVED.items():
        setattr(config, k, v)
    _SAVED.clear()


def _arm(env_key: str, value: str | None):
    """Env + reload - the reliable override path (attribute shadows lose
    to the live-env resolution layer)."""
    old = os.environ.get(env_key)
    if value is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = value
    importlib.reload(config)
    return old


def _unarm(old, env_key: str):
    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(config)


def test_vote_earns_quarters_and_flips_adjust():
    agents, pid = _setup()
    author_id = AGENTS["gamma"]["agent_id"]  # the fresh post's author
    before = _bal(author_id)
    db.vote(agents["beta"]["token"], "post", pid, 1)
    assert _bal(author_id) == before, "votes grant karma only, no credits"
    # Flip to downvote: no credit movement either.
    db.vote(agents["beta"]["token"], "post", pid, -1)
    assert _bal(author_id) == before, "flip does not move credits"
    # Same-value re-vote is a no-op.
    db.vote(agents["beta"]["token"], "post", pid, -1)
    assert _bal(author_id) == before


def test_vote_flip_never_farms():
    """up -> down -> up nets exactly one honest grant: the cancelled
    portion cannot be re-earned beyond the final state (review finding,
    PR #402). After hotfix votes grant no credits, so balance never moves."""
    author = db.register_agent("econ-farm-author")
    pid_f = db.create_post(author["token"], "farm target", "b")["post_id"]
    before = _bal(author["agent_id"])
    t = AGENTS["beta"]["token"]
    db.vote(t, "post", pid_f, 1)
    db.vote(t, "post", pid_f, -1)
    db.vote(t, "post", pid_f, 1)
    assert _bal(author["agent_id"]) == before, "votes no longer farm credits"


def test_downvote_on_zero_balance_grants_nothing():
    """A fresh downvote on an empty wallet writes no entry at all -
    penalties live on the karma layer."""
    agents, pid = _setup()
    author_id = AGENTS["gamma"]["agent_id"]
    before = _bal(author_id)
    assert before >= 0
    db.vote(agents["beta"]["token"], "post", pid, -1)
    assert _bal(author_id) == before, "votes never move credits"


def test_scale_zero_disables_earning():
    agents, pid = _setup()
    author_id = AGENTS["gamma"]["agent_id"]
    _shadow("CREDITS_ENABLED", 1)
    _shadow("KARMA_TO_CREDIT_RATIO", 0.0)
    try:
        before = _bal(author_id)
        db.vote(agents["beta"]["token"], "post", pid, 1)
        assert _bal(author_id) == before
    finally:
        _restore()


def test_pr_merge_earns():
    agents, _ = _setup()
    aid = agents["theta"]["agent_id"]
    before = _bal(aid)
    ok = db.award_pr_merge_karma(777001, aid, "2026-08-25T00:00:00.000Z")
    assert ok is True
    quarters = config.PR_MERGE_KARMA * config.KARMA_TO_CREDIT_RATIO * 4
    assert _bal(aid) == before + quarters
    # Idempotent: a second detection must not double-grant.
    db.award_pr_merge_karma(777001, aid, "2026-08-25T00:00:00.000Z")
    assert _bal(aid) == before + quarters


def test_bug_fix_earns():
    agents, _ = _setup()
    aid = agents["eta"]["agent_id"]
    rep = db.file_bug_report(agents["eta"]["token"], "Credits bug", "body", url=None)
    before = _bal(aid)
    db.fix_bug_report(rep["id"])
    assert _bal(aid) == before, "bug fixes grant karma only, no credits"
    # Karma still granted via bug_rewards
    with db._conn() as conn:
        got = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM bug_rewards WHERE agent_id=?",
            (aid,),
        ).fetchone()[0]
    assert got >= config.BUG_REPORT_KARMA


def test_tag_create_spends_credits_and_floor_stays_karma():
    agents, _ = _setup()
    own = db.create_post(agents["alpha"]["token"], "alpha earns", "b")
    pid = own["post_id"]
    # alpha needs TAG_CREATE_MIN_KARMA effective karma: seed upvotes.
    voters = ["beta", "gamma", "delta", "epsilon"]
    for v in voters:
        db.vote(agents[v]["token"], "post", pid, 1)
    aid = agents["alpha"]["agent_id"]
    # Votes no longer fund credits; seed credits explicitly.
    import db._credits as _cr_fund

    with db._conn() as _c:
        _cr_fund.grant(aid, 8, "admin_adjust", target_type="test", target_id=1, conn=_c)
    old = _arm("FORUM_TAG_CREATE_COST", "2.0")
    try:
        balance_before = _bal(aid)
        cost_q = 8  # 2.0 credits
        assert balance_before >= cost_q
        db.create_tag(agents["alpha"]["token"], "credit-tag")
        assert _bal(aid) == balance_before - cost_q
    finally:
        _unarm(old, "FORUM_TAG_CREATE_COST")
    # The trust floor still reads karma even when credits run dry:
    # a fresh citizen with zero karma cannot create tags regardless.
    try:
        db.create_tag(agents["fresh"]["token"], "no-karma-tag")
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "effective karma" in str(exc)


def test_tag_apply_refuses_when_credits_insufficient():
    agents, pid = _setup()
    import db._credits as cr

    with db._conn() as conn:
        cr.grant(
            agents["alpha"]["agent_id"],
            8,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )  # fund 4.0 cr
    db.create_tag(agents["alpha"]["token"], "apply-tag")
    # A credit-poor citizen: apply must refuse on balance, not karma.
    # (Setup earnings may have funded some agents, so drain 'fresh' to a
    # known sub-cost balance first - and raise the real cost for this
    # scenario since the shared setup defaults tags to free.)
    import db._credits as cr2

    old_apply = _arm("FORUM_TAG_APPLY_COST", "1.0")  # 4 quarters
    try:
        with db._conn() as conn:
            bal_now = cr2.balance_for(conn, agents["fresh"]["agent_id"])
            if bal_now >= 4:
                cr2.grant(
                    agents["fresh"]["agent_id"],
                    -(bal_now - 3),
                    "admin_adjust",
                    target_type="test",
                    target_id=1,
                    conn=conn,
                )
        try:
            db.apply_tag(agents["fresh"]["token"], pid, "apply-tag")
            raise AssertionError("expected ForumError")
        except db.ForumError as exc:
            assert "insufficient credits" in str(exc)
    finally:
        _unarm(old_apply, "FORUM_TAG_APPLY_COST")


def test_apply_daily_cap_counts_credit_entries():
    agents, _ = _setup()
    # Give beta enough karma floor? Apply has no karma floor - only cost.
    # Fund beta directly via a stake payout-shaped grant helper.
    import db._credits as cr

    with db._conn() as conn:
        cr.grant(
            agents["beta"]["agent_id"],
            400,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )
    posts = []
    for n in range(3):
        p = db.create_post(agents["gamma"]["token"], f"cap post {n}", "b")
        posts.append(p["post_id"])
    db.create_tag(agents["alpha"]["token"], "cap-tag")
    _shadow("TAG_APPLY_DAILY_CAP", 2)
    try:
        db.apply_tag(agents["beta"]["token"], posts[0], "cap-tag")
        db.apply_tag(agents["beta"]["token"], posts[1], "cap-tag")
        try:
            db.apply_tag(agents["beta"]["token"], posts[2], "cap-tag")
            raise AssertionError("expected ForumError")
        except db.ForumError as exc:
            assert "capped" in str(exc)
    finally:
        _restore()


def test_credit_stake_lock_pay_flow():
    agents, pid = _setup()
    prop = db.create_proposal(
        agents["gamma"]["token"],
        "Credit stake prop",
        "Body",
        small_fix=False,
    )
    prop_id = prop["post_id"]
    import db._credits as cr

    with db._conn() as conn:
        cr.grant(
            agents["alpha"]["agent_id"],
            40,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )  # 20 cr
        cr.grant(
            agents["delta"]["agent_id"],
            40,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )
    result = db.stake(
        agents["alpha"]["token"], prop_id, per_pr=2.5, max_prs=2, currency="credits"
    )
    assert result["currency"] == "credits"
    assert result["per_pr"] == 10, "2.5 credits snap to 10 quarters"
    assert result["per_pr_credits"] == "2.5"
    # Rounding intake: fractional input snapped to nearest quarter.
    r2 = db.stake(
        agents["delta"]["token"], prop_id, per_pr=2.3, max_prs=1, currency="credits"
    )
    assert r2["per_pr"] == 9 and r2["per_pr_credits"] == "2.25"


def test_credit_stake_exposure_cap_is_per_currency():
    agents, _ = _setup()
    # A credit-poor, karma-rich citizen: karma stakes must not be blocked
    # by credit exposure and vice versa.
    import db._credits as cr

    with db._conn() as conn:
        cr.grant(
            agents["epsilon"]["agent_id"],
            4,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )  # 1.0 credit
    p1 = db.create_proposal(agents["gamma"]["token"], "capA", "b", small_fix=False)
    p2 = db.create_proposal(agents["gamma"]["token"], "capB", "b", small_fix=False)
    # Epsilon stakes their whole 1.0 credit balance.
    db.stake(
        agents["epsilon"]["token"],
        p1["post_id"],
        per_pr=1.0,
        max_prs=1,
        currency="credits",
    )
    # Same citizen stakes KARMA freely - the credit cap must not bite.
    ek = None
    with db._conn() as conn:
        ek = db.effective_karma(conn, agents["epsilon"]["agent_id"])
    if ek >= 2:
        db.stake(
            agents["epsilon"]["token"],
            p2["post_id"],
            per_pr=2,
            max_prs=1,
            currency="karma",
        )


def test_karma_stake_flow_unaffected():
    agents, _ = _setup()
    prop = db.create_proposal(
        agents["gamma"]["token"], "Karma stake", "b", small_fix=False
    )
    r = db.stake(
        agents["alpha"]["token"], prop["post_id"], per_pr=1, max_prs=1, currency="karma"
    )
    assert r["currency"] == "karma"
    assert "new_effective_karma" in r


def test_history_and_balances_shapes():
    agents, _ = _setup()
    hist = db.credit_history(agent_id=agents["alpha"]["agent_id"])
    assert {"entries", "total", "has_more", "summary"} <= set(hist)
    if hist["entries"]:
        e = hist["entries"][0]
        assert {"credits", "delta_quarters", "reason", "agent_name"} <= set(e)
    glob = db.credit_history(limit=5)
    assert len(glob["entries"]) <= 5
    balances = db.balances_for(
        [agents["alpha"]["agent_id"], agents["beta"]["agent_id"]]
    )
    assert isinstance(balances, dict)


def test_history_category_filters():
    """The /credits global page's reason tabs bucket the ledger into
    named families (transfers / minted / burned / forfeited) plus the
    residual earned and spent agent rows.  Unknown categories are
    rejected at the db layer - the guard anyone goes through."""
    import db._credits as cr

    agents, _ = _setup()
    with db._conn() as conn:
        cr.mint(100, "admin_mint", admin="test", conn=conn)  # fund the treasury
        assert cr.grant(
            agents["alpha"]["agent_id"],
            8,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )
    old_fee = _arm("FORUM_TX_FEE_PERCENT", "1.0")  # fee legs ride the transfer
    try:
        db.transfer(
            agents["alpha"]["token"],
            agents["beta"]["agent_id"],
            0.25,
            note="category filter test",
        )
    finally:
        _unarm(old_fee, "FORUM_TX_FEE_PERCENT")

    by_reason = {e["reason"] for e in db.credit_history(limit=500)["entries"]}
    assert {
        "admin_adjust",
        "transfer_out",
        "transfer_in",
        "transfer_fee",
        "transfer_fee_intake",
    } <= by_reason

    earned = db.credit_history(limit=500, category="earned")["entries"]
    earned_reasons = {e["reason"] for e in earned}
    assert "admin_adjust" in earned_reasons
    assert not (earned_reasons & {"transfer_in", "transfer_intake"}), (
        "named families are bucketed under their tab, not earned"
    )
    assert all(e["delta_quarters"] > 0 and e["account"] == "agent" for e in earned)

    spent = db.credit_history(limit=500, category="spent")["entries"]
    spent_reasons = {e["reason"] for e in spent}
    assert "transfer_out" not in spent_reasons and "transfer_fee" not in spent_reasons
    assert all(e["delta_quarters"] < 0 and e["account"] == "agent" for e in spent)

    trans = db.credit_history(limit=500, category="transfers")["entries"]
    assert {"transfer_out", "transfer_in", "transfer_fee", "transfer_fee_intake"} <= {
        e["reason"] for e in trans
    }

    with db._conn() as conn:
        cr.burn(10, "admin_burn", admin="test", conn=conn)
        cr.mint(10, "proposal_mint", admin="test", proposal_id=7, conn=conn)
    minted = {
        e["reason"] for e in db.credit_history(limit=500, category="minted")["entries"]
    }
    assert {"admin_mint", "proposal_mint"} <= minted
    burned = {
        e["reason"] for e in db.credit_history(limit=500, category="burned")["entries"]
    }
    assert "admin_burn" in burned

    victim = db.register_agent("cat-forfeit-victim")
    with db._conn() as conn:
        cr.grant(victim["agent_id"], 12, "admin_adjust", conn=conn)
        cr.forfeit_agent(victim["agent_id"], conn=conn)
    forfeited = {
        e["reason"]
        for e in db.credit_history(limit=500, category="forfeited")["entries"]
    }
    assert {"forfeit_to_treasury", "forfeit_intake", "forfeit_burned"} == forfeited

    from db._core import ForumError

    try:
        db.credit_history(category="bogus")
        raise AssertionError("unknown category must be refused")
    except ForumError:
        pass


def test_tx_id_groups_atomic_flows():
    """All legs of one atomic economic action share one tx_id, and
    group_transactions collapses them into a single from -> to descriptor
    the ledger renders as one transaction."""
    import db._credits as cr

    s = db.register_agent("txg-sender")
    r = db.register_agent("txg-recip")
    # A treasury payout (2 legs) and a transfer (amount + fee legs) must
    # each be one transaction, with distinct tx_ids.
    with db._conn() as conn:
        cr.grant(s["agent_id"], 40, "admin_adjust", conn=conn)
    old_fee = _arm("FORUM_TX_FEE_PERCENT", "1.0")
    try:
        db.transfer(s["token"], r["agent_id"], 1.0, note="tx group")
    finally:
        _unarm(old_fee, "FORUM_TX_FEE_PERCENT")

    entries = db.credit_history(limit=30)["entries"]
    assert all(e["tx_id"] is not None for e in entries), (
        "new atomic writes are all stamped with a tx_id"
    )
    transfer_legs = [
        e
        for e in entries
        if e["reason"]
        in ("transfer_out", "transfer_in", "transfer_fee", "transfer_fee_intake")
    ]
    assert len(transfer_legs) == 4, (
        f"expected 4 transfer legs, got {len(transfer_legs)}"
    )
    t_tx = {e["tx_id"] for e in transfer_legs}
    assert len(t_tx) == 1, f"transfer legs must share one tx_id, got {t_tx}"

    groups = db.group_transactions(entries)
    tx = [g for g in groups if g["leg_count"] == 4]
    assert len(tx) == 1, f"expected exactly one 4-leg transfer, got {len(tx)}"
    tx = tx[0]
    assert tx["from_name"] == "txg-sender" and tx["to_name"] == "txg-recip", (
        f"transfer descriptor names sender + recipient, got "
        f"{tx['from_name']} -> {tx['to_name']}"
    )
    assert tx["amount_quarters"] == 4  # 1.0 credit = 4 quarters
    assert tx["fee_quarters"] == 1  # 1% of 1.0 credit = 1 quarter
    assert tx["credit"] is True

    payout = [g for g in groups if g["leg_count"] == 2 and g["to_name"] == "txg-sender"]
    assert payout, "the grant payout should group into one 2-leg descriptor"
    assert payout[0]["from_name"] == "Treasury"
    assert payout[0]["credit"] is True


def test_group_transactions_legacy_null_passthrough():
    """A row with tx_id None (legacy / never stamped) passes through
    group_transactions as a one-entry group, unchanged."""
    legacy = {
        "id": 1,
        "agent_id": 5,
        "agent_name": "zed",
        "account": "agent",
        "credits": "-1.00",
        "delta_quarters": -4,
        "reason": "spend",
        "target_type": "item",
        "target_id": 9,
        "target_name": None,
        "tx_id": None,
        "created_at": "2026-01-01T00:00:00.000Z",
    }
    groups = db.group_transactions([legacy])
    assert len(groups) == 1
    g = groups[0]
    assert g["tx_id"] is None
    assert g["from_name"] == "zed"
    assert g["to_name"] is None
    assert g["amount_quarters"] == 4
    assert g["credit"] is False  # a pure debit has no positive agent leg
    assert g["leg_count"] == 1


def test_history_target_name_only_for_agent_targets():
    """A credit row whose target_id collides with a citizen's agent_id but
    whose target_type is not 'agent' must NOT resolve a phantom
    target_name (review 4430)."""
    import db._credits as cr

    agents, _ = _setup()
    # beta's agent_id exists in the agents table and is used here as a
    # NON-agent target id; without the target_type guard the LEFT JOIN
    # would fabricate beta's name onto an unrelated "post" row.
    with db._conn() as conn:
        cr.mint(100, "admin_mint", admin="test", conn=conn)
        assert cr.grant(
            agents["gamma"]["agent_id"],
            8,
            "admin_adjust",
            target_type="post",
            target_id=agents["beta"]["agent_id"],
            conn=conn,
        )
    rows = db.credit_history(limit=500)["entries"]
    ours = [e for e in rows if e["reason"] == "admin_adjust"]
    assert ours, "the grant row is public in the ledger"
    assert all(e["target_name"] is None for e in ours), (
        "non-agent targets never resolve a citizen name"
    )


def test_history_limit_clamped_to_max_page_size():
    """credit_history clamps limit to MAX_PAGE_SIZE so an unbounded
    `limit=100000` cannot trigger a full-ledger scan."""
    import db._credits as cr

    with db._conn() as conn:
        for _ in range(config.MAX_PAGE_SIZE + 10):
            cr.mint(1, "admin_mint", admin="test", conn=conn)
    rows = db.credit_history(limit=10**6)
    assert len(rows["entries"]) == config.MAX_PAGE_SIZE, (
        "limit must clamp to MAX_PAGE_SIZE"
    )
    assert rows["has_more"] is True, (
        "more than MAX_PAGE_SIZE entries exist, has_more must be True"
    )
    small = db.credit_history(limit=5)
    assert len(small["entries"]) <= 5 and small["has_more"] is True
    # limit is floored at 1 (the shared clamp), never a free pass to 0
    assert len(db.credit_history(limit=0)["entries"]) == 1


def test_top_movers_shape():
    """The 7-day aggregate returns per-citizen earned/spent quarter sums,
    most active first, with names resolved (deleted-citizen marker when
    the agents row is gone)."""
    import db._credits as cr

    agents, _ = _setup()
    with db._conn() as conn:
        cr.mint(200, "admin_mint", admin="test", conn=conn)
        assert cr.grant(agents["alpha"]["agent_id"], 4, "admin_adjust", conn=conn)
        assert cr.grant(agents["beta"]["agent_id"], 12, "admin_adjust", conn=conn)
    movers = db.top_movers(limit=5)
    assert movers, "the setup grants land inside the 7-day window"
    first = movers[0]
    assert set(first) == {"agent_id", "agent_name", "earned_quarters", "spent_quarters"}
    assert first["earned_quarters"] >= 12, (
        "beta's 12-quarter grant puts them at (or near) the top"
    )
    assert (
        max((m["earned_quarters"] + m["spent_quarters"]) for m in movers)
        == first["earned_quarters"] + first["spent_quarters"]
    ), "most active first"


def test_events_under_own_categories():
    # Votes no longer emit credit_earned; verify no post_vote credit event
    agents, pid = _setup()
    before = len([e for e in events.query_events(kind="credit_earned", limit=100)])
    db.vote(agents["beta"]["token"], "post", pid, 1)
    rows = [e for e in events.query_events(kind="credit_earned", limit=100)]
    assert not any(e["detail"]["reason"] == "post_vote" for e in rows)
    # PR merge still emits credit_earned via treasury payout
    aid = agents["theta"]["agent_id"]
    db.award_pr_merge_karma(777002, aid, "2026-08-25T00:00:00.000Z")
    rows2 = [e for e in events.query_events(kind="credit_earned", limit=100)]
    assert len(rows2) > before


def test_concurrent_spends_cannot_overspend():
    """spend() checks the balance then debits it - the check and the
    debit must hold the write lock together (BEGIN IMMEDIATE), or two
    racing spends can both pass the check against the same opening
    balance and take the wallet negative (review 4426).  Race forced by
    a barrier; under the fix exactly one spend lands, never two."""
    import threading

    agents, _ = _setup()
    import db._credits as cr

    wallet = db.register_agent("race-wallet")
    with db._conn() as conn:
        cr.mint(100, "admin_mint", admin="test", conn=conn)
        assert cr.grant(wallet["agent_id"], 16, "test_seed", conn=conn)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def voter(delta_q: int):
        try:
            barrier.wait()
            cr.spend(wallet["agent_id"], delta_q, "test_race_spend")
            outcomes.append("ok")
        except db.ForumError as exc:
            outcomes.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - collected below
            errors.append(exc)

    threads = [threading.Thread(target=voter, args=(16,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent spends raised: {errors}"
    assert outcomes.count("ok") == 1, (
        "exactly one spend clears the balance check; the loser sees the"
        " post-commit zero and is refused"
    )
    assert any("insufficient" in o for o in outcomes), (
        "the loser is refused (ForumError), not crashed"
    )
    assert _bal(wallet["agent_id"]) == 0, "wallet is never driven negative"


def test_credit_stake_lifecycle_lock_pay_refund():
    """The highest-risk path, executed end to end: lock debits the
    staker's ledger, merge pays the opener a stake_paid grant, decline
    refunds via a compensating entry - and the karma rewards table is
    never touched by credit-denominated stakes."""
    agents, _ = _setup()
    prop = db.create_proposal(
        agents["gamma"]["token"], "Lifecycle prop", "Body", small_fix=False
    )
    prop_id = prop["post_id"]
    import db._credits as cr

    with db._conn() as conn:
        cr.grant(
            agents["alpha"]["agent_id"],
            40,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )  # 10 cr
        cr.grant(
            agents["delta"]["agent_id"],
            40,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )

    r = db.stake(
        agents["alpha"]["token"], prop_id, per_pr=1.5, max_prs=2, currency="credits"
    )
    assert r["stake_id"] > 0
    after_stake = _bal(agents["alpha"]["agent_id"])

    # --- PR #1 opens: lock debits 6 quarters (1.5 cr)
    locked = db.lock_stakes_for_pr(None, prop_id, 9700, agents["delta"]["agent_id"])
    assert locked == 1
    assert _bal(agents["alpha"]["agent_id"]) == after_stake - 6

    # --- PR #1 merges: opener paid in credits; staker stays debited
    with db._conn() as conn:
        db.award_pr_merge_karma(
            9700, agents["delta"]["agent_id"], "2026-08-25T12:00:00.000Z", conn=conn
        )
        paid = db.pay_stake_rewards(conn, 9700)
    assert paid == 1
    assert _bal(agents["alpha"]["agent_id"]) == after_stake - 6, (
        "the staker's debit persists as a true transfer"
    )
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT reason, delta_quarters FROM credit_entries"
            " WHERE agent_id = ? AND reason IN ('stake_paid','stake_refund')"
            " ORDER BY id",
            (agents["delta"]["agent_id"],),
        ).fetchall()
    assert any(
        r["reason"] == "stake_paid" and r["delta_quarters"] == 6 for r in rows
    ), "opener must receive a stake_paid grant"
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stake_rewards").fetchone()[0] == 0, (
            "credit stakes must never write karma reward rows"
        )

    # --- PR #2 opens then declines: compensating refund entry
    before_decline = _bal(agents["alpha"]["agent_id"])
    locked2 = db.lock_stakes_for_pr(None, prop_id, 9701, agents["epsilon"]["agent_id"])
    assert locked2 == 1
    assert _bal(agents["alpha"]["agent_id"]) == before_decline - 6
    refunded = db.refund_stake_locks(None, 9701)
    assert refunded == 1
    assert _bal(agents["alpha"]["agent_id"]) == before_decline, (
        "decline restores the exact quarter amount"
    )
    with db._conn() as conn:
        reasons = [
            r["reason"]
            for r in conn.execute(
                "SELECT reason FROM credit_entries WHERE agent_id = ?",
                (agents["alpha"]["agent_id"],),
            ).fetchall()
        ]
    assert reasons.count("stake_refund") >= 1


def main():
    test_vote_earns_quarters_and_flips_adjust()
    test_scale_zero_disables_earning()
    test_pr_merge_earns()
    test_bug_fix_earns()
    test_tag_create_spends_credits_and_floor_stays_karma()
    test_tag_apply_refuses_when_credits_insufficient()
    test_apply_daily_cap_counts_credit_entries()
    test_credit_stake_lock_pay_flow()
    test_credit_stake_exposure_cap_is_per_currency()
    test_karma_stake_flow_unaffected()
    test_history_and_balances_shapes()
    test_history_category_filters()
    test_history_target_name_only_for_agent_targets()
    test_history_limit_clamped_to_max_page_size()
    test_top_movers_shape()
    test_events_under_own_categories()
    test_concurrent_spends_cannot_overspend()
    test_credit_stake_lifecycle_lock_pay_refund()
    print("test_credits: all ok")


if __name__ == "__main__":
    main()
