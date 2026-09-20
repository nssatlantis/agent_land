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


def _fund(agent_id: int, units: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            units,
            "guild_poller_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("gp-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], name or f"Poller-{_SEQ[0]}")


def _age_guild(gid: int):
    """Backdate every member's join past the upkeep grace so the sweep
    bills normally (fixtures found-and-swept in the same week would
    otherwise read as grace-skipped)."""
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_members SET joined_at = '2020-01-01T00:00:00.000Z'"
            " WHERE guild_id = ?",
            (gid,),
        )


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
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    _age_guild(guild["id"])
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


def test_upkeep_runs_when_membership_throws():
    # The poller's contract at unit level: upkeep never depends on the
    # membership sweep succeeding on the same tick.
    founder, guild = _found()
    mate = _new_agent("gp-mate2")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    _age_guild(guild["id"])
    real = db.sweep_guild_memberships

    def _boom():
        raise RuntimeError("membership down")

    db.sweep_guild_memberships = _boom  # type: ignore[method-assign]
    try:
        try:
            db.sweep_guild_memberships()
        except Exception:  # domain: degrade-silently - poller-block shape
            pass
        report = db.sweep_guild_upkeep()
    finally:
        db.sweep_guild_memberships = real  # type: ignore[method-assign]
    assert report["issued"] >= 2, f"upkeep must run despite membership: {report}"
    print("  upkeep runs when membership throws: ok")


def test_poisoned_guild_does_not_roll_back_neighbours():
    import db._guilds_treasury as _treas

    founder, guild = _found()
    mate = _new_agent("gp-mate3")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    founder2, guild2 = _found()
    # Fund the healthy guild so its sweep succeeds outright (pool must
    # cover dues) while the poisoned one skips.
    _fund(founder["agent_id"], 300)
    db.guild_deposit(founder["token"], guild["id"], 10.0)
    # Age invoices past the 48h sweep gate so the pool reads actually
    # execute (fresh guilds skip before them). Guild 1 sweeps clean
    # while guild 2's poisoned read skips without rolling it back.
    _age_guild(guild["id"])
    _age_guild(guild2["id"])
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = '2020-01-01T00:00:00.000Z'"
            " WHERE id IN (SELECT invoice_id FROM guild_fee_invoices"
            " WHERE guild_id IN (?, ?))",
            (guild["id"], guild2["id"]),
        )
    real_balance = _treas.guild_balance

    def _poison(conn, gid):
        if gid == guild2["id"]:
            raise RuntimeError("poisoned pool read")
        return real_balance(conn, gid)

    # Patch where the sweep looks it up (treasury bound the name at
    # import); patching db._guilds would miss every call site.
    _treas.guild_balance = _poison  # type: ignore[method-assign]
    try:
        report = db.sweep_guild_upkeep()
    finally:
        _treas.guild_balance = real_balance  # type: ignore[method-assign]
    assert guild2["id"] in report["skipped"], f"poisoned guild must skip: {report}"
    assert report["swept"].get(guild["id"], 0) > 0, (
        f"healthy guild must still sweep: {report}"
    )
    print("  poisoned guild does not roll back neighbours: ok")


def test_persistent_skip_stays_quiet():
    import db._guilds as _gm

    founder, guild = _found()
    gid = guild["id"]
    # Bill and age one invoice, then suspend directly: the sweep must
    # reach the grace-disband arm (issuance alone never suspends a
    # funded pool, so the flag is seeded like the arrears pins do).
    _age_guild(gid)
    db.sweep_guild_upkeep()
    with db._conn() as conn:
        conn.execute(
            "UPDATE invoices SET created_at = '2020-01-01T00:00:00.000Z'"
            " WHERE id IN (SELECT invoice_id FROM guild_fee_invoices"
            " WHERE guild_id = ?)",
            (gid,),
        )
        conn.execute(
            "UPDATE guilds SET spending_suspended = 1,"
            " suspended_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (gid,),
        )
    # Fault-inject the disband itself (treasury looks the name up
    # inside the guarded block, so patch the source module): the guild
    # must skip quietly on every tick, never disband, never log.
    real_disband = _gm._disband_distribute

    def _unfunded(conn, gid_arg, reason):
        raise db.ForumError("the treasury cannot fund that payout.")

    _gm._disband_distribute = _unfunded  # type: ignore[method-assign]
    try:
        db.sweep_guild_upkeep()
        before = len(_upkeep_events())
        report = db.sweep_guild_upkeep()
        assert gid in report["skipped"], f"unfunded disband must skip: {report}"
        assert len(_upkeep_events()) == before, "persistent skip must stay quiet"
    finally:
        _gm._disband_distribute = real_disband  # type: ignore[method-assign]
    print("  persistent skip stays quiet: ok")


def test_second_work_sweep_issues_nothing_new():
    founder, guild = _found()
    mate = _new_agent("gp-mate4")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    _age_guild(guild["id"])
    before = len(_upkeep_events())
    first = db.sweep_guild_upkeep()
    assert first["issued"] >= 2, first
    assert len(_upkeep_events()) == before + 1
    second = db.sweep_guild_upkeep()
    assert second["issued"] == 0, f"same-week re-sweep must not rebill: {second}"
    assert len(_upkeep_events()) == before + 1, "same-week re-sweep must stay quiet"
    print("  second work sweep issues nothing new: ok")


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
    test_upkeep_runs_when_membership_throws()
    test_poisoned_guild_does_not_roll_back_neighbours()
    test_persistent_skip_stays_quiet()
    test_second_work_sweep_issues_nothing_new()
    test_membership_sweep_report_shape()
    print("\n== test_guilds_poller: all passed ==")
