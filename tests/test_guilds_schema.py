"""Guilds foundation (proposal #525, PR-1): the ledger+roster tables.

PR-1 ships DDL only: guilds, guild_members, guild_ledger, guild_polls,
guild_poll_votes, guild_projects, guild_tranches, guild_designations.
Behavior (found/join/spend/tranches) lands in PR-2 and later; these pins
prove the upgrade path and every CHECK/UNIQUE/FK the engine will rely on.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_schema_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


_TABLES = (
    "guilds",
    "guild_members",
    "guild_ledger",
    "guild_polls",
    "guild_poll_votes",
    "guild_projects",
    "guild_tranches",
    "guild_designations",
)

_INDEXES = (
    "idx_guilds_founder",
    "idx_guilds_status",
    "idx_guild_members_guild",
    "idx_guild_members_agent",
    "idx_guild_ledger_guild",
    "idx_guild_polls_guild",
    "idx_guild_projects_guild",
    "idx_guild_tranches_guild",
    "idx_guild_designations_guild",
)


def _names(conn, kind: str) -> set:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
        ).fetchall()
    }


def _mk_guild(conn, founder_id: int, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO guilds (name, founder_agent_id) VALUES (?, ?)",
        (name, founder_id),
    )
    return cur.lastrowid


def test_upgrade_creates_guild_tables():
    """A pre-guilds database gains all eight tables + indexes on init_db."""
    with db._conn() as conn:
        for table in _TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    db.init_db()
    with db._conn() as conn:
        tables = _names(conn, "table")
        indexes = _names(conn, "index")
    for table in _TABLES:
        assert table in tables, f"{table} missing after init_db"
    for idx in _INDEXES:
        assert idx in indexes, f"{idx} missing after init_db"


def test_guild_status_check():
    """status admits exactly active/suspended/disbanded."""
    founder = _new_agent("gs-check")
    with db._conn() as conn:
        gid = _mk_guild(conn, founder["agent_id"], "Status Guild")
        for ok in ("active", "suspended", "disbanded"):
            conn.execute("UPDATE guilds SET status = ? WHERE id = ?", (ok, gid))
        try:
            conn.execute("UPDATE guilds SET status = 'retired' WHERE id = ?", (gid,))
            raise AssertionError("bad status accepted")
        except sqlite3.IntegrityError:
            pass


def test_guild_name_unique_nocase_and_nonempty():
    """Names are unique regardless of case and never empty."""
    founder = _new_agent("gs-name")
    with db._conn() as conn:
        _mk_guild(conn, founder["agent_id"], "Case Guild")
        try:
            _mk_guild(conn, founder["agent_id"], "case guild")
            raise AssertionError("case-dup name accepted")
        except sqlite3.IntegrityError:
            pass
        try:
            _mk_guild(conn, founder["agent_id"], "")
            raise AssertionError("empty name accepted")
        except sqlite3.IntegrityError:
            pass


def test_members_unique_and_fk():
    """One row per guild+agent; dangling guild/agent rejected."""
    a = _new_agent("gs-mem-a")
    b = _new_agent("gs-mem-b")
    with db._conn() as conn:
        g1 = _mk_guild(conn, a["agent_id"], "Roster One")
        g2 = _mk_guild(conn, a["agent_id"], "Roster Two")
        conn.execute(
            "INSERT INTO guild_members (guild_id, agent_id) VALUES (?, ?)",
            (g1, b["agent_id"]),
        )
        try:
            conn.execute(
                "INSERT INTO guild_members (guild_id, agent_id) VALUES (?, ?)",
                (g1, b["agent_id"]),
            )
            raise AssertionError("duplicate membership accepted")
        except sqlite3.IntegrityError:
            pass
        # Same agent may hold a seat in a second guild.
        conn.execute(
            "INSERT INTO guild_members (guild_id, agent_id, role)"
            " VALUES (?, ?, 'founder')",
            (g2, b["agent_id"]),
        )
        try:
            conn.execute(
                "INSERT INTO guild_members (guild_id, agent_id) VALUES (999999, ?)",
                (b["agent_id"],),
            )
            raise AssertionError("dangling guild accepted")
        except sqlite3.IntegrityError:
            pass
        try:
            conn.execute(
                "INSERT INTO guild_members (guild_id, agent_id) VALUES (?, 999999)",
                (g1,),
            )
            raise AssertionError("dangling agent accepted")
        except sqlite3.IntegrityError:
            pass
        try:
            conn.execute(
                "INSERT INTO guild_members (guild_id, agent_id, role)"
                " VALUES (?, ?, 'officer')",
                (g1, a["agent_id"]),
            )
            raise AssertionError("bad role accepted")
        except sqlite3.IntegrityError:
            pass


def test_ledger_quarters_positive_and_kind():
    """Ledger rows carry strictly positive quarters of a known kind -
    every kind in the CHECK executes, so a typo in any token fails."""
    founder = _new_agent("gs-ledger")
    with db._conn() as conn:
        gid = _mk_guild(conn, founder["agent_id"], "Ledger Guild")
        for kind in (
            "deposit",
            "withdrawal",
            "upkeep",
            "fee",
            "grant_t1",
            "grant_t2",
            "subsidy",
            "match",
            "stake",
            "job",
            "job_escrow",
            "stake_lock",
            "invoice",
            "transfer",
        ):
            conn.execute(
                "INSERT INTO guild_ledger (guild_id, kind, quarters) VALUES (?, ?, 4)",
                (gid, kind),
            )
        for bad in (0, -3):
            try:
                conn.execute(
                    "INSERT INTO guild_ledger (guild_id, kind, quarters)"
                    " VALUES (?, 'deposit', ?)",
                    (gid, bad),
                )
                raise AssertionError(f"quarters={bad} accepted")
            except sqlite3.IntegrityError:
                pass
        try:
            conn.execute(
                "INSERT INTO guild_ledger (guild_id, kind, quarters)"
                " VALUES (?, 'printing', 4)",
                (gid,),
            )
            raise AssertionError("unknown kind accepted")
        except sqlite3.IntegrityError:
            pass


def test_tranche_tier_status_checks():
    """Tiers are T1/T2; statuses follow the tranche lifecycle."""
    founder = _new_agent("gs-tranche")
    with db._conn() as conn:
        gid = _mk_guild(conn, founder["agent_id"], "Tranche Guild")
        conn.execute(
            "INSERT INTO guild_tranches (guild_id, tier, amount_quarters)"
            " VALUES (?, 'T1', 40)",
            (gid,),
        )
        try:
            conn.execute(
                "INSERT INTO guild_tranches (guild_id, tier, amount_quarters)"
                " VALUES (?, 'T3', 40)",
                (gid,),
            )
            raise AssertionError("bad tier accepted")
        except sqlite3.IntegrityError:
            pass
        try:
            conn.execute(
                "INSERT INTO guild_tranches (guild_id, tier, amount_quarters)"
                " VALUES (?, 'T2', 0)",
                (gid,),
            )
            raise AssertionError("zero tranche accepted")
        except sqlite3.IntegrityError:
            pass
        for status in ("proposed", "released", "paused", "expired", "merged"):
            conn.execute(
                "INSERT INTO guild_tranches (guild_id, tier, amount_quarters,"
                " status) VALUES (?, 'T2', 4, ?)",
                (gid, status),
            )
        row = conn.execute(
            "SELECT status FROM guild_tranches WHERE guild_id = ? AND tier = 'T1'",
            (gid,),
        ).fetchone()
        assert row[0] == "proposed", "tranche status must default to proposed"


def test_poll_vote_unique():
    """One advisory ballot per agent per poll."""
    founder = _new_agent("gs-poll")
    voter = _new_agent("gs-voter")
    with db._conn() as conn:
        gid = _mk_guild(conn, founder["agent_id"], "Poll Guild")
        cur = conn.execute(
            "INSERT INTO guild_polls (guild_id, creator_agent_id, question,"
            " closes_at) VALUES (?, ?, 'Build what?', '2030-01-01T00:00:00.000Z')",
            (gid, founder["agent_id"]),
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO guild_poll_votes (poll_id, agent_id, choice)"
            " VALUES (?, ?, 'a')",
            (pid, voter["agent_id"]),
        )
        try:
            conn.execute(
                "INSERT INTO guild_poll_votes (poll_id, agent_id, choice)"
                " VALUES (?, ?, 'b')",
                (pid, voter["agent_id"]),
            )
            raise AssertionError("double ballot accepted")
        except sqlite3.IntegrityError:
            pass


def test_project_designation_shapes():
    """Projects run proposed/active/done; designations need a title."""
    founder = _new_agent("gs-proj")
    with db._conn() as conn:
        gid = _mk_guild(conn, founder["agent_id"], "Project Guild")
        conn.execute(
            "INSERT INTO guild_projects (guild_id, title) VALUES (?, 'Ship it')",
            (gid,),
        )
        try:
            conn.execute(
                "INSERT INTO guild_projects (guild_id, title, status)"
                " VALUES (?, 'X', 'review')",
                (gid,),
            )
            raise AssertionError("bad project status accepted")
        except sqlite3.IntegrityError:
            pass
        try:
            conn.execute(
                "INSERT INTO guild_designations (guild_id, title) VALUES (?, '')",
                (gid,),
            )
            raise AssertionError("empty designation accepted")
        except sqlite3.IntegrityError:
            pass
        for table, col, extra in (
            ("guild_polls", "question", True),
            ("guild_projects", "title", False),
        ):
            # Valid siblings, so only the empty text can trip the CHECK.
            if extra:
                conn.execute(
                    "INSERT INTO guild_polls (guild_id, creator_agent_id,"
                    " question, closes_at) VALUES (?, ?, 'Q?', '2099-01-01T00:00:00.000Z')",
                    (gid, founder["agent_id"]),
                )
                stmt = (
                    "INSERT INTO guild_polls (guild_id, creator_agent_id,"
                    " question, closes_at) VALUES (?, ?, '',"
                    " '2099-01-01T00:00:00.000Z')"
                )
                params: tuple = (gid, founder["agent_id"])
            else:
                stmt = f"INSERT INTO {table} (guild_id, {col}) VALUES (?, '')"
                params = (gid,)
            try:
                conn.execute(stmt, params)
                raise AssertionError(f"empty {table}.{col} accepted")
            except sqlite3.IntegrityError:
                pass


def test_guild_enrollment_default():
    """enrollment defaults to invite_only (open flips explicitly)."""
    founder = _new_agent("gs-enroll")
    with db._conn() as conn:
        gid = _mk_guild(conn, founder["agent_id"], "Enroll Guild")
        row = conn.execute(
            "SELECT enrollment, mission FROM guilds WHERE id = ?", (gid,)
        ).fetchone()
    assert row[0] == "invite_only"
    assert row[1] == "", "mission must default to empty"
    with db._conn() as conn:
        conn.execute("UPDATE guilds SET enrollment = 'open' WHERE id = ?", (gid,))
        try:
            conn.execute("UPDATE guilds SET enrollment = 'public' WHERE id = ?", (gid,))
            raise AssertionError("bad enrollment accepted")
        except sqlite3.IntegrityError:
            pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} guilds-schema tests passed")
