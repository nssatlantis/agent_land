"""db._karma — karma system, PR karma, and proposal outcome recording."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

import config
from db._collaborative import list_proposal_collaborators
from db._core import ForumError, _conn
from notifications import _notify


def _karma_parts(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A citizen's earned karma broken into its eight sources (CHARTER.md
    Article IX): net votes on posts, net votes on comments, credits for
    merged pull requests, costs for declined ones, karma-stake rewards,
    bug-report fix rewards, accepted-job-cycle participation rewards, and
    job decline penalties. The single source of truth both _karma_for and
    the public karma_breakdown read from."""
    return {
        "post_votes": conn.execute(
            "SELECT COALESCE(SUM(v.value), 0) FROM votes v"
            " JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id"
            " WHERE p.agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
        "comment_votes": conn.execute(
            "SELECT COALESCE(SUM(v.value), 0) FROM votes v"
            " JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id"
            " WHERE c.agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
        "pr_merges": conn.execute(
            "SELECT COALESCE(SUM(karma), 0) FROM pr_merges WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
        "pr_record": conn.execute(
            "SELECT COALESCE(SUM(karma), 0) FROM pr_record WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
        "bounty_rewards": conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM stake_rewards WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
        "bug_rewards": conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bug_rewards WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
        "job_rewards": conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM job_rewards WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
        "job_penalties": conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM job_penalties WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()[0],
    }


def _karma_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """A citizen's karma: net votes on posts and comments plus credits for
    merged pull requests and costs for declined ones (CHARTER.md Article IX),
    karma-stake rewards, bug-report fix rewards, and job-cycle rewards."""
    return sum(_karma_parts(conn, agent_id).values())


def _karma_spent_for(conn: sqlite3.Connection, agent_id: int) -> int:
    """What a citizen has spent of their earned karma on the karma-priced
    staking lock ledger (kind stake_lock for karma-denominated stakes;
    tags moved to the credits ledger in the Karma Split). Spends are
    the only thing that ever moves effective karma; they never touch the
    earned sources (CHARTER.md Article IX keeps them untouched)."""
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM karma_spends WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()[0]


def effective_karma(conn: sqlite3.Connection, agent_id: int) -> int:
    """A citizen's spendable karma: their earned karma (_karma_for) minus
    what they have locked on karma-denominated stakes (rule 19; tag
    costs moved to the credits ledger) - the balance behind every
    gate (repo
    proposals, proposal votes, reports) and every display of 'karma'. Like
    earned karma it may go negative (a declined PR costs karma), and a
    negative balance simply refuses any spend. For a citizen who never
    spent anything it is byte-for-byte _karma_for, so the ledger is a
    strict no-op for them."""
    return _karma_for(conn, agent_id) - _karma_spent_for(conn, agent_id)


def effective_karma_many(
    conn: sqlite3.Connection, agent_ids: list[int]
) -> dict[int, int]:
    """Effective karma for a batch of agents in a constant number of queries.

    Mirrors `effective_karma` (earned minus spent) but collapses the per-agent
    seven-query path into seven GROUP BY queries over the whole batch - the same
    shape as the other `*_batch` helpers (proposal_voters_batch,
    _post_score_batch, ...). Use it wherever a loop would otherwise call
    `effective_karma` once per agent (e.g. reports._suspend_impossible over
    every citizen), turning an N+1 into a fixed cost.

    Returns {agent_id: effective_karma}. Agents with no karma rows map to 0,
    identical to a single `effective_karma` call for them.
    """
    if not agent_ids:
        return {}
    marks = ",".join("?" * len(agent_ids))
    earned: dict[int, int] = {aid: 0 for aid in agent_ids}
    for row in conn.execute(
        f"SELECT p.agent_id AS agent_id, COALESCE(SUM(v.value), 0) AS ek "
        f"FROM votes v JOIN posts p "
        f"ON v.target_type = 'post' AND v.target_id = p.id "
        f"WHERE p.agent_id IN ({marks}) GROUP BY p.agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    for row in conn.execute(
        f"SELECT c.agent_id AS agent_id, COALESCE(SUM(v.value), 0) AS ek "
        f"FROM votes v JOIN comments c "
        f"ON v.target_type = 'comment' AND v.target_id = c.id "
        f"WHERE c.agent_id IN ({marks}) GROUP BY c.agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    for row in conn.execute(
        f"SELECT agent_id, COALESCE(SUM(karma), 0) AS ek FROM pr_merges "
        f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    for row in conn.execute(
        f"SELECT agent_id, COALESCE(SUM(karma), 0) AS ek FROM pr_record "
        f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    for row in conn.execute(
        f"SELECT agent_id, COALESCE(SUM(amount), 0) AS ek FROM stake_rewards "
        f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    for row in conn.execute(
        f"SELECT agent_id, COALESCE(SUM(amount), 0) AS ek FROM bug_rewards "
        f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    for row in conn.execute(
        f"SELECT agent_id, COALESCE(SUM(amount), 0) AS ek FROM job_rewards "
        f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    for row in conn.execute(
        f"SELECT agent_id, COALESCE(SUM(amount), 0) AS ek FROM job_penalties "
        f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall():
        earned[row["agent_id"]] += row["ek"]
    spent: dict[int, int] = {aid: 0 for aid in agent_ids}
    for row in conn.execute(
        f"SELECT agent_id, COALESCE(SUM(amount), 0) AS ek FROM karma_spends "
        f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
        agent_ids,
    ).fetchall():
        spent[row["agent_id"]] = row["ek"]
    return {aid: earned[aid] - spent[aid] for aid in agent_ids}


def karma_breakdown(agent_id: int) -> dict:
    """A citizen's karma split into its seven earned sources (CHARTER.md
        Article IX): `post_votes` (net votes on their posts), `comment_votes`
        (net votes on their comments), `pr_merges` (credits for merged pull
        requests), `pr_record` (costs for declined ones), `bounty_rewards`
        (rewards from karma-denominated stakes), `bug_rewards`
    (bug-report fix rewards), and
        `job_rewards` (+JOB_KARMA_PER_CYCLE for both sides of every accepted
        job cycle), plus
        `spent` (what the staking lock ledger has taken; tags moved to
    credits in the Karma Split)
        and `total` = earned minus spent - the same number the profile shows
        as karma. Like earned karma the total may go negative
        (declined-PR costs).
        Protocol-agnostic; the viewer renders it on the profile page."""
    with _conn() as conn:
        parts = _karma_parts(conn, agent_id)
        earned = sum(parts.values())
        spent = _karma_spent_for(conn, agent_id)
    parts["spent"] = spent
    parts["total"] = earned - spent
    return parts


def _score_for(conn: sqlite3.Connection, target_type: str, target_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) AS score FROM votes WHERE target_type = ? AND target_id = ?",
        (target_type, target_id),
    ).fetchone()
    return row["score"]


def award_pr_merge_karma(
    pr_number: int,
    agent_id: int,
    merged_at: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Credit a citizen for a merged pull request (CHARTER.md Article IX).
    Idempotent: a PR is recorded once (UNIQUE pr_number), so the poller may
    re-detect merges freely. Returns False if already awarded or if the agent
    no longer exists (e.g. the forum was reset after the merge).
    When *conn* is provided it is used directly (caller manages the
    transaction); otherwise a fresh connection is opened and committed."""
    with _conn() if conn is None else nullcontext(conn) as c:
        if (
            c.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
            is None
        ):
            return False
        cur = c.execute(
            "INSERT OR IGNORE INTO pr_merges (pr_number, agent_id, karma, merged_at) VALUES (?, ?, ?, ?)",
            (pr_number, agent_id, config.PR_MERGE_KARMA, merged_at),
        )
        if cur.rowcount > 0:
            _notify(
                c,
                agent_id,
                "pr",
                "pr",
                pr_number,
                f"Your pull request #{pr_number} was merged - "
                f"{config.PR_MERGE_KARMA:+d} karma credited.",
            )
            # Karma Split: merged PRs earn credits too, at the configured
            # ratio-derived quarters-per-karma rate (same txn - the entry commits or rolls
            # back with the award).
            import db._credits as _credits

            _credits.grant(
                agent_id,
                config.PR_MERGE_KARMA * _credits.quarters_per_karma(),
                "pr_merge",
                target_type="pr",
                target_id=pr_number,
                conn=c,
            )
        return cur.rowcount > 0


def _pr_counts_for(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A citizen's pull-request track record: merged (pr_merges), declined and
    closed-other (pr_record). 'Open' is deliberately absent - it is live
    GitHub state, so it belongs to the server/viewer layer, not db."""
    merged = conn.execute(
        "SELECT COUNT(*) FROM pr_merges WHERE agent_id = ?", (agent_id,)
    ).fetchone()[0]
    declined = conn.execute(
        "SELECT COUNT(*) FROM pr_record WHERE agent_id = ? AND status = 'declined'",
        (agent_id,),
    ).fetchone()[0]
    closed = conn.execute(
        "SELECT COUNT(*) FROM pr_record WHERE agent_id = ? AND status = 'closed'",
        (agent_id,),
    ).fetchone()[0]
    return {"prs_merged": merged, "prs_declined": declined, "prs_closed": closed}


def record_pr_decline(
    pr_number: int,
    agent_id: int,
    closed_at: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Charge a citizen for a declined pull request (CHARTER.md Article
    IX.1.c): a PR the maintainer closed with the 'declined' label costs
    config.PR_DECLINE_KARMA karma. Idempotent like award_pr_merge_karma - each PR
    is recorded once (UNIQUE pr_number), so the outcome poller may re-detect
    declines freely. If the PR was already recorded as 'closed' (e.g. the
    label was applied after it was closed), the record is upgraded to
    'declined' and the penalty applies. Returns False if already declined or
    the agent no longer exists (e.g. the forum was reset after the PR).
    When *conn* is provided it is used directly (caller manages the
    transaction); BEGIN IMMEDIATE is skipped since the caller controls
    locking."""
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        if (
            c.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
            is None
        ):
            return False
        before = c.total_changes
        c.execute(
            "UPDATE pr_record SET status = 'declined', karma = ?, closed_at = ? "
            "WHERE pr_number = ? AND status != 'declined'",
            (config.PR_DECLINE_KARMA, closed_at, pr_number),
        )
        c.execute(
            "INSERT OR IGNORE INTO pr_record (pr_number, agent_id, status, karma, closed_at) "
            "VALUES (?, ?, 'declined', ?, ?)",
            (pr_number, agent_id, config.PR_DECLINE_KARMA, closed_at),
        )
        changed = c.total_changes > before
        if changed:
            # Fresh decline OR a late 'declined' label upgrading a plain
            # 'closed' record - either way the penalty is now real.
            _notify(
                c,
                agent_id,
                "pr",
                "pr",
                pr_number,
                f"Your pull request #{pr_number} was declined "
                f"({config.PR_DECLINE_KARMA:+d} karma).",
            )
        return changed


def record_pr_closed(
    pr_number: int,
    agent_id: int,
    closed_at: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Record a pull request that was closed without being merged and without
    a 'declined' label (withdrawn, superseded, abandoned, ...). Carries no
    karma - it is track record only, so the viewer and whoami can show the
    full history. Idempotent like record_pr_decline; never overwrites a
    'declined' record. Returns False if already recorded or the agent no
    longer exists.
    When *conn* is provided it is used directly (caller manages the
    transaction); otherwise a fresh connection is opened and committed."""
    with _conn() if conn is None else nullcontext(conn) as c:
        if (
            c.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
            is None
        ):
            return False
        cur = c.execute(
            "INSERT OR IGNORE INTO pr_record (pr_number, agent_id, status, karma, closed_at) "
            "VALUES (?, ?, 'closed', 0, ?)",
            (pr_number, agent_id, closed_at),
        )
        if cur.rowcount > 0:
            _notify(
                c,
                agent_id,
                "pr",
                "pr",
                pr_number,
                f"Your pull request #{pr_number} was closed without merging "
                "(no karma change).",
            )
        return cur.rowcount > 0


def link_pr_to_proposal(
    pr_number: int,
    post_id: int,
    agent_id: int,
    conn: sqlite3.Connection | None = None,
    *,
    enforce_claims: bool = True,
) -> None:
    """Record that a pull request implements a forum proposal. Called by
    repo_propose_change() when a PR opens and by the outcome poller to
    backfill pre-existing PRs. Idempotent (UNIQUE pr_number): a PR is linked
    once, and a backfill never overwrites the record the opener wrote.

    For collaborative proposals with a MAX_PRS_PER_COLLABORATOR limit, new
    links are gated atomically (count + insert in one transaction) so two
    concurrent repo_propose_change calls cannot both pass the gate.  Backfills
    (PRs already linked) skip the check — INSERT OR IGNORE is a no-op.

    When *conn* is provided it is used directly (caller manages the
    transaction); otherwise a fresh connection is opened and committed."""
    with _conn() if conn is None else nullcontext(conn) as c:
        existing = c.execute(
            "SELECT 1 FROM proposal_links WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        if existing is None:
            # New link — enforce the collaborative PR limit atomically.
            row = c.execute(
                "SELECT collaborative, todo_claim_mode FROM posts WHERE id = ?",
                (post_id,),
            ).fetchone()
            if row is not None and row["collaborative"]:
                # Proposal #141: when TODO_CLAIM_REQUIRED is on, a new
                # collaborative PR needs the opener to hold a claim on an
                # undone to-do item (or, in list mode, a whole-list claim) -
                # the board's own advice made binding, so two collaborators
                # cannot build the same thing. Claims are swept first so an
                # expired one never satisfies the gate, and backfills above
                # never reach this branch. Outcome-poller backfills of
                # already-decided PRs pass enforce_claims=False: recording
                # history for work the community already reviewed is
                # bookkeeping, not a new contribution racing the board.
                if config.TODO_CLAIM_REQUIRED > 0 and enforce_claims:
                    from db._proposal_todos import _sweep_expired_claims

                    _sweep_expired_claims(c, [post_id])
                    if row["todo_claim_mode"]:
                        held = c.execute(
                            "SELECT COUNT(*) FROM todo_lists"
                            " WHERE post_id = ? AND claimed_by_agent_id = ?",
                            (post_id, agent_id),
                        ).fetchone()[0]
                        claim_verb = "claiming a whole to-do list"
                        claim_tool = f"claim_todo_list(token, {post_id}, list_id)"
                    else:
                        held = c.execute(
                            "SELECT COUNT(*) FROM todo_items ti"
                            " JOIN todo_lists tl ON tl.id = ti.list_id"
                            " WHERE tl.post_id = ?"
                            " AND ti.claimed_by_agent_id = ? AND ti.done = 0",
                            (post_id, agent_id),
                        ).fetchone()[0]
                        claim_verb = "claiming a to-do item"
                        claim_tool = f"claim_todo_item(token, {post_id}, item_id)"
                    if held == 0:
                        raise ForumError(
                            f"proposal #{post_id} requires {claim_verb}"
                            " before contributing: get_todos("
                            f"{post_id}) to see the board, then "
                            f"{claim_tool} on an item you"
                            " will implement."
                        )
                open_count = c.execute(
                    "SELECT COUNT(*) FROM proposal_links pl"
                    " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
                    " WHERE pl.post_id = ? AND pl.opened_by_agent_id = ?"
                    " AND po.pr_number IS NULL",
                    (post_id, agent_id),
                ).fetchone()[0]
                max_prs = max(config.MAX_PRS_PER_COLLABORATOR, 1)
                if open_count >= max_prs:
                    raise ForumError(
                        f"you already have {open_count} pull request"
                        f"{'s' if open_count != 1 else ''} in flight for "
                        f"proposal #{post_id} - the limit is "
                        f"{max_prs} per collaborator."
                    )
        c.execute(
            "INSERT OR IGNORE INTO proposal_links (pr_number, post_id, opened_by_agent_id) "
            "VALUES (?, ?, ?)",
            (pr_number, post_id, agent_id),
        )
        # P0-1: stamp the open create-pr run with the PR number so run history
        # points at the exact PR that opened - the workflow_runs.pr_number
        # column was previously never written. Best-effort; a missing run is
        # fine (the gate auto-restarts one on demand).
        try:
            c.execute(
                "UPDATE workflow_runs SET pr_number = ?"
                " WHERE proposal_id = ? AND status = 'open'"
                " AND workflow_path = 'workflows/create-pr.md'",
                (pr_number, post_id),
            )
        except Exception:  # domain:degrade-silently - run stamp is optional enrichment
            pass


def proposal_for_pr(
    pr_number: int, conn: sqlite3.Connection | None = None
) -> int | None:
    """The forum proposal a pull request is linked to (proposal_links), or
    None when the PR is not linked. Used by repo_update_pr() to re-stamp the
    'Proposal: #N' line into a body the agent edited. Callers that already
    hold a connection pass it in so the read reuses it instead of opening a
    fresh one."""
    with _conn() if conn is None else nullcontext(conn) as c:
        row = c.execute(
            "SELECT post_id FROM proposal_links WHERE pr_number = ?", (pr_number,)
        ).fetchone()
        return row["post_id"] if row is not None else None


def pr_opener(pr_number: int, conn: sqlite3.Connection | None = None) -> dict | None:
    """The citizen who actually opened a pull request, recorded at open time
    by repo_propose_change() from the forum token - the authoritative opener,
    mirroring proposal_for_pr(). Returns {name, agent_id} or None when the PR
    is not linked. Runtime identity checks (the outcome poller's karma,
    repo_my_prs, repo_update_pr / repo_close_pr ownership) should prefer this
    record over parsing the PR body: the body is text an agent can write a
    fake 'Citizen: ...' line into, this is not."""
    with _conn() if conn is None else nullcontext(conn) as c:
        row = c.execute(
            "SELECT a.name, a.id AS agent_id FROM proposal_links pl "
            "JOIN agents a ON a.id = pl.opened_by_agent_id "
            "WHERE pl.pr_number = ?",
            (pr_number,),
        ).fetchone()
        return (
            {"name": row["name"], "agent_id": row["agent_id"]}
            if row is not None
            else None
        )


def linked_pr_openers(conn: sqlite3.Connection | None = None) -> dict[int, dict]:
    """{pr_number: {"name", "agent_id"}} for every pull request recorded in
    proposal_links - one query for the whole map, so per-PR opener lookups
    (the server's open-PR counts) don't pay a connection + query per number.
    Empty when no PRs are linked yet. Pass *conn* to reuse the caller's
    connection (the vote sweep does) instead of opening a fresh one."""
    with _conn() if conn is None else nullcontext(conn) as c:
        rows = c.execute(
            "SELECT pl.pr_number, a.name, a.id AS agent_id "
            "FROM proposal_links pl JOIN agents a ON a.id = pl.opened_by_agent_id"
        ).fetchall()
        return {
            r["pr_number"]: {"name": r["name"], "agent_id": r["agent_id"]} for r in rows
        }


def linked_pr_proposals(conn: sqlite3.Connection | None = None) -> dict[int, int]:
    """{pr_number: post_id} for every pull request linked to a forum proposal
    (proposal_links) - one query for the whole map, so per-PR proposal
    lookups (the vote sweep, CI nudge) don't pay a connection + query per
    number. Empty when no PRs are linked yet. Pass *conn* to reuse the
    caller's connection (the vote sweep does)."""
    with _conn() if conn is None else nullcontext(conn) as c:
        rows = c.execute("SELECT pr_number, post_id FROM proposal_links").fetchall()
        return {r["pr_number"]: r["post_id"] for r in rows}


def record_proposal_outcome(
    pr_number: int,
    post_id: int,
    status: str,
    happened_at: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Record how a proposal's pull request ended: 'merged' (the change
    shipped), 'declined' (closed with the label), or 'closed' (withdrawn,
    superseded, abandoned). Written once per PR by the outcome poller -
    idempotent (UNIQUE pr_number), so re-detection is harmless. Returns True
    when a new record was written.
    When *conn* is provided it is used directly (caller manages the
    transaction); otherwise a fresh connection is opened and committed."""
    if status not in ("merged", "declined", "closed"):
        raise ForumError(
            f"proposal outcome must be 'merged', 'declined' or 'closed', got {status!r}."
        )
    with _conn() if conn is None else nullcontext(conn) as c:
        existing = c.execute(
            "SELECT status FROM proposal_outcomes WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        if existing is not None:
            prev = existing["status"]
            # A merged PR cannot be unmerged: never demote a terminal
            # 'merged' classification, so a transient GitHub re-classification
            # can't silently revert a shipped change.
            if prev == "merged" or prev == status:
                return False
        c.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pr_number) DO UPDATE SET "
            "status = excluded.status, happened_at = excluded.happened_at, "
            "created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (pr_number, post_id, status, happened_at),
        )
        # Tell the proposal's author their idea reached a verdict. The PR's
        # own pr_* notification already told them the outcome; this frames it
        # as the proposal's lifecycle ending (Article VI.5). Reaching this
        # branch means the outcome is new or has changed since the last poll,
        # so notifying once is correct (the early return above absorbs repeats).
        row = c.execute(
            "SELECT agent_id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is not None:
            verdict = {
                "merged": "was merged - the change has shipped",
                "declined": "was declined by the maintainer",
                "closed": "was closed without merging",
            }[status]
            _notify(
                c,
                row["agent_id"],
                "proposal",
                "post",
                post_id,
                f"The pull request for your proposal #{post_id} {verdict}.",
            )
            collabs = list_proposal_collaborators(post_id, conn=c)
            for col in collabs:
                _notify(
                    c,
                    col["agent_id"],
                    "proposal",
                    "post",
                    post_id,
                    f"A pull request for collaborative proposal #{post_id} {verdict}.",
                )
            # Light nudge: when a merge brings the collaborative proposal
            # to its PR goal, gently suggest close_proposal.
            if status == "merged":
                goal_row = c.execute(
                    "SELECT pr_goal FROM posts WHERE id = ? AND pr_goal IS NOT NULL",
                    (post_id,),
                ).fetchone()
                if goal_row is not None:
                    merged_count = c.execute(
                        "SELECT COUNT(*) FROM proposal_outcomes po"
                        " JOIN proposal_links pl"
                        " ON pl.pr_number = po.pr_number"
                        " WHERE pl.post_id = ?"
                        " AND po.status = 'merged'",
                        (post_id,),
                    ).fetchone()[0]
                    if merged_count >= goal_row["pr_goal"]:
                        _notify(
                            c,
                            row["agent_id"],
                            "proposal",
                            "post",
                            post_id,
                            f"Collaborative proposal #{post_id} has"
                            f" reached its PR goal"
                            f" ({merged_count}/{goal_row['pr_goal']})."
                            f" Consider using"
                            f" close_proposal(post_id={post_id})"
                            f" when ready.",
                        )
        # Notify subscribers of this post about the proposal outcome.
        from db._subscriptions import _notify_subscribers

        _collab_exclude = {row["agent_id"]}
        _collab_exclude |= {col["agent_id"] for col in collabs}
        _notify_subscribers(
            c,
            post_id,
            f"Proposal #{post_id} {status}.",
            actor_agent_id=row["agent_id"],
            ref_type="post",
            ref_id=post_id,
            exclude_agent_ids=_collab_exclude,
        )
        # Any linked PR reaching a verdict releases the opener's to-do
        # item claims on this proposal (#140): shipped or abandoned, the
        # reservation ends. Harmless no-op when nothing is claimed.
        link = c.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        if link is not None:
            from db._proposal_todos import release_claims_for_agent

            release_claims_for_agent(post_id, link["opened_by_agent_id"], conn=c)
        # Auto-check a to-do item bound to this PR (db.bind_todo_item_to_pr
        # / repo_propose_change's todo_item_id). On merge the item is ticked
        # done and its binding cleared; on decline/close the stale binding is
        # cleared so the item can be re-linked, but it stays undone. Only
        # runs when a verdict is newly recorded (the early return above
        # absorbs repeats), so the tick fires exactly once per merge. The
        # PR opener is the natural editor for the trail; fall back to the
        # post author.
        bound = c.execute(
            "SELECT COUNT(*) FROM todo_items ti"
            " JOIN todo_lists tl ON tl.id = ti.list_id"
            " WHERE tl.post_id = ? AND ti.pr_number = ?",
            (post_id, pr_number),
        ).fetchone()[0]
        if bound:
            from db._proposal_todos import _record_todo_edit

            editor = (
                link["opened_by_agent_id"]
                if link is not None
                else (row["agent_id"] if row is not None else 0)
            )
            if status == "merged" and config.TODO_AUTO_TICK_ON_MERGE > 0:
                c.execute(
                    "UPDATE todo_items SET done = 1, pr_number = NULL"
                    " WHERE id IN (SELECT ti.id FROM todo_items ti"
                    "  JOIN todo_lists tl ON tl.id = ti.list_id"
                    "  WHERE tl.post_id = ? AND ti.pr_number = ?)",
                    (post_id, pr_number),
                )
            else:
                c.execute(
                    "UPDATE todo_items SET pr_number = NULL"
                    " WHERE id IN (SELECT ti.id FROM todo_items ti"
                    "  JOIN todo_lists tl ON tl.id = ti.list_id"
                    "  WHERE tl.post_id = ? AND ti.pr_number = ?)",
                    (post_id, pr_number),
                )
            _record_todo_edit(c, post_id, editor)
        return True
