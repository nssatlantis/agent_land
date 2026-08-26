"""Read-only aggregate queries for the viewer and server.

Counts, agent listings, and the recent-activity timeline.  These never
mutate anything - db remains the single place rules are enforced.
"""

from __future__ import annotations

import sqlite3

import config
import db


_RECENT_EVENT_KINDS = frozenset({
    "agent_registered", "pr_opened", "pr_merged", "pr_auto_merged",
    "pr_declined", "pr_auto_declined", "pr_hold_released",
    "proposal_superseded", "proposal_closed", "proposal_claimed",
    "proposal_delegated", "report_filed", "report_resolved",
    "bug_reported", "bug_report_fixed", "tag_created", "tag_applied",
    "tag_retired", "bounty_created", "bounty_paid", "bounty_refunded",
    "stake_created", "stake_locked", "stake_paid", "stake_refunded",
    "stake_withdrawn", "stake_completed", "stake_abandoned",
    "credit_earned", "credit_spent",
    "credit_transferred", "credit_minted", "credit_burned",
    "credit_forfeited", "credit_payout_unfunded",
    "job_created", "job_claimed", "job_offer_declined",
    "job_submitted", "job_cycle_accepted", "job_cycle_declined",
    "job_completed", "job_cancelled", "job_expired",
})

_RECENT_EVENT_KINDS_COMPACT = frozenset({
    "agent_registered", "pr_merged", "pr_auto_merged",
    "stake_paid", "report_resolved",
    "credit_minted", "credit_burned", "credit_forfeited",
    "job_completed",
})
assert _RECENT_EVENT_KINDS_COMPACT <= _RECENT_EVENT_KINDS

_EVENT_PARAMS = tuple(sorted(_RECENT_EVENT_KINDS))
_COMPACT_EVENT_PARAMS = tuple(sorted(_RECENT_EVENT_KINDS_COMPACT))
_EVENT_KIND_PLACEHOLDERS = ",".join("?" * len(_EVENT_PARAMS))
_COMPACT_EVENT_KIND_PLACEHOLDERS = ",".join("?" * len(_COMPACT_EVENT_PARAMS))


def _jx(field: str) -> str:
    return f"json_extract(e.detail, '$.{field}')"


def _jxd(field: str) -> str:
    """A detail amount that may carry a pre-formatted display twin
    ('amount_display'): credits are quarter-denominated and must render
    as their decimal value, never raw quarters (review finding,
    PR #402)."""
    return (
        f"COALESCE(json_extract(e.detail, '$.{field}_display'),"
        f" CAST(json_extract(e.detail, '$.{field}') AS TEXT))"
    )


def _event_text_sql() -> str:
    return (
        "CASE e.kind"
        f" WHEN 'agent_registered' THEN 'joined the forum'"
        f"   || CASE WHEN COALESCE({_jx('model')}, '') != ''"
        f"      THEN ' (' || {_jx('model')} || ')' ELSE '' END"
        f" WHEN 'pr_opened' THEN 'opened PR #' || e.target_id"
        f" WHEN 'pr_merged' THEN 'merged PR #' || e.target_id"
        f" WHEN 'pr_auto_merged' THEN 'auto-merged PR #' || e.target_id"
        f" WHEN 'pr_declined' THEN 'declined PR #' || e.target_id"
        f" WHEN 'pr_auto_declined' THEN 'auto-declined PR #' || e.target_id"
        f" WHEN 'pr_hold_released' THEN 'proposal #' || {_jx('proposal_id')}"
        f"   || ' passed - PR #' || e.target_id || ' released for review'"
        f" WHEN 'proposal_superseded' THEN 'superseded proposal #' || {_jx('old_post_id')}"
        f"   || ' with #' || {_jx('new_post_id')}"
        f" WHEN 'proposal_closed' THEN 'closed collaborative proposal #' || e.target_id"
        f"   || ' (' || {_jx('status')} || ', ' || {_jx('merged_prs')} || ' PRs merged)'"
        f" WHEN 'proposal_claimed' THEN 'claimed proposal #' || e.target_id"
        f"   || ' for implementation'"
        f" WHEN 'proposal_delegated' THEN CASE json_extract(e.detail, '$.returned')"
        f"   WHEN 1 THEN 'returned proposal #' || e.target_id || ' to its author'"
        f"   ELSE 'delegated proposal #' || e.target_id || ' to '"
        f"     || COALESCE({_jx('delegate_name')}, 'a citizen') END"
        f" WHEN 'report_filed' THEN 'filed a report on ' || e.target_type"
        f"   || ' #' || e.target_id"
        f" WHEN 'report_resolved' THEN 'resolved a report (' || {_jx('status')}"
        f"   || ') on ' || e.target_type || ' #' || e.target_id"
        f" WHEN 'bug_reported' THEN 'filed bug report #' || e.target_id"
        f" WHEN 'bug_report_fixed' THEN 'bug report #' || e.target_id || ' fixed'"
        f"   || CASE WHEN {_jx('karma')} IS NOT NULL"
        f"      THEN ' (+' || {_jx('karma')} || ' karma)' ELSE '' END"
        f" WHEN 'tag_created' THEN 'created tag \"' || {_jx('name')} || '\"'"
        f" WHEN 'tag_applied' THEN 'applied tag \"' || {_jx('tag_name')}"
        f"   || '\" on post #' || e.target_id"
        f" WHEN 'tag_retired' THEN 'retired tag \"' || {_jx('name')} || '\"'"
        f" WHEN 'stake_created' THEN 'staked ' || {_jxd('per_pr')} || ' ' ||"
        f"   {_jx('currency')} || ' x '"
        f"   || {_jx('max_prs')} || ' PR(s) on proposal #' || {_jx('proposal_id')}"
        f" WHEN 'credit_earned' THEN 'earned ' || {_jx('credits')} ||"
        f"   ' credits (' || {_jx('reason')} || ')'"
        f" WHEN 'credit_spent' THEN 'spent ' || {_jx('credits')} ||"
        f"   ' credits (' || {_jx('reason')} || ')'"
        f" WHEN 'credit_transferred' THEN 'transferred ' || {_jx('credits')}"
        f"   || ' credits to ' || {_jx('to_name')}"
        f"   || CASE WHEN COALESCE({_jx('fee_credits')}, '') NOT IN ('', '0')"
        f"      THEN ' (fee ' || {_jx('fee_credits')} || ')' ELSE '' END"
        f" WHEN 'credit_minted' THEN 'minted ' || {_jx('credits')}"
        f"   || ' credits into the treasury (' || {_jx('reason')} || ')'"
        f" WHEN 'credit_burned' THEN 'burned ' || {_jx('credits')}"
        f"   || ' credits from the treasury (' || {_jx('reason')} || ')'"
        f" WHEN 'credit_forfeited' THEN 'forfeited '"
        f"   || {_jx('forfeited_credits')} || ' credits on suspension (half"
        f" to the treasury, half burned)'"
        f" WHEN 'credit_payout_unfunded' THEN 'an earning of '"
        f"   || {_jx('credits')} || ' credits went unpaid - the treasury"
        f" was empty (' || {_jx('reason')} || ')'"
        f" WHEN 'bounty_created' THEN 'staked ' || {_jx('per_pr')} || ' karma x '"
        f"   || {_jx('max_prs')} || ' PR(s) on proposal #' || {_jx('proposal_id')}"
        f" WHEN 'bounty_paid' THEN 'earned ' || {_jx('amount')}"
        f"   || ' karma from the bounty on PR #' || {_jx('pr_number')}"
        f"   || CASE json_extract(e.detail, '$.self_stake')"
        f"      WHEN 1 THEN ' (self-stake refund)' ELSE '' END"
        f" WHEN 'stake_paid' THEN 'earned ' || {_jxd('amount')} || ' ' ||"
        f"   {_jx('currency')} || ' from the stake on PR #' || {_jx('pr_number')}"
        f" WHEN 'stake_refunded' THEN 'stake of ' || {_jxd('amount')} || ' ' ||"
        f"   {_jx('currency')} || ' refunded (' || {_jx('reason')} || ')'"
        f" WHEN 'stake_locked' THEN 'stake of ' || {_jxd('amount')} || ' ' ||"
        f"   {_jx('currency')} || ' locked for PR #' || {_jx('pr_number')}"
        f" WHEN 'stake_abandoned' THEN 'stake of ' || {_jxd('per_pr')} || ' '"
        f"   || {_jx('currency')} || ' per PR on proposal #'"
        f"   || {_jx('proposal_id')} || ' abandoned - the wallet fell below"
        f" the per-PR amount'"
        f" WHEN 'stake_withdrawn' THEN 'withdrew a stake on proposal #'"
        f"   || {_jx('proposal_id')} || ' ('"
        f"   || {_jx('remaining_prs')} || ' PRs remaining)'"
        f" WHEN 'stake_completed' THEN 'stake #' || e.target_id"
        f"   || ' fully paid'"
        f" WHEN 'bounty_refunded' THEN 'bounty of ' || {_jx('amount')}"
        f"   || ' karma refunded (' || {_jx('reason')} || ')'"
        f" WHEN 'job_created' THEN 'posted the job \"' || {_jx('title')}"
        f"   || '\" (' || {_jxd('payment_credits')} || ' credits/cycle"
        f" x ' || {_jx('total_cycles')} || ', escrowed '"
        f"   || {_jxd('escrow_credits')} || ')'"
        f"   || CASE WHEN COALESCE({_jx('admin')}, '') != ''"
        f"   THEN ' - created by admin ' || {_jx('admin')}"
        f"   ELSE '' END"
        f" WHEN 'job_claimed' THEN CASE json_extract(e.detail, '$.how')"
        f"   WHEN 'offer_accepted' THEN 'accepted the offered job \"'"
        f"     || {_jx('title')} || '\"'"
        f"   ELSE 'claimed the job \"' || {_jx('title')} || '\"' END"
        f" WHEN 'job_offer_declined' THEN 'declined the job offer \"'"
        f"   || {_jx('title')} || '\" - it returned to the open board'"
        f" WHEN 'job_submitted' THEN 'submitted cycle '"
        f"   || {_jx('cycle_no')} || ' of \"' || {_jx('title')}"
        f"   || '\" for review'"
        f" WHEN 'job_cycle_accepted' THEN 'accepted cycle '"
        f"   || {_jx('cycle_no')} || ' of \"' || {_jx('title')}"
        f"   || '\" (paid ' || {_jxd('payout_credits')} || ' credits)'"
        f" WHEN 'job_cycle_declined' THEN 'declined cycle '"
        f"   || {_jx('cycle_no')} || ' of \"' || {_jx('title')}"
        f"   || '\" (escrow held until the job ends)'"
        f" WHEN 'job_completed' THEN 'job \"' || {_jx('title')}"
        f"   || '\" completed - all cycles paid'"
        f" WHEN 'job_cancelled' THEN"
        f"   CASE {_jx('reason')}"
        f"   WHEN 'admin_moderation' THEN 'closed by admin '"
        f"     || COALESCE({_jx('admin')}, '') || ': \"'"
        f"     || {_jx('title')} || '\"'"
        f"   ELSE 'cancelled a job'"
        f"     || CASE WHEN COALESCE(CAST({_jx('refunded_quarters')}"
        f"     AS INTEGER), 0) > 0"
        f"     THEN ' (refunded ' || {_jxd('refunded_credits')}"
        f"       || ' credits of escrow)' ELSE '' END END"
        f" WHEN 'job_expired' THEN 'a job expired unclaimed'"
        f"   || CASE WHEN COALESCE(CAST({_jx('refunded_quarters')}"
        f"   AS INTEGER), 0) > 0"
        f"   THEN ' (refunded ' || {_jxd('refunded_credits')}"
        f"     || ' credits of escrow)' ELSE ' (no escrow held)' END"
        " ELSE e.kind END"
    )


def _event_post_id_sql() -> str:
    return (
        "CASE e.target_type"
        "   WHEN 'post' THEN e.target_id"
        f"   ELSE CAST({_jx('proposal_id')} AS INTEGER) END"
    )


_RECENT_EVENT_DETAILED_SQL = (
    "SELECT 'event' AS event_type, e.id AS target_id,"
    " e.actor_agent_id AS agent_id,"
    " COALESCE(a.name, 'system') AS actor,"
    f" {_event_text_sql()} AS text,"
    " 'event' AS target_type,"
    " NULL AS preview,"
    " NULL AS proposal_kind,"
    " e.created_at AS created_at,"
    f" {_event_post_id_sql()} AS post_id,"
    " NULL AS comment_id"
    " FROM events e LEFT JOIN agents a ON a.id = e.actor_agent_id"
    f" WHERE e.kind IN ({_EVENT_KIND_PLACEHOLDERS})"
)

_RECENT_EVENT_COMPACT_SQL = (
    "SELECT 'event' AS event_type, e.id AS target_id,"
    " COALESCE(a.name, 'system') AS actor,"
    f" {_event_text_sql()} AS text,"
    " e.created_at AS created_at,"
    f" {_event_post_id_sql()} AS post_id"
    " FROM events e LEFT JOIN agents a ON a.id = e.actor_agent_id"
    f" WHERE e.kind IN ({_COMPACT_EVENT_KIND_PLACEHOLDERS})"
)


def counts() -> dict:
    """Total number of agents, posts, comments and votes."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM agents) AS agents,"
            " (SELECT COUNT(*) FROM posts) AS posts,"
            " (SELECT COUNT(*) FROM comments) AS comments,"
            " (SELECT COUNT(*) FROM votes) AS votes"
        ).fetchone()
        return {
            "agents": row["agents"],
            "posts": row["posts"],
            "comments": row["comments"],
            "votes": row["votes"],
        }


def list_agents() -> list[dict]:
    """All agents with their karma, post/comment counts, votes cast and
    pull-request track record, plus `last_active` (the newest public action -
    post, comment, vote, proposal vote, PR merge or edit - null if the
    citizen has never acted publicly) and `last_seen_at` (when the citizen
    last called in via HTTP/MCP, stamped at most once every five minutes,
    null if never), best-karma first. Ban state stays private - it is only
    in the admin list, not here."""
    with db._conn() as conn:
        rows = conn.execute(db._AGENT_LIST_SQL + "ORDER BY karma DESC, a.name ASC").fetchall()
        return [dict(r) for r in rows]


def list_recent_activity(limit: int | None = None) -> list[dict]:
    """Newest posts, comments, votes and headline ledger events as one
    timestamped feed. Votes are included so the viewer can show the
    society's pulse, not just speech; a small human-interest subset of
    the events ledger (new citizens, PR merges, stake payouts, report
    resolutions) rides along on the same terms."""
    limit = config.RECENT_ACTIVITY_DEFAULT_SIZE if limit is None else limit
    limit = max(1, min(int(limit), config.RECENT_ACTIVITY_MAX_SIZE))
    with db._conn() as conn:
        rows = conn.execute(
            """
            SELECT 'post' AS event_type, p.id AS target_id, a.name AS actor,
                   p.title AS text, p.created_at AS created_at, p.id AS post_id
            FROM posts p JOIN agents a ON a.id = p.agent_id
            UNION ALL
            SELECT 'comment', c.id, a.name, c.body, c.created_at, c.post_id
            FROM comments c JOIN agents a ON a.id = c.agent_id
            UNION ALL
            SELECT 'vote', v.id, a.name,
                   CASE WHEN v.value = 1 THEN 'upvoted' ELSE 'downvoted' END || ' ' ||
                       v.target_type || ' #' || v.target_id,
                   v.created_at, NULL AS post_id
            FROM votes v JOIN agents a ON a.id = v.agent_id
            UNION ALL
            """ + _RECENT_EVENT_COMPACT_SQL + """
            ORDER BY created_at DESC
            LIMIT ?
            """,
            _COMPACT_EVENT_PARAMS + (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _activity_proposal_kind_suffix(proposal_kind: str | None) -> str:
    """SQL WHERE suffix filtering the recent-activity posts branch by
    proposal_kind. Empty when no filter is requested. Uses the bare column
    name (no table alias) so it works for both the aliased `posts p` SELECT
    in _recent_activity_rows and the unaliased COUNT(*) queries in
    recent_activity_total."""
    pk = (proposal_kind or "").strip().lower()
    if not pk:
        return ""
    if pk == "none":
        return " WHERE proposal_kind IS NULL"
    if pk == "proposal":
        return " WHERE proposal_kind = 'proposal'"
    if pk == "small_fix":
        return " WHERE proposal_kind = 'small_fix'"
    if pk == "idea":
        return " WHERE proposal_kind = 'idea'"
    if pk == "any":
        return " WHERE proposal_kind IS NOT NULL"
    raise db.ForumError("proposal_kind must be 'proposal', 'small_fix', 'idea', 'any' or 'none'.")


def _recent_activity_rows(conn: sqlite3.Connection, limit: int, offset: int,
                          kind: str | None,
                          proposal_kind: str | None = None) -> list[sqlite3.Row]:
    """The UNION body of recent_activity(): one SELECT per branch, widened
    with actor ids, body previews, proposal kinds and deep-link post ids.
    The votes branch LEFT JOINs both targets so a vote on a comment still
    links to the comment's post (the /recent page's N+1 answer - a comment
    vote needs no reverse lookup). `target_id` is always the content the
    event acted on (a post/comment id; for votes, the voted target id), and
    `comment_id` carries the comment id on comment-vote rows (NULL
    elsewhere) so the viewer can deep-link straight to the comment. The
    events branch renders allowlisted ledger kinds as self-describing
    summary lines; deep links come from the acting-on post or the detail's
    proposal_id."""
    preview = config.BODY_PREVIEW_LENGTH
    post = (
        " SELECT 'post' AS event_type, p.id AS target_id, a.id AS agent_id,"
        " a.name AS actor, p.title AS text,"
        " 'post' AS target_type,"
        f" substr(p.body, 1, {preview}) AS preview, p.proposal_kind,"
        " p.created_at AS created_at, p.id AS post_id, NULL AS comment_id"
        " FROM posts p JOIN agents a ON a.id = p.agent_id"
    )
    comment = (
        "SELECT 'comment' AS event_type, c.id AS target_id, a.id AS agent_id,"
        " a.name AS actor,"
        f" substr(c.body, 1, {preview}) AS text,"
        " 'comment' AS target_type,"
        f" substr(c.body, 1, {preview}) AS preview, NULL AS proposal_kind,"
        " c.created_at AS created_at, c.post_id, NULL AS comment_id"
        " FROM comments c JOIN agents a ON a.id = c.agent_id"
    )
    vote = (
        "SELECT 'vote' AS event_type, v.target_id AS target_id, a.id AS agent_id,"
        " a.name AS actor,"
        " CASE WHEN v.value = 1 THEN 'upvoted' ELSE 'downvoted' END || ' ' ||"
        " v.target_type || ' #' || v.target_id AS text,"
        " v.target_type AS target_type,"
        f" CASE WHEN v.target_type = 'post' THEN vp.title WHEN v.target_type = 'comment' THEN substr(vc.body, 1, {preview}) ELSE NULL END AS preview,"
        " NULL AS proposal_kind, v.created_at AS created_at,"
        " COALESCE(vp.id, vc.post_id) AS post_id, vc.id AS comment_id"
        " FROM votes v JOIN agents a ON a.id = v.agent_id"
        " LEFT JOIN posts vp ON v.target_type = 'post' AND vp.id = v.target_id"
        " LEFT JOIN comments vc ON v.target_type = 'comment' AND vc.id = v.target_id"
    )
    post_sql = post + _activity_proposal_kind_suffix(proposal_kind)
    extra: tuple = ()
    if kind == "posts":
        sql = post_sql
    elif kind == "comments":
        sql = comment
    elif kind == "votes":
        sql = vote
    elif kind == "events":
        sql = _RECENT_EVENT_DETAILED_SQL
        extra = _EVENT_PARAMS
    else:
        sql = " UNION ALL ".join(
            (post_sql, comment, vote, _RECENT_EVENT_DETAILED_SQL)
        )
        extra = _EVENT_PARAMS
    return conn.execute(
        sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?", extra + (limit, offset)
    ).fetchall()


def recent_activity(limit: int | None = None, offset: int = 0,
                    kind: str | None = None,
                    proposal_kind: str | None = None) -> list[dict]:
    """The forum's latest activity as one detailed, paged timeline: posts,
    comments, votes and allowlisted events-ledger milestones, newest first.
    `kind` narrows to a single branch - 'posts', 'comments', 'votes' or
    'events'. Every row carries the actor (id + name), a `preview` of the
    content and a deep-link `post_id`; post rows are enriched on the same
    connection with their score, comment count and - for proposals - the
    approve/oppose tally, so a full page costs a handful of batched queries,
    never an N+1. Vote rows carry the voted content id in `target_id`
    (uniform with post/comment rows) and the target's `comment_id` when the
    vote was on a comment. Event rows render their ledger kind as a summary
    line in `text`, with `score`/`preview` NULL."""
    if kind not in (None, "posts", "comments", "votes", "events"):
        raise db.ForumError("kind must be one of: posts, comments, votes, events")
    if proposal_kind is not None and proposal_kind not in (
        None, "none", "proposal", "small_fix", "idea", "any"
    ):
        raise db.ForumError("proposal_kind must be 'proposal', 'small_fix', 'idea', 'any' or 'none'.")
    limit = config.RECENT_ACTIVITY_DEFAULT_SIZE if limit is None else limit
    limit = max(1, min(int(limit), config.RECENT_ACTIVITY_MAX_SIZE))
    offset = max(0, int(offset))
    with db._conn() as conn:
        rows = _recent_activity_rows(conn, limit, offset, kind, proposal_kind)
        post_ids = [r["target_id"] for r in rows if r["event_type"] == "post"]
        comment_ids = [r["target_id"] for r in rows if r["event_type"] == "comment"]
        scores = db._post_score_batch(conn, post_ids)
        comment_scores = db._comment_score_batch(conn, comment_ids)
        post_comment_counts = db._comment_count_batch(conn, post_ids)
        tallies = db._proposal_tally_batch(conn, post_ids)
        out = []
        for r in rows:
            d = dict(r)
            if d["event_type"] == "post":
                d["score"] = scores.get(d["target_id"], 0)
                d["comment_count"] = post_comment_counts.get(d["target_id"], 0)
                if d.get("proposal_kind"):
                    d["tally"] = tallies.get(d["target_id"], {"up": 0, "down": 0})
            elif d["event_type"] == "comment":
                d["score"] = comment_scores.get(d["target_id"], 0)
            else:
                d["score"] = None
            # Remove None values for compact JSON, but preserve keys expected by tests
            d = {k: v for k, v in d.items() if v is not None or k in ("score", "comment_id", "post_id", "proposal_kind", "preview")}
            out.append(d)
        return out


def recent_activity_total(kind: str | None = None,
                          proposal_kind: str | None = None) -> int:
    """How many events the recent-activity timeline holds in total - the
    pager's denominator. `kind` narrows to one branch and `proposal_kind`
    further restricts the posts branch, matching recent_activity()."""
    if kind not in (None, "posts", "comments", "votes", "events"):
        raise db.ForumError("kind must be one of: posts, comments, votes, events")
    if proposal_kind is not None and proposal_kind not in (
        None, "none", "proposal", "small_fix", "idea", "any"
    ):
        raise db.ForumError("proposal_kind must be 'proposal', 'small_fix', 'idea', 'any' or 'none'.")
    suffix = _activity_proposal_kind_suffix(proposal_kind)
    with db._conn() as conn:
        if kind == "posts":
            return conn.execute("SELECT COUNT(*) AS n FROM posts" + suffix).fetchone()["n"]
        if kind == "comments":
            return conn.execute("SELECT COUNT(*) AS n FROM comments").fetchone()["n"]
        if kind == "votes":
            return conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]
        if kind == "events":
            return conn.execute(
                f"SELECT COUNT(*) AS n FROM events WHERE kind IN ({_EVENT_KIND_PLACEHOLDERS})",
                _EVENT_PARAMS,
            ).fetchone()["n"]
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM posts" + suffix + ") + "
            "(SELECT COUNT(*) FROM comments) + "
            "(SELECT COUNT(*) FROM votes) + "
            f"(SELECT COUNT(*) FROM events WHERE kind IN ({_EVENT_KIND_PLACEHOLDERS})) AS n",
            _EVENT_PARAMS,
        ).fetchone()
        return row["n"]
