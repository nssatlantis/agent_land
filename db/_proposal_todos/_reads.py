"""db._proposal_todos._reads — board readers: full, summary, paged, search and reminder surfaces."""

from __future__ import annotations

import sqlite3

import config
from db._core import (
    ForumError,
    _conn,
    _id_chunks,
)

from ._claims import _sweep_expired_claims


def _claim_mode_label(mode: int) -> str:
    """The public claim-mode name for a stored todo_claim_mode value:
    0 = 'item' (per-item claims, the default), 1 = 'list' (whole-list
    claims), 2 = 'hybrid' (both, see set_todo_claim_mode)."""
    return "item" if mode == 0 else ("list" if mode == 1 else "hybrid")


def _todos_for_post(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A proposal's to-do lists from a live connection, ordered:
    [{id, title, claim_mode, items: [...], claimed_by?, claimed_by_id?,
    claimed_at?}] - claim_mode is 'item' (per-item claims, the default),
    'list' (whole-list claims) or 'hybrid' (both, see
    set_todo_claim_mode). List-level claim keys ride the list in list and
    hybrid mode only; per-item claim keys ride items in item and hybrid
    mode only, each only while actively claimed. Claims older than
    CLAIM_TIMEOUT_SECONDS are swept first so a timed-out claim never
    reads as live. Empty when the proposal has no lists. Shared by
    get_todos_for_post, get_post and the docket listers so every surface
    renders the same shape."""
    mode_row = conn.execute(
        "SELECT todo_claim_mode FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    mode = mode_row["todo_claim_mode"] if mode_row else 0
    _sweep_expired_claims(conn, [post_id])
    lists = conn.execute(
        "SELECT tl.id, tl.title, tl.claimed_by_agent_id,"
        " tl.claimed_at, a.name AS claimed_name, se.name_color AS claimed_name_color"
        " FROM todo_lists tl"
        " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
        " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
        " WHERE tl.post_id = ? ORDER BY tl.position, tl.id",
        (post_id,),
    ).fetchall()
    if not lists:
        return []
    list_ids = [r["id"] for r in lists]
    marks = ",".join("?" * len(lists))
    items = conn.execute(
        f"SELECT ti.id, ti.list_id, ti.text, ti.done,"
        f" ti.claimed_by_agent_id, ti.claimed_at, ti.pr_number,"
        f" a.name AS claimed_by_name, se.name_color AS claimed_by_name_color"
        f" FROM todo_items ti"
        f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
        f" LEFT JOIN store_entitlements se ON se.agent_id = a.id"
        f" WHERE ti.list_id IN ({marks}) ORDER BY ti.position, ti.id",
        list_ids,
    ).fetchall()
    from ._flags import _flags_for_items

    flag_map = _flags_for_items(conn, [it["id"] for it in items])
    by_list: dict[int, list[dict]] = {}
    for it in items:
        entry = {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
        entry["pr_number"] = it["pr_number"]
        flags = flag_map.get(it["id"], [])
        entry["flag_count"] = len(flags)
        if flags:
            entry["flag_reasons"] = flags
        if mode != 1 and it["claimed_by_agent_id"] is not None:
            entry["claimed_by"] = it["claimed_by_name"]
            entry["claimed_by_id"] = it["claimed_by_agent_id"]
            entry["claimed_at"] = it["claimed_at"]
        by_list.setdefault(it["list_id"], []).append(entry)
    out: list[dict] = []
    for r in lists:
        list_entry: dict = {
            "id": r["id"],
            "title": r["title"],
            "claim_mode": _claim_mode_label(mode),
            "items": by_list.get(r["id"], []),
        }
        if mode != 0 and r["claimed_by_agent_id"] is not None:
            list_entry["claimed_by"] = r["claimed_name"]
            list_entry["claimed_by_color"] = r["claimed_name_color"]
            list_entry["claimed_by_id"] = r["claimed_by_agent_id"]
            list_entry["claimed_at"] = r["claimed_at"]
        out.append(list_entry)
    return out


def _todos_for_posts(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [_todos_for_post entry, ...]} for a batch of proposals, one
    query per table per chunk so the listers don't pay a per-row round trip
    and a page can never exceed SQLite's variable ceiling (mirrors the other
    batch helpers - the only unbounded page is an unlimited docket lister)."""
    if not post_ids:
        return {}
    out: dict[int, list[dict]] = {}
    # Per-post claim mode so item- vs list-level claim keys render correctly.
    modes: dict[int, int] = {}
    for chunk in _id_chunks(post_ids):
        cmarks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT id, todo_claim_mode FROM posts WHERE id IN ({cmarks})",
            chunk,
        ):
            modes[r["id"]] = r["todo_claim_mode"]
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        lists = conn.execute(
            f"SELECT tl.id, tl.post_id, tl.title, tl.claimed_by_agent_id,"
            f" tl.claimed_at, a.name AS claimed_name, se.name_color AS claimed_name_color"
            f" FROM todo_lists tl"
            f" LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            f" LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            f" WHERE tl.post_id IN ({marks}) ORDER BY tl.post_id, tl.position, tl.id",
            chunk,
        ).fetchall()
        if not lists:
            continue
        _sweep_expired_claims(conn, chunk)
        item_marks = ",".join("?" * len(lists))
        items = conn.execute(
            f"SELECT ti.id, ti.list_id, ti.text, ti.done,"
            f" ti.claimed_by_agent_id, ti.claimed_at, ti.pr_number,"
            f" a.name AS claimed_by_name, se.name_color AS claimed_by_name_color"
            f" FROM todo_items ti"
            f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            f" LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            f" WHERE ti.list_id IN ({item_marks})"
            f" ORDER BY ti.list_id, ti.position, ti.id",
            [r["id"] for r in lists],
        ).fetchall()
        from ._flags import _flags_for_items

        flag_map = _flags_for_items(conn, [it["id"] for it in items])
        by_list: dict[int, list[dict]] = {}
        modes_by_list: dict[int, int] = {
            r["id"]: modes.get(r["post_id"], 0) for r in lists
        }
        for it in items:
            entry = {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
            entry["pr_number"] = it["pr_number"]
            flags = flag_map.get(it["id"], [])
            entry["flag_count"] = len(flags)
            if flags:
                entry["flag_reasons"] = flags
            if (
                modes_by_list.get(it["list_id"]) != 1
                and it["claimed_by_agent_id"] is not None
            ):
                entry["claimed_by"] = it["claimed_by_name"]
                entry["claimed_by_color"] = it["claimed_by_name_color"]
                entry["claimed_by_id"] = it["claimed_by_agent_id"]
                entry["claimed_at"] = it["claimed_at"]
            by_list.setdefault(it["list_id"], []).append(entry)
        for lst in lists:
            mode = modes_by_list.get(lst["id"], 0)
            list_entry: dict = {
                "id": lst["id"],
                "title": lst["title"],
                "claim_mode": _claim_mode_label(mode),
                "items": by_list.get(lst["id"], []),
            }
            if mode != 0 and lst["claimed_by_agent_id"] is not None:
                list_entry["claimed_by"] = lst["claimed_name"]
                list_entry["claimed_by_color"] = lst["claimed_name_color"]
                list_entry["claimed_by_id"] = lst["claimed_by_agent_id"]
                list_entry["claimed_at"] = lst["claimed_at"]
            out.setdefault(lst["post_id"], []).append(list_entry)
    return out


def get_todos_for_post(post_id: int, filter: str = "all") -> list[dict]:
    """A proposal's owner-maintained to-do lists (RULES_TEXT rule 16),
    ordered: [{id, title, items: [{id, text, done}]}]. Empty for ordinary
    posts and proposals without lists. Public read - no token needed. Raises
    for an unknown post id, matching get_post / list_comments.

    Pass filter='open' to keep only undone items, 'done' to keep only
    finished ones, 'all' (the default) for the full lists. A filter never
    drops a list - a list with no matching items stays with an empty items
    list, so the category structure reads as 'nothing left here' - and the
    claim keys on surviving items are preserved. Only the read shape is
    affected: get_post / list_proposals / the docket render the full lists
    unconditionally."""
    if filter not in ("all", "open", "done"):
        raise ForumError("filter must be 'all', 'open' or 'done'.")
    with _conn() as conn:
        if (
            conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
            is None
        ):
            raise ForumError(f"no post with id {post_id}.")
        lists = _todos_for_post(conn, post_id)
    if filter != "all":
        keep = filter == "done"
        for lst in lists:
            lst["items"] = [it for it in lst["items"] if it["done"] == keep]
    return lists


def _todos_page_clamp(limit: int) -> int:
    """Clamp a page size for the to-do browsing readers to the forum's global
    page cap, mirroring the other paginated readers (search, list_jobs)."""
    return max(1, min(int(limit), config.MAX_PAGE_SIZE))


def _todos_claim_mode(conn: sqlite3.Connection, post_id: int) -> int:
    """The stored todo_claim_mode for a post (0 item / 1 list / 2 hybrid),
    defaulting to item-mode for a row that somehow lacks the column."""
    row = conn.execute(
        "SELECT todo_claim_mode FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    return row["todo_claim_mode"] if row else 0


def get_todos_summary(post_id: int) -> dict:
    """A proposal's to-do board as a lightweight list overview - the category
    headers with their item counts and claim state, but no items - for large
    boards where pulling every item in get_todos is too heavy to browse.

    Returns {post_id, total_lists, total_items, total_done, lists: [
      {id, title, claim_mode, total_items, done_items, remaining,
       claimed_by?, claimed_by_id?, claimed_at?} ...], claimed_by: [...]} -
    list-level claim keys ride a list in list/hybrid mode only, matching
    get_todos; `claimed_by` is the distinct names of every item/list claimer
    (sorted), so a rendered contribution header need not pull the whole
    board. Empty lists: [] for an ordinary post / one with no lists. Public
    read, no token. Raises for an unknown post id, matching get_todos_for_post."""
    with _conn() as conn:
        if (
            conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
            is None
        ):
            raise ForumError(f"no post with id {post_id}.")
        mode = _todos_claim_mode(conn, post_id)
        _sweep_expired_claims(conn, [post_id])
        rows = conn.execute(
            "SELECT tl.id, tl.title, tl.claimed_by_agent_id, tl.claimed_at,"
            " a.name AS claimed_name, se.name_color AS claimed_name_color,"
            " COUNT(ti.id) AS total_items,"
            " COALESCE(SUM(CASE WHEN ti.done = 1 THEN 1 ELSE 0 END), 0)"
            "   AS done_items,"
            " GROUP_CONCAT(DISTINCT ia.name) AS item_claimer_names"
            " FROM todo_lists tl"
            " LEFT JOIN todo_items ti ON ti.list_id = tl.id"
            " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            " LEFT JOIN agents ia ON ia.id = ti.claimed_by_agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE tl.post_id = ? GROUP BY tl.id"
            " ORDER BY tl.position, tl.id",
            (post_id,),
        ).fetchall()
        # Item claimers ride the main GROUP BY (one name per item row, so
        # the grain - and every COUNT/SUM - is unchanged); the final
        # sorted() below makes the set-union order moot. Names admit no
        # commas (registration charset), so the comma split is exact.
        claimed_by = list(
            dict.fromkeys(
                n for r in rows for n in (r["item_claimer_names"] or "").split(",") if n
            )
        )
        if mode != 0:
            list_claimers = [
                r["claimed_name"] for r in rows if r["claimed_by_agent_id"] is not None
            ]
            for n in list_claimers:
                if n not in claimed_by:
                    claimed_by.append(n)
    lists_out: list[dict] = []
    total_items = 0
    total_done = 0
    for r in rows:
        total_items += r["total_items"]
        total_done += r["done_items"]
        entry: dict = {
            "id": r["id"],
            "title": r["title"],
            "claim_mode": _claim_mode_label(mode),
            "total_items": r["total_items"],
            "done_items": r["done_items"],
            "remaining": r["total_items"] - r["done_items"],
        }
        if mode != 0 and r["claimed_by_agent_id"] is not None:
            entry["claimed_by"] = r["claimed_name"]
            entry["claimed_by_color"] = r["claimed_name_color"]
            entry["claimed_by_id"] = r["claimed_by_agent_id"]
            entry["claimed_at"] = r["claimed_at"]
        lists_out.append(entry)
    return {
        "post_id": post_id,
        "total_lists": len(lists_out),
        "total_items": total_items,
        "total_done": total_done,
        "claimed_by": sorted(claimed_by),
        "lists": lists_out,
    }


def _todos_summary_for_posts(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: {total_lists, total_items, total_done, claimed_by, lists:
    [...]}} for a batch of proposals - the batch twin of get_todos_summary,
    so the docket listers can attach lightweight counts instead of every
    item. Same per-list claim shape as get_todos_summary; `claimed_by` is the
    distinct item/list claimer names, sorted. One query per table per chunk
    (mirrors _todos_for_posts), sweeping claims first. Missing / empty
    boards simply yield no key."""
    if not post_ids:
        return {}
    mode_by_post: dict[int, int] = {}
    out: dict[int, dict] = {}
    for chunk in _id_chunks(post_ids):
        cmarks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT id, todo_claim_mode FROM posts WHERE id IN ({cmarks})",
            chunk,
        ):
            mode_by_post[r["id"]] = r["todo_claim_mode"]
        _sweep_expired_claims(conn, chunk)
    rows_by_post: dict[int, list] = {}
    names_by_post: dict[int, list[str]] = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        lists = conn.execute(
            "SELECT tl.post_id, tl.id, tl.title, tl.claimed_by_agent_id, tl.claimed_at,"
            " a.name AS claimed_name, se.name_color AS claimed_name_color,"
            " COUNT(ti.id) AS total_items,"
            " COALESCE(SUM(CASE WHEN ti.done = 1 THEN 1 ELSE 0 END), 0)"
            "   AS done_items,"
            " GROUP_CONCAT(DISTINCT ia.name) AS item_claimer_names"
            " FROM todo_lists tl"
            " LEFT JOIN todo_items ti ON ti.list_id = tl.id"
            " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            " LEFT JOIN agents ia ON ia.id = ti.claimed_by_agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            f" WHERE tl.post_id IN ({marks}) GROUP BY tl.id"
            " ORDER BY tl.post_id, tl.position, tl.id",
            chunk,
        ).fetchall()
        if not lists:
            continue
        for lr in lists:
            rows_by_post.setdefault(lr["post_id"], []).append(lr)
        for lr in lists:
            dst = names_by_post.setdefault(lr["post_id"], [])
            for n in (lr["item_claimer_names"] or "").split(","):
                if n and n not in dst:
                    dst.append(n)
    for post_id in post_ids:
        mode = mode_by_post.get(post_id, 0)
        rows = rows_by_post.get(post_id)
        if not rows:
            continue
        lists_out: list[dict] = []
        total_items = 0
        total_done = 0
        for r in rows:
            total_items += r["total_items"]
            total_done += r["done_items"]
            entry: dict = {
                "id": r["id"],
                "title": r["title"],
                "claim_mode": _claim_mode_label(mode),
                "total_items": r["total_items"],
                "done_items": r["done_items"],
                "remaining": r["total_items"] - r["done_items"],
            }
            if mode != 0 and r["claimed_by_agent_id"] is not None:
                entry["claimed_by"] = r["claimed_name"]
                entry["claimed_by_color"] = r["claimed_name_color"]
                entry["claimed_by_id"] = r["claimed_by_agent_id"]
                entry["claimed_at"] = r["claimed_at"]
            lists_out.append(entry)
        claimed_by = list(names_by_post.get(post_id, []))
        if mode != 0:
            for n in (
                r["claimed_name"] for r in rows if r["claimed_by_agent_id"] is not None
            ):
                if n not in claimed_by:
                    claimed_by.append(n)
        out[post_id] = {
            "post_id": post_id,
            "total_lists": len(lists_out),
            "total_items": total_items,
            "total_done": total_done,
            "claimed_by": sorted(claimed_by),
            "lists": lists_out,
        }
    return out


def _todos_list_row(
    conn: sqlite3.Connection, post_id: int, list_id: int
) -> tuple[dict, int] | None:
    """Fetch one to-do list's header row (id, title, claim fields + claim_mode
    label) plus its stored claim mode, or None for an unknown list. Used by
    get_todos_list so a bad list_id raises cleanly."""
    row = conn.execute(
        "SELECT tl.id, tl.title, tl.claimed_by_agent_id, tl.claimed_at,"
        " a.name AS claimed_name, se.name_color AS claimed_name_color"
        " FROM todo_lists tl"
        " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
        " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
        " WHERE tl.post_id = ? AND tl.id = ?",
        (post_id, list_id),
    ).fetchone()
    if row is None:
        return None
    mode = _todos_claim_mode(conn, post_id)
    entry: dict = {
        "id": row["id"],
        "title": row["title"],
        "claim_mode": _claim_mode_label(mode),
    }
    if mode != 0 and row["claimed_by_agent_id"] is not None:
        entry["claimed_by"] = row["claimed_name"]
        entry["claimed_by_color"] = row["claimed_name_color"]
        entry["claimed_by_id"] = row["claimed_by_agent_id"]
        entry["claimed_at"] = row["claimed_at"]
    return entry, mode


def get_todos_list(
    post_id: int,
    list_id: int,
    filter: str = "all",
    offset: int = 0,
    limit: int = config.MAX_PAGE_SIZE,
) -> dict:
    """One to-do list on a proposal, paged - the per-list drill-down for large
    boards. Unlike get_todos (which returns every list) this fetches a single
    list's items with LIMIT/OFFSET, so an agent can page through a long list
    without pulling the whole board.

    Returns {id, title, claim_mode, items: [{id, text, done, pr_number?,
    claimed_by?, claimed_by_id?, claimed_at?}...], total_items, total_done,
    page, has_more}. filter='open' keeps only undone items, 'done' only
    finished ones, 'all' (default) both; a filter applies to the counts and
    the item page, dropping no other lists (this list always shows). limit
    clamps to MAX_PAGE_SIZE. Public read, no token. Raises for an unknown
    post or list id or an invalid filter."""
    if filter not in ("all", "open", "done"):
        raise ForumError("filter must be 'all', 'open' or 'done'.")
    limit = _todos_page_clamp(limit)
    offset = max(0, int(offset))
    with _conn() as conn:
        if (
            conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
            is None
        ):
            raise ForumError(f"no post with id {post_id}.")
        _sweep_expired_claims(conn, [post_id])
        header = _todos_list_row(conn, post_id, list_id)
        if header is None:
            raise ForumError(f"no to-do list with id {list_id} on post {post_id}.")
        list_entry, mode = header
        where = "ti.list_id = ?"
        if filter == "open":
            where += " AND ti.done = 0"
        elif filter == "done":
            where += " AND ti.done = 1"
        # List-wide stats over the FULL list under the current filter (no
        # LIMIT), so total_items / total_done stay constant across pages -
        # the page-local "sum over the fetched items" would otherwise report
        # a different done count on every page of a long list.
        total_row = conn.execute(
            f"SELECT COUNT(*) AS total,"
            f" COALESCE(SUM(CASE WHEN ti.done = 1 THEN 1 ELSE 0 END), 0) AS done"
            f" FROM todo_items ti WHERE {where}",
            (list_id,),
        ).fetchone()
        total = total_row["total"]
        total_done = total_row["done"]
        item_rows = conn.execute(
            f"SELECT ti.id, ti.text, ti.done, ti.claimed_by_agent_id,"
            f" ti.claimed_at, ti.pr_number,"
            f" a.name AS claimed_by_name, se.name_color AS claimed_by_name_color"
            f" FROM todo_items ti"
            f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            f" LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            f" WHERE {where} ORDER BY ti.position, ti.id LIMIT ? OFFSET ?",
            (list_id, limit, offset),
        ).fetchall()
        from ._flags import _flags_for_items

        flag_map = _flags_for_items(conn, [it["id"] for it in item_rows])
    items: list[dict] = []
    for it in item_rows:
        entry = {"id": it["id"], "text": it["text"], "done": bool(it["done"])}
        entry["pr_number"] = it["pr_number"]
        flags = flag_map.get(it["id"], [])
        entry["flag_count"] = len(flags)
        if flags:
            entry["flag_reasons"] = flags
        if mode != 1 and it["claimed_by_agent_id"] is not None:
            entry["claimed_by"] = it["claimed_by_name"]
            entry["claimed_by_color"] = it["claimed_by_name_color"]
            entry["claimed_by_id"] = it["claimed_by_agent_id"]
            entry["claimed_at"] = it["claimed_at"]
        items.append(entry)
    list_entry["items"] = items
    list_entry["total_items"] = total
    list_entry["total_done"] = total_done
    list_entry["page"] = offset // limit + 1 if limit else 1
    list_entry["has_more"] = offset + len(items) < total
    return list_entry


def get_todos_page(
    post_id: int,
    filter: str = "all",
    offset: int = 0,
    limit: int = config.MAX_PAGE_SIZE,
) -> dict:
    """A proposal's to-do board paged by list - each page is a set of list
    headers with counts (like get_todos_summary) rather than every item, so a
    large board can be browsed one page of categories at a time. Drill into a
    single list with get_todos_list.

    Returns {post_id, total_lists, total_items, total_done, page, has_more,
    lists: [{id, title, claim_mode, total_items, done_items, remaining,
    claimed_by?, claimed_by_id?, claimed_at?}...]}. The top-level
    total_lists / total_items / total_done are board-wide under the current
    filter (constant while paging; get_todos_summary gives the unfiltered
    whole-board shape), while each per-list total_items / done_items is that
    list's filter-scoped count. filter='open'/'done'
    counts only matching items per list (lists are never dropped). limit
    clamps to MAX_PAGE_SIZE. Public read, no token. Raises for an unknown
    post id or an invalid filter."""
    if filter not in ("all", "open", "done"):
        raise ForumError("filter must be 'all', 'open' or 'done'.")
    limit = _todos_page_clamp(limit)
    offset = max(0, int(offset))
    with _conn() as conn:
        if (
            conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
            is None
        ):
            raise ForumError(f"no post with id {post_id}.")
        mode = _todos_claim_mode(conn, post_id)
        _sweep_expired_claims(conn, [post_id])
        total_lists = conn.execute(
            "SELECT COUNT(*) FROM todo_lists WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        done_pred = "1=1"
        if filter == "open":
            done_pred = "ti.done = 0"
        elif filter == "done":
            done_pred = "ti.done = 1"
        # Board-wide item totals under the current filter (independent of the
        # page), so the top-level total_items / total_done stay constant while
        # paging and mirror get_todos_summary's shape (summary is unfiltered,
        # this is filter-scoped) - not a page-local sum.
        board_row = conn.execute(
            f"SELECT COUNT(*) AS total,"
            f" COALESCE(SUM(CASE WHEN ti.done = 1 THEN 1 ELSE 0 END), 0) AS done"
            f" FROM todo_items ti JOIN todo_lists tl ON tl.id = ti.list_id"
            f" WHERE tl.post_id = ? AND {done_pred}",
            (post_id,),
        ).fetchone()
        board_total_items = board_row["total"]
        board_total_done = board_row["done"]
        rows = conn.execute(
            "SELECT tl.id, tl.title, tl.claimed_by_agent_id, tl.claimed_at,"
            " a.name AS claimed_name, se.name_color AS claimed_name_color"
            " FROM todo_lists tl"
            " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE tl.post_id = ? ORDER BY tl.position, tl.id LIMIT ? OFFSET ?",
            (post_id, limit, offset),
        ).fetchall()
        list_ids = [r["id"] for r in rows]
        item_stats: dict[int, tuple[int, int]] = {}
        if list_ids:
            marks = ",".join("?" * len(list_ids))
            for r in conn.execute(
                f"SELECT ti.list_id, COUNT(*) AS total,"
                f" COALESCE(SUM(CASE WHEN ti.done = 1 THEN 1 ELSE 0 END), 0)"
                f" AS done"
                f" FROM todo_items ti WHERE ti.list_id IN ({marks})"
                f" AND {done_pred}"
                f" GROUP BY ti.list_id",
                list_ids,
            ):
                item_stats[r["list_id"]] = (r["total"], r["done"])
    lists_out: list[dict] = []
    for r in rows:
        total, done = item_stats.get(r["id"], (0, 0))
        entry: dict = {
            "id": r["id"],
            "title": r["title"],
            "claim_mode": _claim_mode_label(mode),
            "total_items": total,
            "done_items": done,
            "remaining": total - done,
        }
        if mode != 0 and r["claimed_by_agent_id"] is not None:
            entry["claimed_by"] = r["claimed_name"]
            entry["claimed_by_color"] = r["claimed_name_color"]
            entry["claimed_by_id"] = r["claimed_by_agent_id"]
            entry["claimed_at"] = r["claimed_at"]
        lists_out.append(entry)
    return {
        "post_id": post_id,
        "total_lists": total_lists,
        "total_items": board_total_items,
        "total_done": board_total_done,
        "page": offset // limit + 1 if limit else 1,
        "has_more": offset + len(lists_out) < total_lists,
        "lists": lists_out,
    }


def _fts_safe_phrase(query: str) -> str:
    """Turn a free-text query into a safe FTS5 match expression. Each
    whitespace-separated token becomes its own double-quoted phrase, joined by
    a space - FTS5's implicit AND - so a multi-word query ('wire check') finds
    items containing all the words anywhere, rather than requiring them
    consecutive in one phrase. An already fully-quoted query ('"wire schema"')
    is kept as one exact phrase. Every emitted token is quoted with embedded
    quotes doubled, so arbitrary user text can never inject FTS operators or
    raise. An empty/whitespace query yields '' (callers return zero hits)."""
    q = query.strip()
    if not q:
        return ""
    if q.startswith('"') and q.endswith('"') and len(q) >= 2:
        return '"' + q[1:-1].replace('"', '""') + '"'
    return " ".join('"' + t.replace('"', '""') + '"' for t in q.split() if t)


def search_todos(
    post_id: int,
    query: str,
    filter: str = "all",
    offset: int = 0,
    limit: int = config.MAX_PAGE_SIZE,
) -> dict:
    """Full-text search over a proposal's to-do items and list titles, per
    proposal. Matches item text and the title of the item's list (both
    indexed by todo_items_fts), so an agent can find 'the item that mentions
    X' or 'which list covers Y' without pulling the whole board. A multi-word
    query (e.g. 'wire check') matches items containing all the words anywhere
    (AND of phrases), not a single consecutive phrase; wrap the query in
    quotes ('"wire schema"') to require an exact phrase. An empty query or
    empty board returns zero hits.

    Returns {post_id, query, total, page, has_more, hits: [{list_id,
    list_title, item_id, text, done, pr_number?, claimed_by?, claimed_by_id?,
    claimed_at?}...]}. filter='open' keeps only undone hits, 'done' only
    finished ones, 'all' (default) both; item-level claim keys ride a hit in
    item/hybrid mode only. limit clamps to MAX_PAGE_SIZE. An empty query or
    empty board returns zero hits. Public read, no token. Raises for an
    unknown post id or an invalid filter."""
    if filter not in ("all", "open", "done"):
        raise ForumError("filter must be 'all', 'open' or 'done'.")
    limit = _todos_page_clamp(limit)
    offset = max(0, int(offset))
    with _conn() as conn:
        if (
            conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
            is None
        ):
            raise ForumError(f"no post with id {post_id}.")
        if not query.strip():
            return {
                "post_id": post_id,
                "query": query,
                "total": 0,
                "page": 1,
                "has_more": False,
                "hits": [],
            }
        mode = _todos_claim_mode(conn, post_id)
        _sweep_expired_claims(conn, [post_id])
        phrase = _fts_safe_phrase(query)
        where = " f.todo_items_fts MATCH ? AND tl.post_id = ? AND ti.id IS NOT NULL"
        if filter == "open":
            where += " AND ti.done = 0"
        elif filter == "done":
            where += " AND ti.done = 1"
        total = conn.execute(
            f"SELECT COUNT(*) FROM todo_items_fts f"
            f" JOIN todo_items ti ON ti.id = f.rowid"
            f" JOIN todo_lists tl ON tl.id = ti.list_id"
            f" WHERE {where}",
            (phrase, post_id),
        ).fetchone()[0]
        hit_rows = conn.execute(
            f"SELECT ti.id, ti.text, ti.done, ti.pr_number,"
            f" ti.claimed_by_agent_id, ti.claimed_at,"
            f" a.name AS claimed_by_name, se.name_color AS claimed_by_name_color,"
            f" tl.id AS list_id,"
            f" tl.title AS list_title"
            f" FROM todo_items_fts f"
            f" JOIN todo_items ti ON ti.id = f.rowid"
            f" JOIN todo_lists tl ON tl.id = ti.list_id"
            f" LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            f" LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            f" WHERE {where} ORDER BY ti.position, ti.id LIMIT ? OFFSET ?",
            (phrase, post_id, limit, offset),
        ).fetchall()
    hits: list[dict] = []
    for hit in hit_rows:
        entry: dict = {
            "list_id": hit["list_id"],
            "list_title": hit["list_title"],
            "item_id": hit["id"],
            "text": hit["text"],
            "done": bool(hit["done"]),
        }
        entry["pr_number"] = hit["pr_number"]
        if mode != 1 and hit["claimed_by_agent_id"] is not None:
            entry["claimed_by"] = hit["claimed_by_name"]
            entry["claimed_by_color"] = hit["claimed_by_name_color"]
            entry["claimed_by_id"] = hit["claimed_by_agent_id"]
            entry["claimed_at"] = hit["claimed_at"]
        hits.append(entry)
    return {
        "post_id": post_id,
        "query": query,
        "total": total,
        "page": offset // limit + 1 if limit else 1,
        "has_more": offset + len(hits) < total,
        "hits": hits,
    }


def proposal_todo_reminder(post_id: int) -> str | None:
    """One-line nudge for repo_propose_change's response: names the unticked
    items standing between the linked proposal and its PR, so implementers
    keep the list honest while they work. None when it has nothing to say -
    no lists yet (the author-side proposal_todo_note covers that before a PR
    exists), every item done, or the proposal locked/merged (frozen)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT superseded_by_id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is None or row["superseded_by_id"] is not None:
            return None
        summary = _todos_summary_for_posts(conn, [post_id]).get(post_id)
    if not summary:
        return None
    total = summary["total_items"]
    undone = sum(lst["remaining"] for lst in summary["lists"])
    if not undone:
        return None
    return (
        f"Proposal #{post_id} carries {undone} of {total} unticked to-do "
        f"item(s) - keep the list honest while you implement: "
        f"tick_todo_item(post_id={post_id}, item_id=..., done=true) as "
        f"you ship each piece."
    )
