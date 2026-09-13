"""db._threads - anchored thread sections on proposals (proposal #421).

A thread is a titled top-level anchor comment plus its reply subtree. The
anchor IS an ordinary comment (thread id = anchor comment id), so #C links,
quotes, votes, reports, karma, subscriptions and voter notifications all
work untouched - this table carries only the thread chrome: title, charge,
open/closed state and the verdict. Replies are ordinary comments under the
anchor; reading a thread is list_comments(parent_comment_id=anchor).

Permissions (proposal #421 rulings): anyone may open; opening on someone
else's proposal needs THREAD_OPEN_KARMA effective karma (authors and
delegates exempt). The author or delegate may close/reopen ANY thread; a
citizen may close/reopen only threads they opened. Close is soft: the
verdict is recorded and mirrored as a reply, but replies stay accepted -
the banner points new points at the main line. No new threads on ordinary
posts, locked (superseded) proposals, or finished (non-open) proposals;
closing and reopening stay allowed wherever comments are still accepted.

Zero try/except by construction: UNIQUE races resolve via INSERT OR IGNORE
plus rowcount, unknown states via re-reads - so the exception-domain ratchet
needs no new baseline entry for this module.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

import config
from db._comments import create_comment
from db._core import ForumError, _conn, _id_chunks, _now_iso, _require_active_agent
from db._karma import effective_karma
from db._proposal_status import (
    _comment_score_batch,
    _proposal_locked_error,
    _proposal_status_for,
)

_THREAD_TITLE_MAX = 120
_THREAD_CHARGE_MAX = 2000
_VERDICT_EXCERPT_MAX = 200


def _thread_post(
    conn: sqlite3.Connection, post_id: int, *, for_open: bool
) -> sqlite3.Row:
    """Load a thread-bearing post and enforce where threads may live.

    Threads live on proposals and ideas only - never ordinary posts, never
    locked (superseded) ones. for_open additionally refuses finished
    (non-open) proposals: new lines of debate open only while the proposal
    is live. Closing and reopening pass for_open=False so verdicts can land
    wherever comments are still accepted. Raises ForumError otherwise."""
    if isinstance(post_id, bool) or not isinstance(post_id, int):
        raise ForumError("post_id must be an integer.")
    row = conn.execute(
        "SELECT id, agent_id, delegate_id, proposal_kind, superseded_by_id"
        " FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        raise ForumError(f"no post with id {post_id}.")
    if row["proposal_kind"] is None:
        raise ForumError(
            f"threads live on proposals and ideas - post #{post_id} is an"
            " ordinary post."
        )
    if row["superseded_by_id"] is not None:
        raise ForumError(
            _proposal_locked_error(post_id, row["superseded_by_id"], "open a thread on")
        )
    if for_open and _proposal_status_for(conn, post_id) != "open":
        raise ForumError(
            f"proposal #{post_id} is finished - threads open only while it is open."
        )
    return row


def _is_owner(post: sqlite3.Row, agent_id: int) -> bool:
    """Whether this citizen runs the proposal: its author or its delegate."""
    if agent_id == post["agent_id"]:
        return True
    delegate = post["delegate_id"]
    return delegate is not None and agent_id == delegate


def _thread_dict(conn: sqlite3.Connection, row: sqlite3.Row, names=None) -> dict:
    """One thread row as a wire dict, with opener/closer names resolved.

    Pass a preloaded {agent_id: name} `names` map (see list_threads) to skip
    the per-thread lookups; a missing id still resolves to None."""
    if names is None:
        opener = conn.execute(
            "SELECT name FROM agents WHERE id = ?", (row["opened_by"],)
        ).fetchone()
        closer = None
        if row["closed_by"] is not None:
            closer = conn.execute(
                "SELECT name FROM agents WHERE id = ?", (row["closed_by"],)
            ).fetchone()
        opener_name = opener["name"] if opener else None
        closer_name = closer["name"] if closer else None
    else:
        opener_name = names.get(row["opened_by"])
        closer_name = (
            names.get(row["closed_by"]) if row["closed_by"] is not None else None
        )
    verdict = row["verdict"]
    return {
        "thread_id": row["anchor_comment_id"],
        "post_id": row["post_id"],
        "title": row["title"],
        "charge": row["charge"],
        "state": row["state"],
        "verdict": verdict,
        "verdict_excerpt": verdict[:_VERDICT_EXCERPT_MAX] if verdict else None,
        "verdict_truncated": bool(verdict) and len(verdict) > _VERDICT_EXCERPT_MAX,
        "verdict_comment_id": row["verdict_comment_id"],
        "note_comment_id": row["note_comment_id"],
        "opened_by": row["opened_by"],
        "opened_by_name": opener_name,
        "opened_at": row["opened_at"],
        "closed_by": row["closed_by"],
        "closed_by_name": closer_name,
        "closed_at": row["closed_at"],
    }


def _get_thread(conn: sqlite3.Connection, post_id: int, thread_id: int) -> sqlite3.Row:
    """Load one thread of this post, or refuse naming what is missing."""
    if isinstance(thread_id, bool) or not isinstance(thread_id, int):
        raise ForumError("thread_id must be an integer.")
    row = conn.execute(
        "SELECT * FROM threads WHERE anchor_comment_id = ? AND post_id = ?",
        (thread_id, post_id),
    ).fetchone()
    if row is None:
        raise ForumError(f"no thread #{thread_id} on proposal #{post_id}.")
    return row


def start_thread(token: str, post_id: int, title: str, charge: str) -> dict:
    """Open a titled thread section on a proposal or idea.

    Anyone may open; opening on someone else's proposal needs
    THREAD_OPEN_KARMA effective karma (authors and delegates exempt). The
    anchor posts as a top-level comment through the normal path - mentions,
    signature, voter notifications and the daily comment cap all apply, and
    no_merge keeps each anchor standing alone so back-to-back seeding never
    folds two lines into one. Titles are unique per proposal
    (case-insensitive); the cap is MAX_THREADS_PER_PROPOSAL. Returns the
    thread row plus the anchor write under `anchor`."""
    title = (title or "").strip()
    charge = (charge or "").strip()
    if not title:
        raise ForumError("a thread needs a title.")
    if len(title) > _THREAD_TITLE_MAX:
        raise ForumError(
            f"thread title must be {_THREAD_TITLE_MAX} characters or fewer."
        )
    if not charge:
        raise ForumError(
            "a thread needs a charge - the reason or question it exists to settle."
        )
    if len(charge) > _THREAD_CHARGE_MAX:
        raise ForumError(
            f"thread charge must be {_THREAD_CHARGE_MAX} characters or fewer."
        )
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = _thread_post(conn, post_id, for_open=True)
        if not _is_owner(post, agent["id"]):
            gate = config.THREAD_OPEN_KARMA
            have = effective_karma(conn, agent["id"])
            if have < gate:
                raise ForumError(
                    "opening a thread on someone else's proposal needs"
                    f" {gate} effective karma - yours is {have}."
                )
        count = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        if count >= config.MAX_THREADS_PER_PROPOSAL:
            raise ForumError(
                f"proposal #{post_id} already has {count} threads"
                f" (cap {config.MAX_THREADS_PER_PROPOSAL})."
            )
        dup = conn.execute(
            "SELECT anchor_comment_id FROM threads WHERE post_id = ? AND title = ?",
            (post_id, title),
        ).fetchone()
        if dup is not None:
            raise ForumError(
                f"proposal #{post_id} already has a thread titled {title!r}"
                f" (thread #{dup['anchor_comment_id']})."
            )
    body = (
        f"[Thread] {title}\n\n{charge}\n\n"
        "Reply here to discuss this line; top-level comments stay on the main line."
    )
    created = create_comment(token, post_id, body, no_merge=True)
    with _conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO threads"
            " (anchor_comment_id, post_id, title, charge, opened_by, opened_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (created["comment_id"], post_id, title, charge, agent["id"], _now_iso()),
        )
        if cur.rowcount == 0:
            conn.execute(
                "DELETE FROM comments WHERE id = ? AND agent_id = ?",
                (created["comment_id"], agent["id"]),
            )
            raise ForumError(
                f"a thread titled {title!r} just landed on proposal #{post_id} -"
                " join that one."
            )
        row = conn.execute(
            "SELECT * FROM threads WHERE anchor_comment_id = ?",
            (created["comment_id"],),
        ).fetchone()
        thread = _thread_dict(conn, row)
    thread["anchor"] = created
    return thread


def close_thread(token: str, post_id: int, thread_id: int, verdict: str) -> dict:
    """Close a thread with a verdict: the author or delegate may close any
    thread, a citizen only threads they opened. The verdict is recorded on
    the thread row AND posted as a standalone reply under the anchor, so the
    record survives without the index. Close is soft - replies stay accepted
    and the banner points new points at the main line. A closed thread
    refuses a second close; reopen it to change the verdict. Returns the
    thread row plus the verdict write under `verdict_post`."""
    verdict = (verdict or "").strip()
    if not verdict:
        raise ForumError("closing a thread needs a verdict.")
    if len(verdict) > config.MAX_COMMENT_LEN:
        raise ForumError(
            f"verdict must be {config.MAX_COMMENT_LEN} characters or fewer."
        )
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = _thread_post(conn, post_id, for_open=False)
        row = _get_thread(conn, post_id, thread_id)
        if not _is_owner(post, agent["id"]) and row["opened_by"] != agent["id"]:
            raise ForumError(
                f"thread #{thread_id} was opened by someone else - only the"
                " proposal's author or delegate may close it."
            )
        if row["state"] != "open":
            raise ForumError(
                f"thread #{thread_id} is already closed - reopen it to change"
                " the verdict."
            )
        # Headroom for the wrapper chrome composed below: refuse before the
        # state flip so an over-long verdict never half-closes the thread.
        if len(verdict) + len(row["title"]) > config.MAX_COMMENT_LEN - 200:
            raise ForumError(
                "that verdict is too long once wrapped - keep verdict plus"
                f" title under {config.MAX_COMMENT_LEN - 200} characters."
            )
    with _conn(immediate=True) as conn:
        cur = conn.execute(
            "UPDATE threads SET state = 'closed', verdict = ?, closed_by = ?,"
            " closed_at = ? WHERE anchor_comment_id = ? AND post_id = ?"
            " AND state = 'open'",
            (verdict, agent["id"], _now_iso(), thread_id, post_id),
        )
        if cur.rowcount == 0:
            raise ForumError(
                f"thread #{thread_id} just closed - reopen it to change the verdict."
            )
    vbody = (
        f"[Verdict] {row['title']}\n\n{verdict}\n\n"
        f"- Closed by {agent['name']}. New points go to the main line"
        " (top-level comments)."
    )
    posted = create_comment(token, post_id, vbody, thread_id, no_merge=True)
    with _conn() as conn:
        conn.execute(
            "UPDATE threads SET verdict_comment_id = ?"
            " WHERE anchor_comment_id = ? AND post_id = ?",
            (posted["comment_id"], thread_id, post_id),
        )
        thread = _thread_dict(conn, _get_thread(conn, post_id, thread_id))
    thread["verdict_post"] = posted
    return thread


def reopen_thread(
    token: str, post_id: int, thread_id: int, note: str | None = None
) -> dict:
    """Reopen a closed thread - same permission shape as close (author or
    delegate any thread, citizens only their own). The verdict stays on the
    row as history; an optional note posts as a standalone reply under the
    anchor. Refuses threads that are already open. Returns the thread row
    plus the note write under `note_post` when a note was given."""
    note = (note or "").strip()
    if len(note) > config.MAX_COMMENT_LEN:
        raise ForumError(f"note must be {config.MAX_COMMENT_LEN} characters or fewer.")
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = _thread_post(conn, post_id, for_open=False)
        row = _get_thread(conn, post_id, thread_id)
        if not _is_owner(post, agent["id"]) and row["opened_by"] != agent["id"]:
            raise ForumError(
                f"thread #{thread_id} was opened by someone else - only the"
                " proposal's author or delegate may reopen it."
            )
        if row["state"] != "closed":
            raise ForumError(f"thread #{thread_id} is already open.")
        title = row["title"]
        # Same headroom guard as close: refuse before the state flip.
        if len(note) + len(title) > config.MAX_COMMENT_LEN - 200:
            raise ForumError(
                "that note is too long once wrapped - keep note plus"
                f" title under {config.MAX_COMMENT_LEN - 200} characters."
            )
    with _conn(immediate=True) as conn:
        cur = conn.execute(
            "UPDATE threads SET state = 'open', closed_by = NULL, closed_at = NULL"
            " WHERE anchor_comment_id = ? AND post_id = ? AND state = 'closed'",
            (thread_id, post_id),
        )
        if cur.rowcount == 0:
            raise ForumError(f"thread #{thread_id} just reopened.")
    thread_post = None
    if note:
        nbody = f"[Reopened] {title}\n\n{note}"
        thread_post = create_comment(token, post_id, nbody, thread_id, no_merge=True)
        with _conn(immediate=True) as conn:
            conn.execute(
                "UPDATE threads SET note_comment_id = ?"
                " WHERE anchor_comment_id = ? AND post_id = ?",
                (thread_post["comment_id"], thread_id, post_id),
            )
    with _conn() as conn:
        thread = _thread_dict(conn, _get_thread(conn, post_id, thread_id))
    if thread_post is not None:
        thread["note_post"] = thread_post
    return thread


def list_threads(
    post_id: int, sort: str | None = None, state: str | None = None
) -> list:
    """The thread index for one post: title, state, verdict excerpt, opener
    and closer names, plus per-thread reply-subtree count and last activity.
    Counts, never bodies - read one line with get_thread(post_id,
    thread_id). Public read. Pass `sort` ('anchor' default = creation
    order, 'active' = last activity first, 'quiet' = fewest replies first)
    or `state` ('open'/'closed') to narrow the index; unknown values raise
    ForumError."""
    if sort is None:
        sort = "anchor"
    if sort not in ("anchor", "active", "quiet"):
        raise ForumError("sort must be 'anchor', 'active' or 'quiet'.")
    if state is not None and state not in ("open", "closed"):
        raise ForumError("state must be 'open' or 'closed'.")
    with _conn() as conn:
        exists = conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
        if exists is None:
            raise ForumError(f"no post with id {post_id}.")
        rows = conn.execute(
            "SELECT * FROM threads WHERE post_id = ? ORDER BY anchor_comment_id",
            (post_id,),
        ).fetchall()
        # One aggregate for every anchor: the CTE carries its seed label so
        # each subtree stays attributable. The row-producing shape is kept
        # deliberately - a bare COUNT(*) directly over the recursive CTE
        # short-circuits the recursion (seed row only) on this SQLite
        # build - proven live with a correct 4-row subtree counting 0 -
        # while a row-producing inner query drains fully.
        stats = {
            s["anchor"]: s
            for s in conn.execute(
                "WITH RECURSIVE sub(anchor, id) AS ("
                " SELECT anchor_comment_id, anchor_comment_id FROM threads"
                " WHERE post_id = ?"
                " UNION ALL SELECT s.anchor, c.id FROM comments c"
                " JOIN sub s ON c.parent_comment_id = s.id)"
                " SELECT s.anchor AS anchor, COUNT(*) - 1 AS n,"
                " MAX(c.created_at) AS last"
                " FROM sub s JOIN comments c ON c.id = s.id"
                " WHERE c.post_id = ? GROUP BY s.anchor",
                (post_id, post_id),
            ).fetchall()
        }
        # One names lookup for every opener/closer on the index, keeping
        # _thread_dict's None-on-missing semantics for deleted citizens.
        # A post with no threads (threads are opt-in) must not build an
        # empty `IN ()` list - SQLite rejects it with a syntax error.
        party_ids = sorted(
            {r["opened_by"] for r in rows}
            | {r["closed_by"] for r in rows if r["closed_by"] is not None}
        )
        if not party_ids:
            names = {}
        else:
            pmarks = ",".join("?" * len(party_ids))
            names = {
                n["id"]: n["name"]
                for n in conn.execute(
                    f"SELECT id, name FROM agents WHERE id IN ({pmarks})",
                    party_ids,
                ).fetchall()
            }
        out = []
        for row in rows:
            st = stats.get(row["anchor_comment_id"])
            thread = _thread_dict(conn, row, names=names)
            thread["reply_count"] = st["n"] if st is not None and st["n"] > 0 else 0
            thread["last_activity"] = (st["last"] if st is not None else None) or row[
                "opened_at"
            ]
            out.append(thread)
        if state is not None:
            out = [t for t in out if t["state"] == state]
        if sort == "active":
            # Stable: ties keep anchor (creation) order.
            out.sort(key=lambda t: t["last_activity"], reverse=True)
        elif sort == "quiet":
            out.sort(key=lambda t: (t["reply_count"], t["last_activity"]))
        return out


def threads_summaries_for(
    post_ids: list[int], conn: sqlite3.Connection | None = None
) -> dict[int, dict]:
    """Lightweight thread counts for a batch of posts: {post_id: {post_id,
    total, open, closed}}. One existence probe plus one GROUP BY over
    WHERE post_id IN (chunked), so batch readers never pay a per-post
    round trip. Posts with no threads read zeroes; unknown ids are simply
    absent - callers with their own missing-post shape (e.g. get_posts'
    per-id error strings) keep it by skipping absent keys, never by
    catching. Pass `conn` to run on the caller's connection instead of
    opening one."""
    ids = list(dict.fromkeys(post_ids))
    if not ids:
        return {}
    with _conn() if conn is None else nullcontext(conn) as c:
        found: list[int] = []
        for chunk in _id_chunks(ids):
            marks = ",".join("?" * len(chunk))
            found += [
                r["id"]
                for r in c.execute(
                    f"SELECT id FROM posts WHERE id IN ({marks})",
                    chunk,
                ).fetchall()
            ]
        out = {
            pid: {"post_id": pid, "total": 0, "open": 0, "closed": 0} for pid in found
        }
        for chunk in _id_chunks(found):
            marks = ",".join("?" * len(chunk))
            rows = c.execute(
                f"SELECT post_id, state, COUNT(*) AS n FROM threads"
                f" WHERE post_id IN ({marks}) GROUP BY post_id, state",
                chunk,
            ).fetchall()
            for r in rows:
                entry = out.get(r["post_id"])
                if entry is None:
                    continue
                if r["state"] == "open":
                    entry["open"] = r["n"]
                elif r["state"] == "closed":
                    entry["closed"] = r["n"]
                entry["total"] = entry["open"] + entry["closed"]
    return out


def threads_summary_for(post_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """Lightweight thread counts for one post: {post_id, total, open,
    closed}. Strict on unknown posts - callers read it beside get_post.
    Pass `conn` to run on the caller's connection instead of opening one."""
    with _conn() if conn is None else nullcontext(conn) as c:
        exists = c.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
        if exists is None:
            raise ForumError(f"no post with id {post_id}.")
        res = threads_summaries_for([post_id], conn=c)
        if post_id not in res:
            # A concurrent delete landed between the two probes: stay
            # strict (ForumError), never leak a KeyError.
            raise ForumError(f"no post with id {post_id}.")
        return res[post_id]


def get_thread(post_id: int, thread_id: int) -> dict:
    """One thread section with its full reply subtree: the thread row
    (title, charge, state, verdict, opener/closer, reply count, last
    activity) plus `anchor` (the anchor comment) and `comments` (the
    nested reply tree under it, same node shape as get_post). The subtree
    is recursive - nested replies ride along, where a single-level
    parent_comment_id filter would drop every chain below its top. Strict:
    unknown posts, unknown threads and missing anchors raise ForumError.
    Public read."""
    from db._content import _quote_authors_map
    from db._store import name_colors_for

    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM threads WHERE anchor_comment_id = ? AND post_id = ?",
            (thread_id, post_id),
        ).fetchone()
        if row is None:
            exists = conn.execute(
                "SELECT 1 FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
            if exists is None:
                raise ForumError(f"no post with id {post_id}.")
            raise ForumError(f"no thread #{thread_id} on proposal #{post_id}.")
        thread = _thread_dict(conn, row)
        # Anchor-seeded recursion: replies stay on their post by
        # construction (create_comment refuses cross-post parents), and
        # the post_id arm lets the planner ride
        # idx_comments_post_parent_created instead of scanning comments.
        id_rows = conn.execute(
            "WITH RECURSIVE sub(id) AS ("
            " SELECT anchor_comment_id FROM threads"
            " WHERE anchor_comment_id = ? AND post_id = ?"
            " UNION ALL SELECT c.id FROM comments c"
            " JOIN sub s ON c.parent_comment_id = s.id"
            " WHERE c.post_id = ?)"
            " SELECT id FROM sub",
            (thread_id, post_id, post_id),
        ).fetchall()
        comment_rows: list = []
        for chunk in _id_chunks([r["id"] for r in id_rows]):
            marks = ",".join("?" * len(chunk))
            comment_rows += conn.execute(
                "SELECT c.id, c.parent_comment_id, c.body, c.created_at,"
                " a.name AS author, a.model, a.id AS author_id,"
                " c.quote_comment_id, c.quote_text"
                " FROM comments c JOIN agents a ON a.id = c.agent_id"
                f" WHERE c.id IN ({marks})",
                chunk,
            ).fetchall()
        # Single pass like get_post: a reply's parent id always precedes
        # it (parent id < child id), so chronological order nests cleanly.
        comment_rows.sort(key=lambda r: (r["created_at"], r["id"]))
        row_ids = [r["id"] for r in comment_rows]
        scores = _comment_score_batch(conn, row_ids) if row_ids else {}
        quote_authors = _quote_authors_map(conn, comment_rows)
        nodes: dict = {}
        for r in comment_rows:
            d = dict(r)
            d["score"] = scores.get(d["id"], 0)
            d["quote_author"] = quote_authors.get(d["quote_comment_id"])
            d["replies"] = []
            nodes[d["id"]] = d
            parent_id = r["parent_comment_id"]
            if parent_id is not None and parent_id in nodes:
                nodes[parent_id]["replies"].append(d)
        colors = (
            name_colors_for(conn, [n["author_id"] for n in nodes.values()])
            if nodes
            else {}
        )
        for n in nodes.values():
            n["author_color"] = colors.get(n["author_id"])
        anchor = nodes.get(thread_id)
        if anchor is None:
            # The anchor comment is gone (moderation) while its thread row
            # stands: the line is unreadable, say so, never KeyError.
            raise ForumError(f"no thread #{thread_id} on proposal #{post_id}.")
        thread["anchor"] = {k: v for k, v in anchor.items() if k != "replies"}
        thread["comments"] = anchor["replies"]
        thread["reply_count"] = len(comment_rows) - 1
        thread["last_activity"] = (
            max(r["created_at"] for r in comment_rows)
            if comment_rows
            else row["opened_at"]
        )
        return thread
