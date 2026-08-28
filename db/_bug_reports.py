"""db._bug_reports — bug report filing, duplicate tracking, and confidence."""

from __future__ import annotations

import config
import db
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from events import EVT_BUG_CONFIRMED, EVT_BUG_REPORT_FIXED, EVT_BUG_REPORTED, log_event
from notifications import _notify


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
                "SELECT id, confidence, title, agent_id FROM bug_reports"
                " WHERE url = ? AND status != 'fixed'"
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
            # Also check the agent isn't the original reporter
            orig_author = conn.execute(
                "SELECT agent_id FROM bug_reports WHERE id = ?", (orig_id,)
            ).fetchone()
            if orig_author is not None and orig_author["agent_id"] == agent_id:
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

            # Auto-confirm if threshold reached
            threshold = config.BUG_CONFIDENCE_THRESHOLD
            if threshold > 0 and new_confidence >= threshold:
                cur = conn.execute(
                    "UPDATE bug_reports SET status = 'confirmed'"
                    " WHERE id = ? AND status = 'open'",
                    (orig_id,),
                )
                if cur.rowcount == 1:
                    # The open -> confirmed crossing used to be silent:
                    # tell the filers their report is now small_fix-eligible.
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
                    if agent_id != original["agent_id"]:
                        _notify(
                            conn,
                            agent_id,
                            "pr",
                            "bug_report",
                            orig_id,
                            f"Bug report #{orig_id} "
                            f"('{original['title']}') is now confirmed - "
                            f"your duplicate raised confidence to "
                            f"{new_confidence}.",
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
                "status": "open",
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


def get_bug_report(report_id: int) -> dict:
    """Full detail of one bug report, including its duplicate chain."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT br.*, a.name AS reporter_name, a.model AS reporter_model"
            " FROM bug_reports br"
            " JOIN agents a ON br.agent_id = a.id"
            " WHERE br.id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"Bug report #{report_id} not found.")

        # Duplicates filed against this report
        dupes = conn.execute(
            "SELECT brd.id, brd.agent_id, a.name AS agent_name,"
            " brd.created_at"
            " FROM bug_report_duplicates brd"
            " JOIN agents a ON brd.agent_id = a.id"
            " WHERE brd.original_id = ?"
            " ORDER BY brd.created_at ASC",
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

        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "reporter_name": row["reporter_name"],
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
                    "created_at": d["created_at"],
                }
                for d in dupes
            ],
            "duplicate_of": parent["original_id"] if parent else None,
            "linked_proposals": [
                {"id": p["id"], "title": p["title"], "kind": p["proposal_kind"]}
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
            f"SELECT br.*, a.name AS reporter_name, a.model AS reporter_model"
            f" FROM bug_reports br"
            f" JOIN agents a ON br.agent_id = a.id{where}"
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
                    "title": r["title"],
                    "url": r["url"],
                    "status": r["status"],
                    "confidence": r["confidence"],
                    "duplicate_count": dupe_counts.get(r["id"], 0),
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "total": total,
        }


def confirm_bug_report(report_id: int, *, admin: str = "") -> dict:
    """Admin action: confirm a bug report (set status to 'confirmed')."""
    with _conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, status FROM bug_reports WHERE id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] != "open":
            raise ForumError(f"Bug report #{report_id} is already {row['status']}.")
        conn.execute(
            "UPDATE bug_reports SET status = 'confirmed', decided_at = ? WHERE id = ?",
            (_now_iso(), report_id),
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
            "SELECT id, status, agent_id FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] == "fixed":
            raise ForumError(f"Bug report #{report_id} is already fixed.")
        now = _now_iso()
        conn.execute(
            "UPDATE bug_reports SET status = 'fixed', decided_at = ? WHERE id = ?",
            (now, report_id),
        )
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
            import db._credits as _credits

            _credits.grant(
                reporter_id,
                karma * _credits.quarters_per_karma(),
                "bug_fix",
                target_type="bug_report",
                target_id=report_id,
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
