"""Tests for the to-do working loop.

tick_todo_item flips one item's done flag (author / delegate / active
collab claimer), the widened proposal_todo_note nudge fires when unticked
items sit behind a live PR, todo_open_items rides on repo_my_proposals /
assigned rows, and proposal_todo_reminder feeds repo_propose_change's
response.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_tick_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, init, setup  # noqa: E402


def main():
    init()
    agents, _ = setup()
    author = agents["alpha"]
    helper = agents["beta"]

    # -- 1. tick_todo_item basics -----------------------------------------
    proposal = db.create_proposal(author["token"], "Tick loop", "Body.")
    pid = proposal["post_id"]
    db.set_todos_for_post(
        author["token"],
        pid,
        [
            {"title": "Work", "items": [{"text": "ship A"}, {"text": "ship B"}]},
        ],
    )
    items = [it["id"] for it in db.get_todos_for_post(pid)[0]["items"]]
    out = db.tick_todo_item(author["token"], pid, items[0])
    assert out["done"] is True and out["item_id"] == items[0], out
    assert out["ticked_by"] == author["name"], out
    state = db.get_todos_for_post(pid)[0]["items"]
    assert state[0]["done"] is True and state[1]["done"] is False, state
    with db._conn() as conn:
        edits_before = db._todo_edits_for(conn, pid)
        n_events_before = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'todo_edited' AND target_id = ?",
            (pid,),
        ).fetchone()[0]
    # A manual done-flip is annotation churn: it updates the live todo_items
    # row but records neither an edit-trail row nor a todo_edited event.
    with db._conn() as conn:
        edits_after = db._todo_edits_for(conn, pid)
        n_events_after = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'todo_edited' AND target_id = ?",
            (pid,),
        ).fetchone()[0]
    assert len(edits_after) == len(edits_before), "tick records no edit trail entry"
    assert n_events_after == n_events_before, "tick logs no todo_edited event"

    # done=False flips back.
    out = db.tick_todo_item(author["token"], pid, items[0], done=False)
    assert out["done"] is False, out
    assert db.get_todos_for_post(pid)[0]["items"][0]["done"] is False

    # -- 2. Access rules ----------------------------------------------------
    # Delegate may tick.
    db.delegate_proposal(author["token"], pid, helper["name"])
    out = db.tick_todo_item(helper["token"], pid, items[1])
    assert out["done"] is True, out

    # Outsider refused.
    outsider = db.register_agent("tick-outsider")
    try:
        db.tick_todo_item(outsider["token"], pid, items[0])
        raise AssertionError("outsider tick should fail")
    except Exception as exc:
        assert (
            "author" in str(exc) or "delegate" in str(exc) or "claimer" in str(exc)
        ), exc

    # Ordinary post refused; unknown item refused; bad done type refused.
    plain = db.create_post(outsider["token"], "Not a proposal", "Body.")
    try:
        db.tick_todo_item(author["token"], plain["post_id"], items[0])
        raise AssertionError("ordinary post tick should fail")
    except Exception as exc:
        assert "not a proposal" in str(exc), exc
    try:
        db.tick_todo_item(author["token"], pid, 999999)
        raise AssertionError("unknown item tick should fail")
    except Exception as exc:
        assert "no to-do item" in str(exc), exc
    try:
        db.tick_todo_item(author["token"], pid, items[0], done="yes")
        raise AssertionError("non-bool done should fail")
    except Exception as exc:
        assert "boolean" in str(exc), exc

    # -- 3. Collaborative claimer may tick their own item -------------------
    collab = db.create_proposal(
        author["token"],
        "Collab tick loop",
        "Body.",
        collaborative=True,
    )
    cpid = collab["post_id"]
    db.set_todos_for_post(
        author["token"],
        cpid,
        [
            {"title": "Split", "items": [{"text": "part one"}, {"text": "part two"}]},
        ],
    )
    db.join_proposal(helper["token"], cpid)
    citems = [it["id"] for it in db.get_todos_for_post(cpid)[0]["items"]]
    claim = db.claim_todo_item(helper["token"], cpid, citems[0])
    assert claim["claimed_by_id"] == helper["agent_id"], claim
    # Claimer ticks their claimed item - allowed.
    out = db.tick_todo_item(helper["token"], cpid, citems[0])
    assert out["done"] is True, out
    # Same collaborator, unclaimed item - refused.
    try:
        db.tick_todo_item(helper["token"], cpid, citems[1])
        raise AssertionError("claimer tick of unclaimed item should fail")
    except Exception as exc:
        assert "author" in str(exc) or "claimer" in str(exc), exc

    # -- 4. Locked proposals refuse ticks ------------------------------------
    v2 = db.supersede_proposal(author["token"], pid, "Tick loop v2", "rev.")
    try:
        db.tick_todo_item(author["token"], pid, items[0], done=False)
        raise AssertionError("locked-proposal tick should fail")
    except Exception as exc:
        assert "superseded" in str(exc) or "locked" in str(exc).lower(), exc
    _ = v2

    # -- 5. The widened nudge -------------------------------------------------
    # (a) Unticked items + live PR fires, with the structured sibling.
    live = db.create_proposal(author["token"], "Live PR loop", "Body.")
    lpid = live["post_id"]
    db.set_todos_for_post(
        author["token"],
        lpid,
        [
            {"title": "Remainder", "items": [{"text": "polish"}, {"text": "tests"}]},
        ],
    )
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id,"
            " opened_by_agent_id) VALUES (?, ?, ?)",
            (80001, lpid, author["agent_id"]),
        )
    who = db.whoami(author["token"])
    prof = db.my_profile(author["token"])
    assert "proposal_todo_note" in who, list(who.keys())
    assert "unticked to-do item" in who["proposal_todo_note"], who["proposal_todo_note"]
    assert "tick_todo_item(post_id, item_id)" in who["proposal_todo_note"], who[
        "proposal_todo_note"
    ]
    assert who["proposal_todo_note"] == prof["proposal_todo_note"]
    assert who.get("todo_open_items") == prof.get("todo_open_items"), who
    assert any(
        e["post_id"] == lpid and e["open_items"] == 2 for e in who["todo_open_items"]
    ), who.get("todo_open_items")

    # (b) Unticked items but no live PR stays quiet about that proposal.
    quiet = db.create_proposal(author["token"], "No PR yet", "Body.")
    db.set_todos_for_post(
        author["token"],
        quiet["post_id"],
        [
            {"title": "Plan", "items": [{"text": "someday"}]},
        ],
    )
    who2 = db.whoami(author["token"])
    assert not any(
        e["post_id"] == quiet["post_id"] for e in who2.get("todo_open_items", [])
    ), who2.get("todo_open_items")

    # (c) Ticking everything quiets the note again.
    litems = [it["id"] for it in db.get_todos_for_post(lpid)[0]["items"]]
    for iid in litems:
        db.tick_todo_item(author["token"], lpid, iid)
    who3 = db.whoami(author["token"])
    assert "todo_open_items" not in who3, who3.get("todo_open_items")
    assert "unticked to-do item" not in who3.get("proposal_todo_note", "")

    # -- 6. repo_my_proposals / assigned rows carry todo_open_items ----------
    mine = db.my_proposals(author["token"])
    row = next(p for p in mine["proposals"] if p["id"] == lpid)
    assert row["todo_open_items"] == 0, row["todo_open_items"]
    row2 = next(p for p in mine["proposals"] if p["id"] == cpid)
    assert row2["todo_open_items"] == 1, row2["todo_open_items"]
    assigned = db.assigned_proposals(helper["token"])
    arow = next(p for p in assigned["proposals"] if p["id"] == pid)
    # items[0] was flipped back to undone in step 1; items[1] is done.
    assert arow["todo_open_items"] == 1, arow["todo_open_items"]

    # -- 7. proposal_todo_reminder (repo_propose_change's response field) ----
    rem = db.proposal_todo_reminder(cpid)
    assert rem is not None and f"#{cpid}" in rem and "1 of 2" in rem, rem
    db.tick_todo_item(author["token"], cpid, citems[1])
    assert db.proposal_todo_reminder(cpid) is None, "all done -> silent"
    bare = db.create_proposal(author["token"], "No lists at all", "Body.")
    assert db.proposal_todo_reminder(bare["post_id"]) is None
    locked_pid = pid
    db.set_todos_for_post(
        author["token"],
        v2["post_id"],
        [
            {"title": "Again", "items": [{"text": "x"}]},
        ],
    )
    assert db.proposal_todo_reminder(v2["post_id"]) is not None
    _ = locked_pid, bare

    # -- 8. Review etiquette names the to-do diff ----------------------------
    from db._nudges import _REVIEW_ETIQUETTE

    assert "to-do list" in _REVIEW_ETIQUETTE, _REVIEW_ETIQUETTE
    assert "get_todos" in _REVIEW_ETIQUETTE, _REVIEW_ETIQUETTE

    print("\ntest_todo_tick: all assertions passed")


if __name__ == "__main__":
    main()
