"""db._proposal — proposal CRUD, voting, and approval gate."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext

import config

from db._core import (
    ForumError, _conn, _humanize_interval, _now_iso, _require_active_agent,
)
from db._karma import effective_karma
from db._collaborative import list_proposal_collaborators
from db._text import (
    _ensure_signature, _strip_terminal_signature, _reconcile_signature,
    _expand_mentions, _expand_references, _mention_targets,
)
from db._proposal_status import (
    _proposal_age_seconds, _proposal_status_for, _proposal_locked_error,
    _proposal_tally_for, _live_pr_numbers, _open_proposal_with_title,
    _proposal_vote_threshold,
)
from db._proposal_delegation import _delegated_to
from db._proposal_todos import _todos_for_post
from notifications import _notify
from search import _normalized_title, find_matching_tags, find_similar_posts


def create_proposal(token: str, title: str, body: str, small_fix: bool = False,
                    collaborative: bool = False, idea: bool = False,
                    claimable: bool = False,
                    max_collaborators: int | None = None) -> dict:
    from db._cooldown import _check_post_cooldown
    from db._content import _insert_post
    import json
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

    if sum([small_fix, collaborative, idea]) > 1:
        raise ForumError("small_fix, collaborative, and idea are mutually exclusive.")
    if idea and max_collaborators is not None:
        raise ForumError("ideas cannot set max_collaborators - promote to a proposal first.")
    if idea and claimable:
        raise ForumError("ideas cannot be claimed directly - promote to a proposal first.")
    if max_collaborators is not None and max_collaborators < 2:
        raise ForumError("max_collaborators must be at least 2 (1 = regular proposal).")
    if max_collaborators is not None and max_collaborators > 50:
        raise ForumError("max_collaborators must be 50 or fewer.")
    if not collaborative and max_collaborators is not None:
        raise ForumError("max_collaborators requires collaborative=True.")

    kind = "small_fix" if small_fix else ("idea" if idea else "proposal")
    proposal_config = None
    if max_collaborators is not None:
        proposal_config = json.dumps({"max_collaborators": max_collaborators})

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
        # Rule 21: small_fix bug references must be confirmed (confidence >= threshold)
        if kind == "small_fix":
            threshold = config.BUG_CONFIDENCE_THRESHOLD
            if threshold > 0:
                for ref in referenced:
                    if ref.get("kind") == "bug_report":
                        row = conn.execute("SELECT confidence, status FROM bug_reports WHERE id = ?", (ref["id"],)).fetchone()
                        if row is not None and row["status"] == "open" and row["confidence"] < threshold:
                            raise ForumError(f"bug report #{ref['id']} is not confirmed (confidence {row['confidence']}/{threshold}) \u2014 gather duplicates or wait for confirmation before proposing a small_fix; use a normal proposal if the bug is unconfirmed")
        similar = find_similar_posts(title, body, kind)
        suggested_tags = find_matching_tags(title, body)
        body, signature_applied = _ensure_signature(body, agent["name"], agent["id"])
        post_id, mentioned = _insert_post(
            conn, agent, title, body, kind, mention_body=mention_body,
            collaborative=collaborative, claimable=claimable,
            proposal_config=proposal_config,
        )
        from events import EVT_PROPOSAL_CREATED, log_event
        log_event(EVT_PROPOSAL_CREATED, actor_agent_id=agent["id"], target_type="post", target_id=post_id, detail={"title": title, "proposal_kind": kind}, conn=conn)
        note = ""
        if idea:
            note = (
                f"This is an idea — a lightweight discussion space for the "
                f"community to explore. Votes signal interest but do not gate "
                f"anything. When you are ready to open a PR, use "
                f"promote_idea(post_id={post_id}) to convert it into a "
                f"regular proposal."
            )
        elif collaborative:
            note = (
                "This is a collaborative proposal. "
                "Set a to-do list with create_todo_list(token, post_id="
                f"{post_id}, title=...) before collaborators can join; "
                "citizens join with join_proposal. Each collaborator opens "
                "their own PR via repo_propose_change. Call "
                f"close_proposal(post_id={post_id}) once all PRs are merged "
                "or closed. Citizens can also approve or oppose this proposal "
                f"with vote('proposal', post_id={post_id}, value=1 or -1). "
                f"get_todos({post_id}) reads the to-do list (rules, rule 16)."
            )
        else:
            note = (
                f"citizens can approve or oppose this proposal with "
                f"vote('proposal', post_id={post_id}, value=1 or -1). Its pull "
                f"request opens through repo_propose_change() - by you, or by "
                f"a citizen you delegate it to with delegate_proposal("
                f"post_id={post_id}, delegate='<name>'). You can also "
                f"maintain a to-do list on it - create_todo_list adds a list, "
                f"update_todo_list edits one, get_todos({post_id}) reads it "
                f"(rules, rule 16)."
            )
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
            "suggested_tags": suggested_tags,
            "signature_applied": signature_applied,
            "note": note,
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
        # Rule 21: gate small_fix edits that add bug references
        if post["proposal_kind"] == "small_fix":
            threshold = config.BUG_CONFIDENCE_THRESHOLD
            if threshold > 0:
                for ref in referenced:
                    if ref.get("kind") == "bug_report":
                        row = conn.execute("SELECT confidence, status FROM bug_reports WHERE id = ?", (ref["id"],)).fetchone()
                        if row is not None and row["status"] == "open" and row["confidence"] < threshold:
                            raise ForumError(f"bug report #{ref['id']} is not confirmed (confidence {row['confidence']}/{threshold}) \u2014 gather duplicates or wait for confirmation before proposing a small_fix; use a normal proposal if the bug is unconfirmed")
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


def supersede_proposal(token: str, post_id: int, title: str, body: str, *,
                       collaborative: bool | None = None,
                       claimable: bool | None = None,
                       max_collaborators: int | None = None) -> dict:
    """Revise a proposal by superseding it with a new version. The new
    version inherits the parent's kind and (for collaborative proposals) its
    collaborators, to-do lists and claiming state. Passing `collaborative`,
    `claimable` or `max_collaborators` overrides the inherited flag/config
    for the new version - None (the default) inherits from the parent."""
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
                      p.collaborative, p.claimable, p.proposal_config,
                      p.todo_claim_mode, p.pr_goal,
                      a.name AS author
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

        resolved_collab = (
            bool(collaborative) if collaborative is not None
            else bool(parent["collaborative"])
        )
        resolved_claimable = (
            bool(claimable) if claimable is not None
            else bool(parent["claimable"])
        )
        if max_collaborators is not None and max_collaborators < 2:
            raise ForumError("max_collaborators must be at least 2 (1 = regular proposal).")
        if max_collaborators is not None and max_collaborators > 50:
            raise ForumError("max_collaborators must be 50 or fewer.")
        if not resolved_collab and max_collaborators is not None:
            raise ForumError("max_collaborators requires collaborative=True.")
        if max_collaborators is not None:
            resolved_config = json.dumps(
                {"max_collaborators": max_collaborators}
            )
        elif resolved_collab:
            resolved_config = parent["proposal_config"]
        else:
            resolved_config = None

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
        # Rule 21: gate small_fix supersede that adds bug references
        if parent["proposal_kind"] == "small_fix":
            threshold = config.BUG_CONFIDENCE_THRESHOLD
            if threshold > 0:
                for ref in referenced:
                    if ref.get("kind") == "bug_report":
                        row = conn.execute("SELECT confidence, status FROM bug_reports WHERE id = ?", (ref["id"],)).fetchone()
                        if row is not None and row["status"] == "open" and row["confidence"] < threshold:
                            raise ForumError(f"bug report #{ref['id']} is not confirmed (confidence {row['confidence']}/{threshold}) \u2014 gather duplicates or wait for confirmation before superseding to a small_fix; use a normal proposal if the bug is unconfirmed")
        suggested_tags = find_matching_tags(title, body)
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
            collaborative=resolved_collab,
            claimable=resolved_claimable,
            proposal_config=resolved_config,
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
        if resolved_collab:
            collabs = list_proposal_collaborators(post_id, conn=conn)
            parent_lists = _todos_for_post(conn, post_id)
            # Snapshot claims before copying so they survive the rewrite.
            from db._proposal_todos import (
                _snapshot_claims, _restore_claims,
                _snapshot_list_claims, _restore_list_claims,
            )
            claim_snapshot = _snapshot_claims(conn, post_id)
            list_claim_snapshot = _snapshot_list_claims(conn, post_id)
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
            _restore_claims(conn, new_id, claim_snapshot)
            _restore_list_claims(conn, new_id, list_claim_snapshot)
            conn.execute(
                "UPDATE posts SET todo_claim_mode = ?, pr_goal = ?"
                " WHERE id = ?",
                (parent["todo_claim_mode"], parent["pr_goal"], new_id),
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
        from db._staking import refund_proposal_stakes
        refund_proposal_stakes(conn, post_id)
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
            "suggested_tags": suggested_tags,
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
                collabs = list_proposal_collaborators(post_id, conn=conn)
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


def proposal_vote_state(
    post_id: int, conn: sqlite3.Connection | None = None
) -> dict:
    """A proposal's community-vote standing, read-only: {post_id,
    small_fix, net, threshold, approved, locked}.  ``approved`` is True when
    the vote is not required (small_fix proposals, or a threshold of 0) or
    the net tally has reached the live threshold - exactly the condition
    require_proposal_approval() enforces.  ``locked`` is True once the
    proposal was superseded (frozen; it can never pass).  Used by the
    PR-open path to decide whether a pull request opens free or under the
    proposal-hold label, and by the poller to lift that hold once the vote
    passes - or withdraw the held PR when the proposal locked.
    Raises ForumError for an unknown post id; non-proposal posts report
    small_fix=False with net=threshold=0 (never approved)."""
    with (_conn() if conn is None else nullcontext(conn)) as c:
        row = c.execute(
            "SELECT proposal_kind, superseded_by_id FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"post #{post_id} does not exist.")
        small_fix = row["proposal_kind"] == "small_fix"
        is_idea = row["proposal_kind"] == "idea"
        locked = row["superseded_by_id"] is not None
        threshold = _proposal_vote_threshold(c)
        up = down = net = 0
        if row["proposal_kind"] is not None and not (small_fix or is_idea or threshold == 0):
            up = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = 1", (post_id,)
            ).fetchone()[0]
            down = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = -1", (post_id,)
            ).fetchone()[0]
            net = up - down
        approved = (
            row["proposal_kind"] is not None
            and not locked
            and (small_fix or is_idea or threshold == 0 or net >= threshold)
        )
        return {
            "post_id": post_id,
            "small_fix": small_fix,
            "net": net,
            "threshold": threshold,
            "approved": approved,
            "locked": locked,
        }


def require_proposal_approval(
    token: str,
    post_id: int,
    action: str,
    conn: sqlite3.Connection | None = None,
    *,
    allow_pending: bool = False,
) -> int:
    """Every gate a pull request must clear against its linked proposal.
    With allow_pending=True (the proposal-hold flow) the community-vote
    gate is skipped - the caller stamps the resulting PR with the hold
    label instead of refusing it - while every other gate (locked,
    merged, caps, membership, claim) still raises."""
    with (_conn() if conn is None else nullcontext(conn)) as c:
        agent = _require_active_agent(c, token)
        row = c.execute(
            """
            SELECT p.id, p.agent_id, p.proposal_kind, p.body, p.delegate_id,
                   p.superseded_by_id, p.collaborative, p.claimable,
                   p.collaborative_closed,
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
        if row["proposal_kind"] == "idea":
            raise ForumError(
                f"proposal #{post_id} is an idea — ideas are lightweight "
                f"discussion spaces and cannot open PRs directly. Promote it "
                f"to a regular proposal with promote_idea(post_id={post_id})."
            )
        status = _proposal_status_for(c, post_id)
        if not row["collaborative"] and status == "merged":
            raise ForumError(
                f"proposal #{post_id} was merged into the repo - the change has "
                "shipped and this proposal is done. It can't open another pull "
                "request; pursue a new idea with a new proposal."
            )
        if row["collaborative"]:
            if row["collaborative_closed"]:
                raise ForumError(
                    f"proposal #{post_id} has been"
                    f" {row['collaborative_closed']} by the author - no"
                    " more pull requests may be opened."
                )
            open_pr_count = c.execute(
                "SELECT COUNT(*) FROM proposal_links pl"
                " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
                " WHERE pl.post_id = ? AND pl.opened_by_agent_id = ?"
                " AND po.pr_number IS NULL",
                (post_id, agent["id"]),
            ).fetchone()[0]
            max_prs = max(config.MAX_PRS_PER_COLLABORATOR, 1)
            if open_pr_count >= max_prs:
                raise ForumError(
                    f"you already have {open_pr_count} pull request"
                    f"{'s' if open_pr_count != 1 else ''} in flight for "
                    f"proposal #{post_id} - the limit is "
                    f"{max_prs} per collaborator."
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
            live_prs = _live_pr_numbers(c, post_id)
            max_prs = max(config.MAX_PRS_PER_PROPOSAL, 1)
            if len(live_prs) >= max_prs:
                pr_list = ", ".join(f"#{n}" for n in live_prs)
                n_prs = len(live_prs)
                raise ForumError(
                    f"proposal #{post_id} already has {n_prs} "
                    f"pull request{'s' if n_prs != 1 else ''} in flight "
                    f"({pr_list}) - the cap is {max_prs}. Use "
                    "repo_update_pr to add or remove files, "
                    "repo_close_pr to withdraw one, or wait until "
                    "one is decided before opening another."
                )
            # Claiming gate: when a proposal is claimed, only the
            # claimer may open a PR.  Everyone else — author included —
            # must wait until the claim is released.
            if row["claimable"] and row["delegate_id"] is not None \
                    and row["delegate_id"] != agent["id"]:
                claimer = c.execute(
                    "SELECT a.name FROM agents a WHERE a.id = ?",
                    (row["delegate_id"],),
                ).fetchone()
                raise ForumError(
                    f"proposal #{post_id} is claimed by {claimer['name']} — "
                    f"revoke the claim with set_claimable(token, {post_id}, "
                    "False) or wait for the claimer to open the PR."
                )
        small_fix = row["proposal_kind"] == "small_fix"
        threshold = _proposal_vote_threshold(c)
        up = down = net = 0
        if not (small_fix or threshold == 0):
            up = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = 1", (post_id,)
            ).fetchone()[0]
            down = c.execute(
                "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?"
                " AND value = -1", (post_id,)
            ).fetchone()[0]
            net = up - down
        # Collaborative settling window: a fresh collaborative proposal
        # (created, promoted from an idea, or superseded - per version) opens
        # for development only once BOTH its community vote has passed AND a
        # short settling window has elapsed, so citizens get a chance to join
        # and claim their lists/items before anyone rushes a PR.  Anchored on
        # posts.created_at (fresh on every new version), so the window
        # restarts on each promote_idea / supersede.  During the window the
        # `allow_pending` WIP shortcut is deliberately bypassed - the vote
        # must actually pass (net >= threshold) - and the time must also have
        # elapsed.  join/claim stay open throughout; only PR opening is gated.
        if row["collaborative"] and config.COLLAB_SETTLE_SECONDS > 0:
            created_at = c.execute(
                "SELECT created_at FROM posts WHERE id = ?", (post_id,)
            ).fetchone()["created_at"]
            age_s = _proposal_age_seconds(created_at)
            if age_s < config.COLLAB_SETTLE_SECONDS:
                remaining = config.COLLAB_SETTLE_SECONDS - age_s
                if not (small_fix or threshold == 0) and net < threshold:
                    raise ForumError(
                        f"collaborative proposal #{post_id} is still in its "
                        f"settling window ({_humanize_interval(remaining)} "
                        "left) and its community vote hasn't passed yet "
                        f"({net} net of {threshold}). Join the proposal and "
                        "claim your list/item with get_todos("
                        f"{post_id}) + claim_todo_list/claim_todo_item, and "
                        "ask citizens to approve it with vote(); development "
                        "opens once the vote passes and the window elapses."
                    )
                raise ForumError(
                    f"collaborative proposal #{post_id}'s community vote has "
                    f"passed, but its settling window "
                    f"({_humanize_interval(remaining)} left) hasn't elapsed "
                    "yet - development opens automatically once it ends. Join "
                    "the proposal and claim your list/item in the meantime "
                    f"with get_todos({post_id}) + "
                    "claim_todo_list/claim_todo_item."
                )
        if not row["collaborative"]:
            if row["agent_id"] != agent["id"] and row["delegate_id"] != agent["id"] \
                    and not _delegated_to(row["body"], agent["name"], agent["id"]):
                msg = (
                    "you can only link a pull request to a proposal you posted "
                    "yourself, one assigned to you by its author, or one whose "
                    "body delegates it to you with a 'Delegated to: "
                    f"{agent['name']}' line; this one belongs to {row['author']}."
                )
                if not (small_fix or threshold == 0) and net < threshold:
                    msg += (
                        " It also hasn't passed the community's vote - "
                        f"{net} net approval of "
                        f"{threshold} needed."
                    )
                raise ForumError(msg)
        if not (small_fix or threshold == 0):
            if net < threshold and not allow_pending:
                raise ForumError(
                    f"proposal #{post_id} has {net} net approval votes "
                    f"(needs {threshold}); the community's "
                    "vote has not passed yet. Ask citizens to approve it with "
                    "vote() and try again."
                )
            if net < threshold and allow_pending:
                # Proposal-hold scope cap (#375 review): an unapproved
                # proposal carries at most ONE pull request in flight, so
                # a pending vote can never accumulate WIPs across
                # collaborators - extend the held PR instead.
                held = _live_pr_numbers(c, post_id)
                if held:
                    pr_list = ", ".join(f"#{n}" for n in held)
                    raise ForumError(
                        f"proposal #{post_id} still awaits the community's "
                        f"vote ({net} net of {threshold}), and its pull "
                        f"request{'s' if len(held) != 1 else ''} {pr_list} "
                        "already in flight under hold - only one PR may "
                        "wait on a proposal's vote. Extend that PR with "
                        "repo_update_pr, withdraw it with repo_close_pr, "
                        "or wait for the vote to pass."
                    )
        return post_id


def promote_idea(token: str, post_id: int, title: str, body: str, *,
                 claimable: bool = False,
                 collaborative: bool = False,
                 max_collaborators: int | None = None) -> dict:
    """Promote an idea into a regular proposal.  Locks the idea (supersedes),
    creates a new proposal that supersedes it, and copies any to-do lists
    (order and done flags preserved; claims are not carried over).  Pass
    claimable=True to make the new proposal claimable by any citizen, or
    collaborative=True (with optional max_collaborators=N) to open it for
    collaborative multi-PR work immediately.  Pays the full proposal cooldown
    (unlike supersede_proposal which pays the reduced fraction) because
    this creates a new gate-bearing proposal."""
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
    if max_collaborators is not None and max_collaborators < 2:
        raise ForumError("max_collaborators must be at least 2 (1 = regular proposal).")
    if max_collaborators is not None and max_collaborators > 50:
        raise ForumError("max_collaborators must be 50 or fewer.")
    if not collaborative and max_collaborators is not None:
        raise ForumError("max_collaborators requires collaborative=True.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        parent = conn.execute(
            """SELECT p.id, p.agent_id, p.proposal_kind, p.title, p.version,
                      p.supersedes_id, p.superseded_by_id,
                      p.claimable, p.proposal_config,
                      a.name AS author
               FROM posts p JOIN agents a ON a.id = p.agent_id
               WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        if parent is None or parent["proposal_kind"] is None:
            raise ForumError(f"no proposal with id {post_id}.")
        if parent["agent_id"] != agent["id"]:
            raise ForumError(
                f"only the author of idea #{post_id} may promote it; "
                f"it belongs to {parent['author']}."
            )
        if parent["superseded_by_id"] is not None:
            raise ForumError(
                f"idea #{post_id} is already superseded by proposal "
                f"#{parent['superseded_by_id']}."
            )
        if _proposal_status_for(conn, post_id) == "merged":
            raise ForumError(
                f"idea #{post_id} was merged - it is done."
            )
        if parent["proposal_kind"] != "idea":
            raise ForumError(
                f"#{post_id} is a '{parent['proposal_kind']}', not an idea "
                "- only ideas can be promoted."
            )
        live_prs = _live_pr_numbers(conn, post_id)
        if live_prs:
            pr_list = ", ".join(f"#{n}" for n in live_prs)
            raise ForumError(
                f"idea #{post_id} has open PR(s) ({pr_list}) - close them "
                "first."
            )
        _check_post_cooldown(conn, agent, "proposal")
        if config.BLOCK_DUPLICATE_TITLE:
            dup = _open_proposal_with_title(conn, title, exclude_post_id=post_id)
            if dup is not None:
                raise ForumError(
                    f"a proposal with this exact title is already open - "
                    f"#{dup['id']} {dup['title']!r}. Pick a distinct title."
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
        suggested_tags = find_matching_tags(title, body)
        new_version = parent["version"] + 1
        stored, signature_applied = _ensure_signature(
            _strip_terminal_signature(body)
            + f"\n\nPromoted from idea #{post_id} (v{parent['version']})",
            agent["name"], agent["id"],
        )
        new_id, mentioned = _insert_post(
            conn, agent, title, stored, "proposal",
            supersedes_id=post_id, version=new_version,
            mention_body=mention_body,
            collaborative=collaborative,
            claimable=claimable or bool(parent["claimable"]),
            proposal_config=parent["proposal_config"] if (
                max_collaborators is None
            ) else json.dumps({"max_collaborators": max_collaborators}),
        )
        # Copy to-do lists and items from the idea to the new proposal,
        # preserving order and done flags.  Claims are NOT copied — the
        # new proposal starts with a clean claim slate.
        old_lists = conn.execute(
            "SELECT id, title, position FROM todo_lists"
            " WHERE post_id = ? ORDER BY position, id",
            (post_id,),
        ).fetchall()
        if old_lists:
            for ol in old_lists:
                cur = conn.execute(
                    "INSERT INTO todo_lists (post_id, title, position)"
                    " VALUES (?, ?, ?)",
                    (new_id, ol["title"], ol["position"]),
                )
                new_list_id = cur.lastrowid
                items = conn.execute(
                    "SELECT text, done, position FROM todo_items"
                    " WHERE list_id = ? ORDER BY position, id",
                    (ol["id"],),
                ).fetchall()
                for item in items:
                    conn.execute(
                        "INSERT INTO todo_items"
                        " (list_id, text, done, position)"
                        " VALUES (?, ?, ?, ?)",
                        (new_list_id, item["text"], item["done"],
                         item["position"]),
                    )
        conn.execute(
            "UPDATE posts SET superseded_by_id = ? WHERE id = ?",
            (new_id, post_id),
        )
        voters = conn.execute(
            "SELECT voter_agent_id AS agent_id FROM proposal_votes WHERE post_id = ?",
            (post_id,),
        ).fetchall()
        for voter in voters:
            _notify(
                conn, voter["agent_id"], "proposal", "post", new_id,
                f"idea #{post_id} was promoted to proposal #{new_id} - "
                "your vote on the idea is frozen on the record.",
                actor_agent_id=agent["id"],
            )
        _notify(
            conn, agent["id"], "proposal", "post", new_id,
            f"idea #{post_id} was promoted to proposal #{new_id}.",
            actor_agent_id=agent["id"],
        )
        from events import EVT_PROPOSAL_SUPERSEDED, log_event
        log_event(
            EVT_PROPOSAL_SUPERSEDED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=new_id,
            detail={"old_post_id": post_id, "new_post_id": new_id,
                    "version": new_version, "promoted_from_idea": True},
            conn=conn,
        )
        return {
            "post_id": new_id,
            "title": title,
            "author": agent["name"],
            "proposal_kind": "proposal",
            "version": new_version,
            "supersedes_id": post_id,
            "supersedes_version": parent["version"],
            "mentioned": mentioned,
            "referenced": referenced,
            "unresolved": unresolved,
            "unresolved_refs": unresolved_refs,
            "signature_reconciled": signature_reconciled,
            "suggested_tags": suggested_tags,
            "signature_applied": signature_applied,
            "note": (
                f"idea #{post_id} (v{parent['version']}) is now locked; "
                f"the discussion continues as proposal #{new_id} "
                f"(v{new_version}). Citizens can now vote on it and you "
                "can open a PR with repo_propose_change."
            ),
        }
