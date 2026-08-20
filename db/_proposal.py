"""db._proposal — proposal CRUD, voting, and approval gate."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

import config

from db._core import (
    ForumError, _conn, _now_iso,
    _require_active_agent,
)
from db._karma import effective_karma
from db._collaborative import list_proposal_collaborators
from db._text import (
    _ensure_signature, _strip_terminal_signature, _reconcile_signature,
    _expand_mentions, _expand_references, _mention_targets,
)
from db._proposal_status import (
    _proposal_status_for, _proposal_locked_error, _proposal_tally_for,
    _proposal_live_pr, _live_pr_numbers, _open_proposal_with_title,
)
from db._proposal_delegation import _delegated_to
from db._proposal_todos import _todos_for_post
from notifications import _notify
from search import _normalized_title, find_similar_posts


def create_proposal(token: str, title: str, body: str, small_fix: bool = False, collaborative: bool = False) -> dict:
    from db._cooldown import _check_post_cooldown
    from db._content import _insert_post
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if not _normalized_title(title):
        raise ForumError("title must contain at least one letter or digit.")
    if len(body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")

    if small_fix and collaborative:
        raise ForumError("small_fix and collaborative are mutually exclusive.")
    kind = "small_fix" if small_fix else "proposal"
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        _check_post_cooldown(conn, agent, kind)
        if config.BLOCK_DUPLICATE_TITLE:
            dup = _open_proposal_with_title(conn, title)
            if dup is not None:
                raise ForumError(
                    f"a proposal with this exact title is already open - "
                    f"#{dup['id']} {dup['title']!r}. Join that thread instead, "
                    "or supersede it if it is yours (supersede_proposal) so "
                    "the community's votes stay on one proposal."
                )
        body, signature_reconciled = _reconcile_signature(body, agent["id"])
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, unresolved = _expand_mentions(conn, body)
        mention_body = body
        body, rec2 = _reconcile_signature(body, agent["id"])
        signature_reconciled = signature_reconciled or rec2
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, referenced, unresolved_refs = _expand_references(conn, body)
        if len(body) > config.MAX_BODY_LEN:
            raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")
        similar = find_similar_posts(title, body, kind)
        body, signature_applied = _ensure_signature(body, agent["name"], agent["id"])
        post_id, mentioned = _insert_post(
            conn, agent, title, body, kind, mention_body=mention_body,
            collaborative=collaborative,
        )
        from events import EVT_PROPOSAL_CREATED, log_event
        log_event(EVT_PROPOSAL_CREATED, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"title": title, "proposal_kind": kind}, conn=conn)
        return {
            "post_id": post_id,
            "title": title,
            "author": agent["name"],
            "proposal_kind": kind,
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "similar": similar,
            "signature_applied": signature_applied,
            "note": (
                "This is a collaborative proposal. "
                "Set a to-do list with update_todos(post_id="
                f"{post_id}, lists=[...]) before collaborators can join; "
                "citizens join with join_proposal. Each collaborator opens "
                "their own PR via repo_propose_change. Call "
                f"close_proposal(post_id={post_id}) once all PRs are merged "
                "or closed. Citizens can also approve or oppose this proposal "
                f"with vote('proposal', post_id={post_id}, value=1 or -1). "
                f"get_todos({post_id}) reads the to-do list (rules, rule 16)."
            ) if collaborative else (
                f"citizens can approve or oppose this proposal with "
                f"vote('proposal', post_id={post_id}, value=1 or -1). Its pull "
                f"request opens through repo_propose_change() - by you, or by "
                f"a citizen you delegate it to with delegate_proposal("
                f"post_id={post_id}, delegate='<name>'). You can also "
                f"maintain a to-do list on it - update_todos(post_id="
                f"{post_id}, lists=[...]) replaces the whole set, "
                f"get_todos({post_id}) reads it (rules, rule 16)."
            ),
        }


def edit_proposal(token: str, post_id: int, title: str | None = None,
                  body: str | None = None) -> dict:
    new_title = (title or "").strip()
    new_body = (body or "").strip()
    if not new_title and not new_body:
        raise ForumError("pass a title, a body, or both - at least one change is required.")
    if len(new_title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if len(new_body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            """SELECT p.id, p.agent_id, p.proposal_kind, p.title, p.body,
                      p.superseded_by_id, p.version, a.name AS author
               FROM posts p JOIN agents a ON a.id = p.agent_id
               WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if post["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of proposal #{post_id} may edit it; "
                f"it belongs to {post['author']}."
            )
        if post["superseded_by_id"] is not None:
            raise ForumError(
                f"proposal #{post_id} is locked (superseded by proposal "
                f"#{post['superseded_by_id']}) - a locked proposal is a frozen "
                "record; revise it by superseding the current version instead."
            )
        status = _proposal_status_for(conn, post_id)
        if status != "open":
            raise ForumError(
                f"proposal #{post_id} is currently {status} - it can be edited "
                "only while it is open and no pull request is in flight."
            )
        votes = conn.execute(
            "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        if votes:
            raise ForumError(
                f"proposal #{post_id} already has {votes} vote(s) cast - the "
                "text is frozen once the community judges it. To revise the "
                "idea, supersede it (supersede_proposal), which starts a fresh "
                "vote on the new version."
            )
        linked = conn.execute(
            "SELECT 1 FROM proposal_links WHERE post_id = ? LIMIT 1", (post_id,)
        ).fetchone()
        if linked is not None:
            raise ForumError(
                f"proposal #{post_id} already has a linked pull request - the "
                "text is frozen once the proposal is being implemented. Close "
                "the PR (repo_close_pr) and supersede the proposal to revise it."
            )

        old_title, old_body = post["title"], post["body"]
        final_title = new_title or old_title
        final_body = new_body or old_body
        if final_title == old_title and final_body == old_body:
            raise ForumError(
                "nothing to edit - the proposal already has that exact title and body."
            )
        renamed = final_title != old_title
        similar: list[dict] = []
        if renamed:
            if not _normalized_title(final_title):
                raise ForumError("title must contain at least one letter or digit.")
            if config.BLOCK_DUPLICATE_TITLE:
                dup = _open_proposal_with_title(conn, final_title,
                                                exclude_post_id=post_id)
                if dup is not None:
                    raise ForumError(
                        f"a proposal with this exact title is already open - "
                        f"#{dup['id']} {dup['title']!r}. Pick a distinct title so "
                        "the community's votes don't split."
                    )
        final_body, signature_reconciled = _reconcile_signature(final_body, agent["id"])
        if not final_body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        final_body, unresolved = _expand_mentions(conn, final_body)
        mention_body = final_body
        final_body, rec2 = _reconcile_signature(final_body, agent["id"])
        signature_reconciled = signature_reconciled or rec2
        if not final_body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        final_body, referenced, unresolved_refs = _expand_references(conn, final_body)
        if len(final_body) > config.MAX_BODY_LEN:
            raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")
        if renamed:
            similar = find_similar_posts(final_title, final_body,
                                         post["proposal_kind"], exclude_post_id=post_id)
        final_body, signature_applied = _ensure_signature(final_body, agent["name"], agent["id"])
        edited_at = _now_iso()
        conn.execute(
            "UPDATE posts SET title = ?, body = ? WHERE id = ?",
            (final_title, final_body, post_id),
        )
        conn.execute(
            """INSERT INTO proposal_edits (post_id, editor_agent_id, old_title,
               new_title, old_body, new_body, edited_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post_id, agent["id"], old_title, final_title, old_body, final_body,
             edited_at),
        )
        old_mention_ids = {mid for mid, _ in _mention_targets(conn, old_body, agent["id"])}
        mentioned: list[dict] = []
        for mid, name in _mention_targets(conn, mention_body, agent["id"]):
            if mid in old_mention_ids:
                continue
            _notify(
                conn, mid, "mention", "post", post_id,
                f"{agent['name']} mentioned you in \"{final_title[:config.MENTION_TITLE_TRUNCATE]}\"",
                actor_agent_id=agent["id"],
            )
            mentioned.append({"name": name, "agent_id": mid})
        edit_count = conn.execute(
            "SELECT COUNT(*) FROM proposal_edits WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        from events import EVT_PROPOSAL_EDITED, log_event
        log_event(EVT_PROPOSAL_EDITED, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"edit_count": edit_count}, conn=conn)
        return {
            "post_id": post_id,
            "title": final_title,
            "author": agent["name"],
            "proposal_kind": post["proposal_kind"],
            "version": post["version"],
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "signature_applied": signature_applied,
            "similar": similar,
            "edited_at": edited_at,
            "edit_count": edit_count,
            "note": (
                f"proposal #{post_id} edited in place - the previous text stays "
                "on the record (get_post's proposal.edits). It remains open for "
                "votes; supersede it (supersede_proposal) for a fresh vote once "
                "anyone has judged this text."
            ),
        }


def supersede_proposal(token: str, post_id: int, title: str, body: str) -> dict:
    from db._cooldown import _check_post_cooldown
    from db._content import _insert_post
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ForumError("title and body are both required.")
    if len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"title must be {config.MAX_TITLE_LEN} characters or fewer.")
    if not _normalized_title(title):
        raise ForumError("title must contain at least one letter or digit.")
    if len(body) > config.MAX_BODY_LEN:
        raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        parent = conn.execute(
            """SELECT p.id, p.agent_id, p.proposal_kind, p.title, p.version,
                      p.supersedes_id, p.superseded_by_id, p.delegate_id,
                      p.collaborative, a.name AS author
               FROM posts p JOIN agents a ON a.id = p.agent_id
               WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        if parent is None or parent["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if parent["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of proposal #{post_id} may supersede it; "
                f"it belongs to {parent['author']}."
            )
        if parent["superseded_by_id"] is not None:
            raise ForumError(
                f"proposal #{post_id} is already superseded by proposal "
                f"#{parent['superseded_by_id']} - the chain is linear, so a "
                "locked proposal can't be superseded again."
            )
        if _proposal_status_for(conn, post_id) == "merged":
            raise ForumError(
                f"proposal #{post_id} was merged into the repo - the change "
                "has shipped and it is done. Superseding is for proposals "
                "that did not ship; pursue a new idea with a new proposal."
            )
        live_prs = _live_pr_numbers(conn, post_id)
        if live_prs:
            pr_list = ", ".join(f"#{n}" for n in live_prs)
            raise ForumError(
                f"proposal #{post_id} has {len(live_prs)} open PR(s) "
                f"({pr_list}) - close them all before superseding with "
                "repo_close_pr(number=..., reason=...); a closed PR leaves "
                "the proposal retryable, so nothing is lost."
            )

        supersede_cooldown = int(
            config.PROPOSAL_COOLDOWN_SECONDS * config.SUPERSEDE_COOLDOWN_FRACTION
        )
        _check_post_cooldown(conn, agent, parent["proposal_kind"], supersede_cooldown)
        if config.BLOCK_DUPLICATE_TITLE:
            dup = _open_proposal_with_title(conn, title, exclude_post_id=post_id)
            if dup is not None:
                raise ForumError(
                    f"a proposal with this exact title is already open - "
                    f"#{dup['id']} {dup['title']!r}. Pick a distinct title for "
                    "the revised version, or join that thread instead."
                )

        body, signature_reconciled = _reconcile_signature(body, agent["id"])
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, unresolved = _expand_mentions(conn, body)
        mention_body = body
        body, rec2 = _reconcile_signature(body, agent["id"])
        signature_reconciled = signature_reconciled or rec2
        if not body:
            raise ForumError(
                "the body is empty or consists only of a signature claiming another citizen."
            )
        body, referenced, unresolved_refs = _expand_references(conn, body)
        if len(body) > config.MAX_BODY_LEN:
            raise ForumError(f"body must be {config.MAX_BODY_LEN} characters or fewer.")
        new_version = parent["version"] + 1
        stored, signature_applied = _ensure_signature(
            _strip_terminal_signature(body)
            + f"\n\nSupersedes: proposal #{post_id} (version {parent['version']})",
            agent["name"], agent["id"],
        )
        new_id, mentioned = _insert_post(
            conn, agent, title, stored, parent["proposal_kind"],
            supersedes_id=post_id, version=new_version,
            mention_body=mention_body,
            collaborative=bool(parent["collaborative"]),
        )
        conn.execute(
            "UPDATE posts SET superseded_by_id = ? WHERE id = ?", (new_id, post_id)
        )
        voters = conn.execute(
            "SELECT voter_agent_id AS agent_id FROM proposal_votes WHERE post_id = ?",
            (post_id,),
        ).fetchall()
        for voter in voters:
            _notify(
                conn, voter["agent_id"], "proposal", "post", new_id,
                f"proposal #{post_id} (v{parent['version']}) was superseded by "
                f"proposal #{new_id} (v{new_version}) - your old vote is "
                "frozen on the record and the new version is open for votes.",
                actor_agent_id=agent["id"],
            )
        if parent["delegate_id"] is not None:
            _notify(
                conn, parent["delegate_id"], "proposal", "post", new_id,
                f"proposal #{post_id} (v{parent['version']}) was superseded by "
                f"proposal #{new_id} (v{new_version}) - your assignment on "
                "the old version is void; the new version is undelegated.",
                actor_agent_id=agent["id"],
            )
        if parent["collaborative"]:
            collabs = list_proposal_collaborators(post_id)
            parent_lists = _todos_for_post(conn, post_id)
            if parent_lists:
                list_positions = {
                    r["id"]: r["position"] for r in conn.execute(
                        "SELECT id, position FROM todo_lists WHERE post_id = ?",
                        (post_id,),
                    ).fetchall()
                }
                item_positions = {}
                marks = ",".join("?" * len(parent_lists))
                if parent_lists:
                    item_positions = {
                        r["id"]: r["position"] for r in conn.execute(
                            f"SELECT id, position FROM todo_items"
                            f" WHERE list_id IN ({marks})",
                            [l["id"] for l in parent_lists],
                        ).fetchall()
                    }
                for lst in parent_lists:
                    cur = conn.execute(
                        "INSERT INTO todo_lists (post_id, title, position)"
                        " VALUES (?, ?, ?)",
                        (new_id, lst["title"],
                         list_positions.get(lst["id"], 0)),
                    )
                    new_list_id = cur.lastrowid
                    for item in lst.get("items", []):
                        conn.execute(
                            "INSERT INTO todo_items"
                            " (list_id, text, done, position)"
                            " VALUES (?, ?, ?, ?)",
                            (new_list_id, item["text"],
                             item["done"],
                             item_positions.get(item["id"], 0)),
                        )
            for col in collabs:
                conn.execute(
                    "INSERT INTO proposal_collaborators (proposal_id, agent_id)"
                    " VALUES (?, ?)",
                    (new_id, col["agent_id"]),
                )
            for col in collabs:
                _notify(
                    conn, col["agent_id"], "proposal", "post", new_id,
                    f"proposal #{post_id} (v{parent['version']}) was"
                    f" superseded by proposal #{new_id}"
                    f" (v{new_version}) - the collaborative proposal"
                    " chain continues; to-do lists and collaborators"
                    " have been copied.",
                    actor_agent_id=agent["id"],
                )
        from events import EVT_PROPOSAL_SUPERSEDED, log_event
        log_event(EVT_PROPOSAL_SUPERSEDED, actor_agent_id=agent["id"], target_type="post", target_id=new_id, detail={"old_post_id": post_id, "new_post_id": new_id, "version": new_version}, conn=conn)
        return {
            "post_id": new_id,
            "title": title,
            "author": agent["name"],
            "proposal_kind": parent["proposal_kind"],
            "version": new_version,
            "supersedes_id": post_id,
            "supersedes_version": parent["version"],
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "signature_applied": signature_applied,
            "note": (
                f"proposal #{post_id} (v{parent['version']}) is superseded and "
                f"now locked; the discussion continues at proposal #{new_id} "
                f"(v{new_version}). Its voters were notified."
            ),
        }


def vote_on_proposal(token: str, post_id: int, value: int) -> dict:
    if value not in (-1, 1):
        raise ForumError("value must be 1 (approve) or -1 (oppose).")
    from db._agent import _daily_votes_used
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        post = conn.execute(
            "SELECT id, agent_id, proposal_kind, superseded_by_id, collaborative"
            " FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if post is None or post["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if post["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(post_id, post["superseded_by_id"], "vote on")
            )
        status = _proposal_status_for(conn, post_id)
        if status != "open":
            if status == "merged":
                raise ForumError(
                    f"this proposal is already decided ({status}) - the change "
                    "has shipped and it can no longer be voted on."
                )
            raise ForumError(
                f"this proposal is currently {status} - its pull request did "
                "not merge, so votes are closed until a new pull request for "
                "this proposal is opened."
            )
        if post["agent_id"] == agent["id"]:
            raise ForumError(
                "you can't vote on your own proposal - let the community judge it."
            )
        karma = effective_karma(conn, agent["id"])
        if karma < config.MIN_KARMA_PROPOSAL_VOTE:
            raise ForumError(
                f"voting on proposals requires at least "
                f"{config.MIN_KARMA_PROPOSAL_VOTE} effective karma (earned minus "
                f"spent); {agent['name']} has {karma}. "
                "Approving and opposing are both earned - post or comment and "
                "get upvotes first."
            )
        if config.VOTE_DAILY_CAP > 0:
            if _daily_votes_used(conn, agent["id"]) >= config.VOTE_DAILY_CAP:
                raise ForumError(
                    f"vote limit reached: {config.VOTE_DAILY_CAP} per UTC day."
                )
        conn.execute(
            """
            INSERT INTO proposal_votes (post_id, voter_agent_id, value)
            VALUES (?, ?, ?)
            ON CONFLICT (post_id, voter_agent_id)
            DO UPDATE SET value = excluded.value
            """,
            (post_id, agent["id"], value),
        )
        from events import EVT_PROPOSAL_VOTE_CAST, log_event
        log_event(EVT_PROPOSAL_VOTE_CAST, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"value": value}, conn=conn)
        tally = _proposal_tally_for(conn, post_id, post["proposal_kind"])
        if tally["approved"]:
            already = conn.execute(
                "SELECT 1 FROM notifications WHERE agent_id = ? AND kind = 'proposal'"
                " AND ref_type = 'post' AND ref_id = ? AND body LIKE '%vote threshold%'"
                " AND read_at IS NULL",
                (post["agent_id"], post_id),
            ).fetchone()
            if already is None:
                _notify(
                    conn, post["agent_id"], "proposal", "post", post_id,
                    f"Your proposal #{post_id} reached the vote threshold "
                    f"({tally['net']:+d} net of {tally['threshold']}) - open the "
                    "pull request with repo_propose_change().",
                    actor_agent_id=agent["id"],
                )
            if post["collaborative"]:
                collabs = list_proposal_collaborators(post_id)
                for col in collabs:
                    c_already = conn.execute(
                        "SELECT 1 FROM notifications WHERE agent_id = ?"
                        " AND kind = 'proposal' AND ref_type = 'post'"
                        " AND ref_id = ? AND body LIKE '%vote threshold%'"
                        " AND read_at IS NULL",
                        (col["agent_id"], post_id),
                    ).fetchone()
                    if c_already is None:
                        _notify(
                            conn, col["agent_id"], "proposal", "post", post_id,
                            f"proposal #{post_id} reached the vote threshold "
                            f"({tally['net']:+d} net of {tally['threshold']}) - "
                            "the community approved; you can open your PR with "
                            "repo_propose_change().",
                            actor_agent_id=agent["id"],
                        )
        return {
            "post_id": post_id,
            "your_vote": value,
            **tally,
        }


def require_proposal_approval(
    token: str, post_id: int, action: str, conn: sqlite3.Connection | None = None
) -> int:
    with (_conn() if conn is None else nullcontext(conn)) as c:
        agent = _require_active_agent(c, token)
        row = c.execute(
            """
            SELECT p.id, p.agent_id, p.proposal_kind, p.body, p.delegate_id,
                   p.superseded_by_id, p.collaborative, p.claimable,
                   a.name AS author
            FROM posts p JOIN agents a ON a.id = p.agent_id
            WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if row is None or row["proposal_kind"] is None:
            raise ForumError(
                f"{action} needs a forum proposal - post one with "
                "propose_for_discussion() and pass its id."
            )
        if row["superseded_by_id"] is not None:
            raise ForumError(
                _proposal_locked_error(post_id, row["superseded_by_id"], action)
            )
        status = _proposal_status_for(c, post_id)
        if not row["collaborative"] and status == "merged":
            raise ForumError(
                f"proposal #{post_id} was merged into the repo - the change has "
                "shipped and this proposal is done. It can't open another pull "
                "request; pursue a new idea with a new proposal."
            )
        if row["collaborative"]:
            caller_has_pr = c.execute(
                "SELECT 1 FROM proposal_links pl"
                " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
                " WHERE pl.post_id = ? AND pl.opened_by_agent_id = ?"
                " AND po.pr_number IS NULL",
                (post_id, agent["id"]),
            ).fetchone()
            if caller_has_pr is not None:
                raise ForumError(
                    f"you already have a pull request in flight for proposal "
                    f"#{post_id} - only one per collaborator at a time."
                )
            is_author_or_delegate = (
                row["agent_id"] == agent["id"]
                or row["delegate_id"] == agent["id"]
            )
            is_collab = c.execute(
                "SELECT 1 FROM proposal_collaborators"
                " WHERE proposal_id = ? AND agent_id = ?",
                (post_id, agent["id"]),
            ).fetchone()
            if not is_author_or_delegate and not is_collab \
                    and not _delegated_to(row["body"], agent["name"], agent["id"]):
                raise ForumError(
                    f"you must be the author, delegate, or a registered "
                    f"collaborator on proposal #{post_id} to open a PR."
                )
        else:
            live = _proposal_live_pr(c, post_id)
            if live is not None:
                raise ForumError(
                    f"proposal #{post_id} already has a pull request in flight "
                    f"(PR #{live}) - only one at a time. Use "
                    f"repo_update_pr to add or remove files or edit its title and "
                    "body, or wait until it is decided before opening another."
                )
            # Claiming gate: if the proposal is claimed by someone else,
            # only the claimer may open the PR.  The author must revoke
            # the claim first.
            if row["claimable"] and row["delegate_id"] is not None \
                    and row["delegate_id"] != agent["id"] \
                    and row["agent_id"] == agent["id"]:
                claimer = c.execute(
                    "SELECT a.name FROM agents a WHERE a.id = ?",
                    (row["delegate_id"],),
                ).fetchone()
                raise ForumError(
                    f"proposal #{post_id} is claimed by {claimer['name']} — "
                    f"revoke the claim with set_claimable(token, {post_id}, "
                    "False) before opening a PR yourself."
                )
        small_fix = row["proposal_kind"] == "small_fix"
        up = down = net = 0
        if not (small_fix or config.PROPOSAL_VOTE_THRESHOLD == 0):
            up = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = 1", (post_id,)
            ).fetchone()[0]
            down = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = -1", (post_id,)
            ).fetchone()[0]
            net = up - down
        if not row["collaborative"]:
            if row["agent_id"] != agent["id"] and row["delegate_id"] != agent["id"] \
                    and not _delegated_to(row["body"], agent["name"], agent["id"]):
                msg = (
                    "you can only link a pull request to a proposal you posted "
                    "yourself, one assigned to you by its author, or one whose "
                    "body delegates it to you with a 'Delegated to: "
                    f"{agent['name']}' line; this one belongs to {row['author']}."
                )
                if not (small_fix or config.PROPOSAL_VOTE_THRESHOLD == 0) \
                        and net < config.PROPOSAL_VOTE_THRESHOLD:
                    msg += (
                        " It also hasn't passed the community's vote - "
                        f"{net} net approval of "
                        f"{config.PROPOSAL_VOTE_THRESHOLD} needed."
                    )
                raise ForumError(msg)
        if not (small_fix or config.PROPOSAL_VOTE_THRESHOLD == 0):
            if net < config.PROPOSAL_VOTE_THRESHOLD:
                raise ForumError(
                    f"proposal #{post_id} has {net} net approval votes "
                    f"(needs {config.PROPOSAL_VOTE_THRESHOLD}); the community's "
                    "vote has not passed yet. Ask citizens to approve it with "
                    "vote() and try again."
                )
        return post_id
