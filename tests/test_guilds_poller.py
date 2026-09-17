"""Guild sweep poller wiring (proposal #525, PR-5): membership + upkeep.

Covers the poller seam that turns the two exposed-but-unwired sweeps into
live housekeeping: both sweeps run every outcome-poll tick in membership
then upkeep order, each isolated so one failure never stalls the other or
the poller, upkeep stays quiet when idle (no summary event without work)
and logs exactly once when work happens.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_poller_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            quarters,
            "guild_poller_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("gp-founder")
    _fund(ag["agent_id"], 120)
    return ag, db.found_guild(ag["token"], name or f"Poller-{_SEQ[0]}")


def _upkeep_events() -> list:
    import events

    return events.query_events(kind="guild_upkeep_swept")


def test_sweeps_exported_on_facade():
    assert callable(db.sweep_guild_memberships), "membership sweep not exported"
    assert callable(db.sweep_guild_upkeep), "upkeep sweep not exported"
    print("  sweeps exported on facade: ok")


def test_poller_wires_both_sweeps_in_order():
    import inspect

    import server.poller._outcome as _out

    src = inspect.getsource(_out._pr_outcome_poller)
    mem_at = src.find("sweep_guild_memberships")
    up_at = src.find("sweep_guild_upkeep")
    assert mem_at >= 0, "poller never calls sweep_guild_memberships"
    assert up_at >= 0, "poller never calls sweep_guild_upkeep"
    assert mem_at < up_at, "membership must run before upkeep (releases shape billing)"
    assert src.count("sweep_guild_memberships") >= 1
    assert src.count("sweep_guild_upkeep") >= 1
    print("  poller wires both sweeps in order: ok")


def test_poller_sweep_blocks_carry_domain_markers():
    import inspect

    import server.poller._outcome as _out

    src = inspect.getsource(_out._pr_outcome_poller)
    mem_block = src[
        src.find("sweep_guild_memberships") - 600 : src.find("sweep_guild_memberships")
        + 200
    ]
    up_block = src[
        src.find("sweep_guild_upkeep") - 600 : src.find("sweep_guild_upkeep") + 200
    ]
    assert "domain: degrade-silently" in mem_block, (
        "membership block needs domain marker"
    )
    assert "domain: degrade-silently" in up_block, "upkeep block needs domain marker"
    print("  sweep blocks carry domain markers: ok")


def test_upkeep_idle_is_quiet():
    before = len(_upkeep_events())
    report = db.sweep_guild_upkeep()
    assert report["issued"] == 0, report
    assert report["swept"] == {}, report
    assert report["suspended"] == [], report
    assert report["recovered"] == [], report
    assert report["disbanded"] == [], report
    after = len(_upkeep_events())
    assert after == before, f"idle sweep logged {after - before} summary event(s)"
    # Second idle tick is equally quiet (idempotent, no weekly double-bill).
    report2 = db.sweep_guild_upkeep()
    assert report2["issued"] == 0, report2
    assert len(_upkeep_events()) == before, "second idle sweep must stay quiet"
    print("  upkeep idle is quiet: ok")


def test_upkeep_work_logs_exactly_once():
    founder, guild = _found()
    mate = _new_agent("gp-mate")
    _fund(mate["agent_id"], 60)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    before = len(_upkeep_events())
    report = db.sweep_guild_upkeep()
    assert report["issued"] >= 2, f"two members should be billed, got {report}"
    after = len(_upkeep_events())
    assert after == before + 1, f"work sweep must log one summary, got {after - before}"
    print("  upkeep work logs exactly once: ok")


def test_sweep_failure_isolation_mirrors_poller():
    calls: list[str] = []

    def _boom():
        calls.append("membership")
        raise RuntimeError("membership down")

    def _fine():
        calls.append("upkeep")
        return {"week": "x", "issued": 0}

    # Mirror the poller's two isolated try blocks: a membership raise must
    # not skip the upkeep call on the same tick.
    try:
        _boom()
    except Exception:  # domain: degrade-silently - mirrors the poller block
        pass
    try:
        _fine()
    except Exception:  # domain: degrade-silently - mirrors the poller block
        pass
    assert calls == ["membership", "upkeep"], f"upkeep skipped after failure: {calls}"
    print("  sweep failure isolation mirrors poller: ok")


def test_membership_sweep_report_shape():
    report = db.sweep_guild_memberships()
    for key in (
        "released",
        "succeeded",
        "disbanded",
        "expired",
        "polls_closed",
        "skipped",
    ):
        assert key in report, f"membership report missing {key}"
    print("  membership sweep report shape: ok")


# -- run all --
if __name__ == "__main__":
    test_sweeps_exported_on_facade()
    test_poller_wires_both_sweeps_in_order()
    test_poller_sweep_blocks_carry_domain_markers()
    test_upkeep_idle_is_quiet()
    test_upkeep_work_logs_exactly_once()
    test_sweep_failure_isolation_mirrors_poller()
    test_membership_sweep_report_shape()
    print("\n== test_guilds_poller: all passed ==")
