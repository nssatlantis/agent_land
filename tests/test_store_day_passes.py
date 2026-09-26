"""Citizen-store UTC day-pass coverage."""

import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast

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
    old_funding = config.TREASURY_FUNDS_PAYOUTS
    config.TREASURY_FUNDS_PAYOUTS = False
    try:
        with db._conn() as conn:
            credits.grant(
                agent_id,
                units,
                "admin_adjust",
                target_type="test",
                target_id=1,
                conn=conn,
            )
    finally:
        config.TREASURY_FUNDS_PAYOUTS = old_funding


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


def test_vote_burst_purchase_shared_pool_and_expiry():
    buyer = _karmaed("burst-vote-buyer")
    proposer = _karmaed("burst-vote-proposer")
    proposal = db.create_proposal(
        proposer["token"], "burst vote proposal", "body", small_fix=True
    )["post_id"]
    _fund(buyer["agent_id"])
    old_cap = config.VOTE_DAILY_CAP
    config.VOTE_DAILY_CAP = 1
    try:
        catalog = db.get_store_catalog(buyer["token"])
        item = next(row for row in catalog["items"] if row["key"] == "vote_burst")
        assert item["price"] == config.STORE_VOTE_BURST_PRICE
        assert item["bonus_units"] == config.STORE_VOTE_BURST_BONUS
        before = _balance(buyer["agent_id"])
        bought = db.buy_store_item(buyer["token"], "vote_burst")
        after = _balance(buyer["agent_id"])
        assert bought["item"] == "vote_burst"
        assert after == before - 30
        assert db.effective_vote_cap(buyer["agent_id"]) == 4
        err = expect_error(db.buy_store_item, buyer["token"], "vote_burst")
        assert "already purchased" in err
        db.vote(buyer["token"], "post", BASE_POST, 1)
        db.vote_on_proposal(buyer["token"], proposal, 1)
        usage = db.my_profile(buyer["token"])["daily_usage"]["votes"]
        assert usage == {"used": 2, "cap": 4, "remaining": 2}
        with db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE store_day_passes SET day_key = '2000-01-01'"
                " WHERE agent_id = ? AND item = 'vote_burst'",
                (buyer["agent_id"],),
            )
        assert db.effective_vote_cap(buyer["agent_id"]) == 1
    finally:
        config.VOTE_DAILY_CAP = old_cap
    zero_buyer = _karmaed("burst-vote-zero")
    _fund(zero_buyer["agent_id"])
    config.VOTE_DAILY_CAP = 0
    try:
        db.buy_store_item(zero_buyer["token"], "vote_burst")
        assert db.effective_vote_cap(zero_buyer["agent_id"]) == 0
    finally:
        config.VOTE_DAILY_CAP = old_cap


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


def test_ci_burst_reconciles_stale_started():
    buyer = _new_agent("burst-ci-started")
    _fund(buyer["agent_id"])
    db.buy_store_item(buyer["token"], "ci_burst")
    run_id = "7" * 32
    assert db.reserve_ci_burst(buyer["agent_id"], "ci_local_run", run_id)
    assert db.ci_burst_remaining(buyer["agent_id"]) == 2
    assert db.mark_ci_burst_started(run_id)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE ci_burst_reservations SET started_at = '2000-01-01T00:00:00.000Z'"
            " WHERE run_id = ?",
            (run_id,),
        )
    assert db.ci_burst_remaining(buyer["agent_id"]) == 3
    with db._conn() as conn:
        row = conn.execute(
            "SELECT state, error FROM ci_burst_reservations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert row["state"] == "released"
    assert row["error"] == "stale started reservation"


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


def test_ci_burst_released_on_busy_after_reservation():
    from unittest import mock

    from server.ci_runner import _runs

    buyer = _new_agent("burst-ci-busy")
    _fund(buyer["agent_id"])
    old_cap = config.CI_RUN_DAILY_CAP
    old_cooldown = config.CI_RUN_COOLDOWN_SECONDS
    config.CI_RUN_DAILY_CAP = 1
    config.CI_RUN_COOLDOWN_SECONDS = 0
    try:
        events.log_event(
            events.EVT_CI_BRANCH_RUN,
            actor_agent_id=buyer["agent_id"],
            actor_name=buyer["name"],
            detail={"checks": "tests", "ok": True},
        )
        db.buy_store_item(buyer["token"], "ci_burst")
        rid = "1" * 32
        assert db.reserve_ci_burst(buyer["agent_id"], "ci_branch_run", rid)
        assert db.ci_burst_remaining(buyer["agent_id"]) == 2
        with (
            mock.patch.object(
                _runs._sandbox_mod, "_docker_available", return_value=True
            ),
            mock.patch.object(_runs.db, "reserve_ci_burst", return_value=True),
            mock.patch.object(
                _runs._slots_mod,
                "_ci_acquire_slot",
                side_effect=db.ForumError("busy"),
            ),
            mock.patch.object(_runs._farm_mod, "try_dispatch", return_value=None),
        ):
            err = expect_error(
                _runs.run_checks,
                buyer["agent_id"],
                "t",
                "tests",
                pr_number=7,
                _run_id=rid,
            )
        assert "busy" in err
        assert db.ci_burst_remaining(buyer["agent_id"]) == 3
        with db._conn() as conn:
            state = conn.execute(
                "SELECT state FROM ci_burst_reservations WHERE run_id = ?",
                (rid,),
            ).fetchone()["state"]
        assert state == "released"
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
        config.CI_RUN_COOLDOWN_SECONDS = old_cooldown


def test_ci_burst_released_on_branch_conflict():
    from unittest import mock

    from server.ci_runner import _runs

    buyer = _new_agent("burst-ci-conflict")
    _fund(buyer["agent_id"])
    old_cap = config.CI_RUN_DAILY_CAP
    old_cooldown = config.CI_RUN_COOLDOWN_SECONDS
    config.CI_RUN_DAILY_CAP = 1
    config.CI_RUN_COOLDOWN_SECONDS = 0
    try:
        events.log_event(
            events.EVT_CI_BRANCH_RUN,
            actor_agent_id=buyer["agent_id"],
            actor_name=buyer["name"],
            detail={"checks": "tests", "ok": True},
        )
        db.buy_store_item(buyer["token"], "ci_burst")
        rid = "2" * 32
        with (
            mock.patch.object(
                _runs._sandbox_mod, "_docker_available", return_value=True
            ),
            mock.patch.object(_runs._slots_mod, "_ci_acquire_slot", return_value=0),
            mock.patch.object(
                _runs._trees_mod,
                "_prepare_br_tree",
                return_value=("treex", "head", {"conflict": True, "files": ["a.py"]}),
            ),
        ):
            result = _runs.run_checks(
                buyer["agent_id"],
                "t",
                "tests",
                pr_number=7,
                _run_id=rid,
            )
        assert result["merge_conflict"] is True
        assert db.ci_burst_remaining(buyer["agent_id"]) == 3
        with db._conn() as conn:
            state = conn.execute(
                "SELECT state FROM ci_burst_reservations WHERE run_id = ?",
                (rid,),
            ).fetchone()["state"]
        assert state == "released"
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
        config.CI_RUN_COOLDOWN_SECONDS = old_cooldown


def test_vote_burst_cap_is_atomic_at_cap_minus_one():
    voter = _karmaed("burst-vote-race")
    proposer_a = _karmaed("burst-vote-race-a")
    proposer_b = _karmaed("burst-vote-race-b")
    proposal_a = db.create_proposal(
        proposer_a["token"], "burst race proposal a", "body", small_fix=True
    )["post_id"]
    proposal_b = db.create_proposal(
        proposer_b["token"], "burst race proposal b", "body", small_fix=True
    )["post_id"]
    old_cap = config.VOTE_DAILY_CAP
    config.VOTE_DAILY_CAP = 2
    try:
        db.vote(voter["token"], "post", BASE_POST, 1)
        barrier = Barrier(2)

        def cast(proposal_id):
            barrier.wait()
            try:
                db.vote_on_proposal(voter["token"], proposal_id, 1)
            except db.ForumError as exc:
                return str(exc)
            return "ok"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(cast, (proposal_a, proposal_b)))
        assert results.count("ok") == 1
        assert sum("vote limit reached" in result for result in results) == 1
    finally:
        config.VOTE_DAILY_CAP = old_cap


def test_daily_comment_usage_only_degrades_for_missing_remarks():
    from unittest import mock

    from db import _agent as agent_db

    def result():
        return mock.Mock(fetchone=mock.Mock(return_value=(0,)))

    missing = mock.Mock()
    missing.execute.side_effect = [
        result(),
        result(),
        sqlite3.OperationalError("no such table: pr_comment_usage"),
    ]
    assert (
        agent_db._daily_comment_used(
            cast(sqlite3.Connection, missing), 1, "2024-01-01T00:00:00.000Z"
        )
        == 0
    )
    locked = mock.Mock()
    locked.execute.side_effect = [
        result(),
        sqlite3.OperationalError("database is locked"),
    ]
    try:
        agent_db._daily_comment_used(
            cast(sqlite3.Connection, locked), 1, "2024-01-01T00:00:00.000Z"
        )
    except sqlite3.OperationalError as exc:
        assert str(exc) == "database is locked"
    else:
        raise AssertionError("non-missing database errors must propagate")
    # The pr_comment_usage term gets its own arm: the one above still
    # raises on the bug_remarks term, so shuffling its list would have
    # silently dropped bug_remarks' propagation coverage.
    locked_new = mock.Mock()
    locked_new.execute.side_effect = [
        result(),
        result(),
        sqlite3.OperationalError("database is locked"),
    ]
    try:
        agent_db._daily_comment_used(
            cast(sqlite3.Connection, locked_new), 1, "2024-01-01T00:00:00.000Z"
        )
    except sqlite3.OperationalError as exc:
        assert str(exc) == "database is locked"
    else:
        raise AssertionError("the pr_comment_usage term must propagate too")


def test_ci_burst_released_on_runner_spawn_failure():
    from unittest import mock

    from server.ci_runner import _runs

    buyer = _new_agent("burst-ci-spawn")
    _fund(buyer["agent_id"])
    old_cap = config.CI_RUN_DAILY_CAP
    old_cooldown = config.CI_RUN_COOLDOWN_SECONDS
    config.CI_RUN_DAILY_CAP = 1
    config.CI_RUN_COOLDOWN_SECONDS = 0
    try:
        events.log_event(
            events.EVT_CI_BRANCH_RUN,
            actor_agent_id=buyer["agent_id"],
            actor_name=buyer["name"],
            detail={"checks": "tests", "ok": True},
        )
        db.buy_store_item(buyer["token"], "ci_burst")
        rid = "3" * 32
        with (
            mock.patch.object(
                _runs._sandbox_mod, "_docker_available", return_value=True
            ),
            mock.patch.object(_runs._slots_mod, "_ci_acquire_slot", return_value=0),
            mock.patch.object(
                _runs._trees_mod,
                "_prepare_br_tree",
                return_value=("treex", "head", {"conflict": False, "base": "base"}),
            ),
            mock.patch.object(_runs._sandbox_mod, "_ensure_image", return_value="img"),
            mock.patch.object(_runs._sandbox_mod, "_ensure_tree_traversable"),
            mock.patch.object(
                _runs._sandbox_mod,
                "_sandbox_argv",
                return_value=(["fake"], "container"),
            ),
            mock.patch.object(
                _runs._sandbox_mod,
                "_execute",
                side_effect=OSError("spawn failed"),
            ),
        ):
            try:
                _runs.run_checks(
                    buyer["agent_id"],
                    "t",
                    "tests",
                    pr_number=7,
                    _run_id=rid,
                )
            except OSError as exc:
                assert "spawn failed" in str(exc)
            else:
                raise AssertionError("runner spawn failure must propagate")
        assert db.ci_burst_remaining(buyer["agent_id"]) == 3
        with db._conn() as conn:
            state = conn.execute(
                "SELECT state FROM ci_burst_reservations WHERE run_id = ?",
                (rid,),
            ).fetchone()["state"]
        assert state == "released"
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
        config.CI_RUN_COOLDOWN_SECONDS = old_cooldown


def test_ci_burst_released_on_preparation_failure():
    from unittest import mock

    from server.ci_runner import _runs

    buyer = _new_agent("burst-ci-prepare")
    _fund(buyer["agent_id"])
    old_cap = config.CI_RUN_DAILY_CAP
    old_cooldown = config.CI_RUN_COOLDOWN_SECONDS
    old_remote = config.CI_FARM_BENCH_REMOTE_FIRST
    config.CI_RUN_DAILY_CAP = 1
    config.CI_RUN_COOLDOWN_SECONDS = 0
    config.CI_FARM_BENCH_REMOTE_FIRST = False
    try:
        events.log_event(
            events.EVT_CI_DB_BENCH_RUN,
            actor_agent_id=buyer["agent_id"],
            actor_name=buyer["name"],
            detail={"checks": "db_benchmark", "ok": True},
        )
        db.buy_store_item(buyer["token"], "ci_burst")
        rid = "4" * 32
        with (
            mock.patch.object(_runs, "_should_gate_bench", return_value=True),
            mock.patch.object(_runs, "_bench_quiet_wait", return_value=1),
            mock.patch.object(
                _runs,
                "_wait_for_quiet",
                side_effect=OSError("quiet preparation failed"),
            ),
        ):
            try:
                _runs.run_checks(
                    buyer["agent_id"],
                    "t",
                    "db_benchmark",
                    _run_id=rid,
                )
            except OSError as exc:
                assert "quiet preparation failed" in str(exc)
            else:
                raise AssertionError("preparation failure must propagate")
        assert db.ci_burst_remaining(buyer["agent_id"]) == 3
        with db._conn() as conn:
            state = conn.execute(
                "SELECT state FROM ci_burst_reservations WHERE run_id = ?",
                (rid,),
            ).fetchone()["state"]
        assert state == "released"
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
        config.CI_RUN_COOLDOWN_SECONDS = old_cooldown
        config.CI_FARM_BENCH_REMOTE_FIRST = old_remote


def test_ci_burst_release_handles_started_state():
    buyer = _new_agent("burst-ci-started-release")
    _fund(buyer["agent_id"])
    db.buy_store_item(buyer["token"], "ci_burst")
    run_id = "5" * 32
    assert db.reserve_ci_burst(buyer["agent_id"], "ci_local_run", run_id)
    assert db.mark_ci_burst_started(run_id)
    assert db.release_ci_burst(run_id, error="abandoned")
    assert db.ci_burst_remaining(buyer["agent_id"]) == 3
    with db._conn() as conn:
        state = conn.execute(
            "SELECT state FROM ci_burst_reservations WHERE run_id = ?",
            (run_id,),
        ).fetchone()["state"]
    assert state == "released"


def test_ci_burst_heartbeat_keeps_reserved_lease_alive():
    buyer = _new_agent("burst-ci-heartbeat")
    _fund(buyer["agent_id"])
    db.buy_store_item(buyer["token"], "ci_burst")
    run_id = "6" * 32
    assert db.reserve_ci_burst(buyer["agent_id"], "ci_local_run", run_id)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE ci_burst_reservations SET created_at = '2000-01-01T00:00:00.000Z'"
            " WHERE run_id = ?",
            (run_id,),
        )
    assert db.heartbeat_ci_burst(run_id)
    assert db.ci_burst_remaining(buyer["agent_id"]) == 2
    with db._conn() as conn:
        state = conn.execute(
            "SELECT state FROM ci_burst_reservations WHERE run_id = ?",
            (run_id,),
        ).fetchone()["state"]
    assert state == "reserved"


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
    assert rows["store_vote_burst"]["key"] == "vote_burst"
    assert rows["store_comment_burst"]["key"] == "comment_burst"
    assert rows["store_ci_burst"]["key"] == "ci_burst"


def test_ci_burst_concurrent_reserve_and_status():
    buyer = _new_agent("burst-ci-race")
    _fund(buyer["agent_id"])
    db.buy_store_item(buyer["token"], "ci_burst")
    errors = []

    def worker(n):
        try:
            for i in range(20):
                rid = f"race-{n}-{i:02d}"
                if db.reserve_ci_burst(buyer["agent_id"], "ci_local_run", rid):
                    assert db.ci_burst_remaining(buyer["agent_id"]) >= 0
                    assert db.release_ci_burst(rid, error="race")
                assert db.ci_burst_remaining(buyer["agent_id"]) >= 0
                # Hammer the fixed line too: the status read path must stay
                # exact under the same contention.
                st = db.ci_kind_status(buyer["agent_id"], "ci_local_run")
                assert st["burst_remaining"] >= 0, st
        except Exception as exc:  # fail-loudly - any OperationalError fails
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    assert not errors, errors[:1]
    assert db.ci_burst_remaining(buyer["agent_id"]) == 3
    with db._conn() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM ci_burst_reservations"
            " WHERE agent_id = ? AND state IN ('reserved', 'started')",
            (buyer["agent_id"],),
        ).fetchone()[0]
    assert live == 0, live
    # The reconcile read takes the caller's conn so a held write txn does
    # not deadlock on a nested BEGIN IMMEDIATE (bug #110: two whoami calls
    # on one conn); reserve keeps its own immediate txn (write must be
    # atomic with the reconcile). Composable-helper contract db/_core/_conn.py.
    import inspect as _inspect

    assert "conn" in _inspect.signature(db.ci_burst_remaining).parameters
    assert "conn" not in _inspect.signature(db.reserve_ci_burst).parameters
    print("  concurrent reserve/status never upgrade-fails, accounting exact")


def test_ci_burst_two_whoami_on_one_held_write_txn():
    # Bug #110 regression: two whoami calls sharing one write txn used to
    # deadlock. whoami runs ci_usage_for(conn=c) while the caller's txn
    # holds RESERVED; ci_burst_remaining's own BEGIN IMMEDIATE then blocked
    # and died after SQLITE_BUSY_TIMEOUT_SECONDS. Threading the caller conn
    # through makes the burst reconcile ride the held txn instead.
    buyer = _new_agent("burst-whoami-twice")
    _fund(buyer["agent_id"])
    db.buy_store_item(buyer["token"], "ci_burst")
    token = buyer["token"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET last_seen_at = last_seen_at WHERE id = ?",
            (buyer["agent_id"],),
        )
        first = db.whoami(token, conn)
        second = db.whoami(token, conn)
        assert second["agent_id"] == first["agent_id"] == buyer["agent_id"]
        assert second["ci_usage"]["ci_local_run"]["burst_remaining"] == 3, second[
            "ci_usage"
        ]["ci_local_run"]
    print("  two whoami on one held write txn: no nested-immediate deadlock")


if __name__ == "__main__":
    for fn in (
        test_vote_burst_purchase_shared_pool_and_expiry,
        test_comment_burst_purchase_shared_pool_and_expiry,
        test_ci_burst_shared_credits_and_reservations,
        test_ci_burst_reconciles_stale_started,
        test_ci_burst_release_and_base_cap_zero,
        test_ci_burst_released_on_busy_after_reservation,
        test_ci_burst_released_on_branch_conflict,
        test_vote_burst_cap_is_atomic_at_cap_minus_one,
        test_daily_comment_usage_only_degrades_for_missing_remarks,
        test_ci_burst_released_on_runner_spawn_failure,
        test_ci_burst_released_on_preparation_failure,
        test_ci_burst_release_handles_started_state,
        test_ci_burst_heartbeat_keeps_reserved_lease_alive,
        test_ci_burst_concurrent_reserve_and_status,
        test_ci_burst_two_whoami_on_one_held_write_txn,
        test_schema_and_stats_surface,
    ):
        fn()
        print(f"PASS {fn.__name__}")
