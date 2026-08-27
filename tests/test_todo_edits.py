"""Tests for the to-do edit trail (todo_edits table).

Every set_todos_for_post call now snapshots the full before/after state
into the todo_edits table, so a destructive wipe is recoverable and
auditable.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_edits_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup, init  # noqa: E402
import moderation  # noqa: E402


def main():
    init()
    agents, _ = setup()
    alpha = agents["alpha"]

    # -- 1. First set_todos_for_post creates an edit with empty old_lists ---
    proposal = db.create_proposal(alpha["token"], "Edit trail test", "Body.")
    pid = proposal["post_id"]

    lists1 = [{"title": "Phase 1", "items": [{"text": "Step A"}, {"text": "Step B"}]}]
    result = db.set_todos_for_post(alpha["token"], pid, lists1)
    assert len(result) == 1, f"expected 1 list, got {len(result)}"

    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid)
    assert len(edits) == 1, f"expected 1 edit, got {len(edits)}"
    e = edits[0]
    assert e["old_lists"] == [], f"first edit old_lists should be [], got {e['old_lists']}"
    assert len(e["new_lists"]) == 1, "first edit new_lists should have 1 list"
    assert e["new_lists"][0]["title"] == "Phase 1"
    assert e["editor"] == "alpha"
    print("  first update creates edit with empty old_lists: ok")

    # -- 2. Second update captures the previous state ----------------------
    lists2 = [
        {"title": "Phase 1", "items": [{"text": "Step A"}, {"text": "Step B"}, {"text": "Step C"}]},
        {"title": "Phase 2", "items": [{"text": "Step X"}]},
    ]
    db.set_todos_for_post(alpha["token"], pid, lists2)

    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid)
    assert len(edits) == 2, f"expected 2 edits, got {len(edits)}"
    e2 = edits[1]
    assert len(e2["old_lists"]) == 1, "second edit old_lists should have 1 list"
    assert e2["old_lists"][0]["title"] == "Phase 1"
    assert len(e2["new_lists"]) == 2, "second edit new_lists should have 2 lists"
    assert e2["editor"] == "alpha"
    print("  second update captures previous state: ok")

    # -- 3. Wipe is recoverable from the edit trail ------------------------
    lists3 = [{"title": "Survivor", "items": [{"text": "Only this"}]}]
    db.set_todos_for_post(alpha["token"], pid, lists3)

    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid)
    assert len(edits) == 3, f"expected 3 edits, got {len(edits)}"
    e3 = edits[2]
    assert len(e3["old_lists"]) == 2, "wipe edit old_lists should have 2 lists"
    assert e3["old_lists"][0]["title"] == "Phase 1"
    assert e3["old_lists"][1]["title"] == "Phase 2"
    assert len(e3["new_lists"]) == 1
    assert e3["new_lists"][0]["title"] == "Survivor"
    current = db.get_todos_for_post(pid)
    assert len(current) == 1
    assert current[0]["title"] == "Survivor"
    print("  wipe recoverable from edit trail: ok")

    # -- 4. Edit trail fields are complete ---------------------------------
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid)
    assert len(edits) == 3
    assert all("editor" in e for e in edits), "every edit should have editor name"
    assert all("old_lists" in e for e in edits), "every edit should have old_lists"
    assert all("new_lists" in e for e in edits), "every edit should have new_lists"
    assert all("edited_at" in e for e in edits), "every edit should have edited_at"
    print("  edit trail fields complete: ok")

    # -- 5. tod_edits cascade-deletes when post is deleted -----------------
    pid2 = db.create_proposal(alpha["token"], "Delete me", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid2, [{"title": "L", "items": [{"text": "I"}]}])
    with db._conn() as conn:
        assert db._todo_edits_for(conn, pid2), "should have edits"
    moderation.delete_post(pid2, "admin")
    with db._conn() as conn:
        assert db._todo_edits_for(conn, pid2) == [], "edits should be gone after post delete"
    print("  todo_edits cascade-deletes on post removal: ok")

    # -- 6. tod_edits persist across multiple updates ----------------------
    pid3 = db.create_proposal(alpha["token"], "Persist", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid3, [{"title": "A", "items": []}])
    db.set_todos_for_post(alpha["token"], pid3, [{"title": "A", "items": [{"text": "X"}]}])
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid3)
    assert len(edits) == 2, f"expected 2 edits, got {len(edits)}"
    db.set_todos_for_post(alpha["token"], pid3, [{"title": "A", "items": [{"text": "X"}]}])
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid3)
    assert len(edits) == 3, f"expected 3 edits, got {len(edits)}"
    print("  edit trail persists across updates: ok")

    # -- 7. tod_edits_batch returns edits for multiple posts ---------------
    pid4 = db.create_proposal(alpha["token"], "Batch", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid4, [{"title": "B", "items": []}])
    with db._conn() as conn:
        batch = db._todo_edits_batch(conn, [pid, pid4])
    assert pid in batch, "batch should contain pid"
    assert pid4 in batch, "batch should contain pid4"
    assert len(batch[pid]) == 3
    assert len(batch[pid4]) == 1
    print("  _todo_edits_batch works: ok")

    # -- 8. tod_edits empty for untouched proposal -------------------------
    pid5 = db.create_proposal(alpha["token"], "Untouched", "Body.")["post_id"]
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid5)
    assert edits == [], "untouched proposal should have no edits"
    print("  untouched proposal has no edits: ok")

    print("\ntest_todo_edits: all assertions passed")


if __name__ == "__main__":
    main()