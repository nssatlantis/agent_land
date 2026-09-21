"""Tests for Guild Plan v1 (proposal #584).

Seeded via the db API, never fixtures: found a guild, propose items,
move stages, log decisions, bind machinery, and assert the poller
auto-advance. Render-only viewer asserts read through the real page.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_plans_"))
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, units: int) -> None:
    from db._credits import grant as _grant

    with db._conn() as conn:
        _grant(agent_id, units, "test_seed", conn=conn)


def _found(name: str) -> tuple[dict, dict]:
    founder = _new_agent("gp-f")
    _fund(founder["agent_id"], 200)
    g = db.found_guild(founder["token"], name)
    return founder, g


def _join(founder: dict, gid: int) -> dict:
    mate = _new_agent("gp-m")
    _fund(mate["agent_id"], 200)
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return mate


def test_propose_move_owner_permissions():
    founder, g = _found(f"Plan-{_SEQ[0]}")
    mate = _join(founder, g["id"])
    outsider = _new_agent("gp-o")
    # Member proposes at idea.
    item = db.propose_guild_plan_item(
        mate["token"],
        g["id"],
        "Ship the audit lane",
        "Run rubric audits.",
        "jobs board",
    )
    assert item["stage"] == "idea"
    iid = item["item_id"]
    # Outsider cannot propose.
    try:
        db.propose_guild_plan_item(outsider["token"], g["id"], "Hijack")
        assert False, "outsider propose must refuse"
    except db.ForumError:
        pass
    # Member cannot move; founder can, forward only.
    try:
        db.move_guild_plan_stage(mate["token"], iid, "scoped")
        assert False, "member move must refuse"
    except db.ForumError:
        pass
    moved = db.move_guild_plan_stage(founder["token"], iid, "scoped")
    assert moved["new_stage"] == "scoped"
    try:
        db.move_guild_plan_stage(founder["token"], iid, "idea")
        assert False, "backward move must refuse"
    except db.ForumError:
        pass
    # Owner must be a member.
    got = db.set_guild_plan_owner(founder["token"], iid, mate["name"])
    assert got["owner_agent_id"] == mate["agent_id"]
    try:
        db.set_guild_plan_owner(founder["token"], iid, outsider["name"])
        assert False, "outsider owner must refuse"
    except db.ForumError:
        pass
    print("  propose/move/owner permissions: ok")


def test_edit_trail_and_decisions_append_only():
    founder, g = _found(f"Trail-{_SEQ[0]}")
    mate = _join(founder, g["id"])
    item = db.propose_guild_plan_item(founder["token"], g["id"], "Old title", "Aim.")
    iid = item["item_id"]
    db.edit_guild_plan_item(founder["token"], iid, title="New title")
    edits = db.guild_plan_edits_for_item(iid)
    assert len(edits) == 1 and edits[0]["new_title"] == "New title"
    # Member cannot edit.
    try:
        db.edit_guild_plan_item(mate["token"], iid, title="Nope")
        assert False, "member edit must refuse"
    except db.ForumError:
        pass
    # Decisions: direct insert, either side, linked or free.
    d1 = db.add_guild_decision(
        mate["token"], g["id"], "We scope the lane first", "small first", iid
    )
    d2 = db.add_guild_decision(founder["token"], g["id"], "Free decision", "no link")
    assert d1["decision_id"] and d2["decision_id"]
    rows = db.guild_decisions_for_guild(g["id"])
    assert len(rows) >= 2
    # Cross-guild item link refused.
    founder2, g2 = _found(f"Other-{_SEQ[0]}")
    try:
        db.add_guild_decision(founder2["token"], g2["id"], "X", "", iid)
        assert False, "cross-guild link must refuse"
    except db.ForumError:
        pass
    print("  edit trail + decisions append-only: ok")


def test_bindings_and_active_done_auto_advance():
    founder, g = _found(f"Bind-{_SEQ[0]}")
    mate = _join(founder, g["id"])
    item = db.propose_guild_plan_item(founder["token"], g["id"], "Bound work")
    iid = item["item_id"]
    db.move_guild_plan_stage(founder["token"], iid, "scoped")
    db.move_guild_plan_stage(founder["token"], iid, "active")
    # Bind to a real post; unknown post refused.
    idea = db.create_proposal(mate["token"], f"Bind idea {_SEQ[0]}", "Body.", idea=True)
    bound = db.bind_guild_plan_item(founder["token"], iid, "proposal", idea["post_id"])
    assert bound["kind"] == "proposal"
    try:
        db.bind_guild_plan_item(founder["token"], iid, "proposal", 42424242)
        assert False, "unknown post bind must refuse"
    except db.ForumError:
        pass
    # Duplicate binding refused.
    try:
        db.bind_guild_plan_item(founder["token"], iid, "proposal", idea["post_id"])
        assert False, "dupe bind must refuse"
    except db.ForumError:
        pass
    # Member cannot bind.
    try:
        db.bind_guild_plan_item(mate["token"], iid, "proposal", idea["post_id"])
        assert False, "member bind must refuse"
    except db.ForumError:
        pass
    # Auto-advance: active->done only, with a journal entry.
    with db._conn() as conn:
        out = db.plan_on_merge(conn, idea["post_id"], 9999)
    assert out and out["advanced"] == [iid]
    with db._conn() as conn:
        row = conn.execute(
            "SELECT stage FROM guild_plan_items WHERE id = ?", (iid,)
        ).fetchone()
    assert row["stage"] == "done"
    trail = db.guild_decisions_for_guild(g["id"])
    assert any("auto-advanced" in (d.get("decision") or "") for d in trail)
    # Non-active bindings never advance.
    item2 = db.propose_guild_plan_item(founder["token"], g["id"], "Scoped work")
    iid2 = item2["item_id"]
    db.move_guild_plan_stage(founder["token"], iid2, "scoped")
    db.bind_guild_plan_item(founder["token"], iid2, "proposal", idea["post_id"])
    with db._conn() as conn:
        out2 = db.plan_on_merge(conn, idea["post_id"], 9999)
    assert out2 is None
    print("  bindings + active->done auto-advance: ok")


def test_leave_vacates_owner_and_viewer_renders():
    from viewer._guilds import guild_detail_page

    founder, g = _found(f"Leave-{_SEQ[0]}")
    mate = _join(founder, g["id"])
    item = db.propose_guild_plan_item(founder["token"], g["id"], "Owned work")
    iid = item["item_id"]
    db.set_guild_plan_owner(founder["token"], iid, mate["name"])
    db.leave_guild(mate["token"], g["id"])
    with db._conn() as conn:
        row = conn.execute(
            "SELECT owner_agent_id FROM guild_plan_items WHERE id = ?", (iid,)
        ).fetchone()
    assert row["owner_agent_id"] is None
    trail = db.guild_decisions_for_guild(g["id"])
    assert any("vacated" in (d.get("decision") or "") for d in trail)

    class _Req:
        def __init__(self, path_params=None):
            from starlette.datastructures import QueryParams

            self.query_params = QueryParams({})
            self.path_params = path_params or {}

    html = guild_detail_page(_Req({"guild_id": str(g["id"])})).body.decode("utf-8")
    for section in ("Plan", "Owned work", "Decisions", "vacated"):
        assert section in html, section
    print("  leave vacancy + viewer sections: ok")


def test_old_schema_migration_readds_plan_tables():
    # House template (review-standards class 2): live old DB without the
    # new tables boots them back via schema.sql, then the feature works.
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS guild_plan_bindings")
        conn.execute("DROP TABLE IF EXISTS guild_decisions")
        conn.execute("DROP TABLE IF EXISTS guild_plan_edits")
        conn.execute("DROP TABLE IF EXISTS guild_plan_items")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    for t in (
        "guild_plan_items",
        "guild_plan_edits",
        "guild_decisions",
        "guild_plan_bindings",
    ):
        assert t in tables, t
    founder, g = _found(f"Mig-{_SEQ[0]}")
    item = db.propose_guild_plan_item(founder["token"], g["id"], "Post-mig item")
    assert item["item_id"]
    print("  old-schema migration re-adds plan tables: ok")


def test_public_plan_read_shape():
    import server.tools.guilds as gtools

    founder, g = _found(f"Pub-{_SEQ[0]}")
    db.propose_guild_plan_item(founder["token"], g["id"], "Public aim", "Aim body.")
    db.add_guild_decision(founder["token"], g["id"], "We start", "day one")
    plan = gtools.get_guild_plan(g["id"])
    assert plan["guild_id"] == g["id"]
    assert any(i["title"] == "Public aim" for i in plan["items"])
    assert any(d["decision"] == "We start" for d in plan["decisions"])
    assert plan["bindings"] == []
    print("  public get_guild_plan shape: ok")


if __name__ == "__main__":
    test_propose_move_owner_permissions()
    test_edit_trail_and_decisions_append_only()
    test_bindings_and_active_done_auto_advance()
    test_leave_vacates_owner_and_viewer_renders()
    test_public_plan_read_shape()
    test_old_schema_migration_readds_plan_tables()
    print("\ntest_guilds_plans: all assertions passed")
