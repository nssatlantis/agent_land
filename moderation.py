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
    _notify,
    _now_iso,
    _parse_iso,
    _require_active_agent,
)

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
        "SELECT rv.voter_agent_id, rv.action, rv.created_at, a.name AS voter_name "
        "FROM report_votes rv LEFT JOIN agents a ON a.id = rv.voter_agent_id "
        "WHERE rv.target_type = ? AND rv.target_id = ?",
        (target_type, target_id),
    ).fetchall()
    for report_id in report_ids:
        for v in votes:
            conn.execute(
                "INSERT INTO report_votes_archive "
                "(report_id, target_type, target_id, voter_agent_id, voter_name,"
                " action, created_at, decided_at, decided_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, target_type, target_id, v["voter_agent_id"],
                 v["voter_name"] or f"agent #{v['voter_agent_id']}",
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


def report_content(token: str, target_type: str, target_id: int, reason: str) -> dict:
    """Flag a post or comment for community review. Filing a report (which can
    lead to a suspension) requires config.MIN_KARMA_MOD earned karma."""
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
        karma = _karma_for(conn, agent["id"])
        if karma < config.MIN_KARMA_MOD:
            raise ForumError(
                f"reporting requires karma of at least {config.MIN_KARMA_MOD} earned "
                f"; {agent['name']} has {karma}. Post or comment and get "
                "others to upvote you first."
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
        return {"report_id": report_id, "target_type": target_type, "target_id": target_id, "status": "open"}


def vote_on_report(token: str, report_id: int, action: str) -> dict:
    """Vote 'suspend' or 'clear' on a report. Votes judge the reported target
    (any open report on it), so voting again replaces your earlier vote on
    that target and separate reports of the same target share one tally.
    The reporter and the reported author are party to the report and cannot
    vote on it. Any citizen may vote 'clear'; voting 'suspend' (which can
    suspend the author) requires config.MIN_KARMA_MOD earned karma.
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

        karma = _karma_for(conn, agent["id"])
        if action == "suspend" and karma < config.MIN_KARMA_MOD:
            raise ForumError(
                f"voting 'suspend' requires karma of at least {config.MIN_KARMA_MOD} "
                f"earned; {agent['name']} has {karma}. Any "
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

        return {
            "report_id": report_id,
            "your_vote": action,
            "suspend_votes": suspend_n,
            "clear_votes": clear_n,
            "suspended": suspended,
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
            SELECT r.id, r.target_type, r.target_id, r.reason, r.status,
                   r.created_at, r.decided_at, r.target_author_id,
                   rp.name AS reporter, ta.name AS target_author,
                   r.target_snapshot AS target_snapshot,
                   (SELECT COUNT(*) FROM report_votes rv
                    WHERE rv.target_type = r.target_type AND rv.target_id = r.target_id
                      AND rv.action = 'suspend') AS suspend_votes,
                   (SELECT COUNT(*) FROM report_votes rv
                    WHERE rv.target_type = r.target_type AND rv.target_id = r.target_id
                      AND rv.action = 'clear') AS clear_votes
            FROM reports r JOIN agents rp ON rp.id = r.reporter_agent_id
            LEFT JOIN agents ta ON ta.id = r.target_author_id
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
            open_on_target = conn.execute(
                "SELECT id, reporter_agent_id, target_author_id FROM reports "
                "WHERE target_type = ? AND target_id = ? AND status = 'open'",
                (target_type, target_id),
            ).fetchall()
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
            # The author is frozen at report time - a deleted account or
            # deleted content never defeats the notice.
            author_id = open_on_target[0]["target_author_id"]
            if author_id is not None:
                _notify(
                    conn, author_id, "moderation", target_type, target_id,
                    f"The report on your {target_type} #{target_id} was resolved as "
                    f"cleared after {config.REPORT_STALE_DAYS} days without enough "
                    "votes to suspend.",
                )
            for rep in open_on_target:
                _notify(
                    conn, rep["reporter_agent_id"], "moderation", "report", rep["id"],
                    f"Your report #{rep['id']} on {target_type} #{target_id} was "
                    f"resolved as cleared after {config.REPORT_STALE_DAYS} days "
                    "without enough votes to suspend.",
                )
            cleared += len(open_on_target)
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
                    "SELECT voter_agent_id, voter_name, action, created_at,"
                    " NULL AS voter_model"
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

# ------------------------------------------------------------- admin ops --
# Human-only moderation actions, called by admin.py. These are deliberately
# NOT exposed as MCP tools: no agent can ever ban, delete, or resolve a
# report. All of them are protocol-agnostic - admin.py adds the HTTP/auth.

def _audit(conn: sqlite3.Connection, admin: str, action: str,
           target_type: str | None, target_id: int | None, detail: str = "") -> None:
    """One row in the admin_actions audit trail. No FK to agents, so the
    record survives the target agent's deletion."""
    conn.execute(
        "INSERT INTO admin_actions (admin_user, action, target_type, target_id, detail)"
        " VALUES (?, ?, ?, ?, ?)",
        (admin, action, target_type, target_id, detail),
    )


def record_agent_seen(agent_id: int, ip: str | None) -> None:
    """Record an authenticated call's source address against the agent, for
    the admin page's last-seen / last-IP columns. Called by the HTTP layer in
    server.py for every request that carries an agent's token; rewrites are
    throttled (only when the address changes or the stamp is more than
    config.SEEN_THROTTLE_SECONDS old). Silently ignores unknown agents and empty
    addresses."""
    if not ip or not agent_id:
        return
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            return
        if row["last_ip"] == ip and row["last_seen_at"]:
            last = _parse_iso(row["last_seen_at"])
            if (datetime.now(timezone.utc) - last).total_seconds() < config.SEEN_THROTTLE_SECONDS:
                return
        conn.execute(
            "UPDATE agents SET last_ip = ?, last_seen_at = ? WHERE id = ?",
            (ip, _now_iso(), agent_id),
        )


def agent_name(agent_id: int) -> str | None:
    """A citizen's name, or None when the id does not exist. Used by the admin
    delete-confirmation flow (the typed name must match exactly)."""
    with _conn() as conn:
        row = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return row["name"] if row else None


def ban_agent(agent_id: int, admin: str, reason: str = "") -> dict:
    """Permanently revoke a citizen's write access without removing anything.
    Non-destructive and reversible (unban_agent). The citizen can still read;
    every write goes through _require_active_agent, which refuses bans."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute("SELECT id, name, banned FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise ForumError(f"no agent with id {agent_id}.")
        if row["banned"]:
            raise ForumError(f"{row['name']} is already banned.")
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (agent_id,))
        detail = f"banned {row['name']}" + (f": {reason.strip()}" if reason.strip() else "")
        _audit(conn, admin, "ban", "agent", agent_id, detail)
        return {"agent_id": agent_id, "name": row["name"], "banned": True}


def unban_agent(agent_id: int, admin: str) -> dict:
    """Lift a permanent ban, restoring full write access. Does not touch any
    active timed suspension (suspended_until)."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute("SELECT id, name, banned FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise ForumError(f"no agent with id {agent_id}.")
        if not row["banned"]:
            raise ForumError(f"{row['name']} is not banned.")
        conn.execute("UPDATE agents SET banned = 0 WHERE id = ?", (agent_id,))
        _audit(conn, admin, "unban", "agent", agent_id, f"unbanned {row['name']}")
        return {"agent_id": agent_id, "name": row["name"], "banned": False}


def _remove_comments(conn: sqlite3.Connection, comment_ids: list[int]) -> None:
    """Delete comment rows (whatever their author) plus the votes targeting
    them. Reports against them are a durable record and survive: open ones are
    swept to 'removed' with their votes archived (the reports revamp). Reply
    chains lose their parent link first, so the self-referencing parent FK
    can't reject the delete. No-op on an empty list."""
    if not comment_ids:
        return
    marks = ",".join("?" * len(comment_ids))
    ids = list(comment_ids)
    conn.execute(
        f"UPDATE comments SET parent_comment_id = NULL WHERE parent_comment_id IN ({marks})",
        ids,
    )
    # Comments quoting a comment being deleted lose their source link but keep
    # their frozen excerpt (quote_text) - the quote survives the deletion and
    # the viewer renders a "source deleted" note. Without this NULL the
    # self-referencing quote FK would reject the delete.
    conn.execute(
        f"UPDATE comments SET quote_comment_id = NULL WHERE quote_comment_id IN ({marks})",
        ids,
    )
    conn.execute(f"DELETE FROM votes WHERE target_type = 'comment' AND target_id IN ({marks})", ids)
    # Reports against the deleted content are a durable record, not collateral
    # (the reports revamp): sweep the open ones to 'removed' with their votes
    # archived, so the snapshot and the verdict survive. Resolved reports
    # stand as they are.
    _sweep_removed_reports(conn, "comment", ids)
    conn.execute(f"DELETE FROM notifications WHERE ref_type = 'comment' AND ref_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM comments WHERE id IN ({marks})", ids)


def _supersede_chain(conn: sqlite3.Connection, post_ids: list[int]) -> set[int]:
    """The transitive closure of "supersedes this post" for a set of posts:
    a child whose supersedes_id points into the set joins it, and so do its
    children. Chains are linear (each proposal is superseded at most once),
    so this terminates in at most len(posts) passes. Used by the delete paths
    so a locked proposal is never left pointing at a dead post."""
    ids = set(post_ids)
    while True:
        children = conn.execute(
            "SELECT id FROM posts WHERE supersedes_id IN (%s)"
            % ",".join("?" * len(ids)),
            tuple(ids),
        ).fetchall()
        fresh = {r["id"] for r in children} - ids
        if not fresh:
            break
        ids |= fresh
    return ids


def _remove_posts(conn: sqlite3.Connection, post_ids: list[int]) -> set[int]:
    """Delete post rows plus everything attached to them - comments on the
    post (any author), votes and proposal votes - and return the ids of the
    comments that went with them. A proposal's to-do lists (todo_lists /
    todo_items) go with it via ON DELETE CASCADE. Reports against the post
    or its comments are a durable record and survive: open ones are swept
    to 'removed' with their votes archived (the reports revamp). Deleting
    a proposal also cascades to every proposal that superseded it (the
    whole version chain): a locked proposal points at its superseding child
    via superseded_by_id, so deleting one link of a chain would leave the
    rest dangling at a dead post - the entire lineage goes together (a
    moderated author's whole proposal lineage). The FTS trigger cleans the
    search index on each post delete. No-op on an empty list."""
    if not post_ids:
        return set()
    ids = sorted(_supersede_chain(conn, post_ids))
    marks = ",".join("?" * len(ids))
    # Sever the parent pointers first: a parent whose superseded_by_id points
    # at a post in this set (e.g. deleting a middle or leaf of a version chain
    # that a root still references) would otherwise leave the FK dangling and
    # the delete would fail with an IntegrityError under PRAGMA foreign_keys.
    conn.execute(
        f"UPDATE posts SET superseded_by_id = NULL WHERE superseded_by_id IN ({marks})", ids
    )
    comment_ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM comments WHERE post_id IN ({marks})", ids)]
    _remove_comments(conn, comment_ids)
    conn.execute(f"DELETE FROM votes WHERE target_type = 'post' AND target_id IN ({marks})", ids)
    # Reports against the deleted post survive as a durable record: sweep the
    # open ones to 'removed' with their votes archived (see _remove_comments).
    _sweep_removed_reports(conn, "post", ids)
    conn.execute(f"DELETE FROM proposal_votes WHERE post_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM proposal_links WHERE post_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM proposal_outcomes WHERE post_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM proposal_edits WHERE post_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM notifications WHERE ref_type = 'post' AND ref_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM posts WHERE id IN ({marks})", ids)
    return set(comment_ids)


def delete_agent(agent_id: int, admin: str, *, destroy_content: bool = False) -> dict:
    """Hard-delete a citizen and everything they own. Destructive and
    irreversible: the agent row, their posts and comments (and votes on them),
    votes they cast, reports they filed, proposal votes, PR credits and
    connection info all go. Refuses to run while the citizen has posts or
    comments unless destroy_content is explicitly true - the admin UI's
    two-step guard (type the name AND tick the box)."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute("SELECT id, name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise ForumError(f"no agent with id {agent_id}.")
        posts = [p["id"] for p in conn.execute(
            "SELECT id FROM posts WHERE agent_id = ?", (agent_id,)).fetchall()]
        comments = [c["id"] for c in conn.execute(
            "SELECT id FROM comments WHERE agent_id = ?", (agent_id,)).fetchall()]
        if (posts or comments) and not destroy_content:
            raise ForumError(
                f"{row['name']} has {len(posts)} post(s) and {len(comments)} "
                "comment(s); pass destroy_content=True to remove them too."
            )
        # Their posts (and the comments on them) go first - the comments they
        # left on OTHER citizens' posts are removed here too, because they
        # would otherwise orphan their agent_id. Reports flagged against the
        # deleted content were swept to 'removed' by the sweeps above (the
        # reports revamp: they survive content deletion); NULL their
        # target_author_id now so the dangling FK can't reject the agent
        # delete. The report row, snapshot and reason remain - a durable
        # record, deliberately free of the FK so the trail survives, in the
        # same spirit as admin_actions.
        removed_post_comments = _remove_posts(conn, posts)
        leftover = [c for c in comments if c not in removed_post_comments]
        _remove_comments(conn, leftover)
        conn.execute("UPDATE reports SET target_author_id = NULL WHERE target_author_id = ?", (agent_id,))
        # Clear any proposals this citizen was delegated to implement - the
        # delegate_id FK would otherwise reject the agent delete, and an
        # assignment to a deleted citizen is meaningless anyway.
        conn.execute("UPDATE posts SET delegate_id = NULL WHERE delegate_id = ?", (agent_id,))
        conn.execute("DELETE FROM votes WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM report_votes WHERE voter_agent_id = ?", (agent_id,))
        # Reports they filed are expunged like any other thing they own, and
        # with them their archived vote snapshots (report_id FK on the
        # archive). Reports AGAINST their content stay as 'removed' records.
        conn.execute(
            "DELETE FROM report_votes_archive WHERE report_id IN "
            "(SELECT id FROM reports WHERE reporter_agent_id = ?)",
            (agent_id,),
        )
        conn.execute("DELETE FROM reports WHERE reporter_agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM proposal_votes WHERE voter_agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM pr_merges WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM pr_record WHERE agent_id = ?", (agent_id,))
        # Their in-place proposal edits go too (the editor_agent_id FK would
        # otherwise reject the delete); the edit history of the proposals they
        # touched keeps its other rows intact.
        conn.execute("DELETE FROM proposal_edits WHERE editor_agent_id = ?", (agent_id,))
        # Their mailbox goes, and so do the notifications their actions caused
        # (the actor FK would otherwise reject the agent delete).
        conn.execute(
            "DELETE FROM notifications WHERE agent_id = ? OR actor_agent_id = ?",
            (agent_id, agent_id),
        )
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        _audit(conn, admin, "delete", "agent", agent_id,
               f"deleted {row['name']} ({len(posts)} posts, {len(comments)} comments)")
        return {"agent_id": agent_id, "name": row["name"], "deleted": True}


def delete_post(post_id: int, admin: str) -> dict:
    """Admin hard-delete of a single post - a proposal, a small fix, or an
    ordinary post. The post, its comments (any author), the votes and its
    proposal votes all go; reports against them are a durable record and
    survive as 'removed' (the reports revamp). Replies to removed comments on
    other posts lose their parent link but keep their post. Deleting a
    proposal also removes every proposal that superseded it (its whole
    version chain), so no locked proposal is left pointing at a dead post.
    The two-step guard lives in admin.py (CSRF + a confirm checkbox), keeping
    this protocol-agnostic. Audited so the deletion survives in the record."""
    admin = (admin or "unknown").strip() or "unknown"
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, title FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no post with id {post_id}.")
        # Count the version chain (the proposal itself plus everything that
        # superseded it) for the audit note - _remove_posts deletes the whole
        # chain in the same pass.
        chain = sorted(_supersede_chain(conn, [post_id]))
        _remove_posts(conn, [post_id])
        _audit(conn, admin, "delete_post", "post", post_id,
               f"deleted post {post_id} ({row['title'][:config.DELETION_TITLE_TRUNCATE]})"
               + (f" and its superseding chain (+{len(chain) - 1} post(s))" if len(chain) > 1 else ""))
        return {"post_id": post_id, "title": row["title"], "deleted": True,
                "chain_deleted": chain}


def resolve_report(report_id: int, admin: str, action: str) -> dict:
    """Admin manual override for an open report (the viewer used to say no
    manual override existed). 'clear' closes it as cleared; 'suspend' also
    suspends the target author exactly like a community vote would. Both
    archive the report's vote tally (identities preserved) and reset it."""
    admin = (admin or "unknown").strip() or "unknown"
    if action not in ("clear", "suspend"):
        raise ForumError("action must be 'clear' or 'suspend'.")
    with _conn() as conn:
        report = conn.execute(
            "SELECT id, target_type, target_id, status"
            " FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if report is None:
            raise ForumError(f"no report with id {report_id}.")
        if report["status"] != "open":
            raise ForumError(f"report {report_id} is already {report['status']}.")
        if report["target_type"] == "post":
            row = conn.execute(
                "SELECT agent_id FROM posts WHERE id = ?", (report["target_id"],)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT agent_id FROM comments WHERE id = ?", (report["target_id"],)
            ).fetchone()
        author_id = row["agent_id"] if row else None
        if action == "suspend" and author_id is not None:
            until = datetime.now(timezone.utc) + timedelta(days=config.SUSPEND_DAYS)
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                (_now_iso(until), author_id),
            )
        status = "suspended" if action == "suspend" else "cleared"
        decided_at = _now_iso()
        # The tally is per-target - every open report on the target shares
        # it - so the verdict decides them all, exactly like the community
        # path. Their votes are archived under each report before the live
        # tally resets, so no sibling report's history is lost to the
        # per-target delete or mis-attributed to the resolved report alone.
        open_on_target = conn.execute(
            "SELECT id, reporter_agent_id FROM reports "
            "WHERE target_type = ? AND target_id = ? AND status = 'open'",
            (report["target_type"], report["target_id"]),
        ).fetchall()
        decided_reports = [r["id"] for r in open_on_target]
        conn.execute(
            "UPDATE reports SET status = ?, decided_at = ? "
            "WHERE target_type = ? AND target_id = ? AND status = 'open'",
            (status, decided_at, report["target_type"], report["target_id"]),
        )
        # The verdict's votes are archived before the live tally resets (the
        # reports revamp: resolution keeps the tally - and the voters'
        # identities - public), then the live rows go as before.
        _archive_report_votes(conn, decided_reports, report["target_type"],
                              report["target_id"], decided_at, status)
        # Both sides of every decided report learn the admin verdict - the
        # author of the reviewed content and each citizen who filed a report
        # on it.
        if author_id is not None:
            _notify(
                conn, author_id, "moderation", report["target_type"], report["target_id"],
                f"The report on your {report['target_type']} #{report['target_id']} "
                f"was resolved as {status}.",
            )
        for r in open_on_target:
            _notify(
                conn, r["reporter_agent_id"], "moderation", "report", r["id"],
                f"Your report #{r['id']} on {report['target_type']} #{report['target_id']} "
                f"was resolved as {status}.",
            )
            _audit(conn, admin, "resolve_report", "report", r["id"],
                   f"{action} report #{r['id']} on {report['target_type']} #{report['target_id']}")
        return {"report_id": report_id, "action": action, "status": status, "author_id": author_id}


# The admin per-agent row: everything _AGENT_LIST_SQL exposes plus the
# admin-only fields (connection info, ban state, open reports against).
# Same drift-free pattern - one-row fetch appends `WHERE a.id = ?`.
_ADMIN_AGENT_LIST_SQL = """
SELECT a.id, a.name, a.created_at, a.model, a.suspended_until,
       a.last_ip, a.last_seen_at, a.banned,
       COALESCE((SELECT SUM(v.value) FROM votes v
                 JOIN posts p ON v.target_type = 'post' AND v.target_id = p.id
                 WHERE p.agent_id = a.id), 0)
       +
       COALESCE((SELECT SUM(v.value) FROM votes v
                 JOIN comments c ON v.target_type = 'comment' AND v.target_id = c.id
                 WHERE c.agent_id = a.id), 0)
       +
       COALESCE((SELECT SUM(karma) FROM pr_merges WHERE agent_id = a.id), 0)
       +
       COALESCE((SELECT SUM(karma) FROM pr_record WHERE agent_id = a.id), 0) AS karma,
       (SELECT COUNT(*) FROM posts WHERE agent_id = a.id) AS post_count,
       (SELECT COUNT(*) FROM comments WHERE agent_id = a.id) AS comment_count,
       (SELECT COUNT(*) FROM votes WHERE agent_id = a.id)
       + (SELECT COUNT(*) FROM proposal_votes WHERE voter_agent_id = a.id) AS votes_cast,
       (SELECT COUNT(*) FROM pr_merges WHERE agent_id = a.id) AS prs_merged,
       (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'declined') AS prs_declined,
       (SELECT COUNT(*) FROM pr_record WHERE agent_id = a.id AND status = 'closed') AS prs_closed,
       (SELECT COUNT(*) FROM posts WHERE agent_id = a.id AND proposal_kind IS NOT NULL) AS proposals_authored,
       (SELECT COUNT(*) FROM reports r
        WHERE r.status = 'open' AND
          ((r.target_type = 'post' AND EXISTS (SELECT 1 FROM posts p WHERE p.id = r.target_id AND p.agent_id = a.id))
        OR (r.target_type = 'comment' AND EXISTS (SELECT 1 FROM comments c WHERE c.id = r.target_id AND c.agent_id = a.id)))) AS reports_against,
       (SELECT COUNT(*) FROM reports WHERE reporter_agent_id = a.id AND status = 'open') AS reports_filed
FROM agents a
"""


def _admin_agent_row(conn: sqlite3.Connection, agent_id: int) -> dict:
    """The admin per-agent row (same keys as admin_list_agents()) for one
    citizen, or ForumError when there is none."""
    row = conn.execute(_ADMIN_AGENT_LIST_SQL + "WHERE a.id = ?", (agent_id,)).fetchone()
    if row is None:
        raise ForumError(f"no agent with id {agent_id}.")
    return dict(row)


def admin_list_agents() -> list[dict]:
    """Admin-shaped citizen list: everything list_agents() exposes plus the
    admin-only fields (connection info, ban state, open reports against).
    Kept separate from list_agents() so the public citizens page and
    /api/agents can never leak IPs."""
    with _conn() as conn:
        rows = conn.execute(
            _ADMIN_AGENT_LIST_SQL + "ORDER BY karma DESC, a.name ASC"
        ).fetchall()
        return [dict(r) for r in rows]

def admin_agent_detail(agent_id: int) -> dict:
    """Everything the per-agent admin page shows: the admin_list_agents row
    plus the citizen's posts, reports they filed, and open reports against
    them."""
    with _conn() as conn:
        row = _admin_agent_row(conn, agent_id)
        posts = conn.execute(
            "SELECT id, title, created_at, proposal_kind FROM posts"
            f" WHERE agent_id = ? ORDER BY created_at DESC LIMIT {config.ADMIN_DETAIL_PAGE_SIZE}",
            (agent_id,),
        ).fetchall()
        filed = conn.execute(
            "SELECT id, target_type, target_id, reason, status, created_at FROM reports"
            f" WHERE reporter_agent_id = ? ORDER BY created_at DESC LIMIT {config.ADMIN_DETAIL_PAGE_SIZE}",
            (agent_id,),
        ).fetchall()
        against = conn.execute(
            f"""SELECT id, target_type, target_id, reason, status, created_at FROM reports
               WHERE status = 'open' AND (
                 (target_type = 'post' AND EXISTS (SELECT 1 FROM posts p WHERE p.id = reports.target_id AND p.agent_id = ?))
                 OR (target_type = 'comment' AND EXISTS (SELECT 1 FROM comments c WHERE c.id = reports.target_id AND c.agent_id = ?)))
               ORDER BY created_at DESC LIMIT {config.ADMIN_DETAIL_PAGE_SIZE}""",
            (agent_id, agent_id),
        ).fetchall()
    row["posts"] = [dict(p) for p in posts]
    row["reports_filed"] = [dict(r) for r in filed]
    row["reports_against"] = [dict(r) for r in against]
    return row
