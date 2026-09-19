"""Upkeep grace pins: no arrears row is issued for a member's first ISO
week; incumbents bill normally and the following week bills everyone.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_grace_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()

_SEQ = [0]
_OLD_JOIN = "2020-01-01T00:00:00.000Z"


def _new_agent(prefix):
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _found(name=None):
    import db._credits as _cr

    ag = _new_agent("grace-founder")
    with db._conn() as _c:
        assert _cr.grant(ag["agent_id"], 600, "grace_seed", conn=_c)
    return ag, db.found_guild(ag["token"], name or f"Grace-{_SEQ[0]}")


def _arrears(gid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT member_agent_id FROM guild_fee_arrears WHERE guild_id = ?",
            (gid,),
        ).fetchall()


def _age_member(gid, aid):
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_members SET joined_at = ? WHERE guild_id = ? AND agent_id = ?",
            (_OLD_JOIN, gid, aid),
        )


def test_first_week_free_then_billed():
    founder, guild = _found()
    gid = guild["id"]
    # Founded this week: the sweep issues nothing.
    db.sweep_guild_upkeep()
    assert _arrears(gid) == [], _arrears(gid)
    # Aged a week: the same sweep bills normally.
    _age_member(gid, founder["agent_id"])
    db.sweep_guild_upkeep()
    rows = _arrears(gid)
    assert [r[0] for r in rows] == [founder["agent_id"]], rows
    print("  first week free, then billed: ok")


def test_midweek_joiner_unbilled_incumbent_billed():
    founder, guild = _found()
    gid = guild["id"]
    _age_member(gid, founder["agent_id"])
    mate = _new_agent("grace-mate")
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.sweep_guild_upkeep()
    billed = sorted(r[0] for r in _arrears(gid))
    assert billed == [founder["agent_id"]], billed
    print("  midweek joiner unbilled, incumbent billed: ok")


if __name__ == "__main__":
    test_first_week_free_then_billed()
    test_midweek_joiner_unbilled_incumbent_billed()
    print("\n== test_guilds_upkeep_grace: all passed ==")
