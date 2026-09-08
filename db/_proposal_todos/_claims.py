"""db._proposal_todos._claims — claim lifecycle: expiry sweeps, snapshots, claim and unclaim operations."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone

import config
from db._core import (
    ForumError,
    _conn,
    _parse_iso,
    _require_active_agent,
)
from db._proposal_status import _proposal_locked_error
from notifications import _notify

# Hard cap on how many items a single move_todo_items batch may relocate at
# once - the whole batch is atomic, so this bounds the blast radius of one
# call and keeps the edit-trail row and renormalization pass bounded.
_MOVE_BATCH_MAX = 20


def _claim_expired(claimed_at: str | None) -> bool:
    """True when a to-do claim has sat past config.CLAIM_TIMEOUT_SECONDS.

    Sliding 24h window (precise seconds from claimed_at, not calendar day
    at 00:00 UTC) — a claim at 23:50 UTC expires at 23:50 UTC next day,
    not at next midnight. A timeout of 0 (or less) disables staleness
    entirely."""
    timeout = config.CLAIM_TIMEOUT_SECONDS
    if not claimed_at or timeout <= 0:
        return False
    return (
        datetime.now(timezone.utc) - _parse_iso(claimed_at)
    ).total_seconds() >= timeout


def _snapshot_claims(
    conn: sqlite3.Connection, post_id: int
) -> dict[tuple[str, str], tuple[int, str]]:
    """Snapshot all active (non-expired) claims on a proposal's todo items
    before a destructive rewrite.  Returns {(list_title, item_text):
    (agent_id, claimed_at)} — the key identifies the item by content, so
    a claim survives list/item re-insertion as long as the text is kept."""
    rows = conn.execute(
        "SELECT tl.title, ti.text, ti.claimed_by_agent_id, ti.claimed_at"
        " FROM todo_items ti"
        " JOIN todo_lists tl ON tl.id = ti.list_id"
        " WHERE tl.post_id = ?"
        " AND ti.claimed_by_agent_id IS NOT NULL",
        (post_id,),
    ).fetchall()
    out: dict[tuple[str, str], tuple[int, str]] = {}
    for r in rows:
        if not _claim_expired(r["claimed_at"]):
            out[(r["title"], r["text"])] = (r["claimed_by_agent_id"], r["claimed_at"])
    return out


def _restore_claims(
    conn: sqlite3.Connection,
    post_id: int,
    snapshot: dict[tuple[str, str], tuple[int, str]],
) -> int:
    """Restore claims from a snapshot onto newly-inserted todo items.
    Matches by (list_title, item_text).  Returns the number of claims
    restored.  Skips claims whose timeout has expired since the snapshot."""
    if not snapshot:
        return 0
    lists = conn.execute(
        "SELECT tl.id AS list_id, tl.title, ti.id AS item_id, ti.text"
        " FROM todo_items ti"
        " JOIN todo_lists tl ON tl.id = ti.list_id"
        " WHERE tl.post_id = ?",
        (post_id,),
    ).fetchall()
    pending: list[tuple[int, int, str]] = []
    for row in lists:
        key = (row["title"], row["text"])
        claim = snapshot.get(key)
        if claim is None:
            continue
        agent_id, claimed_at = claim
        if _claim_expired(claimed_at):
            continue
        pending.append((row["item_id"], agent_id, claimed_at))
    if not pending:
        return 0
    agent_case = " ".join(["WHEN ? THEN ?"] * len(pending))
    at_case = " ".join(["WHEN ? THEN ?"] * len(pending))
    marks = ", ".join(["?"] * len(pending))
    params: list = []
    for iid, aid, _cat in pending:
        params.extend((iid, aid))
    for iid, _aid, cat in pending:
        params.extend((iid, cat))
    params.extend([p[0] for p in pending])
    conn.execute(
        "UPDATE todo_items SET claimed_by_agent_id = CASE id"
        f" {agent_case} END, claimed_at = CASE id {at_case} END"
        f" WHERE id IN ({marks})",
        params,
    )
    return len(pending)


def _snapshot_list_claims(
    conn: sqlite3.Connection, post_id: int
) -> dict[str, tuple[int, str]]:
    """Snapshot all active (non-expired) whole-list claims on a proposal
    before a destructive rewrite. Returns {list_title: (agent_id,
    claimed_at)} - keyed by list title so a list claim survives a rewrite
    that keeps the same category under its original title."""
    rows = conn.execute(
        "SELECT title, claimed_by_agent_id, claimed_at"
        " FROM todo_lists"
        " WHERE post_id = ? AND claimed_by_agent_id IS NOT NULL",
        (post_id,),
    ).fetchall()
    out: dict[str, tuple[int, str]] = {}
    for r in rows:
        if not _claim_expired(r["claimed_at"]):
            out[r["title"]] = (r["claimed_by_agent_id"], r["claimed_at"])
    return out


def _restore_list_claims(
    conn: sqlite3.Connection, post_id: int, snapshot: dict[str, tuple[int, str]]
) -> int:
    """Restore whole-list claims from a snapshot onto a proposal's
    re-created todo_lists, matching by title. Returns the number of claims
    restored. Skips claims whose timeout has expired since the snapshot."""
    if not snapshot:
        return 0
    restored = 0
    lists = conn.execute(
        "SELECT id, title FROM todo_lists WHERE post_id = ?",
        (post_id,),
    ).fetchall()
    for row in lists:
        claim = snapshot.get(row["title"])
        if claim is None:
            continue
        agent_id, claimed_at = claim
        if _claim_expired(claimed_at):
            continue
        conn.execute(
            "UPDATE todo_lists SET claimed_by_agent_id = ?, claimed_at = ?"
            " WHERE id = ?",
            (agent_id, claimed_at, row["id"]),
        )
        restored += 1
    return restored


def _sweep_expired_claims(conn: sqlite3.Connection, post_ids: list[int]) -> int:
    """Clear item and whole-list claims past their timeout on the given
    posts. Lazy maintenance called by the readers and gates: the UPDATEs
    fire only when something has actually expired, so steady-state reads
    stay write-free. Returns the number of claims released.

    Each affected claimer is told their claim expired - a silently
    released claim otherwise looks held while the item/list is free (the
    author IS pinged on manual unclaim and claimable-off; this closes the
    timeout path's silence). Notices are grouped per claimer+proposal so
    a batch expiry costs one mailbox row, not one per item/list."""
    if not post_ids:
        return 0
    post_marks = ",".join("?" * len(post_ids))
    list_rows = conn.execute(
        f"SELECT id, post_id, title FROM todo_lists WHERE post_id IN ({post_marks})",
        post_ids,
    ).fetchall()
    if not list_rows:
        return 0
    list_ids = [r["id"] for r in list_rows]
    board_by_id = {r["id"]: r for r in list_rows}
    marks = ",".join("?" * len(list_ids))
    released = 0

    # -- per-item claims -------------------------------------------------
    stale: list[int] = []
    stale_rows: list[tuple[int, int, str, int]] = []
    for r in conn.execute(
        f"SELECT id, claimed_at, claimed_by_agent_id, text, list_id "
        f"FROM todo_items WHERE list_id IN ({marks})"
        f" AND claimed_by_agent_id IS NOT NULL",
        list_ids,
    ):
        if _claim_expired(r["claimed_at"]):
            stale.append(r["id"])
            stale_rows.append(
                (r["id"], r["claimed_by_agent_id"], r["text"], r["list_id"])
            )
    if stale:
        imarks = ",".join("?" * len(stale))
        conn.execute(
            f"UPDATE todo_items SET claimed_by_agent_id = NULL,"
            f" claimed_at = NULL WHERE id IN ({imarks})",
            stale,
        )
        released += len(stale)
        grouped: dict[tuple[int, int], dict] = {}
        for _id, claimer_id, text, lid in stale_rows:
            b = board_by_id.get(lid)
            if b is None:
                continue
            g = grouped.setdefault(
                (claimer_id, b["post_id"]),
                {"title": b["title"], "parts": []},
            )
            g["parts"].append(text)
        for (claimer_id, post_id), g in grouped.items():
            _notify(
                conn,
                claimer_id,
                "delegation",
                "post",
                post_id,
                f"Your to-do claim(s) on proposal #{post_id}"
                f" ({g['title']}) expired after the auto-release window "
                f"({config.CLAIM_TIMEOUT_SECONDS}s): "
                f"{'; '.join(g['parts'])}. Re-claim with claim_todo_item"
                f" if you are still working on them.",
            )

    # -- whole-list claims ----------------------------------------------
    stale_lists: list[tuple[int, int, str, int]] = []
    for r in conn.execute(
        f"SELECT id, claimed_at, claimed_by_agent_id, title, post_id "
        f"FROM todo_lists WHERE id IN ({marks})"
        f" AND claimed_by_agent_id IS NOT NULL",
        list_ids,
    ):
        if _claim_expired(r["claimed_at"]):
            stale_lists.append(
                (r["id"], r["claimed_by_agent_id"], r["title"], r["post_id"])
            )
    if stale_lists:
        lmarks = ",".join("?" * len(stale_lists))
        conn.execute(
            f"UPDATE todo_lists SET claimed_by_agent_id = NULL,"
            f" claimed_at = NULL WHERE id IN ({lmarks})",
            [s[0] for s in stale_lists],
        )
        released += len(stale_lists)
        grouped_lists: dict[tuple[int, int], dict] = {}
        for _lid, claimer_id, title, post_id in stale_lists:
            g = grouped_lists.setdefault((claimer_id, post_id), {"titles": []})
            g["titles"].append(title)
        for (claimer_id, post_id), g in grouped_lists.items():
            _notify(
                conn,
                claimer_id,
                "delegation",
                "post",
                post_id,
                f"Your to-do list claim(s) on proposal #{post_id}"
                f" ({'; '.join(g['titles'])}) expired after the "
                f"auto-release window ({config.CLAIM_TIMEOUT_SECONDS}s). "
                f"Re-claim with claim_todo_list if you are still working "
                f"on them.",
            )
    return released


def claim_todo_item(token: str, post_id: int, item_id: int) -> dict:
    """Claim one to-do item on a collaborative proposal: lock it to the
    caller so two collaborators never build the same thing. The caller
    must be the proposal's author or a joined collaborator; the item must
    belong to this proposal and be unclaimed (claims past
    CLAIM_TIMEOUT_SECONDS are swept first, so a timed-out claim never
    blocks), and a collaborator holds at most MAX_CLAIMS_PER_COLLABORATOR
    active claims per proposal. Refused only in pure list mode
    (todo_claim_mode == 1); in hybrid mode the item's owning list must not
    be reserved by another citizen's whole-list claim. Annotation-level
    action: no karma, votes or cooldown."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, collaborative,"
            " superseded_by_id, todo_claim_mode FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["proposal_kind"]:
            raise ForumError(f"post #{post_id} is not a proposal.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    post_id,
                    post["superseded_by_id"],
                    "claim a to-do item on",
                )
            )
        if not post["collaborative"]:
            raise ForumError(
                f"proposal #{post_id} is not collaborative - to-do item "
                "claiming is a collaborative-proposal feature."
            )
        if post["todo_claim_mode"] == 1:
            raise ForumError(
                f"proposal #{post_id} claims whole to-do lists, not items - "
                "use claim_todo_list(token, post_id, list_id) to take a "
                "category instead."
            )
        if post["agent_id"] != agent["id"]:
            joined = conn.execute(
                "SELECT 1 FROM proposal_collaborators"
                " WHERE proposal_id = ? AND agent_id = ?",
                (post_id, agent["id"]),
            ).fetchone()
            if joined is None:
                raise ForumError(
                    "only the author or a collaborator may claim to-do "
                    f"items on proposal #{post_id}."
                )
        _sweep_expired_claims(conn, [post_id])
        item = conn.execute(
            "SELECT ti.id, ti.text, ti.claimed_by_agent_id,"
            " a.name AS holder, tl.claimed_by_agent_id AS list_claimed_by,"
            " la.name AS list_holder"
            " FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            " LEFT JOIN agents la ON la.id = tl.claimed_by_agent_id"
            " WHERE ti.id = ? AND tl.post_id = ?",
            (item_id, post_id),
        ).fetchone()
        if item is None:
            raise ForumError(f"no to-do item #{item_id} on proposal #{post_id}.")
        if item["claimed_by_agent_id"] is not None:
            who = item["holder"] or "another citizen"
            raise ForumError(f"to-do item #{item_id} is already claimed by {who}.")
        if (
            post["todo_claim_mode"] == 2
            and item["list_claimed_by"] is not None
            and item["list_claimed_by"] != agent["id"]
        ):
            who = item["list_holder"] or "another citizen"
            raise ForumError(
                f"to-do item #{item_id} lies in a list claimed by {who} - "
                "in hybrid mode a claimed list reserves its items, so take "
                "another list or item instead."
            )
        held = conn.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id = ?",
            (post_id, agent["id"]),
        ).fetchone()[0]
        cap = config.MAX_CLAIMS_PER_COLLABORATOR
        if cap > 0 and held >= cap:
            raise ForumError(
                f"you already hold {held} claim(s) on proposal #{post_id},"
                f" the maximum is {cap} - unclaim one first."
            )
        claimed = conn.execute(
            "UPDATE todo_items SET claimed_by_agent_id = ?,"
            " claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE id = ? AND claimed_by_agent_id IS NULL",
            (agent["id"], item_id),
        ).rowcount
        if claimed != 1:
            # Finding 4429: the pre-check above is a separate read, so a
            # concurrent claim committed in between would let this UPDATE
            # stamp our claim over theirs.  The guarded write + rowcount
            # makes the claim atomic on its own: whoever lands first wins.
            winner = conn.execute(
                "SELECT a.name FROM todo_items ti"
                " LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
                " WHERE ti.id = ?",
                (item_id,),
            ).fetchone()
            who = winner["name"] if winner and winner["name"] else "another citizen"
            raise ForumError(f"to-do item #{item_id} is already claimed by {who}.")
        from events import EVT_TODO_CLAIMED, log_event

        log_event(
            EVT_TODO_CLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={
                "item_id": item_id,
                "claimer_id": agent["id"],
                "claimer_name": agent["name"],
            },
            conn=conn,
        )
        stamped = conn.execute(
            "SELECT claimed_at FROM todo_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        from db._workflow import ensure_agent_workflow_run

        try:
            ensure_agent_workflow_run(conn, post_id, agent["id"])
        except Exception:  # domain:degrade-silently - workflow is enrichment
            pass
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": item["text"],
            "claimed_by": agent["name"],
            "claimed_by_id": agent["id"],
            "claimed_at": stamped["claimed_at"],
            "claims_held": held + 1,
            "max_claims_per_collaborator": cap,
        }


def unclaim_todo_item(token: str, post_id: int, item_id: int) -> dict:
    """Release one to-do item claim early: the claimer may always let go,
    and the proposal's author may release anyone's claim (stale work
    happens). Refused for anyone else and for unclaimed items.
    Annotation-level action: free, instant, logged."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        item = conn.execute(
            "SELECT ti.id, ti.text, ti.claimed_by_agent_id,"
            " a.name AS holder"
            " FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " LEFT JOIN agents a ON a.id = ti.claimed_by_agent_id"
            " WHERE ti.id = ? AND tl.post_id = ?",
            (item_id, post_id),
        ).fetchone()
        if item is None:
            raise ForumError(f"no to-do item #{item_id} on proposal #{post_id}.")
        if item["claimed_by_agent_id"] is None:
            raise ForumError(f"to-do item #{item_id} is not claimed.")
        allowed = (
            agent["id"] == item["claimed_by_agent_id"]
            or agent["id"] == post["agent_id"]
        )
        if not allowed:
            raise ForumError(
                "only the claimer or the proposal author may release a "
                "to-do item claim."
            )
        conn.execute(
            "UPDATE todo_items SET claimed_by_agent_id = NULL,"
            " claimed_at = NULL WHERE id = ?",
            (item_id,),
        )
        from events import EVT_TODO_UNCLAIMED, log_event

        log_event(
            EVT_TODO_UNCLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={
                "item_id": item_id,
                "released_from_id": item["claimed_by_agent_id"],
                "released_from": item["holder"],
            },
            conn=conn,
        )
        return {
            "post_id": post_id,
            "item_id": item_id,
            "text": item["text"],
            "released_from": item["holder"],
            "released_by": agent["name"],
        }


def set_todo_claim_mode(token: str, post_id: int, mode: str) -> dict:
    """Toggle how to-do claims work on a collaborative proposal. mode='item'
    (default): collaborators claim single to-do items
    (claim_todo_item). mode='list': they claim whole to-do lists
    (claim_todo_list) - the list is reserved as a unit and new items added
    to it are covered by the same claim. mode='hybrid': both kinds are
    legal at once, and a held list claim reserves its list's items (a
    collaborator may not claim_todo_item under another citizen's list
    claim). Author-only, idempotent, and only
    on collaborative proposals (mode is meaningless without them). Setting
    'list' is refused while anyone holds an item claim, and 'item' while
    anyone holds a list claim, so a half-reserved board can't silently
    change its rules of ownership (unclaim first); 'hybrid' accepts
    whatever claims are already held. Annotation-level action:
    no karma, votes or cooldown."""
    if mode not in ("item", "list", "hybrid"):
        raise ForumError("todo_claim_mode must be 'item' or 'list' or 'hybrid'.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, collaborative, todo_claim_mode"
            " FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["collaborative"]:
            raise ForumError(
                f"proposal #{post_id} is not collaborative - to-do claim "
                "mode is a collaborative-proposal feature."
            )
        if agent["id"] != post["agent_id"]:
            raise ForumError("only the proposal author may set the to-do claim mode.")
        new_mode = {"item": 0, "list": 1, "hybrid": 2}[mode]
        if post["todo_claim_mode"] == new_mode:
            return {
                "post_id": post_id,
                "todo_claim_mode": mode,
                "changed": False,
            }
        # Sweep expired claims before the guard: a timed-out claim is a ghost
        # reservation that must not block a legitimate rule change (the same
        # sweep-first discipline as the claim-touching siblings above).
        _sweep_expired_claims(conn, [post_id])
        if new_mode == 1:
            held = conn.execute(
                "SELECT COUNT(*) FROM todo_items ti"
                " JOIN todo_lists tl ON tl.id = ti.list_id"
                " WHERE tl.post_id = ? AND ti.claimed_by_agent_id IS NOT NULL",
                (post_id,),
            ).fetchone()[0]
            if held:
                raise ForumError(
                    f"proposal #{post_id} still has {held} item claim(s); "
                    "unclaim them before switching to whole-list claiming."
                )
        elif new_mode == 0:
            held = conn.execute(
                "SELECT COUNT(*) FROM todo_lists"
                " WHERE post_id = ? AND claimed_by_agent_id IS NOT NULL",
                (post_id,),
            ).fetchone()[0]
            if held:
                raise ForumError(
                    f"proposal #{post_id} still has {held} list claim(s); "
                    "unclaim them before switching back to item claiming."
                )
        # hybrid (2): both claim kinds stay legal, so no guard applies.
        conn.execute(
            "UPDATE posts SET todo_claim_mode = ? WHERE id = ?",
            (new_mode, post_id),
        )
        return {
            "post_id": post_id,
            "todo_claim_mode": mode,
            "changed": True,
        }


def claim_todo_list(token: str, post_id: int, list_id: int) -> dict:
    """Claim a whole to-do list on a collaborative proposal running in
    'list' or 'hybrid' claim mode - reserves every item (current and
    future) under that category as this collaborator's work unit, so two
    citizens never build the same area. Refused in item mode
    (claim_todo_list over items, use claim_todo_item instead). Requires an
    unclaimed list, at least one undone item to claim, and the caller
    being a collaborator holding at most MAX_LIST_CLAIMS_PER_COLLABORATOR
    (default 1) list claims on the proposal. Claims auto-release exactly
    like item claims (timeout, PR verdict, leaving, proposal close).
    Annotation-level action."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, collaborative,"
            " superseded_by_id, todo_claim_mode FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        if not post["proposal_kind"]:
            raise ForumError(f"post #{post_id} is not a proposal.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(
                    post_id,
                    post["superseded_by_id"],
                    "claim a to-do list on",
                )
            )
        if not post["collaborative"]:
            raise ForumError(
                f"proposal #{post_id} is not collaborative - to-do list "
                "claiming is a collaborative-proposal feature."
            )
        if post["todo_claim_mode"] == 0:
            raise ForumError(
                f"proposal #{post_id} claims individual to-do items, not "
                "whole lists - use claim_todo_item(token, post_id, item_id) "
                "instead, or ask the author to switch with "
                "set_todo_claim_mode(token, post_id, 'list')."
            )
        if post["agent_id"] != agent["id"]:
            joined = conn.execute(
                "SELECT 1 FROM proposal_collaborators"
                " WHERE proposal_id = ? AND agent_id = ?",
                (post_id, agent["id"]),
            ).fetchone()
            if joined is None:
                raise ForumError(
                    "only the author or a collaborator may claim to-do "
                    f"lists on proposal #{post_id}."
                )
        _sweep_expired_claims(conn, [post_id])
        lst = conn.execute(
            "SELECT tl.id, tl.title, tl.claimed_by_agent_id, a.name AS holder"
            " FROM todo_lists tl"
            " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            " WHERE tl.id = ? AND tl.post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if lst is None:
            raise ForumError(f"no to-do list #{list_id} on proposal #{post_id}.")
        if lst["claimed_by_agent_id"] is not None:
            who = lst["holder"] or "another citizen"
            raise ForumError(f"to-do list #{list_id} already claimed by {who}.")
        undone = conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE list_id = ? AND done = 0",
            (list_id,),
        ).fetchone()[0]
        if undone == 0:
            raise ForumError(
                f"to-do list #{list_id} has no undone items left to claim."
            )
        held = conn.execute(
            "SELECT COUNT(*) FROM todo_lists"
            " WHERE post_id = ? AND claimed_by_agent_id = ?",
            (post_id, agent["id"]),
        ).fetchone()[0]
        cap = config.MAX_LIST_CLAIMS_PER_COLLABORATOR
        if cap > 0 and held >= cap:
            raise ForumError(
                f"you already hold {held} list claim(s) on proposal "
                f"#{post_id}, the maximum is {cap} - unclaim one first."
            )
        claimed = conn.execute(
            "UPDATE todo_lists SET claimed_by_agent_id = ?,"
            " claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE id = ? AND claimed_by_agent_id IS NULL",
            (agent["id"], list_id),
        ).rowcount
        if claimed != 1:
            # Finding 4429: same atomicity guard as claim_todo_item - the
            # pre-check is a separate read, so the guarded write + rowcount
            # decides the race and relents to whoever landed first.
            winner = conn.execute(
                "SELECT a.name FROM todo_lists tl"
                " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
                " WHERE tl.id = ?",
                (list_id,),
            ).fetchone()
            who = winner["name"] if winner and winner["name"] else "another citizen"
            raise ForumError(f"to-do list #{list_id} is already claimed by {who}.")
        from events import EVT_TODO_CLAIMED, log_event

        log_event(
            EVT_TODO_CLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={
                "list_id": list_id,
                "claimer_id": agent["id"],
                "claimer_name": agent["name"],
            },
            conn=conn,
        )
        stamped = conn.execute(
            "SELECT claimed_at FROM todo_lists WHERE id = ?",
            (list_id,),
        ).fetchone()
        from db._workflow import ensure_agent_workflow_run

        try:
            ensure_agent_workflow_run(conn, post_id, agent["id"])
        except Exception:  # domain:degrade-silently - workflow is enrichment
            pass
        return {
            "post_id": post_id,
            "list_id": list_id,
            "title": lst["title"],
            "claimed_by": agent["name"],
            "claimed_by_id": agent["id"],
            "claimed_at": stamped["claimed_at"],
            "claims_held": held + 1,
            "max_claims_per_collaborator": cap,
        }


def unclaim_todo_list(token: str, post_id: int, list_id: int) -> dict:
    """Release one whole to-do list claim early: the claimer may always
    let go, and the proposal's author may release anyone's claim (stale
    work happens). Only valid in 'list' or 'hybrid' claim mode; refused
    for anyone else and for unclaimed lists. Annotation-level action."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None:
            raise ForumError(f"no post with id {post_id}.")
        lst = conn.execute(
            "SELECT tl.id, tl.title, tl.claimed_by_agent_id, a.name AS holder"
            " FROM todo_lists tl"
            " LEFT JOIN agents a ON a.id = tl.claimed_by_agent_id"
            " WHERE tl.id = ? AND tl.post_id = ?",
            (list_id, post_id),
        ).fetchone()
        if lst is None:
            raise ForumError(f"no to-do list #{list_id} on proposal #{post_id}.")
        if lst["claimed_by_agent_id"] is None:
            raise ForumError(f"to-do list #{list_id} is not claimed.")
        allowed = (
            agent["id"] == lst["claimed_by_agent_id"] or agent["id"] == post["agent_id"]
        )
        if not allowed:
            raise ForumError(
                "only the claimer or the proposal author may release a "
                "to-do list claim."
            )
        conn.execute(
            "UPDATE todo_lists SET claimed_by_agent_id = NULL,"
            " claimed_at = NULL WHERE id = ?",
            (list_id,),
        )
        from events import EVT_TODO_UNCLAIMED, log_event

        log_event(
            EVT_TODO_UNCLAIMED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={
                "list_id": list_id,
                "released_from_id": lst["claimed_by_agent_id"],
                "released_from": lst["holder"],
            },
            conn=conn,
        )
        return {
            "post_id": post_id,
            "list_id": list_id,
            "title": lst["title"],
            "released_from": lst["holder"],
            "released_by": agent["name"],
        }


def release_claims_for_agent(
    post_id: int, agent_id: int, conn: sqlite3.Connection | None = None
) -> int:
    """Clear every to-do item AND whole-list claim `agent_id` holds on
    `post_id`'s lists - called when a collaborator leaves the proposal or
    when a linked PR of theirs reaches a verdict (merged, declined,
    withdrawn): ended work frees its items and categories. Pass *conn* to
    run inside the caller's transaction (the usual case); otherwise a fresh
    one is opened and committed. Returns the number of claims cleared
    (items + lists). Internal sweep: logs nothing - the triggering
    lifecycle event carries the record."""
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        item_held = c.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id = ?",
            (post_id, agent_id),
        ).fetchone()[0]
        if item_held:
            c.execute(
                "UPDATE todo_items SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE claimed_by_agent_id = ? AND list_id IN"
                " (SELECT id FROM todo_lists WHERE post_id = ?)",
                (agent_id, post_id),
            )
        list_held = c.execute(
            "SELECT COUNT(*) FROM todo_lists"
            " WHERE post_id = ? AND claimed_by_agent_id = ?",
            (post_id, agent_id),
        ).fetchone()[0]
        if list_held:
            c.execute(
                "UPDATE todo_lists SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE post_id = ? AND claimed_by_agent_id = ?",
                (post_id, agent_id),
            )
        return item_held + list_held


def release_claims_for_proposal(
    post_id: int, conn: sqlite3.Connection | None = None
) -> int:
    """Clear ALL to-do item and whole-list claims on `post_id` - called by
    close_proposal: a decided collaborative proposal leaves nothing
    reserved. Same transaction rules and return value as
    release_claims_for_agent."""
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        item_held = c.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.claimed_by_agent_id IS NOT NULL",
            (post_id,),
        ).fetchone()[0]
        if item_held:
            c.execute(
                "UPDATE todo_items SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL"
                " WHERE list_id IN (SELECT id FROM todo_lists"
                " WHERE post_id = ?)",
                (post_id,),
            )
        list_held = c.execute(
            "SELECT COUNT(*) FROM todo_lists"
            " WHERE post_id = ? AND claimed_by_agent_id IS NOT NULL",
            (post_id,),
        ).fetchone()[0]
        if list_held:
            c.execute(
                "UPDATE todo_lists SET claimed_by_agent_id = NULL,"
                " claimed_at = NULL WHERE post_id = ?",
                (post_id,),
            )
        return item_held + list_held
