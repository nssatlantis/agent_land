"""Citizen-store UTC day-pass coverage."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_store_day_passes_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, setup  # noqa: E402, I001
import events  # noqa: E402, I001
import db._credits as credits  # noqa: E402, I001

db.init_db()
AGENTS, BASE_POST = setup()
_SEQ = [0]


def _new_agent(prefix):
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id, units=2000):
    with db._conn() as conn:
        credits.grant(
            agent_id,
            units,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=conn,
        )


def _balance(agent_id):
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _log(agent, kind):
    events.log_event(
        kind,
        actor_agent_id=agent["agent_id"],
        actor_name=agent["name"],
        detail={"checks": "tests", "ok": True},
    )


def _karmaed(prefix):
    agent = _new_agent(prefix)
    post = db.create_post(agent["token"], f"{prefix} post", "body")
    db.vote(AGENTS["alpha"]["token"], "post", post["post_id"], 1)
    return agent


def test_comment_burst_purchase_shared_pool_and_expiry():
    buyer = _karmaed("burst-comment-buyer")
    reporter = _karmaed("burst-comment-reporter")
    _fund(buyer["agent_id"])
    old_cap = config.COMMENT_DAILY_CAP
    config.COMMENT_DAILY_CAP = 1
    try:
        catalog = db.get_store_catalog(buyer["token"])
        item = next(row for row in catalog["items"] if row["key"] == "comment_burst")
        assert item["price"] == config.STORE_COMMENT_BURST_PRICE
        assert item["bonus_units"] == config.STORE_COMMENT_BURST_BONUS
        before = _balance(buyer["agent_id"])
        bought = db.buy_store_item(buyer["token"], "comment_burst")
        after = _balance(buyer["agent_id"])
        assert bought["item"] == "comment_burst"
        assert after == before - 30
        assert db.effective_comment_cap(buyer["agent_id"]) == 4
        err = expect_error(db.buy_store_item, buyer["token"], "comment_burst")
        assert "already purchased" in err
        db.create_comment(buyer["token"], BASE_POST, "burst comment")
        report = db.file_bug_report(reporter["token"], "burst remark", "body", None)
        db.remark_bug_report(buyer["token"], report["id"], "burst remark", kind="repro")
        usage = db.my_profile(buyer["token"])["daily_usage"]["comments"]
        assert usage == {"used": 2, "cap": 4, "remaining": 2}
        with db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE store_day_passes SET day_key = '2000-01-01'"
                " WHERE agent_id = ? AND item = 'comment_burst'",
                (buyer["agent_id"],),
            )
        assert db.effective_comment_cap(buyer["agent_id"]) == 1
    finally:
        config.COMMENT_DAILY_CAP = old_cap


def test_ci_burst_shared_credits_and_reservations():
    from server.ci_runner._runs import _gate

    buyer = _new_agent("burst-ci-buyer")
    _fund(buyer["agent_id"])
    old_cap = config.CI_RUN_DAILY_CAP
    old_cooldown = config.CI_RUN_COOLDOWN_SECONDS
    config.CI_RUN_DAILY_CAP = 1
    config.CI_RUN_COOLDOWN_SECONDS = 0
    try:
        _log(buyer, events.EVT_CI_LOCAL_RUN)
        bought = db.buy_store_item(buyer["token"], "ci_burst")
        assert bought["credits_remaining"] == config.STORE_CI_BURST_CREDITS
        assert db.ci_burst_remaining(buyer["agent_id"]) == 3
        usage = db.ci_usage_for(buyer["agent_id"])
        assert usage["ci_local_run"]["burst_remaining"] == 3
        assert usage["ci_format_run"]["burst_remaining"] == 0
        _gate("ci_format_run", buyer["agent_id"], run_id="f" * 32)
        assert db.ci_burst_remaining(buyer["agent_id"]) == 3
        _gate("ci_local_run", buyer["agent_id"], run_id="a" * 32)
        assert db.ci_burst_remaining(buyer["agent_id"]) == 2
        assert db.mark_ci_burst_started("a" * 32)
        assert db.complete_ci_burst("a" * 32, error="0")
        _log(buyer, events.EVT_CI_BRANCH_RUN)
        _gate("ci_branch_run", buyer["agent_id"], run_id="b" * 32)
        assert db.ci_burst_remaining(buyer["agent_id"]) == 1
        _gate("ci_local_run", buyer["agent_id"], run_id="c" * 32)
        assert db.ci_burst_remaining(buyer["agent_id"]) == 0
        err = expect_error(
            _gate,
            "ci_local_run",
            buyer["agent_id"],
            run_id="d" * 32,
        )
        assert "daily CI run cap reached" in err
        assert (
            db.my_profile(buyer["token"])["ci_usage"]
            == db.check_in(buyer["token"])["ci_usage"]
        )
        assert (
            db.whoami(buyer["token"])["ci_usage"]
            == db.my_profile(buyer["token"])["ci_usage"]
        )
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
        config.CI_RUN_COOLDOWN_SECONDS = old_cooldown


def test_ci_burst_release_and_base_cap_zero():
    buyer = _new_agent("burst-ci-release")
    _fund(buyer["agent_id"])
    db.buy_store_item(buyer["token"], "ci_burst")
    run_id = "e" * 32
    assert db.reserve_ci_burst(buyer["agent_id"], "ci_local_run", run_id)
    assert db.ci_burst_remaining(buyer["agent_id"]) == 2
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE ci_burst_reservations SET created_at = '2000-01-01T00:00:00.000Z'"
            " WHERE run_id = ?",
            (run_id,),
        )
    assert db.ci_burst_remaining(buyer["agent_id"]) == 3
    old_cap = config.CI_RUN_DAILY_CAP
    config.CI_RUN_DAILY_CAP = 0
    try:
        from server.ci_runner._runs import _gate

        _gate("ci_local_run", buyer["agent_id"], run_id="f" * 32)
        assert db.ci_burst_remaining(buyer["agent_id"]) == 3
    finally:
        config.CI_RUN_DAILY_CAP = old_cap


def test_schema_and_stats_surface():
    with db._conn() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert {"store_day_passes", "ci_burst_reservations"} <= tables
    assert {
        "idx_store_day_passes_active",
        "idx_ci_burst_reservations_active",
    } <= indexes
    rows = {row["reason"]: row for row in db.store_stats()["items"]}
    assert rows["store_comment_burst"]["key"] == "comment_burst"
    assert rows["store_ci_burst"]["key"] == "ci_burst"


if __name__ == "__main__":
    for fn in (
        test_comment_burst_purchase_shared_pool_and_expiry,
        test_ci_burst_shared_credits_and_reservations,
        test_ci_burst_release_and_base_cap_zero,
        test_schema_and_stats_surface,
    ):
        fn()
        print(f"PASS {fn.__name__}")
