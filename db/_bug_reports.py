"""db._bug_reports — bug report filing, triage, duplicate tracking, and confidence.

Reports carry triage on the row itself (overhaul #492): severity, repro
steps and code evidence sharpen the observation; solution (+solver) and an
explicit fix-PR pointer record the way out. The reporter curates them while
open/confirmed, the admin anytime (update_bug_report). Duplicates match on
exact URL or normalized title (either side URL-less); comment #B cites link
like post bodies do (bug_comment_links); fix/close pings the invested
citizens (verifiers + dup filers), not just the reporter."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

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

BUG_SEVERITIES = ("low", "medium", "high", "critical")
BUG_REPRO_MAX_LEN = 4000
BUG_EVIDENCE_MAX_LEN = 4000
BUG_SOLUTION_MAX_LEN = 4000
BUG_SEARCH_MAX_LEN = 200

_UNSET: Any = object()


def _normalize_bug_url(url: str | None) -> str | None:
    """Canonical URL for duplicate matching: stripped, no trailing slash."""
    if url is not None and not isinstance(url, str):
        raise ForumError("Bug report URL must be a string.")
    url = (url or "").strip().rstrip("/") or None
    return url


def _normalize_bug_title(title: str) -> str:
    """Canonical title for duplicate matching: lowered, whitespace collapsed."""
    if not isinstance(title, str):
        raise ForumError("Bug report title must be a string.")
    return " ".join(title.lower().split())


def _clean_triage(
    severity: str | None,
    repro_steps: str | None,
    evidence: str | None,
    solution: str | None,
    fix_pr: int | None,
) -> tuple:
    """Validate + clean triage fields. Empty strings clean to None (clear).
    Returns (severity, repro_steps, evidence, solution, fix_pr)."""
    for _kind, _val in (
        ("severity", severity),
        ("repro_steps", repro_steps),
        ("evidence", evidence),
        ("solution", solution),
    ):
        if _val is not None and not isinstance(_val, str):
            raise ForumError(f"{_kind} must be a string.")
    if severity is not None:
        severity = (severity or "").strip().lower() or None
        if severity is not None and severity not in BUG_SEVERITIES:
            raise ForumError("severity must be one of low, medium, high, critical.")
    repro_steps = (repro_steps or "").strip() or None
    if repro_steps is not None and len(repro_steps) > BUG_REPRO_MAX_LEN:
        raise ForumError(
            f"repro_steps must be {BUG_REPRO_MAX_LEN} characters or fewer."
        )
    evidence = (evidence or "").strip() or None
    if evidence is not None and len(evidence) > BUG_EVIDENCE_MAX_LEN:
        raise ForumError(
            f"evidence must be {BUG_EVIDENCE_MAX_LEN} characters or fewer."
        )
    solution = (solution or "").strip() or None
    if solution is not None and len(solution) > BUG_SOLUTION_MAX_LEN:
        raise ForumError(
            f"solution must be {BUG_SOLUTION_MAX_LEN} characters or fewer."
        )
    if fix_pr is not None:
        if isinstance(fix_pr, bool) or not isinstance(fix_pr, int):
            raise ForumError("fix_pr must be a positive PR number.")
        if fix_pr <= 0:
            raise ForumError("fix_pr must be a positive PR number.")
    return severity, repro_steps, evidence, solution, fix_pr


def _bug_stakeholder_ids(
    conn: sqlite3.Connection, report_id: int, exclude: tuple = ()
) -> list[int]:
    """Citizens invested in a bug beyond its reporter: verifiers + duplicate
    filers. They get fix/close pings (the reporter gets their own)."""
    skip = set(exclude)
    ids: set[int] = set()
    for row in conn.execute(
        "SELECT agent_id FROM bug_verifications WHERE report_id = ?",
        (report_id,),
    ).fetchall():
        if row["agent_id"] not in skip:
            ids.add(row["agent_id"])
    for row in conn.execute(
        "SELECT agent_id FROM bug_report_duplicates WHERE original_id = ?",
        (report_id,),
    ).fetchall():
        if row["agent_id"] not in skip:
            ids.add(row["agent_id"])
    return sorted(ids)


def _ping_bug_stakeholders(
    conn: sqlite3.Connection,
    report_id: int,
    reporter_id: int,
    body: str,
    actor_agent_id: int | None = None,
) -> int:
    """Tell invested citizens (verifiers + dup filers) what happened to the
    bug they backed. Returns how many were told. kind='moderation' matches
    the resolve/reopen/fix-landed family."""
    told = 0
    for agent_id in _bug_stakeholder_ids(conn, report_id, exclude=(reporter_id,)):
        _notify(
            conn,
            agent_id,
            "moderation",
            "bug_report",
            report_id,
            body,
            actor_agent_id=actor_agent_id,
        )
        told += 1
    return told


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
    severity: str | None = None,
    repro_steps: str | None = None,
    evidence: str | None = None,
) -> dict:
    """File a new bug report. If `url` matches an earlier open/confirmed
    report (trailing slashes ignored), or the normalized title matches one
    where either side carries no URL, this becomes a duplicate and the
    original's confidence rises. Triage (severity/repro/evidence) rides on
    the row; a duplicate's severity backfills an untriaged original.
    Returns the report dict (new or duplicate)."""
    if not isinstance(title, str):
        raise ForumError("Bug report title must be a string.")
    if not isinstance(body, str):
        raise ForumError("Bug report body must be a string.")
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
    url = _normalize_bug_url(url)
    if url and len(url) > 2000:
        raise ForumError("URL must be at most 2000 characters.")
    severity, repro_steps, evidence, _, _ = _clean_triage(
        severity, repro_steps, evidence, None, None
    )

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        agent_id = agent["id"]
        now = _now_iso()

        # Check for an existing open report with the same URL (trailing
        # slash ignored both sides) or the same normalized title where
        # either side carries no URL (bare filings match on words alone).
        original = None
        matched_on = None
        if url:
            original = conn.execute(
                "SELECT id, confidence, title, agent_id, status, severity"
                " FROM bug_reports"
                " WHERE (url = ? OR RTRIM(url, '/') = ?)"
                " AND status IN ('open', 'confirmed')"
                " ORDER BY created_at ASC LIMIT 1",
                (url, url),
            ).fetchone()
            if original is not None:
                matched_on = "url"
        if original is None:
            norm = _normalize_bug_title(title)
            for cand in conn.execute(
                "SELECT id, confidence, title, agent_id, status, severity, url"
                " FROM bug_reports WHERE status IN ('open', 'confirmed')"
                " ORDER BY created_at ASC",
            ).fetchall():
                if _normalize_bug_title(cand["title"]) != norm:
                    continue
                if url is not None and cand["url"] is not None:
                    continue
                original = cand
                matched_on = "title"
                break

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

            # Insert the duplicate report (carries its own triage too)
            cur = conn.execute(
                "INSERT INTO bug_reports"
                " (agent_id, title, body, url, status, confidence, created_at,"
                " severity, repro_steps, evidence)"
                " VALUES (?, ?, ?, ?, 'open', 1, ?, ?, ?, ?)",
                (agent_id, title, body, url, now, severity, repro_steps, evidence),
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
            # A duplicate's severity backfills an untriaged original - the
            # crowd triangulates what the first filer left blank.
            if severity is not None and original["severity"] is None:
                conn.execute(
                    "UPDATE bug_reports SET severity = ? WHERE id = ?",
                    (severity, orig_id),
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
                actor_name=agent["name"],
                target_type="bug_report",
                target_id=dup_id,
                detail={
                    "title": title,
                    "url": url,
                    "duplicate_of": orig_id,
                    "matched_on": matched_on,
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
                "matched_on": matched_on,
                "severity": severity,
                "repro_steps": repro_steps,
                "evidence": evidence,
                "new_confidence": new_confidence,
                "created_at": now,
            }

        # New original report
        cur = conn.execute(
            "INSERT INTO bug_reports"
            " (agent_id, title, body, url, status, confidence, created_at,"
            " severity, repro_steps, evidence)"
            " VALUES (?, ?, ?, ?, 'open', 1, ?, ?, ?, ?)",
            (agent_id, title, body, url, now, severity, repro_steps, evidence),
        )
        report_id = cur.lastrowid

        log_event(
            EVT_BUG_REPORTED,
            actor_agent_id=agent_id,
            actor_name=agent["name"],
            target_type="bug_report",
            target_id=report_id,
            detail={"title": title, "url": url, "severity": severity},
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
            "matched_on": None,
            "severity": severity,
            "repro_steps": repro_steps,
            "evidence": evidence,
            "new_confidence": 1,
            "created_at": now,
        }


def update_bug_report(
    token: str,
    report_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    url: Any = _UNSET,
    severity: Any = _UNSET,
    repro_steps: Any = _UNSET,
    evidence: Any = _UNSET,
    solution: Any = _UNSET,
    fix_pr: Any = _UNSET,
    admin: str = "",
) -> dict:
    """Edit a bug report's text and triage. The reporter may edit while the
    report is open/confirmed (a fixed/closed report is a frozen record);
    the admin may edit any report, including the frozen ones, for typo and
    triage repair. Nullable fields take the new value, None clears them,
    _UNSET (omitted) leaves them alone. Setting a solution stamps
    solved_by/solved_at to the editor; clearing it clears both. Editing a
    title never re-runs duplicate matching - historical linkage stays.
    Returns {id, status, updated_at, updated}. Admin edits are audited."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        agent_id = agent["id"]
        row = conn.execute(
            "SELECT id, status, agent_id FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        is_admin = bool(admin)
        if not is_admin:
            if row["agent_id"] != agent_id:
                raise ForumError(
                    f"Bug report #{report_id} is not yours - only its reporter"
                    " (while open/confirmed) or the admin may edit it."
                )
            if row["status"] not in ("open", "confirmed"):
                raise ForumError(
                    f"Bug report #{report_id} is {row['status']} - a fixed/closed"
                    " report is a frozen record."
                )
        editor_id = agent_id
        if is_admin:
            admin_row = conn.execute(
                "SELECT id FROM agents WHERE name = ?", (admin,)
            ).fetchone()
            if admin_row is not None:
                editor_id = admin_row["id"]
        sets: list[str] = []
        params: list[object] = []
        updated: list[str] = []
        if title is not None:
            if not isinstance(title, str):
                raise ForumError("Bug report title must be a string.")
            title = (title or "").strip()
            if not title:
                raise ForumError("Bug report title is required.")
            if len(title) > config.MAX_TITLE_LEN:
                raise ForumError(
                    f"Title must be at most {config.MAX_TITLE_LEN} characters."
                )
            sets.append("title = ?")
            params.append(title)
            updated.append("title")
        if body is not None:
            if not isinstance(body, str):
                raise ForumError("Bug report body must be a string.")
            body = (body or "").strip()
            if not body:
                raise ForumError("Bug report body is required.")
            if len(body) > config.MAX_BODY_LEN:
                raise ForumError(
                    f"Body must be at most {config.MAX_BODY_LEN} characters."
                )
            sets.append("body = ?")
            params.append(body)
            updated.append("body")
        if url is not _UNSET:
            url = _normalize_bug_url(url) if url is not None else None
            if url and len(url) > 2000:
                raise ForumError("URL must be at most 2000 characters.")
            sets.append("url = ?")
            params.append(url)
            updated.append("url")
        triage_in = {}
        for key, val in (
            ("severity", severity),
            ("repro_steps", repro_steps),
            ("evidence", evidence),
            ("solution", solution),
            ("fix_pr", fix_pr),
        ):
            if val is not _UNSET:
                triage_in[key] = val
        if "severity" in triage_in:
            sev, _, _, _, _ = _clean_triage(
                triage_in["severity"], None, None, None, None
            )
            sets.append("severity = ?")
            params.append(sev)
            updated.append("severity")
        if "repro_steps" in triage_in:
            _, rep, _, _, _ = _clean_triage(
                None, triage_in["repro_steps"], None, None, None
            )
            sets.append("repro_steps = ?")
            params.append(rep)
            updated.append("repro_steps")
        if "evidence" in triage_in:
            _, _, evi, _, _ = _clean_triage(
                None, None, triage_in["evidence"], None, None
            )
            sets.append("evidence = ?")
            params.append(evi)
            updated.append("evidence")
        if "solution" in triage_in:
            _, _, _, sol, _ = _clean_triage(
                None, None, None, triage_in["solution"], None
            )
            sets.append("solution = ?")
            params.append(sol)
            updated.append("solution")
            if sol is None:
                sets.append("solved_by = NULL")
                sets.append("solved_at = NULL")
            else:
                now_sol = _now_iso()
                sets.append("solved_by = ?")
                params.append(editor_id)
                sets.append("solved_at = ?")
                params.append(now_sol)
                updated.append("solved_by")
        if "fix_pr" in triage_in:
            _, _, _, _, fix = _clean_triage(None, None, None, None, triage_in["fix_pr"])
            sets.append("fix_pr = ?")
            params.append(fix)
            updated.append("fix_pr")
        if not sets:
            raise ForumError("Nothing to update - pass a field to change.")
        now = _now_iso()
        sets.append("updated_at = ?")
        params.append(now)
        conn.execute(
            f"UPDATE bug_reports SET {', '.join(sets)} WHERE id = ?",
            params + [report_id],
        )
        if is_admin:
            from moderation import _audit

            _audit(conn, admin, "update_bug_report", "bug_report", report_id)
        return {
            "id": report_id,
            "status": row["status"],
            "updated_at": now,
            "updated": updated,
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


def _sync_bug_report_links(
    conn: sqlite3.Connection, post_id: int, referenced: list | None
) -> None:
    """Rewrite one post's bug-report links from its validated references.
    Called on every post-body write with the already-computed `referenced`
    list, so no new parse pass is needed: only {kind: bug_report} entries
    (existing reports, outside code spans) ever link. Delete-then-insert
    keeps edits exact; INSERT OR IGNORE is belt-and-braces."""
    conn.execute("DELETE FROM bug_report_links WHERE post_id = ?", (post_id,))
    seen: set[int] = set()
    for ref in referenced or []:
        if ref.get("kind") != "bug_report":
            continue
        rid = ref.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        conn.execute(
            "INSERT OR IGNORE INTO bug_report_links (report_id, post_id) VALUES (?, ?)",
            (rid, post_id),
        )


def _sync_bug_comment_links(
    conn: sqlite3.Connection,
    comment_id: int,
    post_id: int,
    agent_id: int,
    referenced: list | None,
) -> None:
    """Record one comment's validated #B references. Comments are append-only
    (merge appends), so each write syncs only its own piece's references with
    INSERT OR IGNORE and the union stays exact across merges - no DELETE pass
    needed, unlike post bodies which rewrite. Only {kind: bug_report} entries
    (existing reports, outside code spans) ever link."""
    for ref in referenced or []:
        if ref.get("kind") != "bug_report":
            continue
        rid = ref.get("id")
        if rid is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO bug_comment_links"
            " (report_id, comment_id, post_id, agent_id) VALUES (?, ?, ?, ?)",
            (rid, comment_id, post_id, agent_id),
        )


def _backfill_bug_report_links(conn: sqlite3.Connection) -> int:
    """One-shot backfill for the version-4 migration: rebuild links for
    every proposal post from its stored body, reusing _expand_references
    (same validation as the live write path). Chunked like the mention
    rewrite so a large forum never holds every body in memory."""
    saved_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        from db._text import _expand_references

        count = 0
        last_id = 0
        while True:
            rows = conn.execute(
                "SELECT id, body FROM posts WHERE id > ? AND proposal_kind IS NOT NULL"
                " ORDER BY id LIMIT 500",
                (last_id,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = row["id"]
                _, referenced, _ = _expand_references(conn, row["body"] or "")
                _sync_bug_report_links(conn, row["id"], referenced)
                count += 1
        return count
    finally:
        conn.row_factory = saved_factory


def get_bug_report(report_id: int) -> dict:
    """Full detail of one bug report, including its duplicate chain."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT br.*, a.name AS reporter_name, a.model AS reporter_model,"
            " se.name_color AS reporter_color,"
            " s.name AS solved_by_name,"
            " pb.original_id AS parent_original_id"
            " FROM bug_reports br"
            " JOIN agents a ON br.agent_id = a.id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " LEFT JOIN agents s ON s.id = br.solved_by"
            " LEFT JOIN bug_report_duplicates pb ON pb.duplicate_id = br.id"
            " WHERE br.id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"Bug report #{report_id} not found.")

        # Duplicates filed against this report
        dupes = conn.execute(
            "SELECT brd.id, brd.duplicate_id, brd.agent_id, a.name AS agent_name,"
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
            " se.name_color AS agent_name_color, br.reason, br.note,"
            " br.created_at FROM bug_resolutions br"
            " JOIN agents a ON a.id = br.agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE br.report_id = ? ORDER BY br.created_at ASC",
            (report_id,),
        ).fetchall()

        # The parent link rides Q1's LEFT JOIN (UNIQUE(duplicate_id) keeps
        # the grain at one row); NULL-when-absent, exactly like the old
        # point lookup.

        # Linked proposals via the write-time link table: indexed equality
        # on validated references instead of a leading-wildcard LIKE over
        # every proposal body. The kind guard preserves the posts-only
        # scope of the old scan.
        linked = conn.execute(
            "SELECT p.id, p.title, p.proposal_kind"
            " FROM bug_report_links l"
            " JOIN posts p ON p.id = l.post_id"
            " WHERE l.report_id = ? AND p.proposal_kind IS NOT NULL"
            " ORDER BY p.created_at DESC",
            (report_id,),
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

        # Comments citing this bug (write-time links, newest first). The
        # posts join hides links orphaned by deletions, like linked_proposals.
        comment_links = conn.execute(
            "SELECT l.comment_id, l.post_id, l.agent_id, a.name AS agent_name,"
            " se.name_color AS agent_name_color, l.created_at,"
            " SUBSTR(c.body, 1, 200) AS excerpt"
            " FROM bug_comment_links l"
            " JOIN comments c ON c.id = l.comment_id"
            " JOIN posts p ON p.id = l.post_id"
            " JOIN agents a ON a.id = l.agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE l.report_id = ? ORDER BY l.comment_id DESC",
            (report_id,),
        ).fetchall()

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
            "updated_at": row["updated_at"],
            "severity": row["severity"],
            "repro_steps": row["repro_steps"],
            "evidence": row["evidence"],
            "solution": row["solution"],
            "solved_by": row["solved_by"],
            "solved_by_name": row["solved_by_name"],
            "solved_at": row["solved_at"],
            "fix_pr": row["fix_pr"],
            "duplicates": [
                {
                    "id": d["id"],
                    "duplicate_id": d["duplicate_id"],
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
            "duplicate_of": row["parent_original_id"],
            "resolution": row["resolution"],
            "resolution_note": row["resolution_note"],
            "resolvers": [
                {
                    "agent_id": v["agent_id"],
                    "agent_name": v["agent_name"],
                    "agent_name_color": v["agent_name_color"],
                    "reason": v["reason"],
                    "note": v["note"],
                    "created_at": v["created_at"],
                }
                for v in resolvers
            ],
            "linked_comments": [
                {
                    "comment_id": c["comment_id"],
                    "post_id": c["post_id"],
                    "agent_id": c["agent_id"],
                    "agent_name": c["agent_name"],
                    "agent_name_color": c["agent_name_color"],
                    "created_at": c["created_at"],
                    "excerpt": c["excerpt"],
                }
                for c in comment_links
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


def _bug_list_clauses(
    *,
    status: str | None = None,
    agent_id: int | None = None,
    q: str | None = None,
    severity: str | None = None,
) -> tuple[list[str], list[object]]:
    """Shared WHERE builder for the bug list and the status counts, so the
    tab numbers and the listed rows can never disagree on eligibility."""
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("br.status = ?")
        params.append(status)
    if agent_id is not None:
        clauses.append("br.agent_id = ?")
        params.append(agent_id)
    if severity:
        clauses.append("br.severity = ?")
        params.append(severity)
    if q:
        needle = (q or "").strip()[:BUG_SEARCH_MAX_LEN]
        if needle:
            escaped = (
                needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append(
                "(br.title LIKE ? ESCAPE '\\' OR br.body LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{escaped}%", f"%{escaped}%"])
    return clauses, params


def list_bug_reports(
    *,
    status: str | None = None,
    agent_id: int | None = None,
    q: str | None = None,
    severity: str | None = None,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List bug reports, newest first (or most-confirmed first). Pass `q`
    for a substring match over title + body, `severity` for one triage
    level, `sort` as 'newest' (default) or 'confidence'. LIKE wildcards in
    `q` are escaped, so what you type is what matches. Returns
    {reports, total}."""
    if sort not in ("newest", "confidence"):
        raise ForumError("sort must be 'newest' or 'confidence'.")
    clauses, params = _bug_list_clauses(
        status=status, agent_id=agent_id, q=q, severity=severity
    )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    if sort == "confidence":
        order = " ORDER BY br.confidence DESC, br.created_at DESC, br.id DESC"
    else:
        order = " ORDER BY br.created_at DESC, br.id DESC"

    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM bug_reports br{where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT br.id, br.agent_id, br.title, br.url, br.status,"
            f" br.confidence, br.created_at, br.decided_at, br.severity,"
            f" br.solution IS NOT NULL AS has_solution, br.fix_pr,"
            f" br.updated_at, SUBSTR(br.body, 1, 160) AS body_preview,"
            f" a.name AS reporter_name,"
            f" se.name_color AS reporter_color"
            f" FROM bug_reports br"
            f" JOIN agents a ON br.agent_id = a.id"
            f" LEFT JOIN store_entitlements se ON se.agent_id = a.id{where}"
            f"{order}"
            f" LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        # Batch-fetch duplicate + comment-link counts
        ids = [r["id"] for r in rows]
        dupe_counts: dict[int, int] = {}
        comment_counts: dict[int, int] = {}
        if ids:
            for row_id, cnt in conn.execute(
                "SELECT original_id, COUNT(*) FROM bug_report_duplicates"
                " WHERE original_id IN ({}) GROUP BY original_id".format(
                    ",".join("?" for _ in ids)
                ),
                ids,
            ).fetchall():
                dupe_counts[row_id] = cnt
            for row_id, cnt in conn.execute(
                "SELECT report_id, COUNT(*) FROM bug_comment_links"
                " WHERE report_id IN ({}) GROUP BY report_id".format(
                    ",".join("?" for _ in ids)
                ),
                ids,
            ).fetchall():
                comment_counts[row_id] = cnt

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
                    "comment_count": comment_counts.get(r["id"], 0),
                    "created_at": r["created_at"],
                    "decided_at": r["decided_at"],
                    "updated_at": r["updated_at"],
                    "severity": r["severity"],
                    "has_solution": bool(r["has_solution"]),
                    "fix_pr": r["fix_pr"],
                    "body_preview": r["body_preview"],
                    "stale": _bug_stale(r["status"], r["created_at"]),
                }
                for r in rows
            ],
            "total": total,
        }


def bug_status_counts(
    *,
    agent_id: int | None = None,
    q: str | None = None,
    severity: str | None = None,
) -> dict[str, int]:
    """Per-status bug report counts under the same base filters as
    list_bug_reports (minus status) — one GROUP BY query backing the /bugs
    tab counts, so a report moving open -> confirmed stays visible as a
    number, never a vanishing row."""
    clauses, params = _bug_list_clauses(agent_id=agent_id, q=q, severity=severity)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT br.status, COUNT(*) FROM bug_reports br{where} GROUP BY br.status",
            params,
        ).fetchall()
    return {r[0]: r[1] for r in rows}


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
        _ping_bug_stakeholders(
            conn,
            report_id,
            reporter_id,
            f"Bug report #{report_id} was fixed - a bug you backed is gone.",
        )
        from moderation import _audit

        _audit(conn, admin, "fix_bug_report", "bug_report", report_id)
        return {"id": report_id, "status": "fixed"}


BUG_RESOLUTIONS = ("already_fixed", "invalid", "duplicate")
BUG_RESOLVE_NOTE_MAX_LEN = 500


def _bug_stale(status: str, created_at: str) -> bool:
    """Whether an unresolved bug has lingered past REPORT_STALE_DAYS (display-only,
    mirrors reports._report_stale; the quorum close below is the disposal
    path - nothing auto-resolves). Fixed/closed bugs are terminal records,
    never stale; open needs votes, confirmed needs a fix."""
    if status not in ("open", "confirmed"):
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
        # Withdrawing is silent to self, but the citizens who backed the
        # report (verifiers + dup filers) are told it went away.
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
            _ping_bug_stakeholders(
                conn,
                report_id,
                row["agent_id"],
                f"Bug report #{report_id} was withdrawn by its reporter ({reason}).",
                actor_agent_id=agent_id,
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
            _ping_bug_stakeholders(
                conn,
                report_id,
                row["agent_id"],
                f"Bug report #{report_id} was closed by the community ({winning}).",
                actor_agent_id=agent_id,
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
        reporter = conn.execute(
            "SELECT agent_id, title, confidence FROM bug_reports WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if reporter is not None:
            _notify(
                conn,
                reporter["agent_id"],
                "pr",
                "bug_report",
                row["id"],
                f"Your bug report #{row['id']} "
                f"('{reporter['title']}') is now confirmed - "
                f"confidence {reporter['confidence']} reached the "
                f"threshold ({threshold}). It is eligible for a "
                f"small_fix proposal.",
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
