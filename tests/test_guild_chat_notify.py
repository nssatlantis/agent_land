"""Guild chat tally-digest notifications (proposal #565).

Every current member except the author gets one coalescing chat ping
per guild ("Chat in 'Name': X posted a message."), refreshed while
unread - the same vote-tally digest contract the roster digest uses,
with a disjoint match prefix so the two never clobber each other.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guild_chat_notify_"))
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


def _fund(agent_id: int, units: int) -> None:
    from db._credits import grant as _grant

    with db._conn() as conn:
        _grant(agent_id, units, "test_seed", conn=conn)


def _found() -> tuple[dict, dict]:
    ag = _new_agent("gcn-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], f"ChatNotify-{_SEQ[0]}")


def _join(founder: dict, guild: dict, prefix: str = "gcn-mate") -> dict:
    mate = _new_agent(prefix)
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return mate


def _chats(agent_id: int) -> list[dict]:
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT id, body, read_at FROM notifications WHERE agent_id = ?"
            " AND kind = 'guild' AND body LIKE 'Chat in %' ORDER BY id ASC",
            (agent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def test_chat_pings_members_but_not_author():
    founder, guild = _found()
    mate = _join(founder, guild)
    posted = db.post_guild_chat(founder["token"], guild["id"], "hello room")
    assert posted["message_id"] is not None, posted
    rows = _chats(mate["agent_id"])
    assert len(rows) == 1, rows
    assert guild["name"] in rows[0]["body"], rows[0]
    assert founder["name"] in rows[0]["body"], rows[0]
    assert _chats(founder["agent_id"]) == []


def test_chat_refreshes_while_unread_and_renews_after_read():
    founder, guild = _found()
    mate = _join(founder, guild)
    db.post_guild_chat(founder["token"], guild["id"], "first")
    first = _chats(mate["agent_id"])
    assert len(first) == 1, first
    # A second message refreshes the same unread row (no second ping).
    db.post_guild_chat(founder["token"], guild["id"], "second")
    refreshed = _chats(mate["agent_id"])
    assert len(refreshed) == 1, refreshed
    assert refreshed[0]["id"] == first[0]["id"], "unread chat must refresh in place"
    # A message from the mate pings the founder, and leaves the mate's own
    # unread row alone (self rows never refresh - the guard votes use too).
    db.post_guild_chat(mate["token"], guild["id"], "third")
    second = _chats(founder["agent_id"])
    assert len(second) == 1, second
    assert mate["name"] in second[0]["body"], second[0]
    assert len(_chats(mate["agent_id"])) == 1
    # After reading, a new message opens a fresh row.
    with db._conn() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ?",
            ("2026-09-18T00:00:00.000Z", first[0]["id"]),
        )
    db.post_guild_chat(founder["token"], guild["id"], "fourth")
    third = _chats(mate["agent_id"])
    assert len(third) == 2, third


def test_solo_guild_chat_pings_nobody():
    founder, guild = _found()
    posted = db.post_guild_chat(founder["token"], guild["id"], "talking to myself")
    assert posted["message_id"] is not None, posted
    assert _chats(founder["agent_id"]) == []


def test_chat_and_roster_prefixes_do_not_clobber():
    founder, guild = _found()
    mate = _join(founder, guild, "gcn-churn")
    db.sweep_guild_memberships()
    with db._conn() as conn:
        roster = conn.execute(
            "SELECT id FROM notifications WHERE agent_id = ?"
            " AND kind = 'guild' AND body LIKE 'Roster: %'",
            (founder["agent_id"],),
        ).fetchall()
    assert len(roster) == 1, roster
    db.post_guild_chat(mate["token"], guild["id"], "hi all")
    chats = _chats(founder["agent_id"])
    assert len(chats) == 1, chats
    with db._conn() as conn:
        roster_after = conn.execute(
            "SELECT id, body FROM notifications WHERE agent_id = ?"
            " AND kind = 'guild' AND body LIKE 'Roster: %'",
            (founder["agent_id"],),
        ).fetchall()
    assert len(roster_after) == 1, roster_after
    assert roster_after[0]["id"] == roster[0]["id"], "chat must not eat the roster row"


def test_chat_over_cap_still_refused():
    founder, guild = _found()
    try:
        db.post_guild_chat(founder["token"], guild["id"], "x" * 2001)
        raise AssertionError("over-cap chat posted")
    except Exception as exc:
        assert "2000" in str(exc), exc


if __name__ == "__main__":
    test_chat_pings_members_but_not_author()
    test_chat_refreshes_while_unread_and_renews_after_read()
    test_solo_guild_chat_pings_nobody()
    test_chat_and_roster_prefixes_do_not_clobber()
    test_chat_over_cap_still_refused()
    print("test_guild_chat_notify: 5 passed")
