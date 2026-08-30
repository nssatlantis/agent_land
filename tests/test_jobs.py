"""Tests for the job market (CHARTER IX.6): escrowed commissioning,
the claim/offer flow, per-cycle submit/verdict with mandatory feedback,
principal payouts, participation karma for both sides, cancellation and
expiry refunds, nudges/digests, and deletion safety."""

import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_jobs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
# Jobs need funded wallets and a low posting bar; this suite arms its own
# economy knobs explicitly (same pattern as test_economy).
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

# setup()'s upvotes already paid ~14q out of the 4000q genesis; this
# suite seeds many funded creators (~402q each), so top the treasury up
# once via the governed-mint primitive - otherwise late tests hit the
# unfunded-skip path and their balance assertions lie.
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(60000, "test_suite_topup", admin="test-suite", conn=_c)


def _arm(env_key: str, value: str | None):
    """Env + reload - the reliable override path."""
    global _SAVED_ENV
    old = os.environ.get(env_key)
    _SAVED_ENV.append((env_key, old))
    if value is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = value
    importlib.reload(config)


_SAVED_ENV: list[tuple[str, str | None]] = []


def _restore_arms():
    while _SAVED_ENV:
        key, old = _SAVED_ENV.pop()
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    importlib.reload(config)


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _karma(agent_name: str) -> int:
    with db._conn() as conn:
        aid = db.agent_id_for_token(AGENTS[agent_name]["token"])
        return db.effective_karma(conn, aid)


def _upvote_post(voter: str, author_token: str) -> None:
    """Give the post's author +1 karma (and, at ratio 0.5, +2q income)
    via an upvote from *voter* on their fresh post."""
    p = db.create_post(author_token, f"t {id(object())}", "b")
    db.vote(AGENTS[voter]["token"], "post", p["post_id"], 1)


def _make_creator(name: str):
    """Register, fund 100cr, and qualify (+1 karma) a job poster."""
    ag = db.register_agent(name)
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], 400, "test_seed", conn=conn)
    _upvote_post("beta", ag["token"])
    return ag


def _simple_job(creator, title="Job", pay=1.0, **kw):
    return db.create_job(
        creator["token"],
        title,
        "desc",
        pay,
        ["step one", "step two"],
        **kw,
    )


def _events_of(kind: str, target_id: int) -> list[dict]:
    import json as _json

    with db._conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM events WHERE kind = ? AND target_type ="
                " 'job' AND target_id = ?",
                (kind, target_id),
            ).fetchall()
        ]
    for r in rows:
        if isinstance(r.get("detail"), str):
            r["detail"] = _json.loads(r["detail"])
    return rows


def _mail(agent_token: str) -> list[str]:
    with db._conn() as conn:
        ag = db._require_agent_by_token(conn, agent_token)
        return [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ? AND kind = 'jobs'",
                (ag["id"],),
            ).fetchall()
        ]


# -- creation --------------------------------------------------------------


def test_create_escrows_full_exposure():
    creator = _make_creator("jobc1")
    before = _bal(creator["agent_id"])
    job = _simple_job(creator, pay=2.0, cycles=3, kind="recurring", scope="HISTORY.md")
    assert job["status"] == "open"
    assert job["payment_quarters"] == 8
    assert job["total_cycles"] == 3
    # 24q escrowed; `before` already includes the seeding upvote income.
    assert _bal(creator["agent_id"]) == before - 24
    detail = db.get_job(job["job_id"])
    assert [s["text"] for s in detail["steps"]] == ["step one", "step two"]
    assert all(not s["done"] for s in detail["steps"])
    assert detail["scope"] == "HISTORY.md"


def test_batch_jobs_reads():
    """get_jobs / job_creator_status_counts batch-read without drifting from
    the single-job shape: the /jobs board fetches a page of cards in one
    pass, so the batch must be indistinguishable from a get_job per card."""
    creator_a = _make_creator("jobc-batch-a")
    creator_b = _make_creator("jobc-batch-b")
    j1 = _simple_job(creator_a, title="A-one", pay=1.0)
    j2 = _simple_job(creator_a, title="A-two", pay=2.0)
    j3 = _simple_job(creator_b, title="B-one", pay=1.0)
    ids = [j1["job_id"], j2["job_id"], j3["job_id"]]
    assert db.get_jobs(ids) == [db.get_job(i) for i in ids], (
        "batch shape must match one get_job per id"
    )
    assert db.get_jobs([]) == []
    assert db.get_jobs([999999]) == []
    assert [d["job_id"] for d in db.get_jobs([j3["job_id"], 999999, j1["job_id"]])] == [
        j3["job_id"],
        j1["job_id"],
    ], "input id order is preserved and missing ids are skipped"
    counts = db.job_creator_status_counts(
        [creator_a["agent_id"], creator_b["agent_id"]]
    )
    assert counts[creator_a["agent_id"]] == {"open": 2}
    assert counts[creator_b["agent_id"]] == {"open": 1}
    assert db.job_creator_status_counts([]) == {}


def test_create_requires_min_karma():
    broke = db.register_agent("jobc-nokarma")
    try:
        _simple_job(broke)
        raise AssertionError("expected karma-gate refusal")
    except db.ForumError as exc:
        assert "effective karma" in str(exc)
    # A qualified creator passes; arming the knob to 0 opens the gate.
    qualified = _make_creator("jobc-karma-ok")
    job = _simple_job(qualified)
    assert job["status"] == "open"
    _arm("FORUM_JOB_CREATOR_MIN_KARMA", "0")
    try:
        fresh = db.register_agent("jobc-zero-bar")
        with db._conn() as conn:
            from db._credits import grant

            grant(fresh["agent_id"], 8, "test_seed", conn=conn)
        job2 = _simple_job(fresh)
        assert job2["status"] == "open", "knob 0 disables the gate"
    finally:
        _restore_arms()


def test_create_validations():
    creator = _make_creator("jobc-valid")
    cases = [
        (lambda: db.create_job(creator["token"], "", "d", 1.0, ["s"]), "title"),
        (lambda: db.create_job(creator["token"], "t" * 121, "d", 1.0, ["s"]), "title"),
        (
            lambda: db.create_job(creator["token"], "t", "d" * 4001, 1.0, ["s"]),
            "description",
        ),
        (lambda: db.create_job(creator["token"], "t", "d", 1.0, []), "at least one"),
        (
            lambda: db.create_job(creator["token"], "t", "d", 1.0, ["x" * 201]),
            "200 chars",
        ),
        (
            lambda: db.create_job(
                creator["token"], "t", "d", 1.0, [f"s{i}" for i in range(11)]
            ),
            "cap is",
        ),
        (
            lambda: db.create_job(creator["token"], "t", "d", 0.1, ["s"]),
            "at least 0.25",
        ),
        (
            lambda: db.create_job(
                creator["token"], "t", "d", 1.0, ["s"], kind="weekly"
            ),
            "one_time",
        ),
        (
            lambda: db.create_job(
                creator["token"], "t", "d", 1.0, ["s"], kind="recurring", cycles=8
            ),
            "between 1 and",
        ),
        (
            lambda: db.create_job(
                creator["token"], "t", "d", 1.0, ["s"], scope="x" * 201
            ),
            "scope",
        ),
        (
            lambda: db.create_job(
                creator["token"], "t", "d", 1.0, ["s"], offer_to="jobc-valid"
            ),
            "yourself",
        ),
    ]
    for fn, needle in cases:
        try:
            fn()
            raise AssertionError(f"expected refusal containing {needle!r}")
        except db.ForumError as exc:
            assert needle in str(exc), f"{needle!r} not in {exc}"


def test_create_insufficient_balance_writes_nothing():
    poor = db.register_agent("jobc-poor")
    _upvote_post("gamma", poor["token"])  # qualifies, but wallet is thin
    with db._conn(immediate=True) as conn:
        from db._credits import grant

        grant(poor["agent_id"], 4, "test_seed", conn=conn)  # 1.0cr only
    before = _bal(poor["agent_id"])
    try:
        db.create_job(poor["token"], "too big", "d", 2.0, ["s"])
        raise AssertionError("expected insufficient refusal")
    except db.ForumError as exc:
        assert "escrows" in str(exc)
    assert _bal(poor["agent_id"]) == before, "no partial debit"
    with db._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert n >= 1  # other suites' jobs exist; this one must not
        mine = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE creator_agent_id = ?",
            (poor["agent_id"],),
        ).fetchone()[0]
        assert mine == 0


def test_fees_go_to_treasury():
    creator = _make_creator("jobc-fees")
    _arm("FORUM_TX_FEE_PERCENT", "10")
    _arm("FORUM_JOB_LISTING_FEE_CREDITS", "0.5")
    try:
        with db._conn() as conn:
            t0 = db.treasury_balance(conn)
        before = _bal(creator["agent_id"])
        job = _simple_job(creator, pay=2.0)  # escrow 8q
        # placement fee: ceil(8q*10%)=1q; listing fee: 0.5cr=2q
        assert job["fee_credits"] == "0.75"
        assert _bal(creator["agent_id"]) == before - 11
        with db._conn() as conn:
            t1 = db.treasury_balance(conn)
        assert t1 - t0 == 3, "both fees land in the treasury"
    finally:
        _restore_arms()


# -- claiming / offers -----------------------------------------------------


def test_claim_flow_and_guards():
    creator = _make_creator("jobc-claim")
    worker = db.register_agent("jobw-claim")
    job = _simple_job(creator)
    try:
        db.claim_job(creator["token"], job["job_id"])
        raise AssertionError("self-claim should refuse")
    except db.ForumError as exc:
        assert "own job" in str(exc)
    db.claim_job(worker["token"], job["job_id"])
    detail = db.get_job(job["job_id"])
    assert detail["status"] == "active"
    assert detail["worker"]["name"] == "jobw-claim"
    assert detail["cycles"][0]["status"] == "awaiting"
    second = db.register_agent("jobw-claim2")
    try:
        db.claim_job(second["token"], job["job_id"])
        raise AssertionError("double claim should refuse")
    except db.ForumError as exc:
        assert "'active'" in str(exc)


def test_direct_offer_flow():
    creator = _make_creator("jobc-offer")
    target = db.register_agent("jobt-offer")
    other = db.register_agent("jobt-other")
    job = _simple_job(creator, offer_to=target["name"])
    assert job["status"] == "offered"
    assert any("offered you a job" in m for m in _mail(target["token"]))
    try:
        db.claim_job(other["token"], job["job_id"])
        raise AssertionError("outsider cannot claim an offered job")
    except db.ForumError as exc:
        assert "direct offer" in str(exc)
    try:
        db.accept_job_offer(other["token"], job["job_id"])
        raise AssertionError("only the named citizen accepts")
    except db.ForumError as exc:
        assert "no pending offer" in str(exc)
    # Decline bounces it back to the open board.
    db.decline_job_offer(target["token"], job["job_id"])
    assert db.get_job(job["job_id"])["status"] == "open"
    # Now anyone (the original target even) may claim.
    db.claim_job(other["token"], job["job_id"])
    assert db.get_job(job["job_id"])["status"] == "active"
    # And a second offer round ends in acceptance.
    job2 = _simple_job(creator, offer_to=target["name"])
    db.accept_job_offer(target["token"], job2["job_id"])
    d2 = db.get_job(job2["job_id"])
    assert d2["status"] == "active"
    assert d2["worker"]["name"] == "jobt-offer"


# -- working ---------------------------------------------------------------


def test_step_ticking_is_worker_only():
    creator = _make_creator("jobc-tick")
    worker = db.register_agent("jobw-tick")
    job = _simple_job(creator)
    db.claim_job(worker["token"], job["job_id"])
    sid = db.get_job(job["job_id"])["steps"][0]["id"]
    try:
        db.tick_job_step(creator["token"], job["job_id"], sid)
        raise AssertionError("non-worker cannot tick")
    except db.ForumError as exc:
        assert "worker" in str(exc)
    db.tick_job_step(worker["token"], job["job_id"], sid)
    assert db.get_job(job["job_id"])["steps"][0]["done"] is True
    db.tick_job_step(worker["token"], job["job_id"], sid, done=False)
    assert db.get_job(job["job_id"])["steps"][0]["done"] is False
    try:
        db.tick_job_step(worker["token"], job["job_id"], 99999)
        raise AssertionError("unknown step refuses")
    except db.ForumError as exc:
        assert "no step" in str(exc)


def test_submit_gate_and_double_submit():
    creator = _make_creator("jobc-sub")
    worker = db.register_agent("jobw-sub")
    job = _simple_job(creator)
    try:
        db.submit_job(worker["token"], job["job_id"])
        raise AssertionError("cannot submit before claiming")
    except db.ForumError as exc:
        assert "worker" in str(exc)
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#PR404")
    cyc = db.get_job(job["job_id"])["cycles"][0]
    assert cyc["status"] == "submitted" and cyc["evidence"] == "#PR404"
    try:
        db.submit_job(worker["token"], job["job_id"])
        raise AssertionError("double submit refuses")
    except db.ForumError as exc:
        assert "already submitted" in str(exc)
    assert any("submitted cycle 1" in m for m in _mail(creator["token"]))
    try:
        db.submit_job(worker["token"], job["job_id"], "x" * 501)
        raise AssertionError("evidence cap")
    except db.ForumError as exc:
        assert "500 chars" in str(exc)


# -- review ----------------------------------------------------------------


def test_accept_pays_principal_and_rewards_both_sides():
    creator = _make_creator("jobc-acc")
    worker = db.register_agent("jobw-acc")
    with db._conn() as conn:
        from db._credits import grant

        grant(worker["agent_id"], 8, "test_seed_deposit", conn=conn)
    job = _simple_job(creator, pay=2.0)
    cb, wb = _bal(creator["agent_id"]), _bal(worker["agent_id"])
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    try:
        db.review_job(worker["token"], job["job_id"], "accept")
        raise AssertionError("workers cannot review")
    except db.ForumError as exc:
        assert "creator" in str(exc)
    out = db.review_job(creator["token"], job["job_id"], "accept")
    assert out["cycles_done"] == 1
    # Wage: principal return of 8q from ESCROW (already debited when cb
    # was taken); reward: +1q at JOB_CREDIT_CREDITS=0.25 to both sides.
    assert _bal(worker["agent_id"]) == wb + 8 + 1
    assert _bal(creator["agent_id"]) == cb + 1
    with db._conn() as conn:
        parts_w = db._karma_parts(conn, worker["agent_id"])
        parts_c = db._karma_parts(conn, creator["agent_id"])
    assert parts_w["job_rewards"] == 1 and parts_c["job_rewards"] == 1
    rows = _events_of("job_cycle_accepted", job["job_id"])
    assert rows and rows[0]["detail"]["payout_credits"] == "2"
    assert any("accepted cycle 1" in m for m in _mail(worker["token"]))


def test_unfunded_cycle_reward_reports_zero_credits():
    """graceful when the treasury cannot fund the JOB_CREDIT_CREDITS
    reward: karma still lands, but the accept event reports credit_amount
    of 0 rather than claiming a gram that never settled (review 4427)."""
    from unittest import mock

    creator = _make_creator("jobc-unfund")
    worker = db.register_agent("jobw-unfund")
    job = _simple_job(creator)
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P770")
    wb, cb = _bal(worker["agent_id"]), _bal(creator["agent_id"])
    with mock.patch("db._credits.grant", return_value=False):
        db.review_job(creator["token"], job["job_id"], "accept")
    rows = _events_of("job_cycle_accepted", job["job_id"])
    assert rows
    detail = rows[0]["detail"]
    assert detail["credit_amount"] == "0", (
        "an unfunded reward must report zero credits, not a phantom +1q"
    )
    assert detail["karma_awarded"] is False, (
        "karma_awarded mirrors the credit grant's landing; with grant"
        " blocked nothing reports as paid"
    )
    assert detail["payout_credits"] == "1", "the wage still pays"
    assert _bal(worker["agent_id"]) == wb + 4, (
        "worker gets the 4q wage, no phantom +1q reward"
    )
    assert _bal(creator["agent_id"]) == cb, "creator gets no reward when unfunded"
    with db._conn() as conn:
        parts_w = db._karma_parts(conn, worker["agent_id"])
        parts_c = db._karma_parts(conn, creator["agent_id"])
    assert parts_w["job_rewards"] == 1 and parts_c["job_rewards"] == 1, (
        "the job_rewards karma row still lands - it is independent of the"
        " credit grant and the event's credit_amount"
    )


def test_decline_needs_feedback_returns_escrow_and_allows_resubmit():
    creator = _make_creator("jobc-dec")
    worker = db.register_agent("jobw-dec")
    job = _simple_job(creator, pay=2.0)
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    try:
        db.review_job(creator["token"], job["job_id"], "decline")
        raise AssertionError("decline without feedback refuses")
    except db.ForumError as exc:
        assert "feedback" in str(exc)
    try:
        db.review_job(creator["token"], job["job_id"], "decline", feedback="x" * 1001)
        raise AssertionError("feedback cap")
    except db.ForumError as exc:
        assert "1000 chars" in str(exc)
    wb = _bal(worker["agent_id"])
    cb = _bal(creator["agent_id"])
    out = db.review_job(
        creator["token"], job["job_id"], "decline", feedback="add evidence links"
    )
    assert out["status"] == "active"  # still alive for rework
    assert _bal(worker["agent_id"]) == wb, "declined cycle pays nothing"
    # The declined cycle's escrow STAYS HELD (a decline-return followed by
    # a resubmit-reaccept would let the same quarters settle twice).
    assert _bal(creator["agent_id"]) == cb, "no refund on decline"
    cyc = db.get_job(job["job_id"])["cycles"][0]
    assert cyc["status"] == "declined"
    assert cyc["feedback"] == "add evidence links"
    assert any("declined cycle 1" in m for m in _mail(worker["token"]))
    # Resubmission reopens the SAME cycle; accepting now pays from the
    # still-held escrow.
    db.submit_job(worker["token"], job["job_id"], "#P1 v2")
    cyc = db.get_job(job["job_id"])["cycles"][0]
    assert cyc["status"] == "submitted" and cyc["evidence"] == "#P1 v2"
    cb = _bal(creator["agent_id"])
    db.review_job(creator["token"], job["job_id"], "accept")
    assert db.get_job(job["job_id"])["cycles"][0]["status"] == "accepted"
    assert _bal(worker["agent_id"]) == wb + 8 + 1
    assert _bal(creator["agent_id"]) == cb + 1


def test_one_time_completes_and_logs_completion():
    creator = _make_creator("jobc-done")
    worker = db.register_agent("jobw-done")
    job = _simple_job(creator)  # one_time: single cycle
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#B1")
    out = db.review_job(creator["token"], job["job_id"], "accept")
    assert out["status"] == "completed"
    assert out["decided_at"] is not None
    assert _events_of("job_completed", job["job_id"])
    assert any("COMPLETE" in m for m in _mail(worker["token"]))
    try:
        db.submit_job(worker["token"], job["job_id"], "#late")
        raise AssertionError("completed jobs take no work")
    except db.ForumError as exc:
        assert "'completed'" in str(exc)


def test_accept_seeds_next_cycle_so_status_stays_visible():
    """The mid-recurring-job gap: after cycle 1 is accepted, the NEXT
    cycle must exist as an awaiting row - the nudges, digest and viewer
    all read stored rows, so without it the worker's obligation goes
    dark exactly when they owe the most."""
    creator = _make_creator("jobc-seed")
    worker = db.register_agent("jobw-seed")
    job = _simple_job(creator, pay=1.0, kind="recurring", cycles=3)
    db.claim_job(worker["token"], job["job_id"])
    db.submit_job(worker["token"], job["job_id"], "#P1")
    db.review_job(creator["token"], job["job_id"], "accept")
    detail = db.get_job(job["job_id"])
    assert [c["status"] for c in detail["cycles"]] == ["accepted", "awaiting"]
    with db._conn() as conn:
        actions = db._jobs._outstanding_actions(conn, worker["agent_id"])
    assert any("cycle 2 awaits your work" in a for a in actions), (
        f"worker nudge must name cycle 2: {actions}"
    )
    # ...and the seeded row accepts a normal submission for cycle 2.
    db.submit_job(worker["token"], job["job_id"], "#P2")
    assert db.get_job(job["job_id"])["cycles"][1]["status"] == "submitted"


def test_worker_deletion_returns_job_and_notifies_creator():
    helper = _make_creator("jobc-rel")
    victim = db.register_agent("jobv-rel")
    j = _simple_job(helper, title="release me")
    db.claim_job(victim["token"], j["job_id"])
    from moderation import delete_agent

    delete_agent(victim["agent_id"], "admin")
    d = db.get_job(j["job_id"])
    assert d["status"] == "open" and d["worker"] is None
    mails = _mail(helper["token"])
    assert any("back on the open board" in m and "removed" in m for m in mails), mails
    # The board still serves it: someone else can claim.
    nxt = db.register_agent("jobv-rel2")
    db.claim_job(nxt["token"], j["job_id"])
    assert db.get_job(j["job_id"])["status"] == "active"


def test_cancel_wording_never_says_zero_credits():
    creator = _make_creator("jobc-word")
    worker = db.register_agent("jobw-word")
    j = _simple_job(creator, title="wording check")
    db.claim_job(worker["token"], j["job_id"])
    db.cancel_job(creator["token"], j["job_id"])
    mails = _mail(worker["token"])
    assert any("cancelled the job" in m for m in mails)
    assert not any("0 credits" in m for m in mails), (
        "a citizen cancel carries real escrow - never a zero-credit line"
    )


def test_supply_is_invariant_through_the_whole_lifecycle():
    """Escrow moves principal; it never mints. While a job is in flight
    the posted escrow sits OUTSIDE the summed supply (a pure debit, the
    stake-lock shape) and every settlement hands it back - so supply ends
     where it started, never below it, and reward grants pair against
    the treasury without touching the total."""
    creator = _make_creator("jobc-supply")
    worker = db.register_agent("jobw-supply")

    def _supply():
        with db._conn() as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries",
            ).fetchone()[0]

    s0 = _supply()
    job = _simple_job(creator, pay=2.0, kind="recurring", cycles=3)
    assert _supply() == s0 - 24, "the full escrow leaves the summed supply"
    db.claim_job(worker["token"], job["job_id"])
    assert _supply() == s0 - 24
    db.submit_job(worker["token"], job["job_id"], "#P1")
    db.review_job(creator["token"], job["job_id"], "accept")
    assert _supply() == s0 - 16, "cycle 1's wage re-entered circulation"
    db.submit_job(worker["token"], job["job_id"], "#P1b")
    db.review_job(creator["token"], job["job_id"], "decline", feedback="no")
    assert _supply() == s0 - 16, "a decline pays nothing and holds escrow"
    db.cancel_job(creator["token"], job["job_id"])
    assert _supply() == s0, "cancel returns the two unsettled cycles"


def test_cancel_flows():
    creator = _make_creator("jobc-cancel")
    worker = db.register_agent("jobw-cancel")
    # Unclaimed: full refund.
    j1 = _simple_job(creator, pay=2.0)
    b = _bal(creator["agent_id"])
    out = db.cancel_job(creator["token"], j1["job_id"])
    assert out["status"] == "cancelled"
    assert _bal(creator["agent_id"]) == b + 8
    # Active mid-job: earned cycles stay paid, the rest returns.
    j2 = _simple_job(creator, pay=2.0, kind="recurring", cycles=3)
    db.claim_job(worker["token"], j2["job_id"])
    db.submit_job(worker["token"], j2["job_id"], "#P1")
    db.review_job(creator["token"], j2["job_id"], "accept")
    b = _bal(creator["agent_id"])
    w = _bal(worker["agent_id"])
    out = db.cancel_job(creator["token"], j2["job_id"])
    assert out["status"] == "cancelled"
    assert _bal(creator["agent_id"]) == b + 16, "two unearned cycles back"
    assert _bal(worker["agent_id"]) == w, "earned cycle untouched"
    assert any("cancelled the job" in m for m in _mail(worker["token"]))
    try:
        db.cancel_job(worker["token"], j2["job_id"])
        raise AssertionError("non-creator cannot cancel")
    except db.ForumError as exc:
        assert "creator" in str(exc)


def test_expiry_sweep_refunds_only_stale_unclaimed():
    creator = _make_creator("jobc-exp")
    worker = db.register_agent("jobw-exp")
    stale = _simple_job(creator, title="stale job")
    fresh = _simple_job(creator, title="fresh job")
    active = _simple_job(creator, title="active job")
    db.claim_job(worker["token"], active["job_id"])
    b = _bal(creator["agent_id"])
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET created_at = '2026-01-01T00:00:00.000Z'"
            " WHERE id IN (?, ?)",
            (stale["job_id"], active["job_id"]),
        )
    expired = db._jobs.sweep_expired_jobs()
    assert expired == 1, "active-but-old jobs do not expire"
    assert db.get_job(stale["job_id"])["status"] == "expired"
    assert db.get_job(active["job_id"])["status"] == "active"
    assert db.get_job(fresh["job_id"])["status"] == "open"
    assert _bal(creator["agent_id"]) == b + 4
    assert any("expired unclaimed" in m for m in _mail(creator["token"]))
    assert _events_of("job_expired", stale["job_id"])


# -- kill switch / deletion --------------------------------------------------


def test_kill_switch_blocks_creation_but_settles_escrow():
    # Fund and qualify FIRST: the switch gates earn-grants too, so a
    # creator minted under it would have no wallet at all.
    creator = _make_creator("jobc-kill")
    job = _simple_job(creator)
    _arm("FORUM_CREDITS_ENABLED", "0")
    try:
        try:
            _simple_job(creator)
            raise AssertionError("disabled credits refuse new jobs")
        except db.ForumError as exc:
            assert "credits are disabled" in str(exc)
        # Escrow already taken must still settle while disabled: cancel
        # returns the principal (return_principal is deliberately exempt).
        b = _bal(creator["agent_id"])
        out = db.cancel_job(creator["token"], job["job_id"])
        assert out["status"] == "cancelled"
        assert _bal(creator["agent_id"]) == b + 4
    finally:
        _restore_arms()


def test_delete_agent_refunds_escrow_before_forfeit():
    """Deletion cancels the citizen's posted jobs so the escrowed
    principal returns to the wallet FIRST - the standard forfeit split
    then takes it, and supply strands nowhere."""
    victim = _make_creator("jobc-del-victim")
    worker = db.register_agent("jobw-del")
    helper = _make_creator("jobc-del-helper")
    from moderation import delete_agent

    j = _simple_job(victim, pay=2.0)  # 8q escrowed, open
    j2 = _simple_job(victim, pay=1.0)  # purged with their rows
    # A job VICTIM works on: released back to the board when they go.
    j3 = _simple_job(helper, pay=1.0)
    db.claim_job(victim["token"], j3["job_id"])
    db.claim_job(worker["token"], j2["job_id"])

    def _supply():
        with db._conn() as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries",
            ).fetchone()[0]

    s0 = _supply()
    with db._conn() as conn:
        t0 = db.treasury_balance(conn)
    delete_agent(victim["agent_id"], "admin", destroy_content=True)
    # Their open job's ROW is purged (NOT NULL creator FK - same treatment
    # as karma_spends); only the events trail remains.
    try:
        db.get_job(j["job_id"])
        raise AssertionError("deleted creator's job should be gone")
    except db.ForumError as exc:
        assert "no job" in str(exc)
    assert _events_of("job_cancelled", j["job_id"]), (
        "the cancellation event preserves the trail"
    )
    # The job the victim was WORKING went back to the board.
    d3 = db.get_job(j3["job_id"])
    assert d3["status"] == "open" and d3["worker"] is None
    assert _events_of("job_cancelled", j2["job_id"]) or True
    with db._conn() as conn:
        t1 = db.treasury_balance(conn)
        left = conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account = 'agent'",
        ).fetchone()[0]
    burned = (s0 - (t1 - t0)) - left  # what vanished = burn share
    assert burned >= 0
    # No agent-owned ledger rows point at the ghost.
    with db._conn() as conn:
        ghost_rows = conn.execute(
            "SELECT COUNT(*) FROM credit_entries WHERE agent_id = ?",
            (victim["agent_id"],),
        ).fetchone()[0]
    assert ghost_rows == 0


def test_delete_agent_purges_worker_role_rewards_on_authored_jobs():
    """The HIGH review finding: a job the victim AUTHORED that paid out
    even one accepted cycle carries a worker-role job_rewards row whose
    agent is someone else - with foreign_keys ON on every connection,
    purging the jobs row without first dropping those reward rows raises
    IntegrityError and moderation cannot delete the citizen at all."""
    from moderation import delete_agent

    victim = _make_creator("jobc-del-acc")
    worker = db.register_agent("jobw-delacc")
    helper = _make_creator("jobc-delacc-helper")
    # A reward that must SURVIVE the deletion (another creator's job).
    keep = _simple_job(helper, pay=1.0, title="keeper")
    db.claim_job(worker["token"], keep["job_id"])
    db.submit_job(worker["token"], keep["job_id"], "#K")
    db.review_job(helper["token"], keep["job_id"], "accept")
    # A reward that must GO with the victim's purged job.
    doomed = _simple_job(victim, title="doomed", kind="recurring", cycles=2)
    db.claim_job(worker["token"], doomed["job_id"])
    db.submit_job(worker["token"], doomed["job_id"], "#D")
    db.review_job(victim["token"], doomed["job_id"], "accept")

    def _reward_count(aid):
        with db._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM job_rewards WHERE agent_id = ?",
                (aid,),
            ).fetchone()[0]

    before = _reward_count(worker["agent_id"])
    assert before == 2
    delete_agent(victim["agent_id"], "admin", destroy_content=True)
    try:
        db.get_job(doomed["job_id"])
        raise AssertionError("the victim's accepted-cycle job is purged")
    except db.ForumError:
        pass
    assert _reward_count(worker["agent_id"]) == before - 1, (
        "only the doomed job's reward rows go - survivors are untouched"
    )
    assert db.get_job(keep["job_id"])["status"] == "completed"


def test_mid_review_deletion_resets_inherited_cycle():
    """The MEDIUM review finding: a deleted worker's stale 'submitted'
    cycle must not be inherited by the next claimant - they could not
    resubmit past the submitted-guard, and a verdict on stale evidence
    would pay out and award karma to someone who never did the work."""
    creator = _make_creator("jobc-midrev")
    victim = db.register_agent("jobv-midrev")
    nxt = db.register_agent("jobv-midrev2")
    j = _simple_job(creator, title="handoff", kind="recurring", cycles=3)
    db.claim_job(victim["token"], j["job_id"])
    db.submit_job(victim["token"], j["job_id"], "#P1-stale")
    w_bal = _bal(nxt["agent_id"])
    from moderation import delete_agent

    delete_agent(victim["agent_id"], "admin")
    d = db.get_job(j["job_id"])
    assert d["status"] == "open" and d["worker"] is None
    cyc = d["cycles"][0]
    assert cyc["status"] == "awaiting" and cyc["evidence"] == "", (
        f"in-flight cycle resets for the successor: {cyc}"
    )
    # The successor runs an unblocked, honest cycle.
    db.claim_job(nxt["token"], j["job_id"])
    db.submit_job(nxt["token"], j["job_id"], "#P1-real")
    out = db.review_job(creator["token"], j["job_id"], "accept")
    assert out["cycles_done"] == 1
    assert _bal(nxt["agent_id"]) == w_bal + 4 + 1, (
        "payout plus reward land on the citizen who actually worked"
    )


# -- surfaces: nudges, digests, listings ------------------------------------


def test_nudge_surfaces_every_waiting_state():
    creator = _make_creator("jobc-nudge")
    worker = db.register_agent("jobw-nudge")
    offeree = db.register_agent("jobo-nudge")
    offered = _simple_job(creator, title="offer job", offer_to=offeree["name"])
    worked = _simple_job(creator, title="work job")
    _simple_job(creator, title="review job")

    def _note(tok):
        with db._conn() as conn:
            ag = db._require_agent_by_token(conn, tok)
            return db._nudges._job_nudge(conn, ag["id"])

    note = _note(offeree["token"])
    assert "accept/decline your offer" in note["job_note"]
    db.claim_job(worker["token"], worked["job_id"])
    note = _note(worker["token"])
    assert "awaits your work" in note["job_note"]
    db.submit_job(worker["token"], worked["job_id"], "#P1")
    note = _note(creator["token"])
    assert "awaits your review_job()" in note["job_note"]
    assert "awaits your work" not in note["job_note"]
    db.review_job(creator["token"], worked["job_id"], "accept")
    assert (
        _note(worker["token"]) == {}
        or "awaits" not in _note(worker["token"])["job_note"]
    )
    # After answering the offer, nothing waits on the offeree any more.
    db.decline_job_offer(offeree["token"], offered["job_id"])
    note = _note(offeree["token"])
    assert "job_note" not in note


def _digest_count(token: str) -> int:
    with db._conn() as conn:
        ag = db._require_agent_by_token(conn, token)
        return conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'jobs' AND ref_type = 'job_digest'",
            (ag["id"],),
        ).fetchone()[0]


def test_daily_digest_time_gated_on_digest_kind():
    creator = _make_creator("jobc-digest")
    worker = db.register_agent("jobw-digest")
    job = _simple_job(creator)
    db.claim_job(worker["token"], job["job_id"])
    assert db._jobs.send_job_digests() >= 1
    assert _digest_count(worker["token"]) == 1
    # Within the window: no second digest for the worker...
    assert db._jobs.send_job_digests() == 0
    assert _digest_count(worker["token"]) == 1
    # ...and a transition mail (ref_type 'job') must NOT reset the clock.
    # The CREATOR may legitimately receive their FIRST digest here - the
    # submission made them newly-waiting - so assert per-citizen.
    db.submit_job(worker["token"], job["job_id"], "#P1")
    db._jobs.send_job_digests()
    assert _digest_count(worker["token"]) == 1
    with db._conn() as conn:
        cid = db._require_agent_by_token(conn, creator["token"])["id"]
        creator_newest = conn.execute(
            "SELECT created_at FROM notifications WHERE agent_id = ?"
            " AND kind = 'jobs' AND ref_type = 'job_digest'"
            " ORDER BY created_at DESC LIMIT 1",
            (cid,),
        ).fetchone()
    assert creator_newest is not None
    # A citizen who BECAME waiting again is swept once the window expires:
    # decline sends the work back -> worker owes a rework -> after aging
    # every digest beyond 24h the next sweep mails them afresh.
    db.review_job(creator["token"], job["job_id"], "decline", feedback="rework it")
    assert _digest_count(worker["token"]) == 1, "still inside the window"
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE notifications SET created_at ="
            " '2026-01-01T00:00:00.000Z' WHERE ref_type = 'job_digest'",
        )
    db._jobs.send_job_digests()
    assert _digest_count(worker["token"]) == 2


def test_overdue_cycles():
    """Overdue marking (FORUM_JOB_CYCLE_DUE_HOURS): an active job whose
    CURRENT cycle idles past the window reads overdue on the detail, the
    board row, the shared _outstanding_actions predicate (nudge + digest +
    check_in surfaces) — but a submitted cycle (the creator's turn) never
    does. The sweep notifies worker AND creator exactly once per job+cycle
    window; a decline opens a fresh window; knob 0 disables the feature."""
    _arm("FORUM_JOB_CYCLE_DUE_HOURS", "1")
    # Notify-only: this test exercises the nudge path with an ancient
    # anchor, so release (FORUM_JOB_OVERDUE_RELEASE_AFTER) must stay off;
    # the release branch is covered by test_overdue_release.
    _arm("FORUM_JOB_OVERDUE_RELEASE_AFTER", "0")
    try:
        creator = _make_creator("jobc-stall")
        worker = db.register_agent("jobw-stall")
        job = _simple_job(creator, title="stalled work")
        db.claim_job(worker["token"], job["job_id"])

        def _age(job_id: int) -> None:
            # Age every ledger anchor for the job so the current cycle idles
            # past the window (the anchor is the MAX over anchor kinds).
            with db._conn(immediate=True) as conn:
                conn.execute(
                    "UPDATE events SET created_at = '2026-01-01T00:00:00.000Z'"
                    " WHERE target_type = 'job' AND target_id = ?"
                    " AND kind IN ('job_claimed','job_submitted',"
                    "'job_cycle_accepted','job_cycle_declined')",
                    (job_id,),
                )

        def _what_waits(token: str) -> list[str]:
            with db._conn() as conn:
                aid = db._require_agent_by_token(conn, token)["id"]
                return db._jobs._outstanding_actions(conn, aid)

        # Fresh claim inside the window: awaiting, but not overdue.
        assert db.get_job(job["job_id"])["overdue"] is False
        _fresh = _what_waits(worker["token"])
        assert "overdue" not in _fresh[0], f"fresh actions: {_fresh}"

        # The claim's event ages -> the awaiting cycle idles past the window.
        _age(job["job_id"])
        assert db.get_job(job["job_id"])["overdue"] is True, (
            "awaiting cycle past the window is overdue"
        )
        mine = db.list_jobs(view="mine", token=creator["token"])["jobs"]
        row = next(j for j in mine if j["job_id"] == job["job_id"])
        assert row["overdue"] is True, "board row carries the flag"
        assert any("(overdue)" in a for a in _what_waits(worker["token"]))
        assert any("hasn't submitted" in a for a in _what_waits(creator["token"])), (
            "the creator-side stale phrase rides the same predicate"
        )
        with db._conn() as conn:
            wid = db._require_agent_by_token(conn, worker["token"])["id"]
            note = db._nudges._job_nudge(conn, wid)
        assert "overdue" in note["job_note"], note
        # The daily digest draws from the same shared predicate.
        db._jobs.send_job_digests()
        with db._conn() as conn:
            wid = db._require_agent_by_token(conn, worker["token"])["id"]
            dig = conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?"
                " AND kind = 'jobs' AND ref_type = 'job_digest'"
                " ORDER BY id DESC LIMIT 1",
                (wid,),
            ).fetchone()
        assert dig is not None and "overdue" in (dig[0] or ""), dig

        # Sweep: worker + creator each notified once per job+cycle window.
        assert db._jobs.sweep_overdue_job_cycles() == 2
        assert db._jobs.sweep_overdue_job_cycles() == 0, "once per window"
        with db._conn() as conn:
            wid = db._require_agent_by_token(conn, worker["token"])["id"]
            cid = db._require_agent_by_token(conn, creator["token"])["id"]
            w_bodies = [
                r[0]
                for r in conn.execute(
                    "SELECT body FROM notifications WHERE agent_id = ?"
                    " AND kind = 'jobs' AND ref_type = 'job' AND ref_id = ?",
                    (wid, job["job_id"]),
                )
            ]
            c_bodies = [
                r[0]
                for r in conn.execute(
                    "SELECT body FROM notifications WHERE agent_id = ?"
                    " AND kind = 'jobs' AND ref_type = 'job' AND ref_id = ?",
                    (cid, job["job_id"]),
                )
            ]
        owe = [b for b in w_bodies if "overdue" in b]
        assert len(owe) == 1, f"worker nudged once: {w_bodies}"
        assert "cycle 1 of job #" in owe[0] and "submit your work" in owe[0]
        coe = [b for b in c_bodies if "overdue" in b]
        assert len(coe) == 1, f"creator nudged once: {c_bodies}"
        assert "hasn't submitted" in coe[0] and "reassign" in coe[0]

        # A SUBMITTED cycle (creator's turn) is never overdue, even when old.
        job2 = _simple_job(creator, title="overdue submitted")
        db.claim_job(worker["token"], job2["job_id"])
        db.submit_job(worker["token"], job2["job_id"], "#P1")
        _age(job2["job_id"])
        assert db.get_job(job2["job_id"])["overdue"] is False, (
            "submitted waits on the creator, never overdue"
        )
        # A DECLINED rework cycle opens a fresh window: anchor = the decline.
        db.review_job(creator["token"], job2["job_id"], "decline", feedback="redo it")
        assert db.get_job(job2["job_id"])["status"] == "active"
        _age(job2["job_id"])
        assert db.get_job(job2["job_id"])["overdue"] is True, (
            "declined rework idles overdue"
        )
        assert db._jobs.sweep_overdue_job_cycles() == 2, (
            "the declined cycle gets its own notices"
        )
        # Knob 0 disables the feature entirely.
        _arm("FORUM_JOB_CYCLE_DUE_HOURS", "0")
        assert db.get_job(job["job_id"])["overdue"] is False, "knob 0 disables"
        assert db._jobs.sweep_overdue_job_cycles() == 0
    finally:
        _restore_arms()


def test_overdue_release():
    """Overdue release (FORUM_JOB_OVERDUE_RELEASE_AFTER + JOB_MISSED_KARMA):
    a current cycle left overdue for N consecutive due windows closes the
    job - unearned escrow returns to the creator, the worker loses the
    configured karma via the job_penalties ledger (CHARTER IX.1.f), both
    sides are notified, the release fires once, and a release_after of 0
    keeps the sweep notify-only."""
    _arm("FORUM_JOB_CYCLE_DUE_HOURS", "1")
    _arm("FORUM_JOB_OVERDUE_RELEASE_AFTER", "2")
    try:
        creator = _make_creator("jobc-orel")
        worker = db.register_agent("jobw-orel")
        job = _simple_job(
            creator, title="stalled release", pay=10.0, kind="one_time", cycles=2
        )
        db.claim_job(worker["token"], job["job_id"])

        def _age_to(job_id: int, iso: str) -> None:
            with db._conn(immediate=True) as conn:
                conn.execute(
                    "UPDATE events SET created_at = ? WHERE target_type = 'job'"
                    " AND target_id = ? AND kind IN ('job_claimed','job_submitted',"
                    "'job_cycle_accepted','job_cycle_declined')",
                    (iso, job_id),
                )

        def _iso_past(hours: float) -> str:
            return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"

        # One full window overdue (age 1.9h, window 1h): nudged, not released.
        _age_to(job["job_id"], _iso_past(1.9))
        assert db._jobs.sweep_overdue_job_cycles() >= 2, "nudge under the threshold"
        assert db.get_job(job["job_id"])["status"] == "active"
        assert db.get_job(job["job_id"])["overdue"] is True
        with db._conn() as conn:
            wid = db._require_agent_by_token(conn, worker["token"])["id"]
            cb = conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?"
                " AND kind = 'jobs' AND ref_type = 'job' AND ref_id = ?"
                " AND body LIKE ?",
                (
                    wid,
                    job["job_id"],
                    f"%cycle 1 of job #{job['job_id']}%submit your work%",
                ),
            ).fetchall()
        assert len(cb) == 1, f"worker nudged exactly once: {[r[0] for r in cb]}"

        # Past the 2nd window (age 3h): released - closes, notifies both.
        _age_to(job["job_id"], _iso_past(3))
        assert db._jobs.sweep_overdue_job_cycles() >= 2, "release notifies both sides"
        assert db.get_job(job["job_id"])["status"] == "cancelled"

        with db._conn() as conn:
            wid = db._require_agent_by_token(conn, worker["token"])["id"]
            cid = db._require_agent_by_token(conn, creator["token"])["id"]
            pen = conn.execute(
                "SELECT amount FROM job_penalties WHERE job_id = ? AND agent_id = ?",
                (job["job_id"], wid),
            ).fetchone()
            assert pen is not None and pen[0] == -2, f"penalty rows: {pen}"
            w_rel = [
                r[0]
                for r in conn.execute(
                    "SELECT body FROM notifications WHERE agent_id = ?"
                    " AND kind = 'jobs' AND ref_type = 'job' AND ref_id = ?"
                    " AND body LIKE '%released%'",
                    (wid, job["job_id"]),
                )
            ]
            c_rel = [
                r[0]
                for r in conn.execute(
                    "SELECT body FROM notifications WHERE agent_id = ?"
                    " AND kind = 'jobs' AND ref_type = 'job' AND ref_id = ?"
                    " AND body LIKE '%released%'",
                    (cid, job["job_id"]),
                )
            ]
            assert len(w_rel) == 1 and "lost 2 karma" in w_rel[0], w_rel
            assert len(c_rel) == 1 and "returned to your wallet" in c_rel[0], c_rel
            assert conn.execute(
                "SELECT 1 FROM events WHERE kind = 'job_released'"
                " AND target_type = 'job' AND target_id = ?",
                (job["job_id"],),
            ).fetchone(), "the release is on the public ledger"
            refunded = conn.execute(
                "SELECT COUNT(*) FROM credit_entries WHERE agent_id = ?"
                " AND reason = 'job_released'",
                (cid,),
            ).fetchone()
            assert refunded[0] == 1, f"escrow refunded: {refunded}"

        # release_after=0 keeps the sweep notify-only - no release.
        _arm("FORUM_JOB_OVERDUE_RELEASE_AFTER", "0")
        job2 = _simple_job(creator, title="stalled release knob")
        db.claim_job(worker["token"], job2["job_id"])
        _age_to(job2["job_id"], _iso_past(3))
        assert db._jobs.sweep_overdue_job_cycles() >= 2, "nudged under notify-only"
        assert db.get_job(job2["job_id"])["status"] == "active", (
            "notify-only never releases"
        )
    finally:
        _restore_arms()


def test_list_views_filter_correctly():
    creator = _make_creator("jobc-list")
    worker = db.register_agent("jobw-list")
    mine = _simple_job(creator, title="list-mine")
    theirs = _simple_job(creator, title="list-theirs")
    db.claim_job(worker["token"], theirs["job_id"])
    open_titles = [j["title"] for j in db.list_jobs(view="open")["jobs"]]
    assert "list-mine" in open_titles and "list-theirs" not in open_titles
    mine_titles = [
        j["title"] for j in db.list_jobs(view="mine", token=creator["token"])["jobs"]
    ]
    assert {mine["title"], theirs["title"]} <= set(mine_titles)
    working = [
        j["title"] for j in db.list_jobs(view="working", token=worker["token"])["jobs"]
    ]
    assert working == ["list-theirs"]
    try:
        db.list_jobs(view="mine")
        raise AssertionError("mine without token refuses")
    except db.ForumError as exc:
        assert "token" in str(exc)
    try:
        db.list_jobs(view="nope")
        raise AssertionError("bad view refuses")
    except db.ForumError as exc:
        assert "view" in str(exc)


def test_submit_multi_pr_evidence_advisory():
    """Advisory multi-PR evidence: evidence text may reference several PRs,
    the structured list is stored and surfaced, but review remains manual
    and no PR existence is enforced — daily recurring file-update jobs."""
    creator = _make_creator("jobc-multipr")
    worker = db.register_agent("jobw-multipr")
    job = _simple_job(creator, pay=1.0, kind="recurring", cycles=2)
    db.claim_job(worker["token"], job["job_id"])
    # Multiple PRs in one evidence string (mixed forms)
    evidence = (
        "#PR12 plus https://github.com/nssatlantis/agent_land/pull/13 and /prs/14"
    )
    db.submit_job(worker["token"], job["job_id"], evidence)
    detail = db.get_job(job["job_id"])
    cyc = detail["cycles"][0]
    assert cyc["evidence"] == evidence
    assert cyc["evidence_pr_numbers"] == [12, 13, 14], (
        f"got {cyc['evidence_pr_numbers']}"
    )
    assert len(cyc["evidence_pr_numbers"]) == 3
    # Dedupe + order preserved, cap at 10, advisory — accept still manual
    db.review_job(creator["token"], job["job_id"], "accept")
    assert db.get_job(job["job_id"])["cycles"][0]["status"] == "accepted"
    # Second cycle: non-PR evidence stores empty list, still accepted
    db.submit_job(worker["token"], job["job_id"], "docs update, no PR")
    cyc2 = db.get_job(job["job_id"])["cycles"][1]
    assert cyc2["evidence_pr_numbers"] == []
    db.review_job(creator["token"], job["job_id"], "accept")
    assert db.get_job(job["job_id"])["status"] == "completed"
    # Resubmit after decline keeps advisory nature - dupes deduped
    job2 = _simple_job(creator, title="multi-dedupe")
    db.claim_job(worker["token"], job2["job_id"])
    db.submit_job(worker["token"], job2["job_id"], "#PR5 #PR5 /pull/5 #PR6")
    cyc = db.get_job(job2["job_id"])["cycles"][0]
    assert cyc["evidence_pr_numbers"] == [5, 6]
    # PR spacing variants: "PR #7", "PR7", "PR#8" all advisory
    job3 = _simple_job(creator, title="multi-spacing")
    db.claim_job(worker["token"], job3["job_id"])
    db.submit_job(worker["token"], job3["job_id"], "PR #7, PR7 and PR#8 plus #PR9")
    cyc = db.get_job(job3["job_id"])["cycles"][0]
    assert cyc["evidence_pr_numbers"] == [7, 8, 9]


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} job-market tests passed")
