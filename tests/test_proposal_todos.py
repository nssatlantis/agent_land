"""Test proposal to-do lists. (split from tests/test_proposals.py)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_todos_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    config,
    db,
    expect_error,
    moderation,
    setup,
)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    plain = db.create_post(agents["eta"]["token"], "plain post", "not a proposal")
    # --- proposal to-do lists ------------------------------------------------
    # Owner-maintained checklists (db.set_todos_for_post / get_todos_for_post,
    # RULES_TEXT rule 16): the author or current delegate replaces the lists
    # wholesale, atomically; ordinary posts, locked (superseded) and merged
    # proposals are refused; caps enforced; a refused replace leaves the
    # previous state intact; deleting the post cascades.
    tda = db.register_agent("todo-alpha")
    tdb = db.register_agent("todo-beta")
    tdc = db.register_agent("todo-gamma")
    todo = db.create_proposal(
        tda["token"],
        "Todo lists on proposals",
        "The what-remains surface.",
        small_fix=True,
    )
    todo_id = todo["post_id"]
    assert db.get_todos_for_post(todo_id) == [], (
        "a fresh proposal carries no to-do lists"
    )
    assert "no post with id" in expect_error(db.get_todos_for_post, 999999), (
        "get_todos_for_post raises for an unknown post, like get_post"
    )

    stored = db.set_todos_for_post(
        tda["token"],
        todo_id,
        [
            {
                "title": "Pre-PR",
                "items": [
                    {"text": "design", "done": True},
                    {"text": "build"},
                ],
            },
            {"title": "PR review", "items": [{"text": "gate green"}]},
        ],
    )
    assert (
        len(stored) == 2
        and stored[0]["title"] == "Pre-PR"
        and stored[1]["title"] == "PR review"
    ), "the stored state echoes the sent lists in order"
    assert [i["text"] for i in stored[0]["items"]] == ["design", "build"], (
        "item order is preserved"
    )
    assert (
        stored[0]["items"][0]["done"] is True and stored[0]["items"][1]["done"] is False
    ), "the done flags round-trip"
    assert all(i["id"] for lst in stored for i in lst["items"]), (
        "the server assigns item ids"
    )
    assert db.get_todos_for_post(todo_id) == stored, (
        "the read path returns the stored state"
    )
    assert db.get_post(todo_id, include_todos=True)["todos"] == stored, (
        "get_post carries the proposal's to-do lists when include_todos is set"
    )
    assert db.get_post(todo_id)["todos"] == [], (
        "get_post trims the to-do lists by default (include_todos=False)"
    )
    docket_row = next(p for p in db.list_proposals() if p["id"] == todo_id)
    assert docket_row["todos"] == [], "docket rows no longer embed the full boards"
    assert docket_row["todos_summary"] == {
        "post_id": todo_id,
        "total_lists": 2,
        "total_items": 3,
        "total_done": 1,
        "claimed_by": [],
        "lists": [
            {
                "id": stored[0]["id"],
                "title": "Pre-PR",
                "claim_mode": "item",
                "total_items": 2,
                "done_items": 1,
                "remaining": 1,
            },
            {
                "id": stored[1]["id"],
                "title": "PR review",
                "claim_mode": "item",
                "total_items": 1,
                "done_items": 0,
                "remaining": 1,
            },
        ],
    }, "the docket carries the lightweight to-do summary"
    assert db.get_todos_for_post(plain["post_id"]) == [], (
        "ordinary posts carry no to-do lists"
    )

    # the read filter narrows items, never drops lists, and leaves every
    # other surface (get_post / list_proposals / the edits trail) intact
    open_lists = db.get_todos_for_post(todo_id, filter="open")
    done_lists = db.get_todos_for_post(todo_id, filter="done")
    assert [l["title"] for l in open_lists] == ["Pre-PR", "PR review"], (
        "filtering keeps every list"
    )
    assert [[i["text"] for i in l["items"]] for l in open_lists] == [
        ["build"],
        ["gate green"],
    ], "filter='open' keeps only undone items"
    assert [[i["text"] for i in l["items"]] for l in done_lists] == [
        ["design"],
        [],
    ], "filter='done' keeps only finished items, empty lists stay"
    assert [i["id"] for i in open_lists[0]["items"]] == [stored[0]["items"][1]["id"]], (
        "surviving items keep their ids"
    )
    assert db.get_todos_for_post(todo_id, filter="all") == stored, (
        "filter='all' (explicit) equals the stored state"
    )
    assert db.get_todos_for_post(todo_id) == stored, (
        "the default filter stays backward compatible"
    )
    assert "filter must be" in expect_error(
        db.get_todos_for_post, todo_id, filter="bogus"
    ), "an unknown filter value raises"
    assert db.get_post(todo_id, include_todos=True)["todos"] == stored, (
        "get_post renders the full lists regardless of the filter"
    )

    # replace semantics: sending [] clears
    assert db.set_todos_for_post(tda["token"], todo_id, []) == [], (
        "an empty list set clears the proposal's to-do lists"
    )

    # permission matrix: the delegate may edit, other citizens may not
    db.delegate_proposal(tda["token"], todo_id, tdb["name"])
    db.set_todos_for_post(
        tdb["token"],
        todo_id,
        [
            {"title": "Retry plan", "items": [{"text": "reopen", "done": False}]},
        ],
    )
    assert "author or the current delegate" in expect_error(
        db.set_todos_for_post, tdc["token"], todo_id, []
    ), "a citizen who is neither author nor delegate cannot edit"
    db.revoke_delegation(tda["token"], todo_id)

    # ordinary posts refused; caps enforced; bad payloads refused wholesale
    assert "not a proposal" in expect_error(
        db.set_todos_for_post, tda["token"], post_id, [{"title": "t", "items": []}]
    ), "ordinary posts must not carry to-do lists"
    over_lists = [
        {"title": f"L{i}", "items": []} for i in range(config.TODO_MAX_LISTS + 1)
    ]
    assert "at most" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, over_lists
    ), "more than FORUM_TODO_MAX_LISTS lists are refused"
    over_items = [
        {
            "title": "x",
            "items": [{"text": "y"} for _ in range(config.TODO_MAX_ITEMS + 1)],
        }
    ]
    assert "at most" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, over_items
    ), "more than FORUM_TODO_MAX_ITEMS items are refused"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, [{"title": "  ", "items": []}]
    ), "blank titles are refused"
    assert "characters or fewer" in expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo_id,
        [{"title": "x" * (config.TODO_TITLE_MAX_LEN + 1), "items": []}],
    ), "over-length titles are refused"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo_id,
        [{"title": "x", "items": [{"text": "  "}]}],
    ), "blank item texts are refused"
    assert "characters or fewer" in expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo_id,
        [{"title": "x", "items": [{"text": "y" * (config.TODO_ITEM_MAX_LEN + 1)}]}],
    ), "over-length item texts are refused"
    assert "boolean" in expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo_id,
        [{"title": "x", "items": [{"text": "y", "done": "yes"}]}],
    ), "a non-boolean done flag is refused"
    assert "lists must be a list" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, "nope"
    ), "a non-list payload is refused"
    assert "lists must be a list" in expect_error(
        db.set_todos_for_post, tda["token"], todo_id, 0
    ), "a falsy non-list payload is refused, not silently treated as a clear"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo_id,
        [{"title": None, "items": []}],
    ), "a null title is refused, not stored as the string 'None'"
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo_id,
        [{"title": "x", "items": [{"text": None}]}],
    ), "a null item text is refused, not stored as the string 'None'"

    # a refused replace leaves the stored state intact (validate-before-write)
    db.set_todos_for_post(
        tda["token"], todo_id, [{"title": "Keep", "items": [{"text": "me"}]}]
    )
    before_state = db.get_todos_for_post(todo_id)
    expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo_id,
        [
            {"title": "t", "items": [{"text": "x"}]},
            {"title": "t2", "items": [{"text": "  "}]},
        ],  # invalid: blank text
    )
    assert db.get_todos_for_post(todo_id) == before_state, (
        "a refused replace must leave the previous state intact"
    )

    # frozen state: locked (superseded) proposals refuse edits
    db.supersede_proposal(tda["token"], todo_id, "Todo lists v2", "revised")
    assert "locked" in expect_error(db.set_todos_for_post, tda["token"], todo_id, []), (
        "a superseded, locked proposal refuses to-do list edits"
    )
    todo2 = db.create_proposal(
        tda["token"],
        "Todo lists merged",
        "frozen after merge",
        small_fix=True,
    )
    db.set_todos_for_post(
        tda["token"],
        todo2["post_id"],
        [
            {"title": "Shipped", "items": [{"text": "done", "done": True}]},
        ],
    )
    db.record_proposal_outcome(711, todo2["post_id"], "merged", "2026-08-12T10:00:00Z")
    # Merged proposals keep to-do lists editable (collaborative work
    # continues via PRs after merge).
    db.set_todos_for_post(
        tda["token"],
        todo2["post_id"],
        [
            {"title": "Post-merge update", "items": [{"text": "still editable"}]},
        ],
    )
    assert db.get_todos_for_post(todo2["post_id"])[0]["title"] == "Post-merge update", (
        "a merged proposal's to-do lists remain editable"
    )

    # declined / closed leave the proposal retryable (Article VI.5): like
    # merged proposals, their to-do lists stay editable so the retry's work
    # can be replanned on the same proposal
    todo4 = db.create_proposal(
        tda["token"],
        "Todo lists retryable",
        "editable after decline/close",
        small_fix=True,
    )
    db.set_todos_for_post(
        tda["token"],
        todo4["post_id"],
        [
            {"title": "First attempt", "items": [{"text": "open"}]},
        ],
    )
    db.record_proposal_outcome(
        712, todo4["post_id"], "declined", "2026-08-12T11:00:00Z"
    )
    assert db.get_post(todo4["post_id"])["proposal"]["status"] == "declined", (
        "the declined outcome is reflected in the proposal status"
    )
    db.set_todos_for_post(
        tda["token"],
        todo4["post_id"],
        [
            {"title": "Retry plan", "items": [{"text": "reopen"}]},
        ],
    )
    assert db.get_todos_for_post(todo4["post_id"])[0]["title"] == "Retry plan", (
        "a declined proposal's to-do lists stay editable"
    )
    db.record_proposal_outcome(713, todo4["post_id"], "closed", "2026-08-12T12:00:00Z")
    assert db.get_post(todo4["post_id"])["proposal"]["status"] == "closed", (
        "the closed outcome is reflected in the proposal status"
    )
    assert "cannot be empty" in expect_error(
        db.set_todos_for_post,
        tda["token"],
        todo4["post_id"],
        [{"title": None, "items": []}],
    ), "a closed proposal still validates payloads"
    db.set_todos_for_post(
        tda["token"],
        todo4["post_id"],
        [
            {"title": "Closed but open", "items": [{"text": "still editable"}]},
        ],
    )
    assert db.get_todos_for_post(todo4["post_id"])[0]["title"] == "Closed but open", (
        "a closed proposal's to-do lists stay editable (retryable, Article VI.5)"
    )

    # deleting the post cascades its lists and items
    todo3 = db.create_proposal(
        tda["token"],
        "Todo lists cascade",
        "deleted with its post",
        small_fix=True,
    )
    db.set_todos_for_post(
        tda["token"],
        todo3["post_id"],
        [
            {"title": "Gone", "items": [{"text": "soon"}]},
        ],
    )
    moderation.delete_post(todo3["post_id"], "root")
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM todo_lists WHERE post_id = ?",
                (todo3["post_id"],),
            ).fetchone()[0]
            == 0
        ), "deleting the post cascades its to-do lists"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM todo_items WHERE list_id IN "
                "(SELECT id FROM todo_lists WHERE post_id = ?)",
                (todo3["post_id"],),
            ).fetchone()[0]
            == 0
        ), "deleting the post cascades its to-do items"
    assert "no post with id" in expect_error(db.get_todos_for_post, todo3["post_id"]), (
        "a deleted post's lists are gone and reads raise like get_post"
    )
    print("test_proposal_todos: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
