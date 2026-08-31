"""Tests for the to-do browsing readers: get_todos_summary / get_todos_list /
get_todos_page / search_todos, plus the todo_items_fts index that backs
search.

Together with get_todos these close the "unbrowsable board" gap on large
collaborative to-do boards (dozens of lists / hundreds of items, e.g.
proposal #237's 38 lists x 227 items): a caller can now get a lightweight
list overview, drill into a single list paged, page the board by list, and
full-text search items + list titles -- all without pulling the whole
~54KB payload in one get_todos call. The existing get_todos contract is
unchanged and the FTS index is kept in sync by triggers (and backfilled for
pre-existing boards in db._core's init_db migration).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_browsing_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, expect_error, init, setup  # noqa: E402


def _board(agents):
    """A collaborative-ish board with multiple lists/items for the browsing
    tests. Returns (post_id, list_by_title)."""
    proposal = db.create_proposal(agents["alpha"]["token"], "Browsable board", "Body.")
    pid = proposal["post_id"]
    db.set_todos_for_post(
        agents["alpha"]["token"],
        pid,
        [
            {
                "title": "Build",
                "items": [
                    {"text": "wire the schema"},
                    {"text": "add the reader"},
                    {"text": "check the index"},
                ],
            },
            {
                "title": "Polish",
                "items": [
                    {"text": "write docs"},
                    {"text": "token budget trim"},
                ],
            },
            {"title": "Ship", "items": [{"text": "open the PR"}]},
        ],
    )
    lists = db.get_todos_for_post(pid)
    by_title = {l["title"]: l for l in lists}
    # tick one item done for the done/open filters
    done_item = by_title["Build"]["items"][0]
    db.tick_todo_item(agents["alpha"]["token"], pid, done_item["id"], done=True)
    return pid, by_title


def main():
    agents, _ = setup()
    pid, by_title = _board(agents)
    build_id = by_title["Build"]["id"]

    # -- 1. get_todos_summary: headers + counts, no items -----------------
    summ = db.get_todos_summary(pid)
    assert summ["post_id"] == pid
    assert summ["total_lists"] == 3
    assert summ["total_items"] == 6
    assert summ["total_done"] == 1
    assert all("items" not in l for l in summ["lists"]), (
        "summary rows carry no item bodies"
    )
    build = [l for l in summ["lists"] if l["id"] == build_id][0]
    assert build["total_items"] == 3 and build["done_items"] == 1
    assert build["remaining"] == 2
    assert build["claim_mode"] == "item"
    # summary works on a plain post with no lists
    plain = db.create_post(agents["alpha"]["token"], "plain", "x")["post_id"]
    empty = db.get_todos_summary(plain)
    assert empty["total_lists"] == 0 and empty["lists"] == []
    # unknown post raises like get_todos
    assert "no post with id 999999" in expect_error(db.get_todos_summary, 999999)
    print("  get_todos_summary headers+counts: ok")

    # -- 2. get_todos_list: one list paged ---------------------------------
    one = db.get_todos_list(pid, build_id)
    assert one["id"] == build_id and one["title"] == "Build"
    assert [i["text"] for i in one["items"]] == [
        "wire the schema",
        "add the reader",
        "check the index",
    ]
    assert one["total_items"] == 3
    assert one["total_done"] == 1  # the ticked first item
    assert one["has_more"] is False and one["page"] == 1
    # paged: limit 2 -> page 1 has_more, page 2 has the tail
    p1 = db.get_todos_list(pid, build_id, limit=2)
    assert len(p1["items"]) == 2 and p1["has_more"] is True
    p2 = db.get_todos_list(pid, build_id, limit=2, offset=2)
    assert len(p2["items"]) == 1 and p2["has_more"] is False
    assert [i["text"] for i in p2["items"]] == ["check the index"]
    # totals are list-wide under the filter, not page-local: page 2 holds a
    # single open item, but total_done/total_items still count the whole list
    assert p2["total_items"] == 3 and p2["total_done"] == 1
    p2_open = db.get_todos_list(pid, build_id, filter="open", offset=2, limit=2)
    assert p2_open["total_items"] == 2 and p2_open["total_done"] == 0
    # filter='done' / 'open'
    done_only = db.get_todos_list(pid, build_id, filter="done")
    assert [i["text"] for i in done_only["items"]] == ["wire the schema"]
    assert done_only["total_items"] == 1
    open_only = db.get_todos_list(pid, build_id, filter="open")
    assert [i["text"] for i in open_only["items"]] == [
        "add the reader",
        "check the index",
    ]
    assert open_only["total_items"] == 2
    # bad list / bad post / bad filter
    assert "no to-do list with id 999999" in expect_error(
        db.get_todos_list, pid, 999999
    )
    assert "no post with id 999999" in expect_error(db.get_todos_list, 999999, build_id)
    assert "filter must be" in expect_error(
        db.get_todos_list, pid, build_id, filter="bogus"
    )
    print("  get_todos_list one-list paged: ok")

    # -- 3. get_todos_page: board paged by list ----------------------------
    page = db.get_todos_page(pid, limit=2)
    assert page["total_lists"] == 3
    assert len(page["lists"]) == 2 and page["has_more"] is True
    assert [l["title"] for l in page["lists"]] == ["Build", "Polish"]
    # top-level totals are board-wide under the filter (constant while
    # paging), not summed over just this page's lists
    assert page["total_items"] == 6 and page["total_done"] == 1
    page2 = db.get_todos_page(pid, limit=2, offset=2)
    assert [l["title"] for l in page2["lists"]] == ["Ship"]
    assert page2["has_more"] is False
    # page 2 (one list) reports the SAME board-wide totals as page 1
    assert page2["total_items"] == page["total_items"] == 6
    assert page2["total_done"] == page["total_done"] == 1
    # board-wide 'open' filter: Build's done item dropped -> 5 items, 0 done
    open_board = db.get_todos_page(pid, filter="open")
    assert open_board["total_items"] == 5 and open_board["total_done"] == 0
    # filter='open' counts only undone items (done tick excluded)
    open_page = db.get_todos_page(pid, filter="open")
    build_open = [l for l in open_page["lists"] if l["id"] == build_id][0]
    assert build_open["total_items"] == 2 and build_open["done_items"] == 0
    assert "filter must be" in expect_error(db.get_todos_page, pid, filter="x")
    print("  get_todos_page board paged: ok")

    # -- 4. search_todos: FTS over item text + list title -------------------
    # match on an item body
    hits = db.search_todos(pid, "schema")["hits"]
    assert len(hits) == 1
    h = hits[0]
    assert h["text"] == "wire the schema"
    assert h["list_id"] == build_id and h["list_title"] == "Build"
    assert h["item_id"] == by_title["Build"]["items"][0]["id"]
    assert h["done"] is True
    # match on a list title (indexed column) - returns every item under it,
    # since each item's index row carries its list title
    title_hits = db.search_todos(pid, "Polish")["hits"]
    assert len(title_hits) == 2, "a list-title match returns all items in it"
    assert all(h["list_title"] == "Polish" for h in title_hits)
    # empty query / no match
    assert db.search_todos(pid, "  ")["total"] == 0
    assert db.search_todos(pid, "zzzz-no-match")["total"] == 0
    # filter narrows hits
    done_hits = db.search_todos(pid, "schema", filter="done")["hits"]
    open_hits = db.search_todos(pid, "schema", filter="open")["hits"]
    assert len(done_hits) == 1 and len(open_hits) == 0
    # unsafe characters do not break the MATCH
    assert db.search_todos(pid, 'OR "x" -y +z')["total"] >= 0
    # multi-word query is an AND of phrases (words anywhere), not one phrase
    assert db.search_todos(pid, "check index")["total"] == 1, (
        "AND-of-tokens: 'check index' matches 'check the index'"
    )
    assert db.search_todos(pid, "schema wire")["total"] == 1, (
        "AND-of-tokens: non-consecutive words still match"
    )
    # an explicitly quoted query stays one exact (consecutive) phrase
    assert db.search_todos(pid, '"check the index"')["total"] == 1
    assert db.search_todos(pid, '"index check"')["total"] == 0, (
        "quoted query requires the exact consecutive phrase"
    )
    # unknown post
    assert "no post with id 999999" in expect_error(db.search_todos, 999999, "schema")
    print("  search_todos item+title FTS: ok")

    # -- 5. FTS index stays in sync via triggers ----------------------------
    # add an item -> searchable; rename a list's title -> title match moves
    added = db.add_todo_item(agents["alpha"]["token"], pid, build_id, "glossary")
    assert db.search_todos(pid, "glossary")["total"] == 1
    # "Verify" is in no title before the rename
    assert db.search_todos(pid, "Verify")["total"] == 0
    db.update_todo_list(agents["alpha"]["token"], pid, build_id, "Build & Verify")
    assert len(db.search_todos(pid, "Verify")["hits"]) == 4, (
        "renamed title reindexes every item under the list (3 original + glossary)"
    )
    # delete an item -> no longer searchable
    db.delete_todo_item(agents["alpha"]["token"], pid, build_id, added["item_id"])
    assert db.search_todos(pid, "glossary")["total"] == 0
    print("  search_todos triggers keep index fresh: ok")

    # -- 6. pagination limit clamps to MAX_PAGE_SIZE ------------------------
    big = db.get_todos_page(pid, limit=config.MAX_PAGE_SIZE * 2)
    assert len(big["lists"]) <= config.MAX_PAGE_SIZE
    # the browseable-board caps were raised in this PR: 5/20 -> 50/50
    assert config.TODO_MAX_LISTS >= 50 and config.TODO_MAX_ITEMS >= 50, (
        "board caps raised so large boards are allowed"
    )
    print("  limit clamps to MAX_PAGE_SIZE: ok")


if __name__ == "__main__":
    init()
    main()
    print("test_todo_browsing: all ok")
