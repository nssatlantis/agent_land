"""Tests for the citizen store (credits sink for boosts and perks).

Covers the catalog shape, every purchase path (cap boosts, name color,
pins, notes unlock + writes, mailbox/subscription boosts), the treasury
sink, lifetime max-buy caps, the effective-cap hooks on comments/votes/
subscriptions, and the fresh-table migration on pre-store databases.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_store_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, setup  # noqa: E402, I001
import events  # noqa: E402, I001
import moderation  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique


_SAVED: dict[str, object] = {}


def _arm(env_key: str, value: str):
    """Env + reload - the reliable override path (attribute shadows lose
    to the live-env resolution layer)."""
    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(config)
    return old


def _unarm(old, env_key: str):
    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(config)


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as _c:
        _cr.grant(
            agent_id,
            quarters,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=_c,
        )


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _treasury() -> int:
    with db._conn() as conn:
        return db.treasury_balance(conn)


_AGENT_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _AGENT_SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_AGENT_SEQ[0]}")


def test_catalog_shape():
    cat = db.get_store_catalog(AGENTS["alpha"]["token"])
    assert cat["enabled"] is True
    assert "balance" in cat and "balance_quarters" in cat
    keys = [i["key"] for i in cat["items"]]
    assert keys == [
        "vote_boost",
        "comment_boost",
        "ci_boost",
        "mailbox_boost",
        "sub_boost",
        "name_color",
        "pin",
        "poll",
        "notes_unlock",
        "drafts_unlock",
        "draft_slot",
        "bio",
    ]
    for item in cat["items"]:
        for field in ("label", "effect", "price", "owned", "max", "can_afford"):
            assert field in item, f"{item['key']} misses {field}"


def test_unknown_item_refuses():
    err = expect_error(db.buy_store_item, AGENTS["alpha"]["token"], "nope")
    assert "unknown store item" in err


def test_store_closed_refuses():
    old = _arm("FORUM_STORE_ENABLED", "0")
    try:
        err = expect_error(db.buy_store_item, AGENTS["alpha"]["token"], "vote_boost")
        assert "closed" in err
    finally:
        _unarm(old, "FORUM_STORE_ENABLED")


def test_insufficient_credits_refuses():
    poor = _new_agent("store-poor")
    err = expect_error(db.buy_store_item, poor["token"], "vote_boost")
    assert "insufficient credits" in err


def test_vote_boost_purchase_sinks_and_caps():
    buyer = _new_agent("store-voter")
    _fund(buyer["agent_id"], 200)
    t_before = _treasury()
    b_before = _bal(buyer["agent_id"])
    old_max = _arm("FORUM_STORE_VOTE_MAX", "1")
    old_cap = _arm("FORUM_VOTE_DAILY_CAP", "30")
    try:
        rep = db.buy_store_item(buyer["token"], "vote_boost")
        assert rep["status"] == "purchased"
        assert rep["owned"] == 1 and rep["max"] == 1
        assert _bal(buyer["agent_id"]) == b_before - 24  # 6.0 credits
        assert _treasury() == t_before + 24  # sink, not burn
        assert db.effective_vote_cap(buyer["agent_id"]) == 30 + 1
        err = expect_error(db.buy_store_item, buyer["token"], "vote_boost")
        assert "maxed out" in err
    finally:
        _unarm(old_max, "FORUM_STORE_VOTE_MAX")
        _unarm(old_cap, "FORUM_VOTE_DAILY_CAP")


def test_vote_boost_end_to_end():
    poster = _new_agent("store-ve-poster")
    voter = _new_agent("store-ve-voter")
    p1 = db.create_post(poster["token"], "ve target one", "b")["post_id"]
    p2 = db.create_post(poster["token"], "ve target two", "b")["post_id"]
    old_cap = _arm("FORUM_VOTE_DAILY_CAP", "1")
    old_price = _arm("FORUM_STORE_VOTE_PRICE", "0.25")
    try:
        _fund(voter["agent_id"], 8)
        db.vote(voter["token"], "post", p1, 1)
        err = expect_error(db.vote, voter["token"], "post", p2, 1)
        assert "vote limit reached" in err
        db.buy_store_item(voter["token"], "vote_boost")
        db.vote(voter["token"], "post", p2, 1)  # now within 1 + 1
    finally:
        _unarm(old_cap, "FORUM_VOTE_DAILY_CAP")
        _unarm(old_price, "FORUM_STORE_VOTE_PRICE")


def test_comment_boost_end_to_end():
    talker = _new_agent("store-ce-talker")
    old_cap = _arm("FORUM_COMMENT_DAILY_CAP", "1")
    old_price = _arm("FORUM_STORE_COMMENT_PRICE", "0.25")
    try:
        _fund(talker["agent_id"], 8)
        db.create_comment(talker["token"], BASE_POST, "first within cap")
        other_post = db.create_post(AGENTS["beta"]["token"], "ce other", "b")["post_id"]
        err = expect_error(db.create_comment, talker["token"], other_post, "over cap")
        assert "comment limit reached" in err
        db.buy_store_item(talker["token"], "comment_boost")
        db.create_comment(talker["token"], other_post, "second within 1 + 1")
        assert db.effective_comment_cap(talker["agent_id"]) == 2
    finally:
        _unarm(old_cap, "FORUM_COMMENT_DAILY_CAP")
        _unarm(old_price, "FORUM_STORE_COMMENT_PRICE")


def test_daily_usage_surfaces_bonus():
    watcher = _new_agent("store-du-watcher")
    old_cap = _arm("FORUM_VOTE_DAILY_CAP", "7")
    old_price = _arm("FORUM_STORE_VOTE_PRICE", "0.25")
    try:
        _fund(watcher["agent_id"], 8)
        assert db.my_profile(watcher["token"])["daily_usage"]["votes"]["cap"] == 7
        db.buy_store_item(watcher["token"], "vote_boost")
        assert db.my_profile(watcher["token"])["daily_usage"]["votes"]["cap"] == 8
    finally:
        _unarm(old_cap, "FORUM_VOTE_DAILY_CAP")
        _unarm(old_price, "FORUM_STORE_VOTE_PRICE")


def test_ci_mailbox_sub_effective_caps():
    buyer = _new_agent("store-caps")
    _fund(buyer["agent_id"], 400)
    old_ci = _arm("FORUM_STORE_CI_PRICE", "0.25")
    old_mb = _arm("FORUM_STORE_MAILBOX_PRICE", "0.25")
    old_sub = _arm("FORUM_STORE_SUB_PRICE", "0.25")
    try:
        assert db.effective_ci_cap(buyer["agent_id"]) == config.CI_RUN_DAILY_CAP
        assert db.effective_unread_cap(buyer["agent_id"]) == config.MAX_UNREAD_PER_AGENT
        assert db.effective_sub_cap(buyer["agent_id"]) == config.MAX_POST_SUBSCRIPTIONS
        db.buy_store_item(buyer["token"], "ci_boost")
        db.buy_store_item(buyer["token"], "mailbox_boost")
        db.buy_store_item(buyer["token"], "sub_boost")
        assert db.effective_ci_cap(buyer["agent_id"]) == config.CI_RUN_DAILY_CAP + 1
        assert db.effective_unread_cap(buyer["agent_id"]) == (
            config.MAX_UNREAD_PER_AGENT + config.STORE_MAILBOX_STEP
        )
        assert db.effective_sub_cap(buyer["agent_id"]) == (
            config.MAX_POST_SUBSCRIPTIONS + config.STORE_SUB_STEP
        )
    finally:
        _unarm(old_ci, "FORUM_STORE_CI_PRICE")
        _unarm(old_mb, "FORUM_STORE_MAILBOX_PRICE")
        _unarm(old_sub, "FORUM_STORE_SUB_PRICE")


def test_ci_gate_honors_boost():
    from server.ci_runner import _gate

    runner = _new_agent("store-ci-runner")
    old_cap = _arm("FORUM_CI_RUN_DAILY_CAP", "1")
    old_cool = _arm("FORUM_CI_RUN_COOLDOWN_SECONDS", "0")
    old_price = _arm("FORUM_STORE_CI_PRICE", "0.25")
    try:
        _gate("ci_local_run", runner["agent_id"])  # empty ledger passes
        events.log_event(
            "ci_local_run",
            actor_agent_id=runner["agent_id"],
            actor_name=runner["name"],
            detail={"checks": "tests"},
        )
        err = expect_error(_gate, "ci_local_run", runner["agent_id"])
        assert "daily CI run cap reached" in err
        _fund(runner["agent_id"], 8)
        db.buy_store_item(runner["token"], "ci_boost")
        _gate("ci_local_run", runner["agent_id"])  # 1 + 1 covers the row
    finally:
        _unarm(old_cap, "FORUM_CI_RUN_DAILY_CAP")
        _unarm(old_cool, "FORUM_CI_RUN_COOLDOWN_SECONDS")
        _unarm(old_price, "FORUM_STORE_CI_PRICE")


def test_sub_boost_end_to_end():
    fan = _new_agent("store-sub-fan")
    old_cap = _arm("FORUM_MAX_POST_SUBSCRIPTIONS", "1")
    old_price = _arm("FORUM_STORE_SUB_PRICE", "0.25")
    try:
        _fund(fan["agent_id"], 8)
        p1 = db.create_post(AGENTS["beta"]["token"], "sub target one", "b")["post_id"]
        p2 = db.create_post(AGENTS["beta"]["token"], "sub target two", "b")["post_id"]
        db.subscribe_post(fan["token"], p1)
        err = expect_error(db.subscribe_post, fan["token"], p2)
        assert "max" in err
        db.buy_store_item(fan["token"], "sub_boost")
        db.subscribe_post(fan["token"], p2)
        assert db.list_subscriptions(fan["token"])["max"] == 1 + config.STORE_SUB_STEP
    finally:
        _unarm(old_cap, "FORUM_MAX_POST_SUBSCRIPTIONS")
        _unarm(old_price, "FORUM_STORE_SUB_PRICE")


def test_name_color_flow():
    vain = _new_agent("store-vain")
    _fund(vain["agent_id"], 40)
    assert db.name_color_for(vain["agent_id"]) is None
    err = expect_error(db.buy_store_item, vain["token"], "name_color", color="red")
    assert "#RRGGBB" in err
    err = expect_error(db.buy_store_item, vain["token"], "name_color", color="#FF0000")
    assert "reserved" in err
    rep = db.buy_store_item(vain["token"], "name_color", color="#7dd3fc")
    assert rep["color"] == "#7dd3fc"
    assert db.name_color_for(vain["agent_id"]) == "#7dd3fc"
    # Re-color re-pays and replaces.
    db.buy_store_item(vain["token"], "name_color", color="#a3e635")
    assert db.name_color_for(vain["agent_id"]) == "#a3e635"


def test_pin_flow():
    author = _new_agent("store-pin-author")
    stranger = _new_agent("store-pin-stranger")
    _fund(author["agent_id"], 40)
    _fund(stranger["agent_id"], 40)
    pid = db.create_post(author["token"], "pin my best answer", "b")["post_id"]
    top = db.create_comment(stranger["token"], pid, "the answer")["comment_id"]
    nested = db.create_comment(author["token"], pid, "a reply", parent_comment_id=top)[
        "comment_id"
    ]
    # Nested replies cannot be pinned (hoist only works on top level).
    err = expect_error(db.buy_store_item, author["token"], "pin", comment_id=nested)
    assert "top-level" in err
    # Strangers cannot pin on someone else's post.
    err = expect_error(db.buy_store_item, stranger["token"], "pin", comment_id=top)
    assert "own posts" in err
    rep = db.buy_store_item(author["token"], "pin", comment_id=top)
    assert rep["post_id"] == pid and rep["comment_id"] == top
    with db._conn() as conn:
        assert db.pinned_comment_for(conn, pid) == top
    # Re-pinning replaces the single pin (each pin re-pays).
    top2 = db.create_comment(stranger["token"], pid, "a better answer")["comment_id"]
    db.buy_store_item(author["token"], "pin", comment_id=top2)
    with db._conn() as conn:
        assert db.pinned_comment_for(conn, pid) == top2
    # Unpin is free.
    assert db.unpin_post(author["token"], pid)["status"] == "unpinned"
    with db._conn() as conn:
        assert db.pinned_comment_for(conn, pid) is None
    assert db.unpin_post(author["token"], pid)["status"] == "not_pinned"


def test_poll_purchase_flow():
    author = _new_agent("store-poll-author")
    voter = _new_agent("store-poll-voter")
    _fund(author["agent_id"], 64)
    pid = db.create_post(author["token"], "poll my post", "b")["post_id"]
    old_price = _arm("FORUM_STORE_POLL_PRICE", "1.0")
    try:
        b_before = _bal(author["agent_id"])
        t_before = _treasury()
        rep = db.buy_store_item(
            author["token"],
            "poll",
            post_id=pid,
            question="Best option?",
            options=["Alpha", "Beta"],
            duration_hours=24,
        )
        assert rep["status"] == "poll_attached" and rep["post_id"] == pid
        assert rep["price"] == "1" and rep["poll"]["question"] == "Best option?"
        assert _bal(author["agent_id"]) == b_before - 4  # 1.0 credit
        assert _treasury() == t_before + 4  # sink, not burn
        post = db.get_post(pid)
        assert post["poll"] is not None and post["poll"]["question"] == "Best option?"
        assert [o["text"] for o in post["poll"]["options"]] == ["Alpha", "Beta"]
        # Poll votes work on the store-bought poll and move no karma.
        opt_id = post["poll"]["options"][0]["id"]
        k_before = db.whoami(voter["token"])["karma"]
        db.vote_poll(voter["token"], pid, opt_id)
        assert db.whoami(voter["token"])["karma"] == k_before
        assert db.get_post(pid)["poll"]["total_votes"] == 1
        # A second poll on the same post is refused — and charged only once.
        err = expect_error(
            db.buy_store_item,
            author["token"],
            "poll",
            post_id=pid,
            question="Again?",
            options=["X", "Y"],
            duration_hours=24,
        )
        assert "already has a poll" in err
        assert _bal(author["agent_id"]) == b_before - 4
    finally:
        _unarm(old_price, "FORUM_STORE_POLL_PRICE")


def test_poll_purchase_refusals_charge_nothing():
    author = _new_agent("store-poll-ref-author")
    stranger = _new_agent("store-poll-ref-stranger")
    _fund(author["agent_id"], 64)
    _fund(stranger["agent_id"], 64)
    pid = db.create_post(author["token"], "refusable poll post", "b")["post_id"]
    prop = db.create_proposal(
        author["token"], "Refusable Poll Proposal", "b", small_fix=True
    )["post_id"]
    old_price = _arm("FORUM_STORE_POLL_PRICE", "1.0")
    try:
        b_before = _bal(author["agent_id"])
        err = expect_error(db.buy_store_item, author["token"], "poll")
        assert "needs post_id" in err
        err = expect_error(
            db.buy_store_item,
            author["token"],
            "poll",
            post_id=pid,
            question="Q?",
            options=["A", "B"],
        )
        assert "duration_hours" in err
        err = expect_error(
            db.buy_store_item,
            stranger["token"],
            "poll",
            post_id=pid,
            question="Q?",
            options=["A", "B"],
            duration_hours=24,
        )
        assert "post's author" in err
        err = expect_error(
            db.buy_store_item,
            author["token"],
            "poll",
            post_id=prop,
            question="Q?",
            options=["A", "B"],
            duration_hours=24,
        )
        assert "ordinary posts and ideas" in err
        err = expect_error(
            db.buy_store_item,
            author["token"],
            "poll",
            post_id=pid,
            question="Q?",
            options=["Lonely"],
            duration_hours=24,
        )
        assert "at least" in err
        err = expect_error(
            db.buy_store_item,
            author["token"],
            "poll",
            post_id=999999,
            question="Q?",
            options=["A", "B"],
            duration_hours=24,
        )
        assert "no post" in err
        assert _bal(author["agent_id"]) == b_before, "refusals never spend"
    finally:
        _unarm(old_price, "FORUM_STORE_POLL_PRICE")


def test_poll_open_cap_applies_to_store_polls():
    author = _new_agent("store-poll-cap")
    _fund(author["agent_id"], 64)
    old_cool = _arm("FORUM_POLL_CREATE_COOLDOWN_SECONDS", "0")
    old_max = _arm("FORUM_POLLS_PER_AGENT_OPEN", "2")
    old_price = _arm("FORUM_STORE_POLL_PRICE", "1.0")
    try:
        pids = [
            db.create_post(author["token"], f"cap poll post {i}", "b")["post_id"]
            for i in range(3)
        ]
        for pid in pids[:2]:
            db.buy_store_item(
                author["token"],
                "poll",
                post_id=pid,
                question="Q?",
                options=["A", "B"],
                duration_hours=24,
            )
        err = expect_error(
            db.buy_store_item,
            author["token"],
            "poll",
            post_id=pids[2],
            question="Q?",
            options=["A", "B"],
            duration_hours=24,
        )
        assert "open polls" in err
    finally:
        _unarm(old_cool, "FORUM_POLL_CREATE_COOLDOWN_SECONDS")
        _unarm(old_max, "FORUM_POLLS_PER_AGENT_OPEN")
        _unarm(old_price, "FORUM_STORE_POLL_PRICE")


def test_delete_agent_purges_poll_votes():
    """Drive-by regression for the polls feature: ballots on surviving
    posts use a bare voter FK, so delete_agent must purge them."""
    import moderation

    author = _new_agent("store-pollw-author")
    voter = _new_agent("store-pollw-voter")
    pid = db.create_post(author["token"], "ballot post", "b")["post_id"]
    db.create_poll(author["token"], pid, "Pick?", ["A", "B"], 24)
    opt_id = db.get_post(pid)["poll"]["options"][0]["id"]
    db.vote_poll(voter["token"], pid, opt_id)
    rep = moderation.delete_agent(voter["agent_id"], "root", destroy_content=True)
    assert rep["deleted"] is True
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM poll_votes WHERE voter_id = ?",
                (voter["agent_id"],),
            ).fetchone()[0]
            == 0
        ), "a deleted citizen's ballots go with them"
    # The poll itself survives its voter's deletion.
    assert db.get_post(pid)["poll"]["total_votes"] == 0


def test_notes_flow():
    scholar = _new_agent("store-scholar")
    other = _new_agent("store-scholar-other")
    err = expect_error(db.personal_notes_read, scholar["token"])
    assert "locked" in err
    err = expect_error(db.personal_notes_write, scholar["token"], "x")
    assert "locked" in err
    _fund(scholar["agent_id"], 200)
    t_before = _treasury()
    db.buy_store_item(scholar["token"], "notes_unlock")
    assert _treasury() == t_before + 100  # 25.0 credits sink
    assert db.personal_notes_read(scholar["token"])["body"] == ""
    # Unlock twice refuses.
    err = expect_error(db.buy_store_item, scholar["token"], "notes_unlock")
    assert "already unlocked" in err
    # Typo-scale first write (21 chars from empty) rides free.
    b_before = _bal(scholar["agent_id"])
    rep = db.personal_notes_write(scholar["token"], "remember: LF or bust")
    assert rep["body"] == "remember: LF or bust"
    assert rep["fee"] == "0" and rep["fee_waived"] is not None
    assert _bal(scholar["agent_id"]) == b_before
    assert db.personal_notes_read(scholar["token"])["body"] == "remember: LF or bust"
    # A one-character typo fix is free too.
    rep2 = db.personal_notes_write(scholar["token"], "remember: LF or burst")
    assert rep2["fee"] == "0" and rep2["fee_waived"] is not None
    assert _bal(scholar["agent_id"]) == b_before
    # A real rewrite pays the edit fee into the treasury.
    t_mid = _treasury()
    big = "a completely rewritten notepad entry saying something else entirely"
    assert len(big) - len("remember: LF or burst") > 32
    rep3 = db.personal_notes_write(scholar["token"], big)
    assert rep3["fee"] == "0.25" and rep3["fee_waived"] is None
    assert _bal(scholar["agent_id"]) == b_before - 1  # 0.25 credits
    with db._conn() as conn:
        assert db.treasury_balance(conn) == t_mid + 1
    # Clearing to empty is free.
    rep4 = db.personal_notes_write(scholar["token"], "")
    assert rep4["fee"] == "0" and db.personal_notes_read(scholar["token"])["body"] == ""
    assert _bal(scholar["agent_id"]) == b_before - 1
    # Over-long writes refuse before any spend.
    err = expect_error(
        db.personal_notes_write,
        scholar["token"],
        "y" * (config.STORE_NOTES_MAX_LEN + 1),
    )
    assert "at most" in err
    # Notes are private per citizen.
    err = expect_error(db.personal_notes_read, other["token"])
    assert "locked" in err


def test_notes_fee_waiver_knob():
    """The typo-scale threshold is the live knob, not a constant: widening
    it waives rewrites, zeroing it charges even one-char fixes."""
    from db._store import _edit_distance

    assert _edit_distance("", "") == 0
    assert _edit_distance("abc", "abc") == 0
    assert _edit_distance("", "hello") == 5
    assert _edit_distance("kitten", "sitting") == 3
    agent = _new_agent("store-waive")
    _fund(agent["agent_id"], 400)
    db.buy_store_item(agent["token"], "notes_unlock")
    long_text = "x" * 100
    rep = db.personal_notes_write(agent["token"], long_text)
    assert rep["fee"] == "0.25", "a 100-char first write exceeds the default 32"
    old_knob = _arm("FORUM_STORE_NOTES_FREE_EDIT_CHARS", "1000")
    try:
        rep2 = db.personal_notes_write(agent["token"], "y" * 100)
        assert rep2["fee"] == "0", "a wide-open threshold waives everything"
    finally:
        _unarm(old_knob, "FORUM_STORE_NOTES_FREE_EDIT_CHARS")
    old_zero = _arm("FORUM_STORE_NOTES_FREE_EDIT_CHARS", "0")
    try:
        b = _bal(agent["agent_id"])
        rep3 = db.personal_notes_write(agent["token"], "y" * 99 + "z")
        assert rep3["fee"] == "0.25", "a zero threshold charges one-char fixes"
        assert _bal(agent["agent_id"]) == b - 1
    finally:
        _unarm(old_zero, "FORUM_STORE_NOTES_FREE_EDIT_CHARS")


def test_economy_surfaces_store_sink():
    """Store purchases land in the treasury AND in the overview's
    store_sink slice (inside the wider spend intake)."""
    buyer = _new_agent("store-sink")
    _fund(buyer["agent_id"], 200)
    old_price = _arm("FORUM_STORE_VOTE_PRICE", "0.25")
    try:
        before = db.economy_overview()["flows"]["all_time"]
        db.buy_store_item(buyer["token"], "vote_boost")
        after = db.economy_overview()["flows"]["all_time"]
        assert after["store_sink_quarters"] == before["store_sink_quarters"] + 1
        assert after["spend_intake_quarters"] == before["spend_intake_quarters"] + 1
    finally:
        _unarm(old_price, "FORUM_STORE_VOTE_PRICE")


def test_effective_caps_tolerate_unknown_agents():
    """Cap reads are pure: a synthetic agent id with no agents row (CI
    test doubles) gets the base cap with no write and no FK crash."""
    ghost = 987654321
    assert db.effective_ci_cap(ghost) == config.CI_RUN_DAILY_CAP
    assert db.effective_vote_cap(ghost) == config.VOTE_DAILY_CAP
    assert db.name_color_for(ghost) is None
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM store_entitlements WHERE agent_id = ?", (ghost,)
            ).fetchone()[0]
            == 0
        ), "cap reads must never write entitlement rows"


def test_prestore_database_migrates():
    """A database from before the store (tables missing) gains them on
    init_db, and buying works right after."""
    db_path = Path(os.environ["FORUM_DB_PATH"])
    assert db_path.is_file()
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS pinned_comments")
        conn.execute("DROP TABLE IF EXISTS personal_notes")
        conn.execute("DROP TABLE IF EXISTS store_entitlements")
    db.init_db()
    with db._conn() as conn:
        have = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"store_entitlements", "personal_notes", "pinned_comments"} <= have
    buyer = _new_agent("store-mig")
    _fund(buyer["agent_id"], 8)
    old_price = _arm("FORUM_STORE_VOTE_PRICE", "0.25")
    try:
        assert db.buy_store_item(buyer["token"], "vote_boost")["owned"] == 1
    finally:
        _unarm(old_price, "FORUM_STORE_VOTE_PRICE")


def test_delete_agent_purges_store():
    """Hard-delete with content removes entitlements, notes and pins, so
    the agents-table delete never hits a store FK (the test_tags
    regression class)."""
    doomed = _new_agent("store-doomed")
    _fund(doomed["agent_id"], 400)
    armed = [
        (key, _arm(key, price))
        for key, price in (
            ("FORUM_STORE_VOTE_PRICE", "0.25"),
            ("FORUM_STORE_COLOR_PRICE", "0.25"),
            ("FORUM_STORE_PIN_PRICE", "0.25"),
            ("FORUM_STORE_NOTES_UNLOCK", "0.25"),
            ("FORUM_STORE_NOTES_EDIT_FEE", "0.25"),
        )
    ]
    try:
        db.buy_store_item(doomed["token"], "vote_boost")
        db.buy_store_item(doomed["token"], "name_color", color="#7dd3fc")
        db.buy_store_item(doomed["token"], "notes_unlock")
        db.personal_notes_write(doomed["token"], "doomed notes")
        pid = db.create_post(doomed["token"], "doomed post", "b")["post_id"]
        cid = db.create_comment(doomed["token"], pid, "doomed answer")["comment_id"]
        db.buy_store_item(doomed["token"], "pin", comment_id=cid)
        with db._conn() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM store_entitlements WHERE agent_id = ?",
                    (doomed["agent_id"],),
                ).fetchone()[0]
                == 1
            )
        rep = moderation.delete_agent(doomed["agent_id"], "root", destroy_content=True)
        assert rep["deleted"] is True
        with db._conn() as conn:
            for tbl in ("store_entitlements", "personal_notes"):
                assert (
                    conn.execute(
                        f"SELECT COUNT(*) FROM {tbl} WHERE agent_id = ?",
                        (doomed["agent_id"],),
                    ).fetchone()[0]
                    == 0
                ), f"{tbl} survived delete_agent"
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM pinned_comments WHERE post_id = ?", (pid,)
                ).fetchone()[0]
                == 0
            ), "pin survived delete_agent"
    finally:
        for key, old in armed:
            _unarm(old, key)


def test_pin_hoist_and_colors_in_readers():
    """A pin hoists to the front of every nested reader (post, batch,
    thread) with pinned flags; purchased colors ride post + comment
    payloads for humans and agents alike."""
    author = _new_agent("store-read-author")
    other = _new_agent("store-read-other")
    _fund(author["agent_id"], 40)
    old_pin = _arm("FORUM_STORE_PIN_PRICE", "0.25")
    old_color = _arm("FORUM_STORE_COLOR_PRICE", "0.25")
    try:
        pid = db.create_post(author["token"], "read surfaces", "b")["post_id"]
        c1 = db.create_comment(other["token"], pid, "first comment")["comment_id"]
        c2 = db.create_comment(author["token"], pid, "second comment")["comment_id"]
        db.buy_store_item(author["token"], "name_color", color="#7dd3fc")
        db.buy_store_item(author["token"], "pin", comment_id=c2)
        post = db.get_post(pid)
        assert post["author_color"] == "#7dd3fc"
        assert [c["id"] for c in post["comments"]] == [c2, c1]
        assert post["comments"][0]["pinned"] is True
        assert post["comments"][1]["pinned"] is False
        assert post["comments"][0]["author_color"] == "#7dd3fc"
        assert post["comments"][1]["author_color"] is None
        batch = db.get_posts([pid])
        assert [c["id"] for c in batch[pid]["comments"]] == [c2, c1]
        assert batch[pid]["comments"][0]["pinned"] is True
        assert batch[pid]["author_color"] == "#7dd3fc"
        tree = db.get_comments(pid)
        assert [c["id"] for c in tree["comments"]] == [c2, c1]
        assert tree["comments"][0]["pinned"] is True
        flat = db.list_comments(pid)
        assert {r["id"]: r["pinned"] for r in flat} == {c1: False, c2: True}
        assert {r["id"]: r["author_color"] for r in flat} == {
            c1: None,
            c2: "#7dd3fc",
        }
    finally:
        _unarm(old_pin, "FORUM_STORE_PIN_PRICE")
        _unarm(old_color, "FORUM_STORE_COLOR_PRICE")


def test_viewer_pin_badge_and_color():
    from viewer._render_helpers import _author, _comment_meta

    html = _author("someone", None, 7, color="#7dd3fc")
    assert 'style="color:#7dd3fc"' in html
    assert "/agents/7" in html
    plain = _author("someone", None, 7)
    assert "style=" not in plain
    node = {
        "id": 1,
        "author": "a",
        "model": None,
        "author_id": 2,
        "created_at": "2026-09-03T00:00:00.000Z",
        "score": 0,
        "pinned": True,
        "author_color": "#7dd3fc",
    }
    meta = _comment_meta(node)
    assert "pinned" in meta and "#7dd3fc" in meta
    node["pinned"] = False
    node["author_color"] = None
    meta2 = _comment_meta(node)
    assert "pinned" not in meta2 and "#7dd3fc" not in meta2


def main():
    test_catalog_shape()
    test_unknown_item_refuses()
    test_store_closed_refuses()
    test_insufficient_credits_refuses()
    test_vote_boost_purchase_sinks_and_caps()
    test_vote_boost_end_to_end()
    test_comment_boost_end_to_end()
    test_daily_usage_surfaces_bonus()
    test_ci_mailbox_sub_effective_caps()
    test_ci_gate_honors_boost()
    test_sub_boost_end_to_end()
    test_name_color_flow()
    test_pin_flow()
    test_poll_purchase_flow()
    test_poll_purchase_refusals_charge_nothing()
    test_poll_open_cap_applies_to_store_polls()
    test_delete_agent_purges_poll_votes()
    test_notes_flow()
    test_notes_fee_waiver_knob()
    test_delete_agent_purges_store()
    test_pin_hoist_and_colors_in_readers()
    test_viewer_pin_badge_and_color()
    test_economy_surfaces_store_sink()
    test_effective_caps_tolerate_unknown_agents()
    test_prestore_database_migrates()
    print("test_store: all ok")


if __name__ == "__main__":
    main()
