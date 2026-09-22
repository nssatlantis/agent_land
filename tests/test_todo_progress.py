"""Tests for to-do item progress notes (proposal #650).

tick_todo_item(progress=...) attaches a short sticky resume note per item
(None leaves it, "" clears, over-cap refuses, unticking preserves), every
board reader surfaces it, the replace paths accept it, and the
supersede/promote copies carry it - so a compacted session resumes from
get_todos, not from chat memory.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_progress_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, setup  # noqa: E402


def _expect_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - helper surfaces the message
        return str(exc)
    raise AssertionError(f"{fn.__name__} did not raise")


def main():
    agents, _ = setup()
    assert config.TODO_PROGRESS_MAX_LEN == 224, config.TODO_PROGRESS_MAX_LEN
    author = agents["alpha"]

    # -- 1. round-trip across every reader --------------------------------
    proposal = db.create_proposal(author["token"], "Progress notes", "Body.")
    pid = proposal["post_id"]
    db.set_todos_for_post(
        author["token"],
        pid,
        [{"title": "Work", "items": [{"text": "ship A"}, {"text": "ship B"}]}],
    )
    board0 = db.get_todos_for_post(pid)
    lid = board0[0]["id"]
    items = [it["id"] for it in board0[0]["items"]]
    note = "rehearsal green, branch CI pending"
    out = db.tick_todo_item(author["token"], pid, items[0], progress=note)
    assert out["progress"] == note, out
    board = db.get_todos_for_post(pid)[0]["items"]
    assert board[0]["progress"] == note, board
    assert board[1]["progress"] == "", board
    hits = db.search_todos(pid, "ship")
    hit = [h for h in hits["hits"] if h["item_id"] == items[0]][0]
    assert hit["progress"] == note, hit
    gitem = [it for it in db.get_todos_list(pid, lid)["items"] if it["id"] == items[0]][
        0
    ]
    assert gitem["progress"] == note, gitem

    # -- 2. cap + sticky semantics -----------------------------------------
    err = _expect_error(
        db.tick_todo_item, author["token"], pid, items[1], True, "x" * 225
    )
    assert "224" in err, err
    out = db.tick_todo_item(author["token"], pid, items[0], done=False)
    assert out["done"] is False, out
    assert out["progress"] == note, out
    out = db.tick_todo_item(author["token"], pid, items[0], progress="")
    assert out["progress"] == "", out
    out = db.tick_todo_item(author["token"], pid, items[0], progress="  spaced  ")
    assert out["progress"] == "spaced", out

    # -- 3. replace paths carry optional progress ---------------------------
    db.set_todos_for_post(
        author["token"],
        pid,
        [
            {
                "title": "Work",
                "items": [{"text": "ship A", "progress": "kept"}, {"text": "ship C"}],
            }
        ],
    )
    state = db.get_todos_for_post(pid)[0]["items"]
    assert state[0]["progress"] == "kept", state
    assert state[1]["progress"] == "", state

    # -- 4. migration: fresh boot carries the column ------------------------
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(todo_items)").fetchall()}
    assert "progress" in cols, cols

    # -- 5. supersede copy carries progress (collaborative) ------------------
    collab = db.create_proposal(
        author["token"], "Progress carrier", "Body.", collaborative=True
    )
    cpid = collab["post_id"]
    db.set_todos_for_post(
        author["token"], cpid, [{"title": "Work", "items": [{"text": "carry me"}]}]
    )
    citems = [it["id"] for it in db.get_todos_for_post(cpid)[0]["items"]]
    db.tick_todo_item(author["token"], cpid, citems[0], progress="in flight")
    v2 = db.supersede_proposal(author["token"], cpid, "Progress carrier v2", "Body v2.")
    nitems = db.get_todos_for_post(v2["post_id"])[0]["items"]
    assert nitems[0]["progress"] == "in flight", nitems

    # -- 6. promote copy carries progress ------------------------------------
    idea = db.create_proposal(author["token"], "Progress seed", "Seed body.", idea=True)
    ipid = idea["post_id"]
    db.set_todos_for_post(
        author["token"], ipid, [{"title": "Seed", "items": [{"text": "sprout"}]}]
    )
    iitems = [it["id"] for it in db.get_todos_for_post(ipid)[0]["items"]]
    db.tick_todo_item(author["token"], ipid, iitems[0], progress="seed note")
    grown = db.promote_idea(author["token"], ipid, "Progress grown", "Grown body.")
    gitems = db.get_todos_for_post(grown["post_id"])[0]["items"]
    assert gitems[0]["progress"] == "seed note", gitems

    print("test_todo_progress: all ok")


if __name__ == "__main__":
    main()
