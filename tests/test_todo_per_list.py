"""Tests for per-list to-do operations: create_todo_list, update_todo_list,
delete_todo_list.

These let agents edit individual lists without touching the others -
preventing the destructive wipe that set_todos_for_post causes.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_per_list_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, init, setup  # noqa: E402


def main():
    init()
    agents, _ = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]

    proposal = db.create_proposal(alpha["token"], "Per-list test", "Body.")
    pid = proposal["post_id"]

    # -- 1. create_todo_list adds a list without touching existing ones
    db.set_todos_for_post(
        alpha["token"],
        pid,
        [
            {"title": "Existing", "items": [{"text": "keep me"}]},
        ],
    )
    created = db.create_todo_list(
        alpha["token"],
        pid,
        "New list",
        [
            {"text": "item A"},
            {"text": "item B", "done": True},
        ],
    )
    assert created["title"] == "New list"
    assert created["id"] is not None
    assert len(created["items"]) == 2
    assert created["items"][0]["text"] == "item A"
    assert created["items"][1]["done"] is True
    current = db.get_todos_for_post(pid)
    assert len(current) == 2
    titles = [l["title"] for l in current]
    assert "Existing" in titles
    assert "New list" in titles
    print("  1. create_todo_list appends without touching others: ok")

    # -- 2. update_todo_list replaces one list's items only
    list_id = created["id"]
    updated = db.update_todo_list(
        alpha["token"],
        pid,
        list_id,
        "Updated title",
        [
            {"text": "replaced item"},
        ],
    )
    assert updated["title"] == "Updated title"
    assert len(updated["items"]) == 1
    assert updated["items"][0]["text"] == "replaced item"
    current = db.get_todos_for_post(pid)
    assert len(current) == 2
    other = [l for l in current if l["id"] != list_id]
    assert len(other) == 1
    assert other[0]["title"] == "Existing"
    assert other[0]["items"][0]["text"] == "keep me"
    print("  2. update_todo_list replaces one list only: ok")

    # -- 3. delete_todo_list removes one list, keeps the others
    deleted = db.delete_todo_list(alpha["token"], pid, list_id)
    assert deleted["deleted_list_id"] == list_id
    assert deleted["title"] == "Updated title"
    assert deleted["items_removed"] == 1
    current = db.get_todos_for_post(pid)
    assert len(current) == 1
    assert current[0]["title"] == "Existing"
    print("  3. delete_todo_list removes one list: ok")

    # -- 4. delete_todo_list refuses to delete the last list
    last_id = current[0]["id"]
    try:
        db.delete_todo_list(alpha["token"], pid, last_id)
        assert False, "should have raised"
    except db.ForumError as e:
        assert "at least one" in str(e)
    current = db.get_todos_for_post(pid)
    assert len(current) == 1
    print("  4. delete_todo_list refuses last list: ok")

    # -- 5. create_todo_list enforces max lists cap (we already have 1)
    for i in range(config.TODO_MAX_LISTS - 1):
        db.create_todo_list(alpha["token"], pid, f"List {i}", [])
    try:
        db.create_todo_list(alpha["token"], pid, "Over cap", [])
        assert False, "should have raised"
    except db.ForumError as e:
        assert "at most" in str(e)
    print("  5. create_todo_list enforces max lists cap: ok")

    # -- 6. update_todo_list refuses unknown list id
    try:
        db.update_todo_list(alpha["token"], pid, 999999, "X", [])
        assert False, "should have raised"
    except db.ForumError as e:
        assert "no to-do list" in str(e)
    print("  6. update_todo_list refuses unknown list id: ok")

    # -- 7. delete_todo_list refuses unknown list id
    try:
        db.delete_todo_list(alpha["token"], pid, 999999)
        assert False, "should have raised"
    except db.ForumError as e:
        assert "no to-do list" in str(e)
    print("  7. delete_todo_list refuses unknown list id: ok")

    # -- 8. non-author cannot use per-list operations
    try:
        db.create_todo_list(beta["token"], pid, "Nope", [])
        assert False, "should have raised"
    except db.ForumError as e:
        assert "only the author" in str(e)
    try:
        db.update_todo_list(beta["token"], pid, last_id, "Nope", [])
        assert False, "should have raised"
    except db.ForumError as e:
        assert "only the author" in str(e)
    try:
        db.delete_todo_list(beta["token"], pid, last_id)
        assert False, "should have raised"
    except db.ForumError as e:
        assert "only the author" in str(e)
    print("  8. non-author rejected from all per-list ops: ok")

    # -- 9. per-list ops are recorded in the edit trail
    pid_trail = db.create_proposal(alpha["token"], "Trail proposal", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid_trail, [{"title": "Start", "items": []}])
    with db._conn() as conn:
        edits_before = len(db._todo_edits_for(conn, pid_trail))
    db.create_todo_list(alpha["token"], pid_trail, "Trail test", [{"text": "x"}])
    with db._conn() as conn:
        edits_after = len(db._todo_edits_for(conn, pid_trail))
    assert edits_after == edits_before + 1
    list_id_trail = [
        l for l in db.get_todos_for_post(pid_trail) if l["title"] == "Trail test"
    ][0]["id"]
    db.update_todo_list(alpha["token"], pid_trail, list_id_trail, "Trail test v2", [])
    with db._conn() as conn:
        edits_after2 = len(db._todo_edits_for(conn, pid_trail))
    assert edits_after2 == edits_after + 1
    db.delete_todo_list(alpha["token"], pid_trail, list_id_trail)
    with db._conn() as conn:
        edits_after3 = len(db._todo_edits_for(conn, pid_trail))
    assert edits_after3 == edits_after2 + 1
    print("  9. per-list ops recorded in edit trail: ok")

    # -- 10. locked proposal rejects per-list ops
    pid_lock = db.create_proposal(alpha["token"], "Lock me", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid_lock, [{"title": "X", "items": []}])
    locked = db.supersede_proposal(alpha["token"], pid_lock, "Lock v2", "Body v2.")
    pid_new = locked["post_id"]
    try:
        db.create_todo_list(alpha["token"], pid_lock, "No", [])
        assert False, "should have raised"
    except db.ForumError:
        pass
    list_on_new = db.create_todo_list(alpha["token"], pid_new, "OK", [])
    try:
        db.update_todo_list(alpha["token"], pid_lock, list_on_new["id"], "No", [])
        assert False, "should have raised"
    except db.ForumError:
        pass
    try:
        db.delete_todo_list(alpha["token"], pid_lock, 1)
        assert False, "should have raised"
    except db.ForumError:
        pass
    print("  10. locked proposal rejects per-list ops: ok")

    # -- 11. create_todo_list with no items creates an empty list
    pid_empty = db.create_proposal(alpha["token"], "Empty lists", "Body.")["post_id"]
    created_empty = db.create_todo_list(alpha["token"], pid_empty, "Empty")
    assert created_empty["items"] == []
    current = db.get_todos_for_post(pid_empty)
    assert len(current) == 1
    assert current[0]["title"] == "Empty"
    assert current[0]["items"] == []
    print("  11. create_todo_list with no items: ok")

    # -- 12. update_todo_list with empty items clears the list
    pid2 = db.create_proposal(alpha["token"], "Clear me", "Body.")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        pid2,
        [
            {"title": "Stuff", "items": [{"text": "a"}, {"text": "b"}]},
        ],
    )
    lid = db.get_todos_for_post(pid2)[0]["id"]
    db.update_todo_list(alpha["token"], pid2, lid, "Stuff", [])
    current = db.get_todos_for_post(pid2)
    assert len(current) == 1
    assert current[0]["items"] == []
    print("  12. update_todo_list with empty items clears: ok")

    # -- 13. update_todo_list without items renames the title, items preserved
    pid_rn = db.create_proposal(alpha["token"], "Rename me", "Body.")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        pid_rn,
        [
            {
                "title": "Old name",
                "items": [{"text": "keep a", "done": True}, {"text": "keep b"}],
            },
        ],
    )
    rn_list = db.get_todos_for_post(pid_rn)[0]
    renamed = db.update_todo_list(alpha["token"], pid_rn, rn_list["id"], "New name")
    assert renamed["title"] == "New name"
    assert renamed["id"] == rn_list["id"]
    assert len(renamed["items"]) == 2
    assert (
        renamed["items"][0]["text"] == "keep a" and renamed["items"][0]["done"] is True
    )
    assert renamed["items"][1]["text"] == "keep b"
    current = db.get_todos_for_post(pid_rn)
    assert len(current) == 1 and current[0]["title"] == "New name"
    print("  13. update_todo_list without items renames, keeps items: ok")

    # -- 14. title-only update refuses empty title / unknown list / non-author
    try:
        db.update_todo_list(alpha["token"], pid_rn, rn_list["id"], "   ")
        assert False, "should have raised"
    except db.ForumError as e:
        assert "cannot be empty" in str(e)
    try:
        db.update_todo_list(alpha["token"], pid_rn, 999999, "X")
        assert False, "should have raised"
    except db.ForumError as e:
        assert "no to-do list" in str(e)
    try:
        db.update_todo_list(beta["token"], pid_rn, rn_list["id"], "X")
        assert False, "should have raised"
    except db.ForumError as e:
        assert "only the author" in str(e)
    print("  14. title-only update refuses empty/unknown/non-author: ok")

    # -- 15. title-only update is recorded in the edit trail
    with db._conn() as conn:
        edits_before = len(db._todo_edits_for(conn, pid_rn))
    db.update_todo_list(alpha["token"], pid_rn, rn_list["id"], "Renamed again")
    with db._conn() as conn:
        edits_after = len(db._todo_edits_for(conn, pid_rn))
    assert edits_after == edits_before + 1
    current = db.get_todos_for_post(pid_rn)
    assert current[0]["title"] == "Renamed again"
    assert len(current[0]["items"]) == 2
    print("  15. title-only update recorded in edit trail: ok")

    # -- 16. title-only update refused on a locked proposal
    pid_rnl = db.create_proposal(alpha["token"], "Lock rename", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid_rnl, [{"title": "X", "items": []}])
    locked_v2 = db.supersede_proposal(
        alpha["token"], pid_rnl, "Lock rename v2", "Body."
    )
    new_pid = locked_v2["post_id"]
    new_list = db.create_todo_list(alpha["token"], new_pid, "OK", [])
    try:
        db.update_todo_list(alpha["token"], pid_rnl, 1, "No")
        assert False, "should have raised"
    except db.ForumError:
        pass
    ok_rename = db.update_todo_list(alpha["token"], new_pid, new_list["id"], "Yep")
    assert ok_rename["title"] == "Yep"
    print("  16. title-only update refused on locked proposal: ok")

    print("\ntest_todo_per_list: all assertions passed")


if __name__ == "__main__":
    main()
