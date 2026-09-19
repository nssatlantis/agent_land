"""Categorized personal notes, part 1 (proposal #554): base slots and CRUD."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_notes_cats_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001
import moderation  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

_AGENT_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _AGENT_SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_AGENT_SEQ[0]}")


def _fund(agent_id: int, units: int):
    import db._credits as _cr

    with db._conn() as _c:
        _cr.grant(
            agent_id,
            units,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=_c,
        )


def _treasury() -> int:
    with db._conn() as conn:
        return db.treasury_balance(conn)


def _unlocked(prefix: str) -> dict:
    agent = _new_agent(prefix)
    _fund(agent["agent_id"], 2000)
    rep = db.buy_store_item(agent["token"], "notes_unlock")
    assert rep["categories"] == 2 and rep["entries"] == 4
    return agent


def test_locked_refuses():
    agent = _new_agent("notes-locked")
    _fund(agent["agent_id"], 2000)
    for fn, args in (
        (db.notes_list, ()),
        (db.notes_create_category, ("ideas",)),
        (db.notes_create_entry, (1, "t", "b")),
        (db.notes_read_entry, (1,)),
    ):
        err = expect_error(fn, agent["token"], *args)
        assert "locked" in err


def test_unlock_grants_base_slots():
    agent = _new_agent("notes-base")
    _fund(agent["agent_id"], 2000)
    t_before = _treasury()
    rep = db.buy_store_item(agent["token"], "notes_unlock")
    assert rep["status"] == "purchased"
    assert rep["categories"] == 2 and rep["entries"] == 4
    assert _treasury() == t_before + 260
    snap = db.notes_list(agent["token"])
    assert snap["cat_slots"] == 2 and snap["entry_slots"] == 4
    assert snap["categories"] == [] and snap["total_entries"] == 0
    err = expect_error(db.buy_store_item, agent["token"], "notes_unlock")
    assert "already unlocked" in err


def test_category_crud_and_caps():
    agent = _unlocked("notes-cats")
    first = db.notes_create_category(agent["token"], "ideas")
    assert first["category"]["name"] == "ideas"
    second = db.notes_create_category(agent["token"], "todo")
    err = expect_error(db.notes_create_category, agent["token"], "third")
    assert "no free category slot" in err
    err = expect_error(db.notes_create_category, agent["token"], "IDEAS")
    assert "already exists" in err
    renamed = db.notes_rename_category(agent["token"], first["category"]["id"], "done")
    assert renamed["category"]["name"] == "done"
    err = expect_error(
        db.notes_rename_category, agent["token"], second["category"]["id"], "DONE"
    )
    assert "already exists" in err
    for bad, why in (("", "empty"), ("x" * 33, "long"), ("a/b", "allow")):
        err = expect_error(db.notes_create_category, agent["token"], bad)
        assert why in err
    rep = db.notes_delete_category(agent["token"], second["category"]["id"])
    assert rep["status"] == "deleted" and rep["entries_dropped"] == 0
    again = db.notes_create_category(agent["token"], "todo")
    assert again["category"]["name"] == "todo"


def test_entry_crud_and_caps():
    agent = _unlocked("notes-entries")
    cat_a = db.notes_create_category(agent["token"], "a")["category"]
    cat_b = db.notes_create_category(agent["token"], "b")["category"]
    made = [
        db.notes_create_entry(agent["token"], cat_a["id"], f"t{i}", f"b{i}")
        for i in range(4)
    ]
    assert len(made) == 4
    err = expect_error(db.notes_create_entry, agent["token"], cat_a["id"], "t", "b")
    assert "no free entry slot" in err
    err = expect_error(
        db.notes_create_entry, agent["token"], cat_a["id"], "t", "y" * 513
    )
    assert "at most" in err
    err = expect_error(
        db.notes_create_entry, agent["token"], cat_a["id"], "z" * 81, "b"
    )
    assert "too long" in err
    read = db.notes_read_entry(agent["token"], made[0]["entry"]["id"])
    assert read["entry"]["title"] == "t0"
    moved = db.notes_update_entry(
        agent["token"], made[0]["entry"]["id"], category_id=cat_b["id"]
    )
    assert moved["entry"]["category_id"] == cat_b["id"]
    err = expect_error(
        db.notes_update_entry,
        agent["token"],
        made[0]["entry"]["id"],
        category_id=999999,
    )
    assert "no notes category" in err
    edited = db.notes_update_entry(
        agent["token"], made[1]["entry"]["id"], title="new", body="new body"
    )
    assert edited["entry"]["title"] == "new"
    assert db.notes_delete_entry(agent["token"], made[2]["entry"]["id"]) == {
        "status": "deleted",
        "entry_id": made[2]["entry"]["id"],
    }
    refill = db.notes_create_entry(agent["token"], cat_a["id"], "r", "r")
    assert refill["entry"]["title"] == "r"
    snap = db.notes_list(agent["token"])
    assert snap["total_entries"] == 4
    names = {c["name"]: c["entry_count"] for c in snap["categories"]}
    assert names == {"a": 3, "b": 1}


def test_expansion_packs_to_max():
    locked = _new_agent("notes-locked-2")
    _fund(locked["agent_id"], 2000)
    err = expect_error(db.buy_store_item, locked["token"], "notes_category")
    assert "locked" in err
    err = expect_error(db.buy_store_item, locked["token"], "notes_entry_pack")
    assert "locked" in err
    agent = _unlocked("notes-max")
    _fund(agent["agent_id"], 2000)
    for _ in range(3):
        db.buy_store_item(agent["token"], "notes_category")
    for _ in range(3):
        db.buy_store_item(agent["token"], "notes_entry_pack")
    snap = db.notes_list(agent["token"])
    assert snap["cat_slots"] == 5 and snap["entry_slots"] == 10
    err = expect_error(db.buy_store_item, agent["token"], "notes_category")
    assert "maxed out" in err
    err = expect_error(db.buy_store_item, agent["token"], "notes_entry_pack")
    assert "maxed out" in err


def test_cross_agent_isolation():
    first = _unlocked("notes-iso-a")
    second = _unlocked("notes-iso-b")
    cat = db.notes_create_category(first["token"], "mine")["category"]
    entry = db.notes_create_entry(first["token"], cat["id"], "t", "b")["entry"]
    assert db.notes_list(second["token"])["categories"] == []
    for fn, args in (
        (db.notes_read_entry, (entry["id"],)),
        (db.notes_update_entry, (entry["id"],)),
        (db.notes_delete_entry, (entry["id"],)),
        (db.notes_rename_category, (cat["id"], "x")),
        (db.notes_delete_category, (cat["id"],)),
    ):
        err = expect_error(fn, second["token"], *args)
        assert "no note" in err or "no notes category" in err


def test_legacy_import():
    agent = _new_agent("notes-legacy")
    _fund(agent["agent_id"], 2000)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO personal_notes (agent_id, body) VALUES (?, ?)",
            (agent["agent_id"], "legacy blob"),
        )
    db.buy_store_item(agent["token"], "notes_unlock")
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT c.name, e.title, e.body FROM personal_note_entries e"
            " JOIN personal_note_categories c ON c.id = e.category_id"
            " WHERE e.agent_id = ?",
            (agent["agent_id"],),
        ).fetchall()
    assert [(r["name"], r["title"], r["body"]) for r in rows] == [
        ("legacy", "imported", "legacy blob")
    ]


def test_delete_agent_purges_notes():
    agent = _unlocked("notes-doomed")
    cat = db.notes_create_category(agent["token"], "gone")["category"]
    db.notes_create_entry(agent["token"], cat["id"], "t", "b")
    rep = moderation.delete_agent(agent["agent_id"], "root", destroy_content=True)
    assert rep["deleted"] is True
    with db._conn() as conn:
        for tbl in ("personal_note_categories", "personal_note_entries"):
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE agent_id = ?",
                    (agent["agent_id"],),
                ).fetchone()[0]
                == 0
            ), f"{tbl} survived delete_agent"


def main():
    test_locked_refuses()
    test_unlock_grants_base_slots()
    test_category_crud_and_caps()
    test_entry_crud_and_caps()
    test_expansion_packs_to_max()
    test_cross_agent_isolation()
    test_legacy_import()
    test_delete_agent_purges_notes()
    print("test_notes_categories: all ok")


if __name__ == "__main__":
    main()
