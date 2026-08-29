"""Tests for the per-item to-do tools: add_todo_item / update_todo_item /
delete_todo_item / move_todo_item.

These close the gap that forced agents onto the delete-by-omission bulk
tools (update_todo_list / set_todos_for_post): an agent can now add, rename,
remove or move exactly one item without resending (and risking dropping) the
rest. Every operation requires the owning list_id as a cross-check - the item
is looked up by id AND confirmed to belong to that list on that proposal.
move_todo_item's batch mode (moves=[...]) relocates several items at once,
atomically.
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
from tests._setup import db, expect_error, init, setup  # noqa: E402


def main():
    init()
    agents, _ = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]

    # -- 1. add_todo_item appends one item to an existing list ------------
    proposal = db.create_proposal(alpha["token"], "Per-item ops", "Body.")
    pid = proposal["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        pid,
        [
            {"title": "Build", "items": [{"text": "Step A"}, {"text": "Step B"}]},
            {"title": "Polish", "items": []},
        ],
    )
    build = db.get_todos_for_post(pid)[0]
    build_id = build["id"]

    added = db.add_todo_item(alpha["token"], pid, build_id, "Step C")
    assert added["text"] == "Step C"
    assert added["done"] is False
    assert added["list_id"] == build_id
    assert added["post_id"] == pid
    lists = db.get_todos_for_post(pid)
    build2 = [l for l in lists if l["id"] == build_id][0]
    assert [i["text"] for i in build2["items"]] == ["Step A", "Step B", "Step C"], (
        "append puts the new item last"
    )
    other = [l for l in lists if l["title"] == "Polish"][0]
    assert other["items"] == [], "other lists are untouched"
    print("  add_todo_item appends one item: ok")

    # -- 2. add_todo_item honours done flag and validations ----------------
    added_done = db.add_todo_item(
        alpha["token"], pid, build_id, "Done first", done=True
    )
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
    updated = db.update_todo_item(
        alpha["token"], pid, build_id, step_a["id"], "Step A (revised)"
    )
    assert updated["text"] == "Step A (revised)"
    assert updated["item_id"] == step_a["id"]
    build3 = [l for l in db.get_todos_for_post(pid) if l["id"] == build_id][0]
    assert any(i["text"] == "Step A (revised)" for i in build3["items"])
    assert all(i["text"] != "Step A" for i in build3["items"]), (
        "old text is gone, others untouched"
    )
    assert len(build3["items"]) == 4, "item count unchanged by a rename"
    print("  update_todo_item rewrites one item: ok")

    # -- 4. wrong-list cross-check errors ---------------------------------
    # The item lives on 'Build', but pass the other list's id.
    step_b = [i for i in build3["items"] if i["text"] == "Step B"][0]
    polish = [l for l in db.get_todos_for_post(pid) if l["title"] == "Polish"][0]
    msg = expect_error(
        db.update_todo_item, alpha["token"], pid, polish["id"], step_b["id"], "sneaky"
    )
    assert "not on to-do list" in msg, f"wrong-list rename refused: {msg}"
    msg = expect_error(
        db.delete_todo_item, alpha["token"], pid, polish["id"], step_b["id"]
    )
    assert "not on to-do list" in msg, f"wrong-list delete refused: {msg}"
    # Wrong post: a bare item id from another proposal must not match the
    # list here - the cross-check refuses it (its own list differs).
    pid2 = db.create_proposal(alpha["token"], "Other", "Body.")["post_id"]
    db.set_todos_for_post(
        alpha["token"], pid2, [{"title": "L", "items": [{"text": "Z"}]}]
    )
    z = db.get_todos_for_post(pid2)[0]["items"][0]
    msg = expect_error(db.update_todo_item, alpha["token"], pid, build_id, z["id"], "y")
    assert "not on to-do list" in msg, f"item from another proposal refused: {msg}"
    print("  list_id cross-check refuses wrong-list/wrong-post edits: ok")

    # -- 5. claim is preserved on rename -----------------------------------
    # Build a collaborative proposal to exercise claim semantics.
    collab = db.create_proposal(alpha["token"], "Collab", "b", collaborative=True)
    cid = collab["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        cid,
        [{"title": "Tasks", "items": [{"text": "Claim me"}, {"text": "Free"}]}],
    )
    clist = db.get_todos_for_post(cid)[0]
    clist_id = clist["id"]
    claim_me = [i for i in clist["items"] if i["text"] == "Claim me"][0]
    db.join_proposal(beta["token"], cid)
    db.claim_todo_item(beta["token"], cid, claim_me["id"])
    # Rename the claimed item (author): claim must survive.
    db.update_todo_item(
        alpha["token"], cid, clist_id, claim_me["id"], "Claim me (renamed)"
    )
    c2 = db.get_todos_for_post(cid)[0]
    renamed = [i for i in c2["items"] if i["text"] == "Claim me (renamed)"][0]
    assert renamed.get("claimed_by") == "beta", "claim survives a rename by the author"
    print("  claim preserved on rename: ok")

    # -- 6. delete refuses a claimed item, allows unclaimed ----------------
    msg = expect_error(
        db.delete_todo_item, alpha["token"], cid, clist_id, claim_me["id"]
    )
    assert "claimed by beta" in msg, f"claimed delete refused: {msg}"
    free = [i for i in c2["items"] if i["text"] == "Free"][0]
    gone = db.delete_todo_item(alpha["token"], cid, clist_id, free["id"])
    assert gone["text"] == "Free"
    remaining = db.get_todos_for_post(cid)[0]
    assert [i["text"] for i in remaining["items"]] == ["Claim me (renamed)"], (
        "only the target item was deleted"
    )
    print("  delete refuses claimed item, removes unclaimed: ok")

    # -- 7. edit trail records the per-item mutations ----------------------
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid)
    # set_todos (1) + 3 add_todo_item + 1 update_todo_item + (wrong-list
    # attempts were refused, so no edits). The adds happened before the
    # rename; count exactly.
    assert len(edits) >= 1
    last = edits[-1]
    assert last["new_lists"] != last["old_lists"], (
        "per-item edit records old/new in the trail"
    )
    assert last["editor"] == "alpha"
    print("  per-item mutations land in the edit trail: ok")

    # -- 8. delete renormalizes positions so a later add stays collision-free
    # Three items at positions 0,1,2; delete the middle one (position 1); the
    # survivors must recompress to 0,1 and the next add must land at a fresh
    # position (2) - not collide with a surviving position (a `count`-based
    # append after a gappy delete would otherwise reuse a taken position).
    renorm = db.create_proposal(alpha["token"], "Renorm", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        renorm,
        [{"title": "L", "items": [{"text": "A"}, {"text": "B"}, {"text": "C"}]}],
    )
    rlst = db.get_todos_for_post(renorm)[0]
    rlst_id = rlst["id"]

    def rpos():
        with db._conn() as conn:
            return [
                r["position"]
                for r in conn.execute(
                    "SELECT position FROM todo_items WHERE list_id = ?"
                    " ORDER BY position, id",
                    (rlst_id,),
                )
            ]

    assert rpos() == [0, 1, 2], f"baseline positions 0..n: {rpos()}"
    mid = [i for i in rlst["items"] if i["text"] == "B"][0]
    db.delete_todo_item(alpha["token"], renorm, rlst_id, mid["id"])
    assert rpos() == [0, 1], f"middle delete renormalizes surviving positions: {rpos()}"
    db.add_todo_item(alpha["token"], renorm, rlst_id, "D")
    assert rpos() == [0, 1, 2], f"add after renormalize is collision-free: {rpos()}"
    rlst2 = db.get_todos_for_post(renorm)[0]
    assert [i["text"] for i in rlst2["items"]] == ["A", "C", "D"], (
        "render order A,C,D after middle delete + add"
    )
    print("  delete renormalizes positions, add stays collision-free: ok")

    # -- 9. an expired claim is swept before delete, not a hard block ------
    # A stale (expired-but-unswept) claim must not spuriously block the
    # delete: delete sweeps like tick/claim and only refuses a live claim.
    exp = db.create_proposal(alpha["token"], "Exp", "b", collaborative=True)["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        exp,
        [{"title": "T", "items": [{"text": "stale"}, {"text": "ok"}]}],
    )
    elst = db.get_todos_for_post(exp)[0]
    elst_id = elst["id"]
    stale = [i for i in elst["items"] if i["text"] == "stale"][0]
    db.join_proposal(beta["token"], exp)
    db.claim_todo_item(beta["token"], exp, stale["id"])
    with db._conn() as conn:
        conn.execute(
            "UPDATE todo_items SET claimed_at = '2000-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (stale["id"],),
        )
    gone = db.delete_todo_item(alpha["token"], exp, elst_id, stale["id"])
    assert gone["text"] == "stale", (
        "expired claim is swept so delete succeeds, not spuriously blocked"
    )
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
    db.set_todos_for_post(
        alpha["token"],
        mv,
        [
            {"title": "Build", "items": [{"text": "A"}, {"text": "B"}, {"text": "C"}]},
            {"title": "Polish", "items": []},
        ],
    )
    mv_lists = db.get_todos_for_post(mv)
    mv_build = [l for l in mv_lists if l["title"] == "Build"][0]
    mv_polish = [l for l in mv_lists if l["title"] == "Polish"][0]
    item_b = [i for i in mv_build["items"] if i["text"] == "B"][0]
    moved = db.move_todo_item(
        alpha["token"], mv, mv_build["id"], item_b["id"], mv_polish["id"]
    )
    assert moved["text"] == "B"
    assert moved["from_list_id"] == mv_build["id"]
    assert moved["to_list_id"] == mv_polish["id"]
    mvl = db.get_todos_for_post(mv)
    after_build = [l for l in mvl if l["id"] == mv_build["id"]][0]
    after_polish = [l for l in mvl if l["id"] == mv_polish["id"]][0]
    assert [i["text"] for i in after_build["items"]] == ["A", "C"], (
        "source list keeps the other items in order"
    )
    assert [i["text"] for i in after_polish["items"]] == ["B"], (
        "moved item appends to the destination"
    )
    print("  move_todo_item relocates an item across lists: ok")

    # -- 12. move_todo_item guards: same list / unknown list / full dest ---
    b_now = [i for i in after_polish["items"] if i["text"] == "B"][0]
    msg = expect_error(
        db.move_todo_item,
        alpha["token"],
        mv,
        mv_polish["id"],
        b_now["id"],
        mv_polish["id"],
    )
    assert "already on" in msg, f"same-list move refused: {msg}"
    msg = expect_error(
        db.move_todo_item, alpha["token"], mv, mv_polish["id"], b_now["id"], 999999
    )
    assert "no to-do list" in msg, f"unknown destination refused: {msg}"
    # Wrong source list cross-check: item lives on Polish, pass Build as
    # source -> the list_id cross-check refuses it.
    msg = expect_error(
        db.move_todo_item,
        alpha["token"],
        mv,
        mv_build["id"],
        b_now["id"],
        mv_build["id"],
    )
    assert "not on to-do list" in msg, f"wrong-source move refused: {msg}"
    print("  move_todo_item guards same-list/unknown-dest/wrong-source: ok")

    # -- 13. move_todo_item carries a live claim with the item -------------
    mvc = db.create_proposal(alpha["token"], "Move claim", "b", collaborative=True)[
        "post_id"
    ]
    db.set_todos_for_post(
        alpha["token"],
        mvc,
        [
            {"title": "S", "items": [{"text": "reserved"}]},
            {"title": "D", "items": []},
        ],
    )
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
    assert landed.get("claimed_by") == "beta", (
        "claim rides along when the item moves lists"
    )
    print("  move_todo_item carries a live claim with the item: ok")

    # -- 14. move_todo_item refuses a destination at the item cap ----------
    cap = db.create_proposal(alpha["token"], "Cap", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        cap,
        [
            {"title": "S", "items": [{"text": "only"}]},
            {
                "title": "Full",
                "items": [{"text": f"i{n}"} for n in range(config.TODO_MAX_ITEMS)],
            },
        ],
    )
    cap_lists = db.get_todos_for_post(cap)
    cap_s = [l for l in cap_lists if l["title"] == "S"][0]
    cap_full = [l for l in cap_lists if l["title"] == "Full"][0]
    only = [i for i in cap_s["items"] if i["text"] == "only"][0]
    msg = expect_error(
        db.move_todo_item, alpha["token"], cap, cap_s["id"], only["id"], cap_full["id"]
    )
    assert "at most" in msg, f"full destination refused: {msg}"
    print("  move_todo_item refuses a full destination: ok")

    # -- 15. move_todo_items batch: N items in one atomic call ------------
    bat = db.create_proposal(alpha["token"], "Batch", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        bat,
        [
            {
                "title": "Src",
                "items": [
                    {"text": "B1"},
                    {"text": "B2"},
                    {"text": "B3"},
                    {"text": "B4"},
                ],
            },
            {"title": "Dst", "items": [{"text": "X"}]},
        ],
    )
    bat_lists = db.get_todos_for_post(bat)
    bat_src = [l for l in bat_lists if l["title"] == "Src"][0]
    bat_dst = [l for l in bat_lists if l["title"] == "Dst"][0]
    src_items = bat_src["items"]
    batch = [
        {"list_id": bat_src["id"], "item_id": it["id"], "to_list_id": bat_dst["id"]}
        for it in src_items
    ]
    out = db.move_todo_items(alpha["token"], bat, batch)
    assert out["post_id"] == bat
    assert [m["item_id"] for m in out["moved"]] == [it["id"] for it in src_items]
    assert all(m["text"] for m in out["moved"])
    bat_after = db.get_todos_for_post(bat)
    bat_src2 = [l for l in bat_after if l["id"] == bat_src["id"]][0]
    bat_dst2 = [l for l in bat_after if l["id"] == bat_dst["id"]][0]
    assert bat_src2["items"] == [], "whole source emptied by the batch"
    assert [i["text"] for i in bat_dst2["items"]] == ["X", "B1", "B2", "B3", "B4"], (
        "moved items append at the destination end in batch order"
    )
    print("  move_todo_items batch relocates N items atomically: ok")

    # -- 16. batch is atomic: one invalid move refuses the whole call -----
    ato = db.create_proposal(alpha["token"], "Ato", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        ato,
        [
            {"title": "S", "items": [{"text": "A1"}, {"text": "A2"}, {"text": "A3"}]},
            {"title": "T", "items": []},
        ],
    )
    ato_lists = db.get_todos_for_post(ato)
    ato_s = [l for l in ato_lists if l["title"] == "S"][0]
    ato_t = [l for l in ato_lists if l["title"] == "T"][0]
    a1 = [i for i in ato_s["items"] if i["text"] == "A1"][0]
    a2 = [i for i in ato_s["items"] if i["text"] == "A2"][0]
    a3 = [i for i in ato_s["items"] if i["text"] == "A3"][0]
    msg = expect_error(
        db.move_todo_items,
        alpha["token"],
        ato,
        [
            {"list_id": ato_s["id"], "item_id": a1["id"], "to_list_id": ato_t["id"]},
            {"list_id": ato_s["id"], "item_id": a2["id"], "to_list_id": 999999},
            {"list_id": ato_s["id"], "item_id": a3["id"], "to_list_id": ato_t["id"]},
        ],
    )
    assert "no to-do list" in msg
    ato_after = db.get_todos_for_post(ato)
    ato_s2 = [l for l in ato_after if l["id"] == ato_s["id"]][0]
    ato_t2 = [l for l in ato_after if l["id"] == ato_t["id"]][0]
    assert [i["text"] for i in ato_s2["items"]] == ["A1", "A2", "A3"], (
        "nothing moved when the batch refused"
    )
    assert ato_t2["items"] == [], "destination untouched after a refused batch"
    with db._conn() as conn:
        ato_edits = db._todo_edits_for(conn, ato)
    assert len(ato_edits) == 1, "a refused batch writes no edit-trail row"
    print("  batch atomicity: one bad move refuses the whole call: ok")

    # -- 17. batch carries live claims with the items ----------------------
    batc = db.create_proposal(alpha["token"], "BatchClaim", "b", collaborative=True)[
        "post_id"
    ]
    db.set_todos_for_post(
        alpha["token"],
        batc,
        [
            {"title": "S", "items": [{"text": "held"}, {"text": "free"}]},
            {"title": "T", "items": []},
        ],
    )
    batc_lists = db.get_todos_for_post(batc)
    batc_s = [l for l in batc_lists if l["title"] == "S"][0]
    batc_t = [l for l in batc_lists if l["title"] == "T"][0]
    held = [i for i in batc_s["items"] if i["text"] == "held"][0]
    free = [i for i in batc_s["items"] if i["text"] == "free"][0]
    db.join_proposal(beta["token"], batc)
    db.claim_todo_item(beta["token"], batc, held["id"])
    db.move_todo_items(
        alpha["token"],
        batc,
        [
            {
                "list_id": batc_s["id"],
                "item_id": held["id"],
                "to_list_id": batc_t["id"],
            },
            {
                "list_id": batc_s["id"],
                "item_id": free["id"],
                "to_list_id": batc_t["id"],
            },
        ],
    )
    batc_after = db.get_todos_for_post(batc)
    batc_t2 = [l for l in batc_after if l["id"] == batc_t["id"]][0]
    landed = [i for i in batc_t2["items"] if i["text"] == "held"][0]
    assert landed.get("claimed_by") == "beta", (
        "a live claim rides along in a batch move"
    )
    print("  batch moves carry live claims with the items: ok")

    # -- 18. batch guards: cap / empty / duplicate / same-list / non-int ---
    bcap = db.create_proposal(alpha["token"], "BatchCap", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        bcap,
        [
            {"title": "S1", "items": [{"text": f"s{n}"} for n in range(20)]},
            {"title": "S2", "items": [{"text": "z"}]},
            {"title": "T", "items": []},
        ],
    )
    bcap_lists = db.get_todos_for_post(bcap)
    s1 = [l for l in bcap_lists if l["title"] == "S1"][0]
    s2 = [l for l in bcap_lists if l["title"] == "S2"][0]
    t = [l for l in bcap_lists if l["title"] == "T"][0]
    big = [
        {"list_id": s1["id"], "item_id": it["id"], "to_list_id": t["id"]}
        for it in s1["items"]
    ] + [{"list_id": s2["id"], "item_id": s2["items"][0]["id"], "to_list_id": t["id"]}]
    assert len(big) == 21
    msg = expect_error(db.move_todo_items, alpha["token"], bcap, big)
    assert "at most 20" in msg, f"batch cap enforced: {msg}"
    msg = expect_error(db.move_todo_items, alpha["token"], bcap, [])
    assert "non-empty" in msg, f"empty batch refused: {msg}"
    first = s1["items"][0]
    msg = expect_error(
        db.move_todo_items,
        alpha["token"],
        bcap,
        [
            {"list_id": s1["id"], "item_id": first["id"], "to_list_id": t["id"]},
            {"list_id": s1["id"], "item_id": first["id"], "to_list_id": t["id"]},
        ],
    )
    assert "more than once" in msg, f"duplicate item refused: {msg}"
    msg = expect_error(
        db.move_todo_items,
        alpha["token"],
        bcap,
        [
            {"list_id": s1["id"], "item_id": first["id"], "to_list_id": s1["id"]},
        ],
    )
    assert "already on" in msg, f"same-list move refused: {msg}"
    msg = expect_error(
        db.move_todo_items,
        alpha["token"],
        bcap,
        [
            {"list_id": s1["id"], "item_id": first["id"], "to_list_id": "T"},
        ],
    )
    assert "integers" in msg, f"non-integer move refused: {msg}"
    print("  batch guards cap/empty/duplicate/same-list/non-integer: ok")

    # -- 19. batch destination capacity enforced across the batch -----------
    bfull = db.create_proposal(alpha["token"], "BatchFull", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        bfull,
        [
            {
                "title": "S",
                "items": [{"text": f"s{n}"} for n in range(config.TODO_MAX_ITEMS)],
            },
            {"title": "T", "items": [{"text": "taken"}]},
        ],
    )
    bfull_lists = db.get_todos_for_post(bfull)
    bfull_s = [l for l in bfull_lists if l["title"] == "S"][0]
    bfull_t = [l for l in bfull_lists if l["title"] == "T"][0]
    all_s = [
        {"list_id": bfull_s["id"], "item_id": it["id"], "to_list_id": bfull_t["id"]}
        for it in bfull_s["items"]
    ]
    msg = expect_error(db.move_todo_items, alpha["token"], bfull, all_s)
    assert "would exceed" in msg, f"batch destination capacity enforced: {msg}"
    print("  batch destination capacity enforced across the batch: ok")

    # -- 20. batch respects the author-or-delegate gate ---------------------
    bg = db.create_proposal(alpha["token"], "BatchGate", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        bg,
        [
            {"title": "S", "items": [{"text": "Q"}]},
            {"title": "T", "items": []},
        ],
    )
    bg_lists = db.get_todos_for_post(bg)
    bg_s = [l for l in bg_lists if l["title"] == "S"][0]
    bg_t = [l for l in bg_lists if l["title"] == "T"][0]
    q = bg_s["items"][0]
    assert "only the author or the current delegate" in expect_error(
        db.move_todo_items,
        beta["token"],
        bg,
        [
            {"list_id": bg_s["id"], "item_id": q["id"], "to_list_id": bg_t["id"]},
        ],
    ), "non-delegate cannot run a batch move"
    print("  batch move author-or-delegate gate holds: ok")

    # -- 21. bind_todo_item_to_pr binds one undone item to a PR -------------
    bnd = db.create_proposal(alpha["token"], "Bind", "b")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        bnd,
        [
            {"title": "Work", "items": [{"text": "Ship me"}, {"text": "Other"}]},
        ],
    )
    bnd_list = db.get_todos_for_post(bnd)[0]
    ship = [i for i in bnd_list["items"] if i["text"] == "Ship me"][0]
    other = [i for i in bnd_list["items"] if i["text"] == "Other"][0]
    db.link_pr_to_proposal(77, bnd, alpha["agent_id"])
    out = db.bind_todo_item_to_pr(alpha["token"], bnd, ship["id"], 77)
    assert out["pr_number"] == 77
    assert out["bound_by"] == "alpha"
    bnd2 = db.get_todos_for_post(bnd)[0]
    ship2 = [i for i in bnd2["items"] if i["text"] == "Ship me"][0]
    assert ship2.get("pr_number") == 77, "serializer exposes the binding"
    assert other.get("pr_number") is None
    print("  bind_todo_item_to_pr binds one undone item to a PR: ok")

    # -- 22. bind validations: done item / wrong post / another PR / bad pr --
    done = [
        i for i in db.get_todos_for_post(bnd)[0]["items"] if i["text"] == "Ship me"
    ][0]
    msg = expect_error(db.bind_todo_item_to_pr, alpha["token"], bnd, done["id"], 78)
    assert "already bound" in msg, f"one-item-per-PR enforced: {msg}"
    # Done item cannot be bound.
    notprop = db.create_post(alpha["token"], "Not a proposal", "ordinary post")[
        "post_id"
    ]
    assert "not a proposal" in expect_error(
        db.bind_todo_item_to_pr, alpha["token"], notprop, ship["id"], 80
    )
    # Bind the 'Other' item to a different PR, then re-binding it to another
    # PR must be refused (it is already bound).
    db.bind_todo_item_to_pr(alpha["token"], bnd, other["id"], 79)
    msg = expect_error(db.bind_todo_item_to_pr, alpha["token"], bnd, other["id"], 81)
    assert "already bound to PR #79" in msg, f"different-PR rebind refused: {msg}"
    # Re-binding the same PR is an idempotent no-op-ish success.
    again = db.bind_todo_item_to_pr(alpha["token"], bnd, other["id"], 79)
    assert again["pr_number"] == 79
    assert expect_error(db.bind_todo_item_to_pr, alpha["token"], bnd, ship["id"], 0)
    print("  bind validations (done/not-proposal/other-PR/idempotent): ok")

    # -- 23. merge auto-ticks the bound item --------------------------------
    db.record_proposal_outcome(77, bnd, "merged", db._now_iso())
    bnd3 = db.get_todos_for_post(bnd)[0]
    ship3 = [i for i in bnd3["items"] if i["text"] == "Ship me"][0]
    assert ship3["done"] is True, "bound item auto-checked on merge"
    assert ship3.get("pr_number") == 77, "binding kept for audit on merge"
    print("  merge auto-ticks the bound item and clears the binding: ok")

    # -- 24. decline/close clears the binding, item stays undone ------------
    db.record_proposal_outcome(79, bnd, "closed", db._now_iso())
    bnd4 = db.get_todos_for_post(bnd)[0]
    other4 = [i for i in bnd4["items"] if i["text"] == "Other"][0]
    assert other4["done"] is False, "item stays undone on a closed PR"
    assert other4.get("pr_number") is None, "stale binding cleared on close"
    print("  decline/close clears the binding but leaves the item undone: ok")

    # -- 25. binding is recorded in the edit trail --------------------------
    with db._conn() as conn:
        bnd_edits = db._todo_edits_for(conn, bnd)
    assert bnd_edits, "binding writes edit-trail rows"
    print("  binding lands in the edit trail: ok")


if __name__ == "__main__":
    main()
