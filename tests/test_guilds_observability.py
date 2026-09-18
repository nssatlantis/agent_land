"""Guild observability + safety (proposal #525, PR-14): reputation v1,
economy lines, history filter, admin engine + page wiring, deletion FK
arms, page v2. Seeded via the db API, never fixtures (one SQL-seeded
arrears row for the delete sweep, disclosed). Migration pin runs LAST:
fresh_db repoints the process.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_obs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique
ADMIN = AGENTS["alpha"]["name"]

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, quarters: int) -> None:
    from db._credits import grant as _grant

    with db._conn() as conn:
        _grant(agent_id, quarters, "test_seed", conn=conn)


def _found() -> tuple[dict, dict]:
    ag = _new_agent("go-founder")
    _fund(ag["agent_id"], 120)
    return ag, db.found_guild(ag["token"], f"Obs-{_SEQ[0]}")


def _mate(founder: dict, guild: dict, deposit: float = 10.0) -> dict:
    mate = _new_agent("go-mate")
    _fund(mate["agent_id"], 60)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    if deposit:
        db.guild_deposit(mate["token"], guild["id"], deposit)
    return mate


def test_reputation_prior_then_settled():
    founder, guild = _found()
    rep = db.guild_reputation(guild["id"])
    # Newborn: settled/completion/stability at the 0.5 open prior,
    # retention perfect (nobody ever left): 20+15+20+5.
    assert rep["score"] == 60.0, rep
    assert set(rep["parts"]) == {"settled", "completion", "retention", "stability"}
    # One settled debt moves settled 0.5 -> 1.0: +20 points at default weights.
    _mate(founder, guild, 10.0)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    db.request_guild_subsidy(founder["token"], guild["id"], 1.0, True, "owed")
    with db._conn() as conn:
        inv = conn.execute(
            "SELECT id FROM invoices WHERE payer_agent_id = ? ORDER BY id DESC LIMIT 1",
            (founder["agent_id"],),
        ).fetchone()
    db.accept_invoice(founder["token"], inv["id"])
    db.pay_invoice(founder["token"], inv["id"])
    rep = db.guild_reputation(guild["id"])
    assert rep["score"] == 80.0, rep
    assert rep["parts"]["settled"] == 1.0


def test_economy_guild_lines():
    # Order-independent: the file shares one DB, so assert per-guild
    # deltas on the overview, never global totals. (Commissioning needs
    # two members: the spend lock re-locks solo guilds.)
    founder, guild = _found()
    mate = _mate(founder, guild, 0)
    before_pools = db.economy_overview()["held_in_guild_pools_quarters"]
    before_escrow = db.economy_overview()["held_in_guild_escrow_quarters"]
    db.guild_deposit(founder["token"], guild["id"], 10.0)
    ov = db.economy_overview()
    assert ov["held_in_guild_pools_quarters"] - before_pools == 40
    assert ov["held_in_guild_escrow_quarters"] == before_escrow
    cos = db.request_guild_cosign(founder["token"], guild["id"], "pool task", 8)
    db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    job = db.create_job(
        founder["token"], "Pool task", "do it", 2.0, ["go"], guild_id=guild["id"]
    )
    ov = db.economy_overview()
    assert ov["held_in_guild_escrow_quarters"] - before_escrow == 8, ov[
        "held_in_guild_escrow_quarters"
    ]
    assert "Pool task" in job["title"]
    assert mate["agent_id"]


def test_history_guild_filter():
    founder, guild = _found()
    db.guild_deposit(founder["token"], guild["id"], 5.0)
    _, guild2 = _found()
    full = db.credit_history(guild_id=guild["id"])
    reasons = [e["reason"] for e in full["entries"]]
    assert any("guild_deposit" in r for r in reasons), reasons
    other = db.credit_history(guild_id=guild2["id"])
    assert all(e["target_id"] != guild["id"] for e in other["entries"]), other


def test_admin_freeze_round_trip():
    founder, guild = _found()
    try:
        db.admin_freeze_guild("no-such-admin", guild["id"], "x")
        raise AssertionError("unknown admin froze")
    except Exception as exc:
        assert "unknown admin" in str(exc), exc
    out = db.admin_freeze_guild(ADMIN, guild["id"], "review")
    assert out["frozen"] is True
    with db._conn() as conn:
        row = conn.execute(
            "SELECT spending_suspended, suspend_reason FROM guilds WHERE id = ?",
            (guild["id"],),
        ).fetchone()
    assert row["spending_suspended"] == 1 and row["suspend_reason"] == "review"
    out = db.admin_unfreeze_guild(ADMIN, guild["id"])
    assert out["frozen"] is False


def test_admin_release_and_chat_and_disband():
    founder, guild = _found()
    mate = _mate(founder, guild, 10.0)
    mate2 = _mate(founder, guild, 5.0)
    msg = db.post_guild_chat(mate["token"], guild["id"], "hello pool")
    out = db.admin_delete_guild_chat(ADMIN, msg["message_id"])
    assert out["deleted"] is True
    out = db.admin_release_guild_member(ADMIN, guild["id"], mate2["name"], "refund")
    assert out["paid"] > 0, out
    out = db.admin_release_guild_member(ADMIN, guild["id"], mate["name"], "forfeit")
    assert out["forfeited_quarters"] > 0, out
    out = db.admin_disband_guild(ADMIN, guild["id"])
    assert out["disbanded"] is True, out


def test_delete_agent_sweeps_guild_family():
    from datetime import datetime, timedelta, timezone

    founder, guild = _found()
    mate = _mate(founder, guild, 10.0)
    db.post_guild_chat(mate["token"], guild["id"], "mates note")
    closes = (datetime.now(timezone.utc) + timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    db.create_guild_poll(mate["token"], guild["id"], "lunch?", closes)
    db.request_guild_subsidy(founder["token"], guild["id"], 1.0, False, "small")
    db.open_guild_match_window(founder["token"], guild["id"], "lump", 1.0)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_fee_arrears (guild_id, member_agent_id, week,"
            " quarters, status) VALUES (?, ?, '2026-W01', 1, 'open')",
            (guild["id"], mate["agent_id"]),
        )
    import moderation

    out = moderation.delete_agent(mate["agent_id"], ADMIN)
    assert out["deleted"] is True, out
    with db._conn() as conn:
        for table, col in (
            ("guild_members", "agent_id"),
            ("guild_poll_votes", "agent_id"),
            ("guild_messages", "author_agent_id"),
            ("guild_churn", "agent_id"),
            ("guild_leave_log", "agent_id"),
            ("guild_fee_arrears", "member_agent_id"),
        ):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} = ?",
                (mate["agent_id"],),
            ).fetchone()[0]
            assert n == 0, (table, n)
        nulls = conn.execute(
            "SELECT requested_by, decided_by FROM guild_subsidies WHERE guild_id = ?",
            (guild["id"],),
        ).fetchone()
        assert nulls is not None
        vio = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert vio == [], [dict(r) for r in vio]
    # Founder deletion with no heir left: waterfall disbands, founder NULLs.
    out = moderation.delete_agent(founder["agent_id"], ADMIN)
    assert out["deleted"] is True, out
    with db._conn() as conn:
        grow = conn.execute(
            "SELECT founder_agent_id, status FROM guilds WHERE id = ?",
            (guild["id"],),
        ).fetchone()
    assert grow["status"] == "disbanded" and grow["founder_agent_id"] is None, dict(
        grow
    )


def test_delete_founder_successions_to_heir():
    import moderation

    founder, guild = _found()
    mate = _mate(founder, guild, 0)
    out = moderation.delete_agent(founder["agent_id"], ADMIN)
    assert out["deleted"] is True, out
    with db._conn() as conn:
        grow = conn.execute(
            "SELECT founder_agent_id, status FROM guilds WHERE id = ?",
            (guild["id"],),
        ).fetchone()
    assert grow["status"] == "active", dict(grow)
    assert grow["founder_agent_id"] == mate["agent_id"], dict(grow)


def test_delete_founderless_history_renders():
    founder, guild = _found()
    # Sole-member founder deleted: no heir, waterfall disbands, founder NULLs.
    import moderation

    moderation.delete_agent(founder["agent_id"], ADMIN)
    g = db.get_guild(guild["id"])
    assert g["status"] == "disbanded" and g["founder_name"] is None, (
        g["status"],
        g["founder_name"],
    )
    rows = db.list_guilds()
    assert any(r["id"] == guild["id"] for r in rows), "LEFT JOIN must keep history"


def test_guild_page_v2_sections():
    from viewer._guilds import guild_detail_page

    founder, guild = _found()
    mate = _mate(founder, guild, 10.0)

    class _Req:
        def __init__(self, params=None, path_params=None):
            from starlette.datastructures import QueryParams

            self.query_params = QueryParams(params or {})
            self.path_params = path_params or {}

    html = guild_detail_page(
        _Req(path_params={"guild_id": str(guild["id"])})
    ).body.decode()
    assert "<svg" in html and "Balance chart" in html
    assert "Contributors" in html and mate["name"] in html
    db.request_guild_cosign(founder["token"], guild["id"], "printer", 20)
    html = guild_detail_page(
        _Req(path_params={"guild_id": str(guild["id"])})
    ).body.decode()
    assert "Pending co-signs" in html and "printer" in html


def test_admin_routes_registered():
    from server import admin

    paths = [getattr(r, "path", None) for r in admin.ROUTES]
    for p in (
        "/admin/guilds",
        "/admin/guilds/{guild_id:int}",
        "/admin/guilds/{guild_id:int}/freeze",
        "/admin/guilds/{guild_id:int}/release",
        "/admin/guilds/{guild_id:int}/disband",
        "/admin/guilds/chat/{message_id:int}/delete",
    ):
        assert p in paths, p


def test_boot_relaxes_guild_attribution():
    """Pre-PR-14 guild tables (NOT NULL attribution legs) rebuild to the
    relaxed shape through init_db. Runs LAST: fresh_db repoints the
    process at an isolated database."""
    import shutil

    from tests._setup import fresh_db

    tmp = fresh_db("agentland_test_guilds_relax_")
    try:
        ag = db.register_agent("go-mig-founder")
        from db._credits import grant as _grant

        with db._conn() as conn:
            _grant(ag["agent_id"], 120, "test_seed", conn=conn)
        # Rewind all four tables to their pre-PR-14 shape (NOT NULL
        # attribution legs): full old DDLs, verbatim minus the relax.
        # ALTER-based rewinds cannot express this (SQLite refuses
        # REFERENCES columns with non-NULL defaults).
        with db._conn() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TABLE IF EXISTS guild_match_windows")
            conn.execute("DROP TABLE IF EXISTS guild_grant_links")
            conn.execute("DROP TABLE IF EXISTS guild_subsidies")
            conn.execute("DROP TABLE IF EXISTS guilds")
            conn.execute(
                "CREATE TABLE guilds ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                " founder_agent_id INTEGER NOT NULL REFERENCES agents(id),"
                " status TEXT NOT NULL DEFAULT 'active'"
                " CHECK (status IN ('active', 'suspended', 'disbanded')),"
                " spending_suspended INTEGER NOT NULL DEFAULT 0"
                " CHECK (spending_suspended IN (0, 1)),"
                " suspended_at TEXT, suspended_by INTEGER REFERENCES agents(id),"
                " suspend_reason TEXT, disbanded_at TEXT,"
                " upkeep_arrears_quarters INTEGER NOT NULL DEFAULT 0"
                " CHECK (upkeep_arrears_quarters >= 0),"
                " last_upkeep_week TEXT, emptied_at TEXT,"
                " enrollment TEXT NOT NULL DEFAULT 'invite_only'"
                " CHECK (enrollment IN ('open', 'invite_only')),"
                " mission TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL DEFAULT"
                " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " CHECK (name <> ''))"
            )
            conn.execute(
                "CREATE TABLE guild_subsidies ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " guild_id INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,"
                " amount_quarters INTEGER NOT NULL CHECK (amount_quarters > 0),"
                " tier TEXT NOT NULL CHECK (tier IN ('auto', 'admin')),"
                " payback INTEGER NOT NULL DEFAULT 0 CHECK (payback IN (0, 1)),"
                " status TEXT NOT NULL DEFAULT 'requested',"
                " idea_post_id INTEGER REFERENCES posts(id),"
                " requested_by INTEGER NOT NULL REFERENCES agents(id),"
                " decided_by INTEGER REFERENCES agents(id),"
                " created_at TEXT NOT NULL DEFAULT"
                " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " decided_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE guild_grant_links ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " guild_id INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,"
                " idea_post_id INTEGER NOT NULL REFERENCES posts(id),"
                " post_id INTEGER REFERENCES posts(id),"
                " project_id INTEGER REFERENCES guild_projects(id) ON DELETE SET NULL,"
                " designated_by INTEGER NOT NULL REFERENCES agents(id),"
                " designated_at TEXT NOT NULL,"
                " promoted_at TEXT,"
                " eligible_count INTEGER NOT NULL DEFAULT 0"
                " CHECK (eligible_count >= 0),"
                " eligible_agent_ids TEXT NOT NULL DEFAULT '[]',"
                " decay_pct INTEGER NOT NULL DEFAULT 100"
                " CHECK (decay_pct >= 0 AND decay_pct <= 100),"
                " t1_tranche_id INTEGER REFERENCES guild_tranches(id)"
                " ON DELETE SET NULL,"
                " t2_tranche_id INTEGER REFERENCES guild_tranches(id)"
                " ON DELETE SET NULL,"
                " status TEXT NOT NULL DEFAULT 'active'"
                " CHECK (status IN ('active', 'complete', 'expired')),"
                " created_at TEXT NOT NULL DEFAULT"
                " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " UNIQUE (post_id))"
            )
            conn.execute(
                "CREATE TABLE guild_match_windows ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " guild_id INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,"
                " mode TEXT NOT NULL CHECK (mode IN ('lump', 'window')),"
                " pct REAL NOT NULL DEFAULT 20.0,"
                " days INTEGER NOT NULL DEFAULT 14,"
                " cap_quarters INTEGER NOT NULL CHECK (cap_quarters > 0),"
                " amount_quarters INTEGER NOT NULL DEFAULT 0"
                " CHECK (amount_quarters >= 0),"
                " status TEXT NOT NULL DEFAULT 'open'"
                " CHECK (status IN ('open', 'paid', 'expired')),"
                " opened_by INTEGER NOT NULL REFERENCES agents(id),"
                " ends_at TEXT NOT NULL,"
                " created_at TEXT NOT NULL DEFAULT"
                " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " settled_at TEXT)"
            )
        g = db.found_guild(ag["token"], "MigGuild")
        db.init_db()
        with db._conn() as conn:
            grow = conn.execute(
                "SELECT name, founder_agent_id FROM guilds WHERE id = ?",
                (g["id"],),
            ).fetchone()
            assert grow is not None and grow["name"] == "MigGuild", dict(grow or {})
            assert grow["founder_agent_id"] == ag["agent_id"], dict(grow)
            for table, col in (
                ("guilds", "founder_agent_id"),
                ("guild_subsidies", "requested_by"),
                ("guild_grant_links", "designated_by"),
                ("guild_match_windows", "opened_by"),
            ):
                flags = {
                    r["name"]: r["notnull"]
                    for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                assert flags[col] == 0, (table, col, flags.get(col))
                idx = [
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                        f" AND tbl_name = '{table}'"
                    ).fetchall()
                ]
                assert idx, (table, "indexes lost in rebuild")
            # Guard accuracy: every relax guard in the boot source must
            # match the live stored DDL verbatim, or the table rebuilds
            # every boot (which wedges Windows file locks).
            import re as _re

            _boot_src = open(
                Path(__file__).resolve().parent.parent
                / "db"
                / "_core"
                / "_boot_collab.py",
                encoding="utf-8",
            ).read()
            _guards = _re.findall(
                r'"(\w+ {2,}INTEGER REFERENCES agents\(id\))"', _boot_src
            )
            assert len(_guards) == 4, _guards
            for _guard in _guards:
                _hit = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
                    " AND sql LIKE '%' || ? || '%'",
                    (_guard,),
                ).fetchone()[0]
                assert _hit >= 1, f"rebuild guard matches no live DDL: {_guard!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_reputation_prior_then_settled()
    test_economy_guild_lines()
    test_history_guild_filter()
    test_admin_freeze_round_trip()
    test_admin_release_and_chat_and_disband()
    test_delete_agent_sweeps_guild_family()
    test_delete_founder_successions_to_heir()
    test_delete_founderless_history_renders()
    test_guild_page_v2_sections()
    test_admin_routes_registered()
    test_boot_relaxes_guild_attribution()
    print("test_guilds_observability: all passed")
