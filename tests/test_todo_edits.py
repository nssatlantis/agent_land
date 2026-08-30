"""Tests for the to-do edit trail (todo_edits table).

Every set_todos_for_post call now snapshots the full before/after state
into the todo_edits table, so a destructive wipe is recoverable and
auditable.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_edits_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import moderation  # noqa: E402
from db._proposal_todos import (  # noqa: E402
    _resolved_state_for_post,
    _store_todo_edit,
)
from tests._setup import db, init, setup  # noqa: E402


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
    assert e["old_lists"] == [], (
        f"first edit old_lists should be [], got {e['old_lists']}"
    )
    assert len(e["new_lists"]) == 1, "first edit new_lists should have 1 list"
    assert e["new_lists"][0]["title"] == "Phase 1"
    assert e["editor"] == "alpha"
    print("  first update creates edit with empty old_lists: ok")

    # -- 2. Second update captures the previous state ----------------------
    lists2 = [
        {
            "title": "Phase 1",
            "items": [{"text": "Step A"}, {"text": "Step B"}, {"text": "Step C"}],
        },
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
    db.set_todos_for_post(
        alpha["token"], pid2, [{"title": "L", "items": [{"text": "I"}]}]
    )
    with db._conn() as conn:
        assert db._todo_edits_for(conn, pid2), "should have edits"
    moderation.delete_post(pid2, "admin")
    with db._conn() as conn:
        assert db._todo_edits_for(conn, pid2) == [], (
            "edits should be gone after post delete"
        )
    print("  todo_edits cascade-deletes on post removal: ok")

    # -- 6. tod_edits persist across multiple updates ----------------------
    pid3 = db.create_proposal(alpha["token"], "Persist", "Body.")["post_id"]
    db.set_todos_for_post(alpha["token"], pid3, [{"title": "A", "items": []}])
    db.set_todos_for_post(
        alpha["token"], pid3, [{"title": "A", "items": [{"text": "X"}]}]
    )
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid3)
    assert len(edits) == 2, f"expected 2 edits, got {len(edits)}"
    db.set_todos_for_post(
        alpha["token"], pid3, [{"title": "A", "items": [{"text": "X"}]}]
    )
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

    # -- 9. New rows store only the after side, as compact JSON ------------
    pid7 = db.create_proposal(alpha["token"], "Compact", "Body.")["post_id"]
    db.set_todos_for_post(
        alpha["token"], pid7, [{"title": "L1", "items": [{"text": "A"}]}]
    )
    db.set_todos_for_post(
        alpha["token"],
        pid7,
        [
            {"title": "L1", "items": [{"text": "A"}, {"text": "B"}]},
            {"title": "L2", "items": []},
        ],
    )
    with db._conn() as conn:
        raw = conn.execute(
            "SELECT old_lists, new_lists FROM todo_edits WHERE post_id = ? ORDER BY id",
            (pid7,),
        ).fetchall()
    assert len(raw) == 2
    for row in raw:
        assert row["old_lists"] == "", (
            "new-format row stores the '' sentinel, not a second snapshot"
        )
        expected = json.dumps(json.loads(row["new_lists"]), separators=(",", ":"))
        assert row["new_lists"] == expected, (
            "new_lists stored without separator whitespace (compact JSON)"
        )
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid7)
    assert len(edits) == 2
    assert edits[0]["old_lists"] == [], "first edit before side derives to []"
    assert edits[1]["old_lists"] == edits[0]["new_lists"], (
        "derived before side equals the previous edit's after side"
    )
    assert edits[1]["new_lists"][0]["items"][-1]["text"] == "B"
    assert edits[1]["new_lists"][1]["title"] == "L2"
    print("  compact rows reconstruct the full before/after trail: ok")

    # -- 10. Mixed-era chains: legacy rows keep their own snapshot ----------
    pid8 = db.create_proposal(alpha["token"], "Mixed era", "Body.")["post_id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO todo_edits (post_id, editor_agent_id, old_lists, new_lists)"
            " VALUES (?, ?, ?, ?)",
            (
                pid8,
                alpha["agent_id"],
                "[]",
                json.dumps([{"title": "Legacy", "items": [{"text": "L"}]}]),
            ),
        )
    # a real mutation then writes the compact format on top of the legacy row
    db.set_todos_for_post(
        alpha["token"],
        pid8,
        [{"title": "Legacy", "items": [{"text": "L"}, {"text": "L2"}]}],
    )
    with db._conn() as conn:
        edits = db._todo_edits_for(conn, pid8)
        batch = db._todo_edits_batch(conn, [pid8])
    assert len(edits) == 2
    assert edits[0]["old_lists"] == [], "legacy first row keeps its [] snapshot"
    assert edits[0]["new_lists"][0]["title"] == "Legacy"
    assert edits[1]["old_lists"] == edits[0]["new_lists"], (
        "compact before side derives from the legacy row's after side"
    )
    assert edits[1]["new_lists"][0]["items"][-1]["text"] == "L2"
    assert batch[pid8] == edits, "batch reader reconstructs the same trail"
    print("  mixed legacy/compact chains reconstruct correctly: ok")

    # -- 11. Small mutations store a compact delta row ---------------------
    pid9 = db.create_proposal(alpha["token"], "Delta", "Body.")["post_id"]
    db.set_todos_for_post(
        alpha["token"], pid9, [{"title": "L", "items": [{"text": "A"}, {"text": "B"}]}]
    )
    lst = db.get_todos_for_post(pid9)[0]
    a_item, b_item = lst["items"][0], lst["items"][1]
    # A tick and a rename are small diffable changes -> delta rows.
    db.tick_todo_item(alpha["token"], pid9, b_item["id"])
    db.update_todo_item(alpha["token"], pid9, lst["id"], a_item["id"], "A-renamed")
    with db._conn() as conn:
        raw = conn.execute(
            "SELECT new_lists FROM todo_edits WHERE post_id = ? ORDER BY id", (pid9,)
        ).fetchall()
        edits = db._todo_edits_for(conn, pid9)
    assert len(raw) == 3
    assert raw[0]["new_lists"].lstrip().startswith("["), "first edit is a snapshot"
    for r in raw[1:]:
        assert '"type":"delta"' in r["new_lists"], (
            "small mutation stored as a delta row"
        )
    assert len(edits) == 3
    # Round-trip: each edit's old_lists equals the previous edit's new_lists.
    for i in range(1, 3):
        assert edits[i]["old_lists"] == edits[i - 1]["new_lists"], (
            "delta chain derives the same before/after trail as snapshots"
        )
    assert edits[2]["new_lists"][0]["items"][0]["text"] == "A-renamed"
    assert edits[2]["new_lists"][0]["items"][1]["done"] is True
    print("  small mutations store compact delta rows, trail reconstructs: ok")

    # -- 12. Wholesale structural change falls back to a full snapshot -----
    pid10 = db.create_proposal(alpha["token"], "Snapshot", "Body.")["post_id"]
    db.set_todos_for_post(
        alpha["token"],
        pid10,
        [{"title": "L1", "items": [{"text": "A"}, {"text": "B"}]}],
    )
    # A distinct rewrite (removes a list, renames, adds a list) is big enough
    # that the writer stores an exact snapshot, not a sprawling delta.
    db.set_todos_for_post(
        alpha["token"],
        pid10,
        [
            {"title": "L1", "items": [{"text": "A"}]},
            {"title": "L2", "items": [{"text": "C"}]},
        ],
    )
    with db._conn() as conn:
        raw = conn.execute(
            "SELECT new_lists FROM todo_edits WHERE post_id = ? ORDER BY id", (pid10,)
        ).fetchall()
        edits = db._todo_edits_for(conn, pid10)
    assert len(raw) == 2
    assert '"type":"delta"' not in raw[1]["new_lists"], (
        "wholesale rewrite stored as a full snapshot"
    )
    assert edits[1]["new_lists"][1]["title"] == "L2"
    assert edits[1]["old_lists"] == edits[0]["new_lists"]
    print("  wholesale structural change stores a full snapshot: ok")

    # -- 13. Round-trip verify backstop: an underivable change -> snapshot --
    pid11 = db.create_proposal(alpha["token"], "Verify backstop", "Body.")["post_id"]
    db.set_todos_for_post(
        alpha["token"], pid11, [{"title": "L", "items": [{"text": "X"}, {"text": "Y"}]}]
    )
    with db._conn() as conn:
        prev = _resolved_state_for_post(conn, pid11)
        # A same-list reorder (same item ids, swapped order) is not
        # representable by tick/ren/add/del ops, so the round-trip verify
        # must reject the delta and store an exact snapshot.
        reordered = [dict(lst) for lst in prev]
        reordered[0]["items"] = [
            dict(reordered[0]["items"][1]),
            dict(reordered[0]["items"][0]),
        ]
        _store_todo_edit(conn, pid11, alpha["agent_id"], reordered)
        raw = conn.execute(
            "SELECT new_lists FROM todo_edits WHERE post_id = ? ORDER BY id DESC LIMIT 1",
            (pid11,),
        ).fetchone()
        edits = db._todo_edits_for(conn, pid11)
    assert '"type":"delta"' not in raw["new_lists"], (
        "underivable change falls back to a full snapshot"
    )
    assert [it["text"] for it in edits[-1]["new_lists"][0]["items"]] == ["Y", "X"], (
        "snapshot preserves the reordered state exactly"
    )
    print("  round-trip verify falls back to snapshot for underivable change: ok")

    print("\ntest_todo_edits: all assertions passed")


if __name__ == "__main__":
    main()
