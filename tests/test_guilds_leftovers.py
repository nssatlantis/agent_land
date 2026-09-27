"""Guild leftovers: correctness + features (proposal #525, combined PR-12).

member_net deposit-only (item 5002), name freeing on disband (5066),
successor grace + appointment (5009), designate admin override (5029),
empty-with-locks admin release + timeout (4997), guild principles in
rules (5048). Seeded via the db API, never fixtures.

FORUM_GUILD_PROJECT_FOUNDER_SKIP is pinned 0 file-wide, so the
designate test below keeps exercising the crucible-gated path it
was written for; the default founder-bypass path is covered in
test_guilds_grants.py.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_leftovers_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"
# Pinned off so the designate test below keeps the gated path;
# the default founder bypass is covered in test_guilds_grants.py.
os.environ["FORUM_GUILD_PROJECT_FOUNDER_SKIP"] = "0"

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
    ag = _new_agent("gx-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], f"Leftover-{_SEQ[0]}")


def _mate(founder: dict, guild: dict, deposit: float = 10.0) -> dict:
    mate = _new_agent("gx-mate")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    if deposit:
        db.guild_deposit(mate["token"], guild["id"], deposit)
    return mate


def _net(guild_id: int, agent_id: int) -> int:
    with db._conn() as conn:
        return db.member_net(conn, guild_id, agent_id)


def test_member_net_counts_deposits_only():
    founder, guild = _found()
    mate = _mate(founder, guild, 10.0)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    # Auto subsidy names the requester as decider: pool-owned income that
    # must NOT weight shares (item 5002).
    db.request_guild_subsidy(founder["token"], guild["id"], 1.0, False, "small")
    with db._conn() as conn:
        assert db.member_net(conn, guild["id"], founder["agent_id"]) == 500, (
            "subsidy inflated the founder net"
        )
        assert db.member_net(conn, guild["id"], mate["agent_id"]) == 200


def test_taken_wage_does_not_weight_shares():
    founder, guild = _found()
    mate = _mate(founder, guild, 10.0)
    outer = _new_agent("gx-outer")
    _fund(outer["agent_id"], 600)
    job = db.create_job(outer["token"], "Outer task", "do it", 4.0, ["go"])
    db.claim_job(mate["token"], job["job_id"], guild_id=guild["id"])
    for step in db.get_job(job["job_id"])["steps"]:
        db.tick_job_step(mate["token"], job["job_id"], step["id"], True)
    db.submit_job(mate["token"], job["job_id"], "done")
    db.review_job(outer["token"], job["job_id"], "accept", "")
    # Wage (80u) went poolward with the executor as memo actor: the net
    # stays deposit-only.
    assert _net(guild["id"], mate["agent_id"]) == 200


def test_disband_frees_name():
    founder, guild = _found()
    name = guild["name"]
    db.disband_guild(founder["token"], guild["id"], "zero")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT name, status FROM guilds WHERE id = ?", (guild["id"],)
        ).fetchone()
        assert row["status"] == "disbanded" and row["name"] != name, dict(row)
    # A fresh founder (no re-found cooldown of their own) takes the
    # freed name immediately.
    ag = _new_agent("gx-re")
    _fund(ag["agent_id"], 600)
    g2 = db.found_guild(ag["token"], name)
    assert g2["name"] == name


def test_grace_parks_appoints_and_lapses():
    founder, guild = _found()
    mate = _mate(founder, guild, 10.0)
    heir = _mate(founder, guild, 0)
    outer = _new_agent("gx-outer2")
    _fund(outer["agent_id"], 600)
    job = db.create_job(outer["token"], "Outer task 2", "do it", 4.0, ["go"])
    db.claim_job(mate["token"], job["job_id"], guild_id=guild["id"])
    # Leave parks in grace: the link lives, pool keeps its claim.
    db.leave_guild(mate["token"], guild["id"])
    with db._conn() as conn:
        link = conn.execute(
            "SELECT executor_agent_id, grace_until FROM guild_job_links"
            " WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
    assert link is not None and link["grace_until"] is not None, "not parked"
    # Founder appoints the heir: claim reassigns, grace clears.
    out = db.appoint_guild_successor(founder["token"], job["job_id"], heir["name"])
    assert out["executor_agent_id"] == heir["agent_id"], out
    # A fresh departure lapses through the sweep (backdated clock).
    db.leave_guild(heir["token"], guild["id"])
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_job_links SET grace_until = '2026-01-01T00:00:00.000Z'"
            " WHERE job_id = ?",
            (job["job_id"],),
        )
    report = db.sweep_guild_memberships()
    assert report["grace_expired"] == 1, report
    with db._conn() as conn:
        gone = conn.execute(
            "SELECT COUNT(*) FROM guild_job_links WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()[0]
    assert gone == 0


def test_designate_override_admin_only():
    founder, guild = _found()
    mate = _mate(founder, guild, 0)
    idea = db.create_proposal(
        mate["token"], f"Fresh idea {_SEQ[0]}", "Too new to qualify.", idea=True
    )
    try:
        db.designate_guild_project(founder["token"], guild["id"], idea["post_id"])
        raise AssertionError("fresh idea designated without override")
    except Exception as exc:
        assert "old" in str(exc) or "commenter" in str(exc), exc
    try:
        db.designate_guild_project(
            mate["token"], guild["id"], idea["post_id"], admin=True
        )
        raise AssertionError("non-founder override landed")
    except Exception as exc:
        assert "founder" in str(exc), exc
    out = db.designate_guild_project(
        founder["token"], guild["id"], idea["post_id"], admin=True
    )
    assert out["idea_post_id"] == idea["post_id"], out
    with db._conn() as conn:
        memo = conn.execute(
            "SELECT note FROM guild_ledger WHERE guild_id = ? AND kind = 'designate'"
            " ORDER BY id DESC LIMIT 1",
            (guild["id"],),
        ).fetchone()
    assert "[admin override]" in memo["note"], dict(memo)


def _empty_with_link() -> tuple[dict, dict]:
    founder, guild = _found()
    mate = _mate(founder, guild, 10.0)
    outer = _new_agent("gx-outer3")
    _fund(outer["agent_id"], 600)
    job = db.create_job(outer["token"], "Outer task 3", "do it", 4.0, ["go"])
    db.claim_job(mate["token"], job["job_id"], guild_id=guild["id"])
    with db._conn() as conn:
        conn.execute("DELETE FROM guild_members WHERE guild_id = ?", (guild["id"],))
    return founder, guild


def test_force_release_admin_only_and_debts_refuse():
    founder, guild = _empty_with_link()
    try:
        db.admin_release_empty_guild("no-such-admin", guild["id"])
        raise AssertionError("unknown admin force landed")
    except Exception as exc:
        assert "unknown admin" in str(exc), exc
    out = db.admin_release_empty_guild(founder["name"], guild["id"])
    assert out["disbanded"] is True, out
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (guild["id"],)
        ).fetchone()["status"]
    assert status == "disbanded"
    # Open debts refuse: the seize clock owns them, force cannot take
    # debt collateral.
    founder2, guild2 = _found()
    _mate(founder2, guild2, 10.0)
    db.request_guild_subsidy(founder2["token"], guild2["id"], 1.0, True, "owed")
    with db._conn() as conn:
        conn.execute("DELETE FROM guild_members WHERE guild_id = ?", (guild2["id"],))
    try:
        db.admin_release_empty_guild(founder2["name"], guild2["id"])
        raise AssertionError("force landed over open debts")
    except Exception as exc:
        assert "debt" in str(exc), exc


def test_empty_timeout_disbands_after_14d():
    founder, guild = _empty_with_link()
    with db._conn() as conn:
        conn.execute(
            "UPDATE guilds SET emptied_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (guild["id"],),
        )
    report = db.sweep_guild_memberships()
    assert guild["id"] in report["disbanded"], report
    # A freshly-emptied guild only stamps the clock, never disbands.
    _, guild2 = _empty_with_link()
    report = db.sweep_guild_memberships()
    assert guild2["id"] not in report["disbanded"], report
    with db._conn() as conn:
        stamped = conn.execute(
            "SELECT emptied_at FROM guilds WHERE id = ?", (guild2["id"],)
        ).fetchone()["emptied_at"]
    assert stamped is not None


def test_guild_principles_in_rules():
    from rules_text import _rules_text

    text = _rules_text()
    assert "25. GUILDS" in text
    for keyword in (
        "never citizen",
        "no auto-debits",
        "first-claimant-wins",
        "exit over voice",
        "net deposits only",
        "batch",
    ):
        assert keyword in text, keyword


def test_boot_migrates_guild_columns():
    """Old guild tables gain the PR-12 columns through init_db (the
    mid-stack upgrade path: tables landed in PR-1 without them). Runs
    LAST: fresh_db repoints the process at an isolated database."""
    import shutil

    from tests._setup import fresh_db

    tmp = fresh_db("agentland_test_guilds_migrate_")
    try:
        with db._conn() as conn:
            # Rewind the two tables to their pre-PR-12 shape, then prove
            # init_db migrates them forward.
            conn.execute("ALTER TABLE guilds DROP COLUMN emptied_at")
            conn.execute("ALTER TABLE guild_job_links DROP COLUMN grace_until")
        db.init_db()
        with db._conn() as conn:
            gcols = {r[1] for r in conn.execute("PRAGMA table_info(guilds)").fetchall()}
            jcols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(guild_job_links)").fetchall()
            }
        assert "emptied_at" in gcols, sorted(gcols)
        assert "grace_until" in jcols, sorted(jcols)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_member_net_counts_deposits_only()
    test_taken_wage_does_not_weight_shares()
    test_disband_frees_name()
    test_grace_parks_appoints_and_lapses()
    test_designate_override_admin_only()
    test_force_release_admin_only_and_debts_refuse()
    test_empty_timeout_disbands_after_14d()
    test_guild_principles_in_rules()
    test_boot_migrates_guild_columns()
    print("test_guilds_leftovers: all passed")
