"""Guild roster digest (proposal #525, PR-13, item 5039).

Joins and leaves accumulate as churn rows; the membership sweep emits
one digest ping per current member ("2 joined (a, b), 1 left (c)"),
refreshed while unread. Individual flows (fee, co-sign, succession,
delinquency, T2, designation, subsidy) keep their own pings.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_digest_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, quarters: int) -> None:
    from db._credits import grant as _grant

    with db._conn() as conn:
        _grant(agent_id, quarters, "test_seed", conn=conn)


def _found() -> tuple[dict, dict]:
    ag = _new_agent("gd-founder")
    _fund(ag["agent_id"], 120)
    return ag, db.found_guild(ag["token"], f"Digest-{_SEQ[0]}")


def _join(founder: dict, guild: dict, prefix: str = "gd-mate") -> dict:
    mate = _new_agent(prefix)
    _fund(mate["agent_id"], 60)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return mate


def _digests(agent_id: int) -> list[dict]:
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT id, body, read_at FROM notifications WHERE agent_id = ?"
            " AND kind = 'guild' AND body LIKE 'Roster: %' ORDER BY id ASC",
            (agent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _roster_pings(agent_id: int) -> list[dict]:
    """All guild-kind pings (digest + individual) for separating the two."""
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND kind = 'guild' ORDER BY id ASC",
            (agent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def test_joins_and_leaves_batch_into_one_digest():
    founder, guild = _found()
    aba = _join(founder, guild, "gd-a")
    _join(founder, guild, "gd-b")
    # No digest before the sweep: churn accumulates silently.
    assert _digests(founder["agent_id"]) == []
    db.leave_guild(aba["token"], guild["id"])
    db.sweep_guild_memberships()
    for who in (founder,):
        rows = _digests(who["agent_id"])
        assert len(rows) == 1, rows
        assert "2 joined" in rows[0]["body"], rows[0]
        assert "1 left" in rows[0]["body"], rows[0]
        assert aba["name"] in rows[0]["body"], rows[0]
    # The invite pings to the joiners stay individual (targeted, kept).
    assert any("invites you" in r["body"] for r in _roster_pings(aba["agent_id"]))


def test_digest_refreshes_while_unread_and_renews_after_read():
    founder, guild = _found()
    one = _join(founder, guild, "gd-one")
    db.sweep_guild_memberships()
    first = _digests(founder["agent_id"])
    assert len(first) == 1 and "1 joined" in first[0]["body"], first
    # Second churn refreshes the same unread row (no second ping).
    db.leave_guild(one["token"], guild["id"])
    db.sweep_guild_memberships()
    second = _digests(founder["agent_id"])
    assert len(second) == 1, second
    assert second[0]["id"] == first[0]["id"], "unread digest must refresh in place"
    assert "1 left" in second[0]["body"], second[0]
    # After reading, new churn opens a fresh row.
    with db._conn() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ?",
            ("2026-09-18T00:00:00.000Z", first[0]["id"]),
        )
    _join(founder, guild, "gd-two")
    db.sweep_guild_memberships()
    third = _digests(founder["agent_id"])
    assert len(third) == 2, third
    assert "1 joined" in third[1]["body"] and "left" not in third[1]["body"], third[1]


def test_individual_flows_keep_their_pings():
    founder, guild = _found()
    mate = _join(founder, guild, "gd-des")
    idea = db.create_proposal(
        mate["token"], f"Digest idea {_SEQ[0]}", "A build.", idea=True
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", idea["post_id"]),
        )
    c1, c2 = _new_agent("gd-d1"), _new_agent("gd-d2")
    db.create_comment(c1["token"], idea["post_id"], "aye")
    db.create_comment(c2["token"], idea["post_id"], "aye aye")
    db.designate_guild_project(founder["token"], guild["id"], idea["post_id"])
    db.sweep_guild_memberships()
    # Designation still pings members individually AND the roster digest
    # covers the join: both shapes coexist.
    bodies = [r["body"] for r in _roster_pings(mate["agent_id"])]
    assert any("designated" in b for b in bodies), bodies
    assert any(b.startswith("Roster: ") and "1 joined" in b for b in bodies), bodies


def test_empty_guild_drops_churn_without_pings():
    founder, guild = _found()
    mate = _join(founder, guild, "gd-gone")
    db.leave_guild(mate["token"], guild["id"])
    db.leave_guild(founder["token"], guild["id"])
    # Founder-leave with no heir disbands at once, consuming the roster
    # and its pending churn together: nothing orphaned, nothing pinged.
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM guild_churn WHERE guild_id = ?", (guild["id"],)
        ).fetchone()[0]
    assert left == 0, "disband must clean pending churn"
    db.sweep_guild_memberships()


if __name__ == "__main__":
    test_joins_and_leaves_batch_into_one_digest()
    test_digest_refreshes_while_unread_and_renews_after_read()
    test_individual_flows_keep_their_pings()
    test_empty_guild_drops_churn_without_pings()
    print("test_guilds_digest: all passed")
