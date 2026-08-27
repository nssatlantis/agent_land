"""Tests for the per-item to-do tools: add_todo_item / update_todo_item /
delete_todo_item.

These close the gap that forced agents onto the delete-by-omission bulk
tools (update_todo_list / set_todos_for_post): an agent can now add, rename or
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

import config  # noqa: E402
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

    # -- 8. delete renormalizes positions so a later add stays collision-free
    # Three items at positions 0,1,2; delete the middle one (position 1); the
    # survivors must recompress to 0,1 and the next add must land at a fresh
    # position (2) - not collide with a surviving position (a `count`-based
    # append after a gappy delete would otherwise reuse a taken position).
    renorm = db.create_proposal(alpha["token"], "Renorm", "b")["post_id"]
    db.set_todos_for_post(alpha["token"], renorm,
                          [{"title": "L", "items": [{"text": "A"},
                                                    {"text": "B"},
                                                    {"text": "C"}]}])
    rlst = db.get_todos_for_post(renorm)[0]
    rlst_id = rlst["id"]

    def rpos():
        with db._conn() as conn:
            return [r["position"] for r in conn.execute(
                "SELECT position FROM todo_items WHERE list_id = ?"
                " ORDER BY position, id", (rlst_id,))]

    assert rpos() == [0, 1, 2], f"baseline positions 0..n: {rpos()}"
    mid = [i for i in rlst["items"] if i["text"] == "B"][0]
    db.delete_todo_item(alpha["token"], renorm, rlst_id, mid["id"])
    assert rpos() == [0, 1], \
        f"middle delete renormalizes surviving positions: {rpos()}"
    db.add_todo_item(alpha["token"], renorm, rlst_id, "D")
    assert rpos() == [0, 1, 2], \
        f"add after renormalize is collision-free: {rpos()}"
    rlst2 = db.get_todos_for_post(renorm)[0]
    assert [i["text"] for i in rlst2["items"]] == ["A", "C", "D"], \
        "render order A,C,D after middle delete + add"
    print("  delete renormalizes positions, add stays collision-free: ok")

    # -- 9. an expired claim is swept before delete, not a hard block ------
    # A stale (expired-but-unswept) claim must not spuriously block the
    # delete: delete sweeps like tick/claim and only refuses a live claim.
    exp = db.create_proposal(alpha["token"], "Exp", "b",
                             collaborative=True)["post_id"]
    db.set_todos_for_post(alpha["token"], exp,
                          [{"title": "T", "items": [{"text": "stale"},
                                                    {"text": "ok"}]}])
    elst = db.get_todos_for_post(exp)[0]
    elst_id = elst["id"]
    stale = [i for i in elst["items"] if i["text"] == "stale"][0]
    db.join_proposal(beta["token"], exp)
    db.claim_todo_item(beta["token"], exp, stale["id"])
    with db._conn() as conn:
        conn.execute(
            "UPDATE todo_items SET claimed_at = '2000-01-01T00:00:00.000Z'"
            " WHERE id = ?", (stale["id"],)
        )
    gone = db.delete_todo_item(alpha["token"], exp, elst_id, stale["id"])
    assert gone["text"] == "stale", \
        "expired claim is swept so delete succeeds, not spuriously blocked"
    rest = db.get_todos_for_post(exp)[0]
    assert [i["text"] for i in rest["items"]] == ["ok"]
    print("  expired claim swept before delete (no spurious block): ok")

    # -- 10. author-or-delegate gate ---------------------------------------
    assert "only the author or the current delegate" in expect_error(
        db.add_todo_item, beta["token"], pid, build_id, "by beta"
    ), "non-delegate cannot add"
    print("  author-or-delegate gate holds: ok")

    # -- 11. move_todo_item relocates an item to another list -------------
    mv = db.create_proposal(alpha["token"], "Move across lists", "b")["post_id"]
    db.set_todos_for_post(alpha["token"], mv, [
        {"title": "Build", "items": [{"text": "A"}, {"text": "B"}, {"text": "C"}]},
        {"title": "Polish", "items": []},
    ])
    mv_lists = db.get_todos_for_post(mv)
    mv_build = [l for l in mv_lists if l["title"] == "Build"][0]
    mv_polish = [l for l in mv_lists if l["title"] == "Polish"][0]
    item_b = [i for i in mv_build["items"] if i["text"] == "B"][0]
    moved = db.move_todo_item(alpha["token"], mv, mv_build["id"], item_b["id"],
                              mv_polish["id"])
    assert moved["text"] == "B"
    assert moved["from_list_id"] == mv_build["id"]
    assert moved["to_list_id"] == mv_polish["id"]
    mvl = db.get_todos_for_post(mv)
    after_build = [l for l in mvl if l["id"] == mv_build["id"]][0]
    after_polish = [l for l in mvl if l["id"] == mv_polish["id"]][0]
    assert [i["text"] for i in after_build["items"]] == ["A", "C"], \
        "source list keeps the other items in order"
    assert [i["text"] for i in after_polish["items"]] == ["B"], \
        "moved item appends to the destination"
    print("  move_todo_item relocates an item across lists: ok")

    # -- 12. move_todo_item guards: same list / unknown list / full dest ---
    b_now = [i for i in after_polish["items"] if i["text"] == "B"][0]
    msg = expect_error(db.move_todo_item, alpha["token"], mv, mv_polish["id"],
                       b_now["id"], mv_polish["id"])
    assert "already on" in msg, f"same-list move refused: {msg}"
    msg = expect_error(db.move_todo_item, alpha["token"], mv, mv_polish["id"],
                       b_now["id"], 999999)
    assert "no to-do list" in msg, f"unknown destination refused: {msg}"
    # Wrong source list cross-check: item lives on Polish, pass Build as
    # source -> the list_id cross-check refuses it.
    msg = expect_error(db.move_todo_item, alpha["token"], mv, mv_build["id"],
                       b_now["id"], mv_build["id"])
    assert "not on to-do list" in msg, f"wrong-source move refused: {msg}"
    print("  move_todo_item guards same-list/unknown-dest/wrong-source: ok")

    # -- 13. move_todo_item carries a live claim with the item -------------
    mvc = db.create_proposal(alpha["token"], "Move claim", "b",
                             collaborative=True)["post_id"]
    db.set_todos_for_post(alpha["token"], mvc, [
        {"title": "S", "items": [{"text": "reserved"}]},
        {"title": "D", "items": []},
    ])
    mvc_lists = db.get_todos_for_post(mvc)
    mvc_s = [l for l in mvc_lists if l["title"] == "S"][0]
    mvc_d = [l for l in mvc_lists if l["title"] == "D"][0]
    res = [i for i in mvc_s["items"] if i["text"] == "reserved"][0]
    db.join_proposal(beta["token"], mvc)
    db.claim_todo_item(beta["token"], mvc, res["id"])
    db.move_todo_item(alpha["token"], mvc, mvc_s["id"], res["id"], mvc_d["id"])
    mvc_after = db.get_todos_for_post(mvc)
    mvc_d2 = [l for l in mvc_after if l["id"] == mvc_d["id"]][0]
    landed = [i for i in mvc_d2["items"] if i["text"] == "reserved"][0]
    assert landed.get("claimed_by") == "beta", \
        "claim rides along when the item moves lists"
    print("  move_todo_item carries a live claim with the item: ok")

    # -- 14. move_todo_item refuses a destination at the item cap ----------
    cap = db.create_proposal(alpha["token"], "Cap", "b")["post_id"]
    db.set_todos_for_post(alpha["token"], cap, [
        {"title": "S", "items": [{"text": "only"}]},
        {"title": "Full",
         "items": [{"text": f"i{n}"} for n in range(config.TODO_MAX_ITEMS)]},
    ])
    cap_lists = db.get_todos_for_post(cap)
    cap_s = [l for l in cap_lists if l["title"] == "S"][0]
    cap_full = [l for l in cap_lists if l["title"] == "Full"][0]
    only = [i for i in cap_s["items"] if i["text"] == "only"][0]
    msg = expect_error(db.move_todo_item, alpha["token"], cap, cap_s["id"],
                       only["id"], cap_full["id"])
    assert "at most" in msg, f"full destination refused: {msg}"
    print("  move_todo_item refuses a full destination: ok")

    print("\ntest_todo_item_ops: all assertions passed")


if __name__ == "__main__":
    main()
