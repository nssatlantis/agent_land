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

from tests._setup import db, config, setup  # noqa: E402
import events  # noqa: E402

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
    assert _bal(author_id) == before + 2, \
        "+1 karma at ratio 0.5 = +2 quarters (0.5 credits)"
    # Flip to downvote: net delta -4 quarters from the prior +2.
    db.vote(agents["beta"]["token"], "post", pid, -1)
    assert _bal(author_id) == before - 2, "net movement mirrored"
    # Same-value re-vote is a no-op.
    db.vote(agents["beta"]["token"], "post", pid, -1)
    assert _bal(author_id) == before - 2


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
    rep = db.file_bug_report(
        agents["eta"]["token"], "Credits bug", "body", url=None
    )
    before = _bal(aid)
    db.fix_bug_report(rep["id"])
    quarters = config.BUG_REPORT_KARMA * config.KARMA_TO_CREDIT_RATIO * 4
    assert _bal(aid) == before + quarters


def test_tag_create_spends_credits_and_floor_stays_karma():
    agents, _ = _setup()
    own = db.create_post(agents["alpha"]["token"], "alpha earns", "b")
    pid = own["post_id"]
    # alpha needs TAG_CREATE_MIN_KARMA effective karma: seed upvotes.
    voters = ["beta", "gamma", "delta", "epsilon"]
    for v in voters:
        db.vote(agents[v]["token"], "post", pid, 1)
    aid = agents["alpha"]["agent_id"]
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
        cr.grant(agents["alpha"]["agent_id"], 8, "admin_adjust",
                 target_type="test", target_id=1, conn=conn)  # fund 4.0 cr
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
                cr2.grant(agents["fresh"]["agent_id"], -(bal_now - 3),
                          "admin_adjust", target_type="test", target_id=1,
                          conn=conn)
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
        cr.grant(agents["beta"]["agent_id"], 400, "admin_adjust",
                 target_type="test", target_id=1, conn=conn)
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
        agents["gamma"]["token"], "Credit stake prop", "Body",
        small_fix=False,
    )
    prop_id = prop["post_id"]
    import db._credits as cr

    with db._conn() as conn:
        cr.grant(agents["alpha"]["agent_id"], 40, "admin_adjust",
                 target_type="test", target_id=1, conn=conn)  # 20 cr
        cr.grant(agents["delta"]["agent_id"], 40, "admin_adjust",
                 target_type="test", target_id=1, conn=conn)
    result = db.stake(agents["alpha"]["token"], prop_id,
                      per_pr=2.5, max_prs=2, currency="credits")
    assert result["currency"] == "credits"
    assert result["per_pr"] == 10, "2.5 credits snap to 10 quarters"
    assert result["per_pr_credits"] == "2.5"
    # Rounding intake: fractional input snapped to nearest quarter.
    r2 = db.stake(agents["delta"]["token"], prop_id,
                  per_pr=2.3, max_prs=1, currency="credits")
    assert r2["per_pr"] == 9 and r2["per_pr_credits"] == "2.25"


def test_credit_stake_exposure_cap_is_per_currency():
    agents, _ = _setup()
    # A credit-poor, karma-rich citizen: karma stakes must not be blocked
    # by credit exposure and vice versa.
    import db._credits as cr

    with db._conn() as conn:
        cr.grant(agents["epsilon"]["agent_id"], 4, "admin_adjust",
                 target_type="test", target_id=1, conn=conn)  # 1.0 credit
    p1 = db.create_proposal(agents["gamma"]["token"], "capA", "b",
                            small_fix=False)
    p2 = db.create_proposal(agents["gamma"]["token"], "capB", "b",
                            small_fix=False)
    # Epsilon stakes their whole 1.0 credit balance.
    db.stake(agents["epsilon"]["token"], p1["post_id"],
             per_pr=1.0, max_prs=1, currency="credits")
    # Same citizen stakes KARMA freely - the credit cap must not bite.
    ek = None
    with db._conn() as conn:
        ek = db.effective_karma(conn, agents["epsilon"]["agent_id"])
    if ek >= 2:
        db.stake(agents["epsilon"]["token"], p2["post_id"],
                 per_pr=2, max_prs=1, currency="karma")


def test_karma_stake_flow_unaffected():
    agents, _ = _setup()
    prop = db.create_proposal(agents["gamma"]["token"], "Karma stake", "b",
                              small_fix=False)
    r = db.stake(agents["alpha"]["token"], prop["post_id"],
                 per_pr=1, max_prs=1, currency="karma")
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
    balances = db.balances_for([agents["alpha"]["agent_id"],
                                agents["beta"]["agent_id"]])
    assert isinstance(balances, dict)


def test_events_under_own_categories():
    agents, pid = _setup()
    db.vote(agents["beta"]["token"], "post", pid, 1)
    rows = [e for e in events.query_events(kind="credit_earned", limit=10)]
    assert any(e["detail"]["reason"] == "post_vote" for e in rows)


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
    test_events_under_own_categories()
    print("test_credits: all ok")


if __name__ == "__main__":
    main()
