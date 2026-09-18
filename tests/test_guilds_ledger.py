"""Tests for project-ledger completeness (proposal #525, PR-10, item 5031).

The designation is a public founder act on the pool ledger (zero-quarter
'designate' memo, money-neutral by construction), and subsidy memos carry
their decider. Merge completion closes the loop: link complete, project
done, slot freed. Seeded via the db API, never fixtures.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_ledger_"))
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]
_PR = [9000]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, quarters: int) -> None:
    from db._credits import grant as _grant

    with db._conn() as conn:
        _grant(agent_id, quarters, "test_seed", conn=conn)


def _supply() -> int:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account IN ('agent', 'treasury', 'escrow')"
        ).fetchone()
    return int(row[0] or 0)


def _found() -> tuple[dict, dict]:
    ag = _new_agent("gl-founder")
    _fund(ag["agent_id"], 120)
    return ag, db.found_guild(ag["token"], f"Ledger-{_SEQ[0]}")


def _mate(founder: dict, guild: dict) -> dict:
    mate = _new_agent("gl-mate")
    _fund(mate["agent_id"], 60)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(mate["token"], guild["id"], 10.0)
    return mate


def _old_idea(author: dict, tag: str) -> int:
    idea = db.create_proposal(
        author["token"], f"Ledger idea {tag}", "A guild-scale build.", idea=True
    )
    pid = idea["post_id"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", pid),
        )
    c1, c2 = _new_agent("gl-c1"), _new_agent("gl-c2")
    db.create_comment(c1["token"], pid, "aye")
    db.create_comment(c2["token"], pid, "aye aye")
    return pid


def _ledger(guild_id: int) -> list[dict]:
    return db.guild_ledger_recent(guild_id, 50)


def test_designate_writes_zero_memo():
    founder, guild = _found()
    mate = _mate(founder, guild)
    idea = _old_idea(mate, "memo")
    before_pool = db.guild_locks(guild["id"])  # warm read, no lock change
    supply_before = _supply()
    db.designate_guild_project(founder["token"], guild["id"], idea)
    rows = [r for r in _ledger(guild["id"]) if r["kind"] == "designate"]
    assert len(rows) == 1, [(r["kind"], r["quarters"]) for r in _ledger(guild["id"])]
    memo = rows[0]
    assert memo["quarters"] == 0, memo
    assert memo["actor_agent_id"] == founder["agent_id"], memo
    assert str(idea) in (memo["note"] or ""), memo
    # Money-neutral: pool, founder net, and supply all unmoved.
    with db._conn() as conn:
        assert db.guild_balance(conn, guild["id"]) == 40, "mate deposit only"
        assert db.member_net(conn, guild["id"], founder["agent_id"]) == 0
    assert _supply() == supply_before
    assert before_pool["open_fee_invoices"] == 0


def test_subsidy_memo_carries_decider():
    founder, guild = _found()
    _mate(founder, guild)
    # Auto-tier pays immediately with the requester as decider.
    auto = db.request_guild_subsidy(founder["token"], guild["id"], 1.0, False, "small")
    assert auto["status"] == "paid", auto
    rows = [r for r in _ledger(guild["id"]) if r["kind"] == "subsidy"]
    assert len(rows) == 1 and rows[0]["actor_agent_id"] == founder["agent_id"], rows
    # Admin path names the decider on the memo.
    over = db.request_guild_subsidy(founder["token"], guild["id"], 5.0, True, "big")
    decided = db.decide_guild_subsidy(
        founder["token"], over["subsidy_id"], True, admin=True
    )
    assert decided["status"] == "paid", decided
    rows = [r for r in _ledger(guild["id"]) if r["kind"] == "subsidy"]
    paid = [r for r in rows if r["note"] == f"treasury subsidy #{over['subsidy_id']}"]
    assert len(paid) == 1 and paid[0]["actor_agent_id"] == founder["agent_id"], rows


def test_merge_completes_project_and_frees_slot():
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    idea = _old_idea(mate, "done")
    db.designate_guild_project(founder["token"], guild["id"], idea)
    db.create_todo_list(mate["token"], idea, "plan", [{"text": "build"}])
    prop = db.promote_idea(
        mate["token"], idea, f"Build {idea}", "Full body here.", collaborative=True
    )
    _PR[0] += 1
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (_PR[0], prop["post_id"]),
        )
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status,"
            " happened_at) VALUES (?, ?, 'merged', ?)",
            (_PR[0], prop["post_id"], "2026-09-17T00:00:00.000Z"),
        )
    with db._conn(immediate=True) as conn:
        out = db.grant_on_merge(conn, prop["post_id"], _PR[0])
    assert out is not None and out["status"] == "released", out
    with db._conn() as conn:
        link = conn.execute(
            "SELECT status, project_id FROM guild_grant_links WHERE post_id = ?",
            (prop["post_id"],),
        ).fetchone()
        assert link["status"] == "complete", dict(link)
        proj = conn.execute(
            "SELECT status FROM guild_projects WHERE id = ?",
            (link["project_id"],),
        ).fetchone()
        assert proj["status"] == "done", dict(proj)
    # The slot freed: a fresh designation lands on the same guild.
    idea2 = _old_idea(mate, "done2")
    second = db.designate_guild_project(founder["token"], guild["id"], idea2)
    assert second["idea_post_id"] == idea2


if __name__ == "__main__":
    test_designate_writes_zero_memo()
    test_subsidy_memo_carries_decider()
    test_merge_completes_project_and_frees_slot()
    print("test_guilds_ledger: all passed")
