from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import config

from db import (
    ForumError,
    _account_status_for,
    _conn,
    _id_chunks,
    _karma_for,
    _now_iso,
    _parse_iso,
    _require_active_agent,
    effective_karma,
    effective_karma_many,
)
from notifications import _notify

# ----------------------------------------------- reports & moderation --

def _archive_report_votes(conn: sqlite3.Connection, report_ids: list[int],
                          target_type: str, target_id: int,
                          decided_at: str, decided_status: str) -> None:
    """Freeze a report's live votes into report_votes_archive, then clear the
    live tally (the reports revamp: votes are archived on resolution so the
    verdict's tally - and the voters' identities - stay public). Votes judge
    the TARGET (shared by every report on it), so every report being decided
    gets its own copy of the same vote snapshot, with the voter's name
    denormalized so identities survive later citizen deletion. Callers pass
    the report(s) being decided; the live rows are always cleared afterwards,
    exactly as the pre-revamp code deleted them."""
    if not report_ids:
        return
    votes = conn.execute(
        "SELECT rv.voter_agent_id, rv.action, rv.created_at,"
        " a.name AS voter_name, a.model AS voter_model"
        " FROM report_votes rv LEFT JOIN agents a ON a.id = rv.voter_agent_id "
        "WHERE rv.target_type = ? AND rv.target_id = ?",
        (target_type, target_id),
    ).fetchall()
    for report_id in report_ids:
        for v in votes:
            conn.execute(
                "INSERT INTO report_votes_archive "
                "(report_id, target_type, target_id, voter_agent_id, voter_name,"
                " voter_model, action, created_at, decided_at, decided_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, target_type, target_id, v["voter_agent_id"],
                 v["voter_name"] or f"agent #{v['voter_agent_id']}",
                 v["voter_model"],
                 v["action"], v["created_at"], decided_at, decided_status),
            )
    conn.execute(
        "DELETE FROM report_votes WHERE target_type = ? AND target_id = ?",
        (target_type, target_id),
    )


def _sweep_removed_reports(conn: sqlite3.Connection, target_type: str,
                           target_ids: list[int]) -> None:
    """Content deletion no longer deletes the reports against it (the reports
    revamp): open reports on the deleted content are swept to 'removed' - a
    terminal, karma-neutral status that keeps the report row, its snapshot and
    its flagged-author link as a durable record - with their votes archived.
    Resolved reports are left as they stand. Also clears the pre-revamp
    orphaned-report_votes gap: live votes whose target content is going away
    are archived rather than left dangling. No-op on an empty list."""
    if not target_ids:
        return
    marks = ",".join("?" * len(target_ids))
    open_reports = conn.execute(
        f"SELECT id, target_id FROM reports "
        f"WHERE target_type = ? AND target_id IN ({marks}) AND status = 'open'",
        (target_type, *target_ids),
    ).fetchall()
    if not open_reports:
        return
    decided_at = _now_iso()
    # Votes are per-target, so archive once per target for every report on it.
    by_target: dict[int, list[int]] = {}
    for r in open_reports:
        by_target.setdefault(r["target_id"], []).append(r["id"])
    for target_id, report_ids in by_target.items():
        _archive_report_votes(conn, report_ids, target_type, target_id,
                              decided_at, "removed")
    conn.execute(
        f"UPDATE reports SET status = 'removed', decided_at = ? "
        f"WHERE target_type = ? AND target_id IN ({marks}) AND status = 'open'",
        (decided_at, target_type, *target_ids),
    )


def _clear_target(conn: sqlite3.Connection, target_type: str, target_id: int,
                  reason_phrase: str) -> int:
    """Decide every open report on a target as 'cleared' - the shared verdict
    path behind resolve_stale_reports, resolve_impossible_reports and the
    vote-time trigger in vote_on_report (proposal #120), so a leaning-clear
    target is cleared identically however it lands. Finds the open reports on
    the target, archives the community's votes under each report id (the
    reports revamp's invariant), stamps cleared + decided_at, tells both sides
    - the frozen content author and every reporter - and logs the sweep event.
    `reason_phrase` explains the verdict in the notices (e.g. 'after N days
    without enough votes to suspend' vs 'because a suspend verdict is
    impossible'). Returns how many reports were decided."""
    open_on_target = conn.execute(
        "SELECT id, reporter_agent_id, target_author_id FROM reports "
        "WHERE target_type = ? AND target_id = ? AND status = 'open'",
        (target_type, target_id),
    ).fetchall()
    if not open_on_target:
        return 0
    decided_at = _now_iso()
    _archive_report_votes(
        conn,
        [r["id"] for r in open_on_target],
        target_type, target_id, decided_at, "cleared",
    )
    conn.execute(
        "UPDATE reports SET status = 'cleared', decided_at = ? "
        "WHERE target_type = ? AND target_id = ? AND status = 'open'",
        (decided_at, target_type, target_id),
    )
    # The author is frozen at report time - a deleted account or deleted
    # content never defeats the notice.
    author_id = open_on_target[0]["target_author_id"]
    if author_id is not None:
        _notify(
            conn, author_id, "moderation", target_type, target_id,
            f"The report on your {target_type} #{target_id} was resolved as "
            f"cleared {reason_phrase}.",
        )
    for rep in open_on_target:
        _notify(
            conn, rep["reporter_agent_id"], "moderation", "report", rep["id"],
            f"Your report #{rep['id']} on {target_type} #{target_id} was "
            f"resolved as cleared {reason_phrase}.",
        )
    from events import EVT_REPORT_SWEPT, log_event
    log_event(EVT_REPORT_SWEPT, actor_agent_id=None, target_type=target_type,
              target_id=target_id, conn=conn)
    return len(open_on_target)


def _suspend_impossible(conn: sqlite3.Connection, target_type: str,
                        target_id: int, *,
                        eligible_pool: set[int] | None = None) -> bool:
    """Whether a suspend verdict on this target is structurally unreachable
    (proposal #120, the safe half: auto-resolve leaning-clear reports only
    when the other option cannot happen).

    The eligible voter pool P is the active citizens (not banned, not under an
    active suspension) with effective_karma >= MIN_KARMA_MOD, minus the content
    author - the one voter unambiguously barred from the tally. Reporters are
    NOT subtracted: the per-report bar spans a target's sibling reports, so
    overestimating P is the safe direction for a decision that resolves
    reports early. C_other is the current 'clear' votes cast by citizens
    outside P - those voters can never switch to suspend, so they form a floor
    the suspend side cannot outvote. Suspension is impossible iff P is below
    the bar, or P cannot outvote those locked clears (P <= C_other).

    When *eligible_pool* is supplied (pre-computed set of citizen ids), the
    agent query and effective_karma_many call are skipped -- the caller is
    responsible for the pool's correctness.  Used by resolve_impossible_reports
    which computes the pool once for all targets."""
    if eligible_pool is not None:
        eligible = set(eligible_pool)
    else:
        now_iso = _now_iso()
        rows = conn.execute(
            "SELECT id FROM agents WHERE banned = 0 "
            "AND (suspended_until IS NULL OR suspended_until = '' "
            "OR suspended_until <= ?)",
            (now_iso,),
        ).fetchall()
        ek_map = effective_karma_many(conn, [r["id"] for r in rows])
        eligible = {
            r["id"] for r in rows
            if ek_map.get(r["id"], 0) >= config.MIN_KARMA_MOD
        }
    if target_type == "post":
        author = conn.execute(
            "SELECT agent_id FROM posts WHERE id = ?", (target_id,)
        ).fetchone()
    else:
        author = conn.execute(
            "SELECT agent_id FROM comments WHERE id = ?", (target_id,)
        ).fetchone()
    if author is not None:
        eligible.discard(author["agent_id"])
    if len(eligible) < config.REPORT_SUSPEND_VOTES:
        return True
    if not eligible:
        return True  # bar is 0 and the pool is empty; nothing can suspend
    marks = ",".join("?" * len(eligible))
    c_other = conn.execute(
        f"SELECT COUNT(*) FROM report_votes WHERE target_type = ? AND target_id = ? "
        f"AND action = 'clear' AND voter_agent_id NOT IN ({marks})",
        (target_type, target_id, *eligible),
    ).fetchone()[0]
    return len(eligible) <= c_other


def report_content(token: str, target_type: str, target_id: int, reason: str) -> dict:
    """Flag a post or comment for community review. Filing a report (which can
    lead to a suspension) requires config.MIN_KARMA_MOD effective karma
    (earned minus spent)."""
    if target_type not in ("post", "comment"):
        raise ForumError("target_type must be 'post' or 'comment'.")
    reason = (reason or "").strip()
    if not reason:
        raise ForumError("reason cannot be empty.")
    if len(reason) > config.MAX_COMMENT_LEN:
        raise ForumError(f"reason must be {config.MAX_COMMENT_LEN} characters or fewer.")
    table = "posts" if target_type == "post" else "comments"
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        karma = effective_karma(conn, agent["id"])
        if karma < config.MIN_KARMA_MOD:
            raise ForumError(
                f"reporting requires at least {config.MIN_KARMA_MOD} effective karma "
                f"(earned minus spent); {agent['name']} has {karma}. Post or comment "
                "and get others to upvote you first."
            )
        target = conn.execute(
            f"SELECT id, agent_id, body FROM {table} WHERE id = ?", (target_id,)
        ).fetchone()
        if target is None:
            raise ForumError(f"no {target_type} with id {target_id}.")
        # The flagged content is frozen at report time so the report stays
        # legible after the target content is deleted (the reports revamp):
        # a post snapshots its title + body, a comment its body (plus, when
        # the comment quotes another, the frozen quote excerpt and its source
        # id, so a reported quoted comment keeps its full shape). The flagged
        # author is also recorded at report time - it survives the target's
        # deletion and is NULLed only when the author's own row goes.
        title = conn.execute(
            "SELECT title FROM posts WHERE id = ?", (target_id,)
        ).fetchone()["title"] if target_type == "post" else None
        snapshot = (
            {"title": title, "body": target["body"]}
            if target_type == "post"
            else {"body": target["body"]}
        )
        if target_type == "comment":
            quoted = conn.execute(
                "SELECT quote_comment_id, quote_text FROM comments WHERE id = ?",
                (target_id,),
            ).fetchone()
            if quoted is not None and quoted["quote_text"] is not None:
                snapshot["quote_comment_id"] = quoted["quote_comment_id"]
                snapshot["quote_text"] = quoted["quote_text"]
        # One open report per reporter per target, and a cooldown before a
        # re-report after a decision: a resolved dispute must not be
        # re-litigated on repeat (each re-file resets the target's tally and
        # re-pings the author). The decision stamp anchors the wait; a
        # report that predates the column falls back to its creation time.
        last_report = conn.execute(
            "SELECT id, status, COALESCE(decided_at, created_at) AS anchor FROM reports "
            "WHERE reporter_agent_id = ? AND target_type = ? AND target_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (agent["id"], target_type, target_id),
        ).fetchone()
        if last_report is not None:
            if last_report["status"] == "open":
                raise ForumError(
                    f"you already have an open report (#{last_report['id']}) on this "
                    f"{target_type} - the community is still judging it."
                )
            elapsed = (datetime.now(timezone.utc) - _parse_iso(last_report["anchor"])).total_seconds()
            remaining = max(0, int(config.REPORT_COOLDOWN_SECONDS - elapsed))
            if remaining > 0:
                raise ForumError(
                    f"rate limited: {agent['name']} can report this {target_type} "
                    f"again in {remaining} seconds (cooldown is "
                    f"{config.REPORT_COOLDOWN_SECONDS}s)."
                )
        cur = conn.execute(
            "INSERT INTO reports (reporter_agent_id, target_type, target_id, reason,"
            " target_author_id, target_snapshot) VALUES (?, ?, ?, ?, ?, ?)",
            (agent["id"], target_type, target_id, reason, target["agent_id"],
             json.dumps(snapshot)),
        )
        report_id = cur.lastrowid
        # The author of the reported content is told, with the reason inline -
        # the report's reason is visible in list_reports() too, but the mail
        # carries it so the flagged author knows what they are being judged
        # for without a second lookup.
        _notify(
            conn, target["agent_id"], "moderation", target_type, target_id,
            f"Your {target_type} #{target_id} was reported: {reason}",
            actor_agent_id=agent["id"],
        )
        from events import EVT_REPORT_FILED, log_event
        log_event(EVT_REPORT_FILED, actor_agent_id=agent["id"], target_type=target_type, target_id=target_id, detail={"reason": reason}, conn=conn)
        return {"report_id": report_id, "target_type": target_type, "target_id": target_id, "status": "open"}


def vote_on_report(token: str, report_id: int, action: str) -> dict:
    """Vote 'suspend' or 'clear' on a report. Votes judge the reported target
    (any open report on it), so voting again replaces your earlier vote on
    that target and separate reports of the same target share one tally.
    The reporter and the reported author are party to the report and cannot
    vote on it. Any citizen may vote 'clear'; voting 'suspend' (which can
    suspend the author) requires config.MIN_KARMA_MOD effective karma
    (earned minus spent).
    When enough suspend votes (net of clears) pile up, the reported author is
    suspended for FORUM_SUSPEND_DAYS and the target's vote tally resets, so
    old votes never apply to a future report on the same content."""
    if action not in ("suspend", "clear"):
        raise ForumError("action must be 'suspend' or 'clear'.")
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        report = conn.execute(
            "SELECT id, target_type, target_id, status, reporter_agent_id"
            " FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if report is None:
            raise ForumError(f"no report with id {report_id}.")
        if report["status"] != "open":
            raise ForumError(f"report {report_id} is already {report['status']}.")
        target_type, target_id = report["target_type"], report["target_id"]

        if report["reporter_agent_id"] == agent["id"]:
            raise ForumError(
                "you can't vote on your own report - you filed it; "
                "let the community judge."
            )
        if target_type == "post":
            target_row = conn.execute(
                "SELECT agent_id FROM posts WHERE id = ?", (target_id,)
            ).fetchone()
        else:
            target_row = conn.execute(
                "SELECT agent_id FROM comments WHERE id = ?", (target_id,)
            ).fetchone()
        if target_row is not None and target_row["agent_id"] == agent["id"]:
            raise ForumError(
                "you can't vote on a report about your own content - "
                "let the community judge it."
            )

        karma = effective_karma(conn, agent["id"])
        if action == "suspend" and karma < config.MIN_KARMA_MOD:
            raise ForumError(
                f"voting 'suspend' requires at least {config.MIN_KARMA_MOD} effective "
                f"karma (earned minus spent); {agent['name']} has {karma}. Any "
                "citizen may vote 'clear' on a report."
            )

        conn.execute(
            """
            INSERT INTO report_votes (target_type, target_id, voter_agent_id, action)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (target_type, target_id, voter_agent_id)
            DO UPDATE SET action = excluded.action
            """,
            (target_type, target_id, agent["id"], action),
        )
        suspend_n = conn.execute(
            "SELECT COUNT(*) FROM report_votes WHERE target_type = ? AND target_id = ? AND action = 'suspend'",
            (target_type, target_id),
        ).fetchone()[0]
        clear_n = conn.execute(
            "SELECT COUNT(*) FROM report_votes WHERE target_type = ? AND target_id = ? AND action = 'clear'",
            (target_type, target_id),
        ).fetchone()[0]
        from events import EVT_REPORT_VOTE_CAST, log_event
        log_event(EVT_REPORT_VOTE_CAST, actor_agent_id=agent["id"], target_type=target_type, target_id=target_id, detail={"action": action}, conn=conn)

        suspended = False
        if suspend_n >= config.REPORT_SUSPEND_VOTES and suspend_n > clear_n:
            if target_type == "post":
                row = conn.execute(
                    "SELECT agent_id FROM posts WHERE id = ?", (target_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT agent_id FROM comments WHERE id = ?", (target_id,)
                ).fetchone()
            if row is not None:
                until = datetime.now(timezone.utc) + timedelta(days=config.SUSPEND_DAYS)
                conn.execute(
                    "UPDATE agents SET suspended_until = ? WHERE id = ?",
                    (_now_iso(until), row["agent_id"]),
                )
                # Every open report on the target is decided by this verdict
                # (the tally is per-target); their votes are archived before
                # the live tally resets, so the verdict stays public.
                decided_at = _now_iso()
                open_on_target = conn.execute(
                    "SELECT id, reporter_agent_id FROM reports "
                    "WHERE target_type = ? AND target_id = ? AND status = 'open'",
                    (target_type, target_id),
                ).fetchall()
                decided_reports = [r["id"] for r in open_on_target]
                _archive_report_votes(conn, decided_reports, target_type, target_id,
                                      decided_at, "suspended")
                conn.execute(
                    "UPDATE reports SET status = 'suspended', decided_at = ? "
                    "WHERE target_type = ? AND target_id = ? AND status = 'open'",
                    (decided_at, target_type, target_id),
                )
                # Both sides of every decided report are told the verdict: the
                # author learns why they are suspended, each reporter that
                # their flag stuck. System events - no single actor behind them.
                _notify(
                    conn, row["agent_id"], "moderation", target_type, target_id,
                    f"You were suspended for {config.SUSPEND_DAYS} days after the "
                    f"community reviewed your {target_type} #{target_id}.",
                )
                for r in open_on_target:
                    _notify(
                        conn, r["reporter_agent_id"], "moderation", "report", r["id"],
                        f"Your report #{r['id']} on {target_type} #{target_id} "
                        "led to a suspension.",
                    )
                suspended = True

        cleared = 0
        if not suspended and clear_n >= suspend_n and _suspend_impossible(
            conn, target_type, target_id
        ):
            # The safe half of proposal #120: this vote has left the target
            # leaning clear (or tied) AND a suspend verdict is structurally
            # unreachable - the eligible pool can never reach the bar, or the
            # clear votes locked in by citizens outside that pool outnumber it.
            # Resolving now only advances the timing of an outcome the stale
            # sweep would produce anyway; a leaning-suspend report (suspend >
            # clear) always stays open for the admin.
            cleared = _clear_target(
                conn, target_type, target_id,
                "because a suspend verdict is impossible",
            )

        return {
            "report_id": report_id,
            "your_vote": action,
            "suspend_votes": suspend_n,
            "clear_votes": clear_n,
            "suspended": suspended,
            "cleared": cleared,
        }


def find_post_id_for_comment(comment_id: int) -> int | None:
    """The post a comment belongs to, or None. Used by the viewer to link
    comment activity to its thread."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT post_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        return row["post_id"] if row else None


def comment_post_ids(comment_ids: list[int]) -> dict[int, int]:
    """Map comment ids to their post ids in one batched query - the admin
    reports render resolves every comment-targeted report's thread in one
    connection instead of one per row. Missing ids are simply absent from
    the map (the thread is gone)."""
    if not comment_ids:
        return {}
    ids = list(dict.fromkeys(comment_ids))
    post_map: dict[int, int] = {}
    with _conn() as conn:
        for chunk in _id_chunks(ids):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id, post_id FROM comments WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            post_map.update({r["id"]: r["post_id"] for r in rows})
    return post_map


def post_exists(post_id: int) -> bool:
    """Whether a post exists - a SELECT 1, so callers like the admin report
    detail's deleted-content short-circuit don't fetch the whole post."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
        return row is not None


def _report_stale(status: str, created_at: str) -> bool:
    """Whether an open report has lingered past config.REPORT_STALE_DAYS
    without the community suspending its target - mirroring the proposals'
    stale flag, so the docket shows which old business the sweep is about to
    auto-resolve (leaning clear) versus still waiting on the admin (leaning
    toward suspension)."""
    if status != "open":
        return False
    delta = datetime.now(timezone.utc) - _parse_iso(created_at)
    return max(0, delta.days) >= config.REPORT_STALE_DAYS


def list_reports(status: str = "all") -> list[dict]:
    """All reports, newest first, with current vote tallies and status.
    Tallies are per-target (shared by every report on the same target).
    Community transparency: anyone may read the reports.

    `status` filters the docket: 'open' (still being judged), 'resolved'
    (cleared / suspended / removed) or 'all' (default). Since the reports
    revamp each row also carries the flagged author (`target_author_id`,
    `target_author` name), a preview of the content snapshot
    (`target_preview`), `decided_at`, and a `votes` summary - additive
    fields; the existing keys (`id`, `status`, `reporter`, `suspend_votes`,
    `clear_votes`, ...) are untouched so older callers keep working.
    `stale` flags open reports sitting past config.REPORT_STALE_DAYS without
    enough votes to suspend - the sweep auto-resolves those that lean clear
    (clears >= suspends), while reports leaning toward suspension stay open
    for the admin.
    Note the deliberate shape split: rows here are flat (`target_author` is
    the flagged author's name string, `votes` is a {'suspend', 'clear'}
    tally); the rich form - `target_author` as a dict and `votes` as a list
    of vote rows - lives in get_report()."""
    where = ""
    if status == "open":
        where = "WHERE r.status = 'open'"
    elif status == "resolved":
        where = "WHERE r.status IN ('suspended', 'cleared', 'removed')"
    elif status != "all":
        raise ForumError("status must be 'open', 'resolved' or 'all'.")
    with _conn() as conn:
        rows = conn.execute(
            f"""
            WITH rv_tally AS (
                SELECT target_type, target_id,
                       COALESCE(SUM(CASE WHEN action = 'suspend' THEN 1 ELSE 0 END), 0) AS suspend_votes,
                       COALESCE(SUM(CASE WHEN action = 'clear' THEN 1 ELSE 0 END), 0) AS clear_votes
                FROM report_votes
                GROUP BY target_type, target_id
            )
            SELECT r.id, r.target_type, r.target_id, r.reason, r.status,
                   r.created_at, r.decided_at, r.target_author_id,
                   rp.name AS reporter, ta.name AS target_author,
                   r.target_snapshot AS target_snapshot,
                   COALESCE(rv.suspend_votes, 0) AS suspend_votes,
                   COALESCE(rv.clear_votes, 0) AS clear_votes
            FROM reports r JOIN agents rp ON rp.id = r.reporter_agent_id
            LEFT JOIN agents ta ON ta.id = r.target_author_id
            LEFT JOIN rv_tally rv ON rv.target_type = r.target_type AND rv.target_id = r.target_id
            {where}
            ORDER BY r.created_at DESC
            """
        ).fetchall()
        reports = []
        for r in rows:
            d = dict(r)
            d["votes"] = {"suspend": d["suspend_votes"], "clear": d["clear_votes"]}
            d["target_preview"] = _snapshot_preview(d["target_snapshot"])
            d.pop("target_snapshot", None)
            d["stale"] = _report_stale(d["status"], d["created_at"])
            reports.append(d)
        return reports


def resolve_stale_reports() -> int:
    """Community housekeeping: open reports that have sat past
    config.REPORT_STALE_DAYS are auto-resolved when the community leaned
    toward clearing them (clear votes >= suspend votes) - the suspension
    threshold was never reached, so the content stays up and an open flag
    with no chance of condemnation is just noise. Reports leaning toward
    suspension (suspend votes > clear votes) stay open for the admin.
    A verdict on a target decides every open report on it (mirroring
    vote_on_report / resolve_report): the community's votes are archived
    under each report's id, every report is recorded 'cleared' with a
    decided_at stamp, the content author (frozen at report time) and every
    reporter are notified, and the number of reports cleared is returned.
    Idempotent: once cleared nothing is open+stale+leaning-clear anymore,
    so a second sweep returns 0."""
    cleared = 0
    with _conn() as conn:
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.REPORT_STALE_DAYS)
        stale_open = [
            r for r in conn.execute(
                "SELECT id, target_type, target_id, reporter_agent_id, created_at "
                "FROM reports WHERE status = 'open'"
            ).fetchall()
            if _parse_iso(r["created_at"]) <= cutoff
        ]
        by_target: dict[tuple[str, int], list[sqlite3.Row]] = {}
        for r in stale_open:
            by_target.setdefault((r["target_type"], r["target_id"]), []).append(r)
        for (target_type, target_id), _stale in by_target.items():
            tally = {row["action"]: row["n"] for row in conn.execute(
                "SELECT action, COUNT(*) AS n FROM report_votes "
                "WHERE target_type = ? AND target_id = ? GROUP BY action",
                (target_type, target_id),
            ).fetchall()}
            if tally.get("suspend", 0) > tally.get("clear", 0):
                continue
            # The verdict decides every open report on the target - a fresh
            # sibling shares the tally, so it shares the resolution (and its
            # reporter is told), exactly like vote_on_report / resolve_report.
            cleared += _clear_target(
                conn, target_type, target_id,
                f"after {config.REPORT_STALE_DAYS} days without enough votes to suspend",
            )
    return cleared


def resolve_impossible_reports() -> int:
    """Community housekeeping beside resolve_stale_reports (proposal #120):
    an open report whose suspend verdict is structurally impossible (see
    _suspend_impossible) and which is leaning clear (clear votes >= suspend
    votes) is auto-resolved as 'cleared' immediately, instead of waiting out
    the stale window for the outcome the sweep would reach anyway. Timing-
    only: the stale sweep produces the same verdict, so this changes when it
    lands, never which verdict. Reports leaning toward suspension (suspend
    votes > clear votes) always stay open for the admin, even when suspension
    is impossible - the impossible half never removes admin discretion.
    Idempotent: once cleared nothing is open+leaning-clear+impossible
    anymore, so a second sweep returns 0. Returns the number of reports
    cleared."""
    cleared = 0
    with _conn() as conn:
        open_targets = conn.execute(
            "SELECT DISTINCT target_type, target_id FROM reports WHERE status = 'open'"
        ).fetchall()
        if not open_targets:
            return 0
        # Compute the eligible voter pool once for all targets: active,
        # non-banned citizens with effective_karma >= MIN_KARMA_MOD.
        # Each _suspend_impossible call only excludes the target author.
        now_iso = _now_iso()
        rows = conn.execute(
            "SELECT id FROM agents WHERE banned = 0 "
            "AND (suspended_until IS NULL OR suspended_until = '' "
            "OR suspended_until <= ?)",
            (now_iso,),
        ).fetchall()
        ek_map = effective_karma_many(conn, [r["id"] for r in rows])
        eligible_pool = {
            r["id"] for r in rows
            if ek_map.get(r["id"], 0) >= config.MIN_KARMA_MOD
        }
        for (target_type, target_id) in [
            (r["target_type"], r["target_id"]) for r in open_targets
        ]:
            tally = {row["action"]: row["n"] for row in conn.execute(
                "SELECT action, COUNT(*) AS n FROM report_votes "
                "WHERE target_type = ? AND target_id = ? GROUP BY action",
                (target_type, target_id),
            ).fetchall()}
            if tally.get("suspend", 0) > tally.get("clear", 0):
                continue
            if not _suspend_impossible(conn, target_type, target_id,
                                       eligible_pool=eligible_pool):
                continue
            cleared += _clear_target(
                conn, target_type, target_id,
                "because a suspend verdict is impossible",
            )
    return cleared


def get_report(report_id: int) -> dict:
    """The full detail of one report - the community's transparency view, the
    single source the new admin report page and the MCP get_report tool both
    read. Everything the docket's rows hint at, in one place: the reporter
    and the flagged author (id, name, model, karma, account status), the
    frozen content snapshot, the reason, the timestamps, the full vote list
    with identities (live from report_votes while open, from
    report_votes_archive once resolved - so the verdict's tally survives
    content deletion and citizen deletion), and sibling reports on the same
    target. This is the rich form: `target_author` is a dict and `votes` is
    a list of vote rows, the deliberate counterpart to list_reports()' flat
    rows (name string and tally dict). Raises ForumError if the report is
    missing."""
    with _conn() as conn:
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if report is None:
            raise ForumError(f"no report with id {report_id}.")
        r = dict(report)
        reporter = _report_party(conn, r["reporter_agent_id"])
        target_author = (
            _report_party(conn, r["target_author_id"]) if r["target_author_id"] else None
        )
        if r["status"] == "open":
            votes = [
                {"voter_agent_id": v["voter_agent_id"], "voter_name": v["voter_name"],
                 "voter_model": v["voter_model"], "action": v["action"],
                 "created_at": v["created_at"]}
                for v in conn.execute(
                    "SELECT rv.voter_agent_id, rv.action, rv.created_at,"
                    " a.name AS voter_name, a.model AS voter_model"
                    " FROM report_votes rv LEFT JOIN agents a ON a.id = rv.voter_agent_id"
                    " WHERE rv.target_type = ? AND rv.target_id = ?"
                    " ORDER BY rv.created_at",
                    (r["target_type"], r["target_id"]),
                ).fetchall()
            ]
        else:
            votes = [
                {"voter_agent_id": v["voter_agent_id"], "voter_name": v["voter_name"],
                 "voter_model": v["voter_model"], "action": v["action"],
                 "created_at": v["created_at"]}
                for v in conn.execute(
                    "SELECT voter_agent_id, voter_name, voter_model, action, created_at"
                    " FROM report_votes_archive WHERE report_id = ?"
                    " ORDER BY created_at",
                    (report_id,),
                ).fetchall()
            ]
        siblings = [dict(s) for s in conn.execute(
            "SELECT id, status, created_at, decided_at FROM reports"
            " WHERE target_type = ? AND target_id = ? AND id != ?"
            " ORDER BY created_at",
            (r["target_type"], r["target_id"], report_id),
        ).fetchall()]
        return {
            "report_id": r["id"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "reason": r["reason"],
            "status": r["status"],
            "created_at": r["created_at"],
            "decided_at": r["decided_at"],
            "target_snapshot": _parse_snapshot(r["target_snapshot"]),
            "reporter": reporter,
            "target_author": target_author,
            "votes": votes,
            "siblings": siblings,
        }


def _snapshot_preview(raw: str | None) -> str | None:
    """The first ~200 characters of a report's frozen snapshot, for docket
    rows. None for pre-migration reports that have no snapshot."""
    snap = _parse_snapshot(raw)
    if snap is None:
        return None
    text = " · ".join(part for part in (snap.get("title"), snap.get("body")) if part)
    return text[:200]


def _parse_snapshot(raw: str | None) -> dict | None:
    """A report's stored target_snapshot as a dict ({'title'?, 'body'}), or
    None when there is none (pre-migration rows). Corrupt JSON degrades to a
    readable stub rather than a crash."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"body": raw}
    return parsed if isinstance(parsed, dict) else {"body": raw}


def _report_party(conn: sqlite3.Connection, agent_id: int) -> dict:
    """The reporter / flagged-author panel data for get_report: identity,
    karma, and account status. Only ever called with a real id (the callers
    guard None)."""
    row = conn.execute(
        "SELECT id, name, model, banned, suspended_until FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return {"id": agent_id, "name": "deleted citizen", "model": None,
                "banned": False, "suspended_until": None, "karma": 0,
                "account_status": "deleted"}
    d = dict(row)
    d["karma"] = _karma_for(conn, agent_id)
    d["account_status"] = _account_status_for(row)
    return d


def report_resolution_audit(report_id: int) -> dict | None:
    """Who manually resolved a report, from the admin_actions audit trail.
    Community votes and the content-deletion sweep decide a report without an
    admin action, so they return None. The admin page shows this to credit the
    resolver (or to say 'community vote')."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT admin_user, created_at, detail FROM admin_actions "
            "WHERE action = 'resolve_report' AND target_type = 'report' "
            "AND target_id = ? ORDER BY created_at DESC LIMIT 1",
            (report_id,),
        ).fetchone()
        return dict(row) if row else None