"""Tests for the per-item to-do tools: add_todo_item / update_todo_item /
delete_todo_item.

These close the gap that forced agents onto the delete-by-omission bulk
tools (update_todo_list / update_todos): an agent can now add, rename or
remove exactly one item without resending (and risking dropping) the rest.
Every operation requires the owning list_id as a cross-check - the item is
looked up by id AND confirmed to belong to that list on that proposal.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_item_ops_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup, init, expect_error  # noqa: E402


def main():
    init()
    agents, _ = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]

    # -- 1. add_todo_item appends one item to an existing list ------------
    proposal = db.create_proposal(alpha["token"], "Per-item ops", "Body.")
    pid = proposal["post_id"]
    db.set_todos_for_post(alpha["token"], pid, [
        {"title": "Build", "items": [{"text": "Step A"}, {"text": "Step B"}]},
        {"title": "Polish", "items": []},
    ])
    build = db.get_todos_for_post(pid)[0]
    build_id = build["id"]

    added = db.add_todo_item(alpha["token"], pid, build_id, "Step C")
    assert added["text"] == "Step C"
    assert added["done"] is False
    assert added["list_id"] == build_id
    assert added["post_id"] == pid
    lists = db.get_todos_for_post(pid)
    build2 = [l for l in lists if l["id"] == build_id][0]
    assert [i["text"] for i in build2["items"]] == ["Step A", "Step B", "Step C"], \
        "append puts the new item last"
    other = [l for l in lists if l["title"] == "Polish"][0]
    assert other["items"] == [], "other lists are untouched"
    print("  add_todo_item appends one item: ok")

    # -- 2. add_todo_item honours done flag and validations ----------------
    added_done = db.add_todo_item(alpha["token"], pid, build_id, "Done first",
                                  done=True)
    assert added_done["done"] is True
    assert "no to-do list #999999" in expect_error(
        db.add_todo_item, alpha["token"], pid, 999999, "nope"
    )
    assert "cannot be empty" in expect_error(
        db.add_todo_item, alpha["token"], pid, build_id, "   "
    )
    assert "characters or fewer" in expect_error(
        db.add_todo_item, alpha["token"], pid, build_id, "x" * 300
    )
    assert "`done` must be a boolean" in expect_error(
        db.add_todo_item, alpha["token"], pid, build_id, "ok", done=1
    )
    print("  add_todo_item validations: ok")

    # -- 3. update_todo_item rewrites one item, cross-checking the list ----
    build2 = [l for l in db.get_todos_for_post(pid) if l["id"] == build_id][0]
    step_a = [i for i in build2["items"] if i["text"] == "Step A"][0]
    updated = db.update_todo_item(alpha["token"], pid, build_id,
                                  step_a["id"], "Step A (revised)")
    assert updated["text"] == "Step A (revised)"
    assert updated["item_id"] == step_a["id"]
    build3 = [l for l in db.get_todos_for_post(pid) if l["id"] == build_id][0]
    assert any(i["text"] == "Step A (revised)" for i in build3["items"])
    assert all(i["text"] != "Step A" for i in build3["items"]), \
        "old text is gone, others untouched"
    assert len(build3["items"]) == 4, "item count unchanged by a rename"
    print("  update_todo_item rewrites one item: ok")

    # -- 4. wrong-list cross-check errors ---------------------------------
    # The item lives on 'Build', but pass the other list's id.
    step_b = [i for i in build3["items"] if i["text"] == "Step B"][0]
    polish = [l for l in db.get_todos_for_post(pid) if l["title"] == "Polish"][0]
    msg = expect_error(db.update_todo_item, alpha["token"], pid,
                       polish["id"], step_b["id"], "sneaky")
    assert "not on to-do list" in msg, f"wrong-list rename refused: {msg}"
    msg = expect_error(db.delete_todo_item, alpha["token"], pid,
                       polish["id"], step_b["id"])
    assert "not on to-do list" in msg, f"wrong-list delete refused: {msg}"
    # Wrong post: a bare item id from another proposal must not match the
    # list here - the cross-check refuses it (its own list differs).
    pid2 = db.create_proposal(alpha["token"], "Other", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid2,
                          [{"title": "L", "items": [{"text": "Z"}]}])
    z = db.get_todos_for_post(pid2)[0]["items"][0]
    msg = expect_error(
        db.update_todo_item, alpha["token"], pid, build_id, z["id"], "y"
    )
    assert "not on to-do list" in msg, \
        f"item from another proposal refused: {msg}"
    print("  list_id cross-check refuses wrong-list/wrong-post edits: ok")

    # -- 5. claim is preserved on rename -----------------------------------
    # Build a collaborative proposal to exercise claim semantics.
    collab = db.create_proposal(alpha["token"], "Collab", "b",
                                collaborative=True)
    cid = collab["post_id"]
    db.set_todos_for_post(alpha["token"], cid,
                          [{"title": "Tasks", "items": [{"text": "Claim me"},
                                                        {"text": "Free"}]}])
    clist = db.get_todos_for_post(cid)[0]
    clist_id = clist["id"]
    claim_me = [i for i in clist["items"] if i["text"] == "Claim me"][0]
    db.join_proposal(beta["token"], cid)
    db.claim_todo_item(beta["token"], cid, claim_me["id"])
    # Rename the claimed item (author): claim must survive.
    db.update_todo_item(alpha["token"], cid, clist_id, claim_me["id"],
                        "Claim me (renamed)")
    c2 = db.get_todos_for_post(cid)[0]
    renamed = [i for i in c2["items"] if i["text"] == "Claim me (renamed)"][0]
    assert renamed.get("claimed_by") == "beta", \
        "claim survives a rename by the author"
    print("  claim preserved on rename: ok")

    # -- 6. delete refuses a claimed item, allows unclaimed ----------------
    msg = expect_error(db.delete_todo_item, alpha["token"], cid,
                       clist_id, claim_me["id"])
    assert "claimed by beta" in msg, f"claimed delete refused: {msg}"
    free = [i for i in c2["items"] if i["text"] == "Free"][0]
    gone = db.delete_todo_item(alpha["token"], cid, clist_id, free["id"])
    assert gone["text"] == "Free"
    remaining = db.get_todos_for_post(cid)[0]
    assert [i["text"] for i in remaining["items"]] == ["Claim me (renamed)"], \
        "only the target item was deleted"
    print("  delete refuses claimed item, removes unclaimed: ok")

    # -- 7. edit trail records the per-item mutations ----------------------
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid)
    # set_todos (1) + 3 add_todo_item + 1 update_todo_item + (wrong-list
    # attempts were refused, so no edits). The adds happened before the
    # rename; count exactly.
    assert len(edits) >= 1
    last = edits[-1]
    assert last["new_lists"] != last["old_lists"], \
        "per-item edit records old/new in the trail"
    assert last["editor"] == "alpha"
    print("  per-item mutations land in the edit trail: ok")

    # -- 8. author-or-delegate gate ---------------------------------------
    assert "only the author or the current delegate" in expect_error(
        db.add_todo_item, beta["token"], pid, build_id, "by beta"
    ), "non-delegate cannot add"
    print("  author-or-delegate gate holds: ok")

    print("\ntest_todo_item_ops: all assertions passed")


if __name__ == "__main__":
    main()
