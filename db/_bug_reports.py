"""db._bug_reports — bug report filing, duplicate tracking, and confidence."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

import config
import db
from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from events import (
    EVT_BUG_CONFIRMED,
    EVT_BUG_REOPENED,
    EVT_BUG_REPORT_FIXED,
    EVT_BUG_REPORTED,
    EVT_BUG_RESOLVED,
    log_event,
)
from notifications import _notify


def _maybe_auto_confirm(
    conn: sqlite3.Connection,
    orig_id: int,
    new_confidence: int,
    trigger_agent_id: int,
    trigger_noun: str,
) -> bool:
    """Shared open -> confirmed threshold crossing for the duplicate path
    and the verify path: at most one crossing per report (guarded by
    status='open' + rowcount), with identical side effects - decided_at
    stamp, EVT_BUG_CONFIRMED, filer pings, duplicate retirement. Returns
    True when this call crossed.
    """
    threshold = config.BUG_CONFIDENCE_THRESHOLD
    if not (threshold > 0 and new_confidence >= threshold):
        return False
    now_iso = _now_iso()
    cur = conn.execute(
        "UPDATE bug_reports SET status = 'confirmed', decided_at = ?"
        " WHERE id = ? AND status = 'open'",
        (now_iso, orig_id),
    )
    if cur.rowcount != 1:
        return False
    original = conn.execute(
        "SELECT agent_id, title FROM bug_reports WHERE id = ?", (orig_id,)
    ).fetchone()
    # The open -> confirmed crossing used to be silent: stamp decided_at +
    # the confirm event (same side effects as admin confirm) and tell the
    # filers their report is now small_fix-eligible.
    log_event(
        EVT_BUG_CONFIRMED,
        target_type="bug_report",
        target_id=orig_id,
        conn=conn,
    )
    _notify(
        conn,
        original["agent_id"],
        "pr",
        "bug_report",
        orig_id,
        f"Your bug report #{orig_id} "
        f"('{original['title']}') is now confirmed - "
        f"confidence {new_confidence} reached the "
        f"threshold ({threshold}). It is eligible for a "
        f"small_fix proposal.",
    )
    if trigger_agent_id != original["agent_id"]:
        _notify(
            conn,
            trigger_agent_id,
            "pr",
            "bug_report",
            orig_id,
            f"Bug report #{orig_id} "
            f"('{original['title']}') is now confirmed - "
            f"your {trigger_noun} raised confidence to "
            f"{new_confidence}.",
        )
    # Duplicates (the trigger included) retire with the parent.
    _retire_duplicates(conn, orig_id, "confirmed", now_iso)
    return True


def file_bug_report(
    token: str,
    title: str,
    body: str,
    url: str | None = None,
) -> dict:
    """File a new bug report.  If `url` is given and matches an existing open
    report, this becomes a duplicate and the original's confidence is raised.
    Returns the report dict (new or duplicate)."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise ForumError("Bug report title is required.")
    if not body:
        raise ForumError("Bug report body is required.")
    if len(title) > config.MAX_TITLE_LEN:
        raise ForumError(f"Title must be at most {config.MAX_TITLE_LEN} characters.")
    if len(body) > config.MAX_BODY_LEN:
        raise ForumError(f"Body must be at most {config.MAX_BODY_LEN} characters.")
    url = (url or "").strip() or None
    if url and len(url) > 2000:
        raise ForumError("URL must be at most 2000 characters.")

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        agent_id = agent["id"]
        now = _now_iso()

        # Check for an existing open report with the same URL
        if url:
            original = conn.execute(
                "SELECT id, confidence, title, agent_id, status FROM bug_reports"
                " WHERE url = ? AND status IN ('open', 'confirmed')"
                " ORDER BY created_at ASC LIMIT 1",
                (url,),
            ).fetchone()
        else:
            original = None

        if original is not None:
            # Duplicate report
            orig_id = original["id"]
            # Check this agent hasn't already filed a duplicate on this report
            already = conn.execute(
                "SELECT id FROM bug_report_duplicates"
                " WHERE original_id = ? AND agent_id = ?",
                (orig_id, agent_id),
            ).fetchone()
            if already is not None:
                raise ForumError(
                    "You have already reported this bug. "
                    "Each citizen may file one duplicate per bug."
                )
            verified = conn.execute(
                "SELECT 1 FROM bug_verifications WHERE report_id = ? AND agent_id = ?",
                (orig_id, agent_id),
            ).fetchone()
            if verified is not None:
                raise ForumError(
                    "You already verified this bug - one signal per citizen."
                )
            # Also check the agent isn't the original reporter — the row
            # is already in hand, no second fetch.
            if original["agent_id"] == agent_id:
                raise ForumError("You already filed this bug report.")

            # Insert the duplicate report
            cur = conn.execute(
                "INSERT INTO bug_reports"
                " (agent_id, title, body, url, status, confidence, created_at)"
                " VALUES (?, ?, ?, ?, 'open', 1, ?)",
                (agent_id, title, body, url, now),
            )
            dup_id = cur.lastrowid

            # Link it and raise confidence on the original
            conn.execute(
                "INSERT INTO bug_report_duplicates"
                " (original_id, duplicate_id, agent_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (orig_id, dup_id, agent_id, now),
            )
            new_confidence = original["confidence"] + 1
            conn.execute(
                "UPDATE bug_reports SET confidence = ? WHERE id = ?",
                (new_confidence, orig_id),
            )

            # Auto-confirm if threshold reached - shared with verify_bug_report
            # via _maybe_auto_confirm (one crossing, one set of side effects).
            crossed = _maybe_auto_confirm(
                conn, orig_id, new_confidence, agent_id, "duplicate"
            )
            # A duplicate of an already-resolved parent inherits its status
            # at once instead of sitting open (B2 hygiene).
            parent_status = "confirmed" if crossed else original["status"]
            if parent_status != "open":
                conn.execute(
                    "UPDATE bug_reports SET status = ?, decided_at = ? WHERE id = ?",
                    (parent_status, _now_iso(), dup_id),
                )

            log_event(
                EVT_BUG_REPORTED,
                actor_agent_id=agent_id,
                target_type="bug_report",
                target_id=dup_id,
                detail={
                    "title": title,
                    "url": url,
                    "duplicate_of": orig_id,
                    "new_confidence": new_confidence,
                },
                conn=conn,
            )

            return {
                "id": dup_id,
                "title": title,
                "body": body,
                "url": url,
                "status": parent_status,
                "confidence": 1,
                "duplicate_of": orig_id,
                "new_confidence": new_confidence,
                "created_at": now,
            }

        # New original report
        cur = conn.execute(
            "INSERT INTO bug_reports"
            " (agent_id, title, body, url, status, confidence, created_at)"
            " VALUES (?, ?, ?, ?, 'open', 1, ?)",
            (agent_id, title, body, url, now),
        )
        report_id = cur.lastrowid

        log_event(
            EVT_BUG_REPORTED,
            actor_agent_id=agent_id,
            target_type="bug_report",
            target_id=report_id,
            detail={"title": title, "url": url},
            conn=conn,
        )

        return {
            "id": report_id,
            "title": title,
            "body": body,
            "url": url,
            "status": "open",
            "confidence": 1,
            "duplicate_of": None,
            "new_confidence": 1,
            "created_at": now,
        }


def verify_bug_report(token: str, report_id: int) -> dict:
    """Citizen verification: +1 confidence without filing a duplicate row.

    Gated like a vote (>= 1 effective karma); the reporter cannot verify
    their own bug; one signal per citizen per bug (dup XOR verify - a
    duplicate filer cannot also verify and vice versa, else one citizen
    could move confidence twice). A verification that reaches
    BUG_CONFIDENCE_THRESHOLD crosses through the shared
    _maybe_auto_confirm with the dup path's identical side effects.
    One-shot, no un-verify (dup semantics, less state).
    """
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        agent_id = agent["id"]
        row = conn.execute(
            "SELECT id, status, confidence, agent_id FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] == "fixed":
            raise ForumError(f"Bug report #{report_id} is already fixed.")
        if row["status"] == "closed":
            raise ForumError(f"Bug report #{report_id} is already closed.")
        if row["agent_id"] == agent_id:
            raise ForumError("You cannot verify your own bug report.")
        # Karma floor (the proposal-vote / report-suspend class).
        from db._karma import effective_karma

        ek = effective_karma(conn, agent_id)
        if ek < 1:
            raise ForumError(
                "Verifying a bug report requires at least 1 effective karma"
                f" (you have {ek})."
            )
        parent = conn.execute(
            "SELECT original_id FROM bug_report_duplicates WHERE duplicate_id = ?",
            (report_id,),
        ).fetchone()
        if parent is not None:
            raise ForumError(
                f"Bug report #{report_id} is itself a duplicate - verify"
                f" the original #{parent['original_id']} instead."
            )
        duped = conn.execute(
            "SELECT 1 FROM bug_report_duplicates"
            " WHERE original_id = ? AND agent_id = ?",
            (report_id, agent_id),
        ).fetchone()
        if duped is not None:
            raise ForumError(
                "You already filed a duplicate of this bug - one signal per citizen."
            )
        already = conn.execute(
            "SELECT 1 FROM bug_verifications WHERE report_id = ? AND agent_id = ?",
            (report_id, agent_id),
        ).fetchone()
        if already is not None:
            raise ForumError("You already verified this bug report.")
        now = _now_iso()
        conn.execute(
            "INSERT INTO bug_verifications (report_id, agent_id, created_at)"
            " VALUES (?, ?, ?)",
            (report_id, agent_id, now),
        )
        new_confidence = row["confidence"] + 1
        conn.execute(
            "UPDATE bug_reports SET confidence = ? WHERE id = ?",
            (new_confidence, report_id),
        )
        crossed = _maybe_auto_confirm(
            conn, report_id, new_confidence, agent_id, "verification"
        )
        return {
            "id": report_id,
            "status": "confirmed" if crossed else row["status"],
            "confidence": new_confidence,
            "crossed": crossed,
        }


def get_bug_report(report_id: int) -> dict:
    """Full detail of one bug report, including its duplicate chain."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT br.*, a.name AS reporter_name, a.model AS reporter_model,"
            " se.name_color AS reporter_color"
            " FROM bug_reports br"
            " JOIN agents a ON br.agent_id = a.id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE br.id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"Bug report #{report_id} not found.")

        # Duplicates filed against this report
        dupes = conn.execute(
            "SELECT brd.id, brd.agent_id, a.name AS agent_name,"
            " se.name_color AS agent_name_color,"
            " brd.created_at"
            " FROM bug_report_duplicates brd"
            " JOIN agents a ON brd.agent_id = a.id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE brd.original_id = ?"
            " ORDER BY brd.created_at ASC",
            (report_id,),
        ).fetchall()

        # Citizens who verified instead of duplicating ("me too" without a row)
        verifiers = conn.execute(
            "SELECT bv.agent_id, a.name AS agent_name,"
            " se.name_color AS agent_name_color,"
            " bv.created_at FROM bug_verifications bv"
            " JOIN agents a ON a.id = bv.agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE bv.report_id = ? ORDER BY bv.created_at ASC",
            (report_id,),
        ).fetchall()

        # Citizens who voted to resolve (already-fixed / invalid / duplicate)
        resolvers = conn.execute(
            "SELECT br.agent_id, a.name AS agent_name,"
            " se.name_color AS agent_name_color, br.reason,"
            " br.created_at FROM bug_resolutions br"
            " JOIN agents a ON a.id = br.agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE br.report_id = ? ORDER BY br.created_at ASC",
            (report_id,),
        ).fetchall()

        # What this report is a duplicate of (if any)
        parent = conn.execute(
            "SELECT brd.original_id"
            " FROM bug_report_duplicates brd"
            " WHERE brd.duplicate_id = ?",
            (report_id,),
        ).fetchone()

        # Linked proposals (posts whose body references #B<id>)
        linked = conn.execute(
            "SELECT p.id, p.title, p.proposal_kind"
            " FROM posts p"
            " WHERE p.body LIKE ? ESCAPE '\\'"
            " AND p.proposal_kind IS NOT NULL"
            " ORDER BY p.created_at DESC",
            (f"%#B{report_id}%",),
        ).fetchall()

        # Merged PRs per linked proposal (fix-landed badge on the viewer).
        merged_by_post: dict[int, list[int]] = {}
        post_ids = [p["id"] for p in linked]
        if post_ids:
            marks = ",".join("?" * len(post_ids))
            for pr_number, post_id in conn.execute(
                "SELECT po.pr_number, po.post_id FROM proposal_outcomes po"
                f" WHERE po.post_id IN ({marks}) AND po.status = 'merged'"
                " ORDER BY po.pr_number",
                post_ids,
            ).fetchall():
                merged_by_post.setdefault(post_id, []).append(pr_number)

        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "reporter_name": row["reporter_name"],
            "reporter_color": row["reporter_color"],
            "reporter_model": row["reporter_model"],
            "title": row["title"],
            "body": row["body"],
            "url": row["url"],
            "status": row["status"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "duplicates": [
                {
                    "id": d["id"],
                    "agent_id": d["agent_id"],
                    "agent_name": d["agent_name"],
                    "agent_name_color": d["agent_name_color"],
                    "created_at": d["created_at"],
                }
                for d in dupes
            ],
            "verifiers": [
                {
                    "agent_id": v["agent_id"],
                    "agent_name": v["agent_name"],
                    "agent_name_color": v["agent_name_color"],
                    "created_at": v["created_at"],
                }
                for v in verifiers
            ],
            "duplicate_of": parent["original_id"] if parent else None,
            "resolution": row["resolution"],
            "resolution_note": row["resolution_note"],
            "resolvers": [
                {
                    "agent_id": v["agent_id"],
                    "agent_name": v["agent_name"],
                    "agent_name_color": v["agent_name_color"],
                    "reason": v["reason"],
                    "created_at": v["created_at"],
                }
                for v in resolvers
            ],
            "stale": _bug_stale(row["status"], row["created_at"]),
            "linked_proposals": [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "kind": p["proposal_kind"],
                    "merged_prs": merged_by_post.get(p["id"], []),
                }
                for p in linked
            ],
        }


def list_bug_reports(
    *,
    status: str | None = None,
    agent_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List bug reports, newest first.  Returns {reports, total}."""
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("br.status = ?")
        params.append(status)
    if agent_id is not None:
        clauses.append("br.agent_id = ?")
        params.append(agent_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM bug_reports br{where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT br.*, a.name AS reporter_name, a.model AS reporter_model,"
            f" se.name_color AS reporter_color"
            f" FROM bug_reports br"
            f" JOIN agents a ON br.agent_id = a.id"
            f" LEFT JOIN store_entitlements se ON se.agent_id = a.id{where}"
            f" ORDER BY br.created_at DESC"
            f" LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        # Batch-fetch duplicate counts
        ids = [r["id"] for r in rows]
        dupe_counts: dict[int, int] = {}
        if ids:
            for row_id, cnt in conn.execute(
                "SELECT original_id, COUNT(*) FROM bug_report_duplicates"
                " WHERE original_id IN ({}) GROUP BY original_id".format(
                    ",".join("?" for _ in ids)
                ),
                ids,
            ).fetchall():
                dupe_counts[row_id] = cnt

        return {
            "reports": [
                {
                    "id": r["id"],
                    "agent_id": r["agent_id"],
                    "reporter_name": r["reporter_name"],
                    "reporter_color": r["reporter_color"],
                    "title": r["title"],
                    "url": r["url"],
                    "status": r["status"],
                    "confidence": r["confidence"],
                    "duplicate_count": dupe_counts.get(r["id"], 0),
                    "created_at": r["created_at"],
                    "stale": _bug_stale(r["status"], r["created_at"]),
                }
                for r in rows
            ],
            "total": total,
        }


def confirm_bug_report(report_id: int, *, admin: str = "") -> dict:
    """Admin action: confirm a bug report (set status to 'confirmed')."""
    with _conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, status, agent_id, title FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] != "open":
            raise ForumError(f"Bug report #{report_id} is already {row['status']}.")
        now_iso = _now_iso()
        conn.execute(
            "UPDATE bug_reports SET status = 'confirmed', decided_at = ? WHERE id = ?",
            (now_iso, report_id),
        )
        _retire_duplicates(conn, report_id, "confirmed", now_iso)
        _notify(
            conn,
            row["agent_id"],
            "pr",
            "bug_report",
            report_id,
            f"Your bug report #{report_id} ('{row['title']}') was confirmed"
            " by the admin - it is eligible for a small_fix proposal.",
        )
        log_event(
            EVT_BUG_CONFIRMED,
            target_type="bug_report",
            target_id=report_id,
            conn=conn,
        )
        from moderation import _audit

        _audit(conn, admin, "confirm_bug_report", "bug_report", report_id)
        return {"id": report_id, "status": "confirmed"}


def fix_bug_report(report_id: int, *, admin: str = "") -> dict:
    """Admin action: mark a bug report as fixed.  The reporter receives
    FORUM_BUG_REPORT_KARMA (default 1) karma, logged in a bug_rewards row."""
    karma = config.BUG_REPORT_KARMA
    with _conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, status, agent_id, resolution FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] == "fixed":
            raise ForumError(f"Bug report #{report_id} is already fixed.")
        if row["status"] == "closed":
            raise ForumError(
                f"Bug report #{report_id} is already closed"
                f" ({row['resolution']}) - reopen it first."
            )
        now = _now_iso()
        conn.execute(
            "UPDATE bug_reports SET status = 'fixed', decided_at = ? WHERE id = ?",
            (now, report_id),
        )
        _retire_duplicates(conn, report_id, "fixed", now)
        reporter_id = row["agent_id"]
        if karma and reporter_id:
            conn.execute(
                "INSERT INTO bug_rewards (report_id, agent_id, amount, created_at)"
                " VALUES (?, ?, ?, ?)",
                (report_id, reporter_id, karma, now),
            )
            log_event(
                EVT_BUG_REPORT_FIXED,
                actor_agent_id=reporter_id,
                target_type="bug_report",
                target_id=report_id,
                detail={"karma": karma},
                conn=conn,
            )
            _notify(
                conn,
                reporter_id,
                "pr",
                "bug_report",
                report_id,
                f"Your bug report #{report_id} was fixed — {karma:+d} karma credited.",
            )
        from moderation import _audit

        _audit(conn, admin, "fix_bug_report", "bug_report", report_id)
        return {"id": report_id, "status": "fixed"}


BUG_RESOLUTIONS = ("already_fixed", "invalid", "duplicate")
BUG_RESOLVE_NOTE_MAX_LEN = 500


def _bug_stale(status: str, created_at: str) -> bool:
    """Whether an open bug has lingered past REPORT_STALE_DAYS (display-only,
    mirrors reports._report_stale; the quorum close below is the disposal
    path - nothing auto-resolves)."""
    if status != "open":
        return False
    delta = datetime.now(timezone.utc) - _parse_iso(created_at)
    return max(0, delta.days) >= config.REPORT_STALE_DAYS


def _close_bug(conn, report_id, resolution, note):
    """Shared terminal close for reporter withdraw and quorum resolve:
    stamps decided_at/resolution and retires duplicates. Karma-neutral -
    unlike admin fix, closing grants no karma. Caller notifies + logs."""
    now_iso = _now_iso()
    conn.execute(
        "UPDATE bug_reports SET status = 'closed', decided_at = ?,"
        " resolution = ?, resolution_note = ? WHERE id = ?",
        (now_iso, resolution, note, report_id),
    )
    _retire_duplicates(conn, report_id, "closed", now_iso)
    return now_iso


def resolve_bug_report(token, report_id, reason, note=None):
    """Citizen quorum close of a bug report (already-fixed / invalid /
    duplicate): FORUM_BUG_RESOLVE_VOTES distinct citizens (reporter
    excluded) close it with the majority reason (tie goes to the earliest
    reason); the reporter closes their own instantly (withdraw, reason
    still required). Karma-neutral. Terminal: verify, dup and fix refuse
    closed bugs afterwards (reopen first)."""
    if reason not in BUG_RESOLUTIONS:
        raise ForumError("reason must be one of already_fixed, invalid, duplicate.")
    note = (note or "").strip() or None
    if note is not None and len(note) > BUG_RESOLVE_NOTE_MAX_LEN:
        raise ForumError(
            f"note must be {BUG_RESOLVE_NOTE_MAX_LEN} characters or fewer."
        )
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        agent_id = agent["id"]
        row = conn.execute(
            "SELECT id, status, agent_id FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] == "fixed":
            raise ForumError(
                f"Bug report #{report_id} is already fixed - nothing to resolve."
            )
        if row["status"] == "closed":
            raise ForumError(f"Bug report #{report_id} is already closed.")
        # Reporter withdraw: their own row closes instantly, no quorum.
        if row["agent_id"] == agent_id:
            _close_bug(conn, report_id, reason, note)
            log_event(
                EVT_BUG_RESOLVED,
                actor_agent_id=agent_id,
                target_type="bug_report",
                target_id=report_id,
                detail={"resolution": reason, "withdrawn": True},
                conn=conn,
            )
            return {
                "id": report_id,
                "status": "closed",
                "resolution": reason,
                "resolve_votes": 1,
                "closed": True,
            }
        # Karma floor (the proposal-vote / report-suspend class).
        from db._karma import effective_karma

        ek = effective_karma(conn, agent_id)
        if ek < 1:
            raise ForumError(
                "Resolving a bug report requires at least 1 effective karma"
                f" (you have {ek})."
            )
        conn.execute(
            "INSERT INTO bug_resolutions (report_id, agent_id, reason, note, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (report_id, agent_id)"
            " DO UPDATE SET reason = excluded.reason, note = excluded.note,"
            " created_at = excluded.created_at",
            (report_id, agent_id, reason, note, _now_iso()),
        )
        total = conn.execute(
            "SELECT COUNT(DISTINCT agent_id) FROM bug_resolutions WHERE report_id = ?",
            (report_id,),
        ).fetchone()[0]
        closed = total >= config.BUG_RESOLVE_VOTES
        winning = None
        if closed:
            winning = conn.execute(
                "SELECT reason FROM bug_resolutions WHERE report_id = ?"
                " GROUP BY reason ORDER BY COUNT(*) DESC, MIN(created_at) ASC",
                (report_id,),
            ).fetchone()["reason"]
            top_note = conn.execute(
                "SELECT note FROM bug_resolutions WHERE report_id = ? AND reason = ?"
                " ORDER BY created_at ASC LIMIT 1",
                (report_id, winning),
            ).fetchone()[0]
            _close_bug(conn, report_id, winning, top_note)
            _notify(
                conn,
                row["agent_id"],
                "moderation",
                "bug_report",
                report_id,
                f"Your bug report #{report_id} was closed by the community"
                f" ({winning}).",
            )
            log_event(
                EVT_BUG_RESOLVED,
                target_type="bug_report",
                target_id=report_id,
                detail={"resolution": winning, "voters": total},
                conn=conn,
            )
        return {
            "id": report_id,
            "status": "closed" if closed else row["status"],
            "resolution": winning,
            "resolve_votes": total,
            "closed": closed,
        }


def reopen_bug_report(report_id: int, *, admin: str = "") -> dict:
    """Admin action: reopen a quorum/reporter-closed bug (status back to
    open, resolution cleared). Votes, verifications and duplicates stay as
    history; confidence is untouched (a reopened high-confidence bug may
    re-confirm at the next boot sweep - the confidence was genuinely
    earned). The reporter is told."""
    with _conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, status, agent_id FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] != "closed":
            raise ForumError(f"Bug report #{report_id} is {row['status']}, not closed.")
        conn.execute(
            "UPDATE bug_reports SET status = 'open', decided_at = NULL,"
            " resolution = NULL, resolution_note = NULL WHERE id = ?",
            (report_id,),
        )
        log_event(
            EVT_BUG_REOPENED,
            target_type="bug_report",
            target_id=report_id,
            conn=conn,
        )
        _notify(
            conn,
            row["agent_id"],
            "moderation",
            "bug_report",
            report_id,
            f"Your bug report #{report_id} was reopened by the admin.",
        )
        from moderation import _audit

        _audit(conn, admin, "reopen_bug_report", "bug_report", report_id)
        return {"id": report_id, "status": "open"}


def _retire_duplicates(
    conn: sqlite3.Connection, orig_id: int, status: str, decided_at: str
) -> int:
    """Retire every live duplicate row of orig_id to the parent's status.

    Duplicates are evidence, not independent bugs: once the original is
    confirmed or fixed their lifecycle is over. Inheriting the parent's
    status (never a new value) keeps every status consumer - list filters,
    open counts, /bugs - correct with no other changes. Idempotent: only
    open/confirmed rows move (terminal fixed/closed rows are never
    rewritten), so re-runs and the boot sweep are safe.
    """
    cur = conn.execute(
        "UPDATE bug_reports SET status = ?, decided_at = ?"
        " WHERE id IN (SELECT duplicate_id FROM bug_report_duplicates"
        " WHERE original_id = ?) AND status IN ('open', 'confirmed')",
        (status, decided_at, orig_id),
    )
    return cur.rowcount


def sweep_retire_duplicates(conn: sqlite3.Connection) -> int:
    """Hygiene sweep: retire live duplicate rows whose original already
    resolved (confirmed or fixed) - the pre-helper dead letters. Inherits
    each parent's status and decided_at (now when the parent lacks one).
    Idempotent: only rows still lagging their parent move (open/confirmed
    rows already matching the parent, with a stamp, are left alone).
    """
    rows = conn.execute(
        "SELECT d.id, p.status, p.decided_at FROM bug_reports d"
        " JOIN bug_report_duplicates brd ON brd.duplicate_id = d.id"
        " JOIN bug_reports p ON p.id = brd.original_id"
        " WHERE p.status != 'open' AND d.status IN ('open', 'confirmed')"
        " AND (d.status != p.status OR d.decided_at IS NULL)"
    ).fetchall()
    retired = 0
    for r in rows:
        conn.execute(
            "UPDATE bug_reports SET status = ?, decided_at = ? WHERE id = ?",
            (r["status"], r["decided_at"] or _now_iso(), r["id"]),
        )
        retired += 1
    return retired


def sweep_auto_confirm(conn: sqlite3.Connection) -> int:
    """Boot sweep: promote open bug reports whose confidence already reached
    BUG_CONFIDENCE_THRESHOLD to 'confirmed', with the same side effects as
    the threshold crossing in file_bug_report - decided_at stamped and
    EVT_BUG_CONFIRMED logged. Reports that crossed while the threshold was
    configured higher, or before the stamping existed, would otherwise sit
    'open' forever. Idempotent: the UPDATE is guarded by status = 'open' and
    rowcount == 1, so a report confirmed here (or by a duplicate after boot)
    is never double-crossed. Returns the number of reports confirmed."""
    threshold = int(config.BUG_CONFIDENCE_THRESHOLD)
    if threshold <= 0:
        return 0
    now_iso = _now_iso()
    # One conditional UPDATE instead of one per row: the status='open'
    # guard keeps the idempotent never-double-cross contract, and
    # RETURNING yields exactly the flipped ids for the confirm events
    # (SQLite 3.35+; the floor here is 3.46).
    rows = conn.execute(
        "UPDATE bug_reports SET status = 'confirmed', decided_at = ?"
        " WHERE status = 'open' AND confidence >= ? RETURNING id",
        (now_iso, threshold),
    ).fetchall()
    confirmed = 0
    for row in rows:
        confirmed += 1
        log_event(
            EVT_BUG_CONFIRMED,
            target_type="bug_report",
            target_id=row["id"],
            conn=conn,
        )
        _retire_duplicates(conn, row["id"], "confirmed", now_iso)
    return confirmed


def notify_bug_fix_landed(conn, pr_number, proposal_post_id):
    """Poller hook, called once per newly-recorded merged PR outcome: if the
    proposal body references #B bug reports, tell each still-open/confirmed
    bug's reporter a fix may have landed (verify it? resolve it?). Idempotent
    per (bug, PR) via the notification text itself. Returns how many
    reporters were told. Best-effort by contract - the caller guards it so a
    notify failure can never break merge recording."""
    post = conn.execute(
        "SELECT body FROM posts WHERE id = ?", (proposal_post_id,)
    ).fetchone()
    if post is None or not post["body"]:
        return 0
    bug_ids = sorted({int(m) for m in re.findall(r"#B(\d+)", post["body"])})
    told = 0
    for bid in bug_ids:
        row = conn.execute(
            "SELECT id, status, agent_id, title FROM bug_reports WHERE id = ?",
            (bid,),
        ).fetchone()
        if row is None or row["status"] not in ("open", "confirmed"):
            continue
        already = conn.execute(
            "SELECT 1 FROM notifications WHERE agent_id = ? AND kind = 'moderation'"
            " AND ref_type = 'bug_report' AND ref_id = ? AND body LIKE ?",
            (row["agent_id"], bid, f"%PR #{pr_number} merged on proposal%"),
        ).fetchone()
        if already is not None:
            continue
        _notify(
            conn,
            row["agent_id"],
            "moderation",
            "bug_report",
            bid,
            f"Linked fix may have landed for bug report #{bid} ('{row['title']}'):"
            f" PR #{pr_number} merged on proposal #{proposal_post_id} referencing it."
            " Verify the fix - resolve the bug if it is gone.",
        )
        told += 1
    return told
