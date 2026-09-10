"""Tests for the heartbeat settle matrix (server.poller._anchor, #381).

_settle_dispatch takes fabricated-or-live result dicts, so the whole
matrix pins directly with no harness and no mocks: blessed passes
through; held + buyer refunds; returned-infra + buyer restores the bank;
a failed refund restores instead of losing both; every non-bless lands a
skip-audit row. Plus the fresh-anchor quiet skip of _heartbeat_tick
(no dispatch, no ledger row).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_settle_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
import events  # noqa: E402, I001

QUIET = {"quiet": True, "contended": False, "quiet_wait_s": 0.0}


def _skips():
    return events.query_events(kind=events.EVT_BENCH_HEARTBEAT_SKIPPED, limit=50)


def _bal(agent_id: int) -> int:
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as conn:
        assert _cr.grant(agent_id, quarters, "admin_adjust", conn=conn)


def _banked_buyer(prefix: str) -> dict:
    buyer = db.register_agent(prefix)
    _fund(buyer["agent_id"], 200)
    rep = db.buy_store_item(buyer["token"], "blessed_bench")
    assert rep["owned"] == 1, "buyer starts with one banked run"
    assert _bank(buyer) == 1, "bank reads back"
    return buyer


def _take(buyer: dict):
    import db._store as _store

    with db._conn(immediate=True) as conn:
        _store._take_blessed_bench(conn, buyer["agent_id"])


def _bank(buyer: dict) -> int:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT blessed_benches FROM store_entitlements WHERE agent_id = ?",
            (buyer["agent_id"],),
        ).fetchone()
    return int(row["blessed_benches"])


def _arm(env_key: str, value: str):
    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(__import__("config"))
    return old


def _unarm(old, env_key: str):
    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(__import__("config"))


def main():
    agents, _ = setup()
    import server.poller._anchor as tick

    held = {
        "outcome": "held",
        "run_event_id": 11,
        "decision": "held: 3 queries drifted (anchor aging; resolve the drift)",
    }
    infra = {
        "outcome": "infra",
        "run_event_id": None,
        "decision": "infra: dispatched bench left no fresh native ledger row",
    }

    # Blessed passes through untouched: no audit row, no money, no bank move.
    b0 = _banked_buyer("settle-blessed")
    n0 = len(_skips())
    out = tick._settle_dispatch(
        {
            "outcome": "blessed",
            "run_event_id": 12,
            "decision": "blessed: heartbeat run ev12",
        },
        b0["agent_id"],
    )
    assert out["outcome"] == "blessed" and out["run_event_id"] == 12, "passthrough"
    assert out["buyer_id"] == b0["agent_id"], "buyer carried"
    assert _bank(b0) == 1 and len(_skips()) == n0, "blessed moves nothing"

    # Held + buyer: price refunded, bank stays spent, audit row names both.
    b1 = _banked_buyer("settle-held")
    before = _bal(b1["agent_id"])
    _take(b1)
    out = tick._settle_dispatch(dict(held), b1["agent_id"])
    assert out["outcome"] == "held", "outcome preserved"
    assert _bal(b1["agent_id"]) - before == 8, "2-credit price refunded"
    assert _bank(b1) == 0, "one attempt per purchase"
    rows = _skips()
    assert len(rows) == n0 + 1 and rows[0]["detail"]["buyer_id"] == b1["agent_id"], (
        "hold audited with the buyer"
    )
    assert "auto-refunded" in rows[0]["detail"]["reason"], "refund named in the row"
    assert rows[0]["detail"]["run_event_id"] == 11, "run linked in the row"

    # Held, no buyer: audit only, nobody paid.
    n1 = len(_skips())
    out = tick._settle_dispatch(dict(held), None)
    assert out["outcome"] == "held" and out["buyer_id"] is None, "buyerless passthrough"
    assert len(_skips()) == n1 + 1, "buyerless hold still audited"

    # Returned-infra + buyer (MAJOR-1): bank restored, no money moved, audit.
    b2 = _banked_buyer("settle-infra")
    before = _bal(b2["agent_id"])
    _take(b2)
    out = tick._settle_dispatch(dict(infra), b2["agent_id"])
    assert out["outcome"] == "infra", "outcome preserved"
    assert _bank(b2) == 1, "bank restored - the attempt never happened"
    assert _bal(b2["agent_id"]) == before, "no credit movement on restore"
    assert "bank restored" in _skips()[0]["detail"]["reason"], "restore audited"

    # Returned-infra, no buyer: audit only.
    n2 = len(_skips())
    out = tick._settle_dispatch(dict(infra), None)
    assert out["outcome"] == "infra", "buyerless infra passes through"
    assert len(_skips()) == n2 + 1, "buyerless infra still audited"

    # Failed refund (dry treasury): bank restored instead of losing both.
    b3 = _banked_buyer("settle-dry")
    _take(b3)
    old = _arm("FORUM_CREDITS_ENABLED", "0")
    try:
        out = tick._settle_dispatch(dict(held), b3["agent_id"])
    finally:
        _unarm(old, "FORUM_CREDITS_ENABLED")
    assert out["outcome"] == "held", "outcome preserved"
    assert _bank(b3) == 1, "failed refund restores the bank for a retry"
    assert "refund failed" in _skips()[0]["detail"]["reason"], "failure audited"

    # Fresh-anchor quiet skip: no dispatch, no ledger row, buyer untouched.
    subject = db.register_agent("settle-tick")
    events.log_event(
        events.EVT_CI_DB_BENCH_RUN,
        actor_agent_id=subject["agent_id"],
        actor_name=subject["name"],
        detail={
            "checks": "db_benchmark",
            "mode": "native",
            "ok": True,
            "exit_code": 0,
            "duration_seconds": 20.0,
            "head_sha": "beef1234567890abcdef1234567890abcdef1",
            "bench_load": dict(QUIET),
            "summary": {
                "bench": "db_benchmark",
                "regressions": 0,
                "bench_errors": [],
                "timings_median_ms": {"a": 10.0},
            },
        },
    )
    run = events.query_events(kind=events.EVT_CI_DB_BENCH_RUN, limit=1)[0]["id"]
    assert db.bless_heartbeat_run(run, reason="heartbeat", blessed_by=None).startswith(
        "blessed:"
    ), "scratch anchor blesses"
    n3 = len(_skips())
    out = tick._heartbeat_tick()
    assert out["outcome"] == "skipped", f"fresh anchor stands down ({out})"
    assert len(_skips()) == n3, "quiet skip writes no ledger row"

    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_bench_settle: all assertions passed")


if __name__ == "__main__":
    main()
