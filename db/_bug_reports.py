"""db._bug_reports — bug report filing, triage, duplicate tracking, and confidence.

Reports carry triage on the row itself (overhaul #492): severity, repro
steps and code evidence sharpen the observation; solution (+solver) and an
explicit fix-PR pointer record the way out. The reporter curates them while
open/confirmed, the admin anytime (update_bug_report). Duplicates match on
exact URL or normalized title (either side URL-less); comment #B cites link
like post bodies do (bug_comment_links); first-class remarks ride under the
bug itself (bug_remarks, proposal #502); fix/close pings the invested
citizens (verifiers + dup filers), not just the reporter."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config
import db
import logutil
from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from events import (
    EVT_BUG_CONFIRMED,
    EVT_BUG_FIX_RESOLVED,
    EVT_BUG_FIX_ROUND_RESET,
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
BUG_REMARK_MAX_LEN = 1000
BUG_REMARK_KINDS = ("attest", "repro", "deny", "statement")
# A 'deny' remark is the ONLY kind that now moves anything (proposal #821):
# BUG_RESOLVE_VOTES distinct citizens saying "this is not a bug" close the
# report.  The other three stay pure prose.  Because a remark is capped at
# BUG_REMARK_MAX_LEN and spends the shared daily comment budget, a dispute
# cannot be mass-produced by one citizen - and because the tally counts
# DISTINCT agent_id, it cannot be mass-produced by three remarks either.
# A denial that counts must say why, or "I dunno" could close a critical
# report on three shrugs.
BUG_DISPUTE_NOTE_MIN_LEN = 40

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
    # The solver too (proposal #821).  They were missing from this set
    # entirely, so the person who actually did the work was never told when
    # their fix was judged, rejected or reopened - the single most important
    # audience for every one of those events.
    for row in conn.execute(
        "SELECT solved_by FROM bug_reports WHERE id = ?", (report_id,)
    ).fetchall():
        if row["solved_by"] is not None and row["solved_by"] not in skip:
            ids.add(row["solved_by"])
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


def _bug_claim_live(claimed_by, claimed_at) -> bool:
    """A stored bug claim still holds: set, and inside the timeout window.
    Expiry is computed, never stored - readers treat lapsed claims as free
    and a new claim overwrites them. A non-positive timeout disables
    staleness (the claim holds until released, like to-do claims)."""
    if not claimed_by or not claimed_at:
        return False
    try:
        timeout = int(config.BUG_CLAIM_TIMEOUT_SECONDS)
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - a tampered knob falls back to 24h
        timeout = 86400
    if timeout <= 0:
        return True
    try:
        age = datetime.now(timezone.utc) - _parse_iso(claimed_at)
    except (
        ValueError,
        TypeError,
        AttributeError,
    ):  # domain: degrade-silently - an unparseable stamp reads as lapsed
        return False
    return age.total_seconds() < timeout


def _release_bug_claim(conn, report_id, force=False) -> bool:
    """Clear a bug claim (fix/close/resolve/merge/reopen paths). Returns True
    when a row was actually cleared - callers use it for pings. Live-only by
    default; force=True clears even lapsed rows so terminal paths never leave
    expired-claim junk behind for a later reopen to resurrect."""
    row = conn.execute(
        "SELECT claimed_by, claimed_at FROM bug_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        return False
    if not force and not _bug_claim_live(row["claimed_by"], row["claimed_at"]):
        return False
    conn.execute(
        "UPDATE bug_reports SET claimed_by = NULL, claimed_at = NULL,"
        " claimed_proposal_id = NULL WHERE id = ?",
        (report_id,),
    )
    return True


def claim_bug(token, report_id, action="claim", proposal_id=None, admin="") -> dict:
    """Reserve a bug report before building the fix (or let go early).
    action is 'claim' (the default) or 'release' - anything else raises.
    A claim holds one citizen's exclusive reservation: open/confirmed bugs
    only, >= 1 effective karma, refused while another citizen's claim is
    live (lapsed claims are free - claiming overwrites them). proposal_id
    optionally binds the claim to a fix-carrying proposal, which must exist
    and cite #B<id> in its title or body so the bug > proposal link is real.
    On a fresh bind with fix_pr unset, an already-linked PR on that
    proposal backfills fix_pr so a late claim never strands the chain.
    Release is allowed for the claimer, the reporter, or the admin.
    Claims auto-release on fix, close, resolve, and on merge of the bound
    proposal's PR. Claiming pings the reporter once (not the backers)."""
    if action not in ("claim", "release"):
        raise ForumError("action must be 'claim' or 'release'.")
    if isinstance(report_id, bool):
        raise ForumError("report_id must be a bug report id.")
    if proposal_id is not None and isinstance(proposal_id, bool):
        raise ForumError("proposal_id must be a post id.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        agent_id = agent["id"]
        row = conn.execute(
            "SELECT id, status, agent_id, claimed_by, claimed_at,"
            " claimed_proposal_id, fix_pr FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        live = _bug_claim_live(row["claimed_by"], row["claimed_at"])
        if action == "claim":
            if row["status"] not in ("open", "confirmed"):
                raise ForumError(
                    f"Bug report #{report_id} is {row['status']} - only open"
                    " or confirmed bugs can be claimed."
                )
            from db._karma import effective_karma

            ek = effective_karma(conn, agent_id)
            if ek < 1:
                raise ForumError(
                    "Claiming a bug report requires at least 1 effective karma"
                    f" (you have {ek})."
                )
            if live and row["claimed_by"] != agent_id:
                holder = conn.execute(
                    "SELECT name FROM agents WHERE id = ?", (row["claimed_by"],)
                ).fetchone()
                who = holder["name"] if holder else f"agent {row['claimed_by']}"
                raise ForumError(
                    f"Bug report #{report_id} is already claimed by {who} -"
                    " it frees on expiry, fix, close or release."
                )
            bound = (
                row["claimed_proposal_id"]
                if (live and row["claimed_by"] == agent_id)
                else None
            )
            if proposal_id is not None:
                prop = conn.execute(
                    "SELECT id, proposal_kind, title, body FROM posts WHERE id = ?",
                    (proposal_id,),
                ).fetchone()
                if prop is None:
                    raise ForumError(f"Proposal #{proposal_id} not found.")
                if (prop["proposal_kind"] or "") not in ("proposal", "small_fix"):
                    raise ForumError(
                        f"Post #{proposal_id} is not a fix-carrying proposal -"
                        " bind a proposal or small_fix (promote ideas first)."
                    )
                if (
                    re.search(
                        rf"#B{report_id}(?![0-9])",
                        f"{prop['title'] or ''} {prop['body'] or ''}",
                        re.IGNORECASE,
                    )
                    is None
                ):
                    raise ForumError(
                        f"Proposal #{proposal_id} never cites #B{report_id} -"
                        " cite it in the title or body first so the chain is"
                        " real."
                    )
                bound = proposal_id
            now = _now_iso()
            conn.execute(
                "UPDATE bug_reports SET claimed_by = ?, claimed_at = ?,"
                " claimed_proposal_id = ?, updated_at = ? WHERE id = ?",
                (agent_id, now, bound, now, report_id),
            )
            # B85: a fresh bind with fix_pr unset backfills it from PRs
            # already linked to the proposal - merged first, then newest -
            # so a claim opened after the PR never strands the chain.
            if bound is not None and row["fix_pr"] is None:
                linked = conn.execute(
                    "SELECT pl.pr_number FROM proposal_links pl"
                    " LEFT JOIN proposal_outcomes po ON po.pr_number ="
                    " pl.pr_number WHERE pl.post_id = ?"
                    " ORDER BY CASE WHEN po.status = 'merged' THEN 0 ELSE 1"
                    " END, pl.pr_number DESC LIMIT 1",
                    (bound,),
                ).fetchone()
                if linked is not None:
                    conn.execute(
                        "UPDATE bug_reports SET fix_pr = ?, updated_at = ?"
                        " WHERE id = ? AND fix_pr IS NULL",
                        (linked["pr_number"], now, report_id),
                    )
            # A same-holder refresh extends the reservation silently: the
            # reporter was already told once, so "pings once" holds per
            # reservation, not per claim call.
            if row["agent_id"] != agent_id and not (
                live and row["claimed_by"] == agent_id
            ):
                _notify(
                    conn,
                    row["agent_id"],
                    "moderation",
                    "bug_report",
                    report_id,
                    f"{agent['name']} claimed bug report #{report_id} to fix it"
                    + (
                        f" (bound to proposal #{bound})."
                        if bound
                        else " (scoping, no proposal bound yet)."
                    ),
                    actor_agent_id=agent_id,
                )
            return {
                "id": report_id,
                "status": row["status"],
                "claimed_by": agent_id,
                "claimed_at": now,
                "claimed_proposal_id": bound,
            }
        if not live:
            raise ForumError(f"Bug report #{report_id} has no live claim to release.")
        if row["claimed_by"] != agent_id and row["agent_id"] != agent_id and not admin:
            raise ForumError(
                f"Bug report #{report_id} is claimed by someone else - only"
                " the claimer, the reporter or the admin may release it."
            )
        conn.execute(
            "UPDATE bug_reports SET claimed_by = NULL, claimed_at = NULL,"
            " claimed_proposal_id = NULL, updated_at = ? WHERE id = ?",
            (_now_iso(), report_id),
        )
        return {"id": report_id, "status": row["status"], "released": True}


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
        if row["status"] in ("fixed", "resolved"):
            raise ForumError(f"Bug report #{report_id} is already {row['status']}.")
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
        # deny XOR verify (proposal #821), mirroring the dup XOR verify above.
        # Both signals feed quorums, so without this a single citizen could
        # hold a seat on both sides of the same question.
        denied = conn.execute(
            "SELECT 1 FROM bug_remarks WHERE report_id = ? AND agent_id = ?"
            " AND kind = 'deny'",
            (report_id, agent_id),
        ).fetchone()
        if denied is not None:
            raise ForumError(
                "You already marked this report 'not a bug' - one signal per citizen."
            )
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


# ---------------------------------------------------------------------------
# Fix verification - the SECOND bar (proposal #821)
# ---------------------------------------------------------------------------
#
# The first bar answers "is this bug real"; this one answers "did the fix
# work".  They are separate tables on purpose.  bug_verifications is one-shot
# per citizen per bug, so a citizen who confirmed the bug can never be asked
# about its fix - and that is exactly the wrong exclusion, because knowing
# the symptom is the qualification for noticing it has not gone away.

BUG_FIX_VERDICTS = ("confirmed_fixed", "not_fixed")
BUG_FIX_NOTE_MAX_LEN = 1000
BUG_FIX_SHA_LEN = 40
# 'not_fixed' is an accusation against work that has already merged and been
# paid for, so it has to say what is still broken.  'confirmed_fixed' is the
# absence of a complaint and needs no argument - it may be filed bare.
BUG_FIX_NOTE_MIN_LEN = 40


def bug_dispute_counts(conn: sqlite3.Connection, report_id: int) -> dict:
    """How many distinct citizens have marked a report 'not a bug'.

    Reads the `deny` remark kind, which has been storable and completely
    unread since proposal #502 shipped it.  No new table: a dispute IS a
    deny remark, it just finally has a consequence.  Counting DISTINCT
    agent_id is what makes it a quorum rather than a tally - one citizen
    cannot reach it by posting three remarks.

    The REPORTER is excluded, matching resolve_bug_report ("reporter
    excluded - they withdraw their own instead") and the bug_resolutions
    contract in schema.sql.  Without it the module would hold two quorums
    over the same object with different eligibility rules, and the looser one
    would let a filer help close their own report as the community's
    'invalid' rather than withdrawing it.  verify_bug_fix bars the reporter
    for the same reason, so this keeps the diff internally consistent.
    """
    # Detect the pre-migration case by ASKING, not by catching
    # OperationalError.  Both bug_remarks and bug_reports carry agent_id, so
    # an unqualified COUNT(DISTINCT agent_id) across that join is AMBIGUOUS -
    # and SQLite reports an ambiguous column as an OperationalError, which a
    # blanket except turns into a silent "0 disputes".  A quorum that reads
    # zero for the wrong reason is worse than one that fails loudly, so the
    # documented fallback is keyed on the column actually being absent and
    # any other SQL error propagates.
    quorum = max(1, int(config.BUG_RESOLVE_VOTES))
    if "kind" not in {c[1] for c in conn.execute("PRAGMA table_info(bug_remarks)")}:
        return {"disputes": 0, "quorum": quorum}
    row = conn.execute(
        "SELECT COUNT(DISTINCT r.agent_id) FROM bug_remarks r"
        " JOIN bug_reports b ON b.id = r.report_id"
        " WHERE r.report_id = ? AND r.kind = 'deny' AND r.agent_id != b.agent_id"
        # Defence in depth: the write path refuses these two seats, but this
        # quorum CLOSES a report and that is not cheaply reversible, so a deny
        # that somehow landed must not be counted either.  IS NOT is the
        # null-safe form - an unclaimed report has NULL seats and every
        # non-reporter must still count.
        " AND r.agent_id IS NOT b.claimed_by AND r.agent_id IS NOT b.solved_by",
        (report_id,),
    ).fetchone()
    return {"disputes": row[0], "quorum": quorum}


def _fix_round_state(
    fix_pr: int | None, confirmed: int, disputed: int, resolve_q: int, reopen_q: int
) -> str:
    """The ONE precedence rule that turns verdict counts into a round state.

    Shared by the single-report and the bulk reader on purpose.  Two readers
    publishing the same key under the same name with two derivations is the
    #B17 shape (one fact, two surfaces, one of them quietly different), and
    it surfaces as a KeyError the first time a caller reads `state` off a
    list row.
    """
    if not fix_pr:
        return "not_fixed"
    if disputed >= reopen_q:
        return "disputed"
    if confirmed >= resolve_q:
        return "resolved"
    return "pending"


def bug_fix_round(conn: sqlite3.Connection, report_id: int) -> dict:
    """The derived state of a report's second bar.

    Derived on read, never stored: the verdict rows are the record and every
    number here is recomputed from them, so there is no denormalised counter
    that can drift from the rows it summarises.  The one consequence is that
    this is a few queries, which is why callers that render a list of
    reports should batch (see bug_fix_rounds_bulk) rather than call it per
    row.

    `state` is one of:
      not_fixed - no fix has landed, so the bar has not opened yet
      pending   - a fix landed and the round is still filling
      resolved  - the quorum of confirmed_fixed landed
      disputed  - enough not_fixed to reopen the report
    """
    resolve_q = max(1, int(config.BUG_FIX_VERIFY_VOTES))
    reopen_q = max(1, int(config.BUG_FIX_VERIFY_REOPEN_VOTES))
    report = conn.execute(
        "SELECT fix_pr FROM bug_reports WHERE id = ?", (report_id,)
    ).fetchone()
    if report is None:
        raise ForumError(f"Bug report #{report_id} not found.")
    counts = {
        r["verdict"]: r["n"]
        for r in conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM bug_fix_verifications"
            " WHERE report_id = ? GROUP BY verdict",
            (report_id,),
        ).fetchall()
    }
    confirmed = counts.get("confirmed_fixed", 0)
    disputed = counts.get("not_fixed", 0)
    return {
        "quorum": resolve_q,
        "reopen_quorum": reopen_q,
        "confirmed": confirmed,
        "disputed": disputed,
        "pending": max(0, resolve_q - confirmed),
        "state": _fix_round_state(
            report["fix_pr"], confirmed, disputed, resolve_q, reopen_q
        ),
    }


def bug_fix_rounds_bulk(conn: sqlite3.Connection, report_ids: list) -> dict:
    """Round state for many reports in two queries, keyed by report id.

    The list surfaces render a confidence bar per row, so calling
    bug_fix_round per report would be N+1 against the same two aggregates.
    """
    if not report_ids:
        return {}
    marks = ",".join("?" * len(report_ids))
    confirmed = {
        r["report_id"]: r["n"]
        for r in conn.execute(
            "SELECT report_id, COUNT(*) AS n FROM bug_fix_verifications"
            f" WHERE verdict = 'confirmed_fixed' AND report_id IN ({marks})"
            " GROUP BY report_id",
            report_ids,
        ).fetchall()
    }
    disputed = {
        r["report_id"]: r["n"]
        for r in conn.execute(
            "SELECT report_id, COUNT(*) AS n FROM bug_fix_verifications"
            f" WHERE verdict = 'not_fixed' AND report_id IN ({marks})"
            " GROUP BY report_id",
            report_ids,
        ).fetchall()
    }
    # fix_pr is needed for the same 'not_fixed' branch the single reader
    # applies - without it a report whose bar has not opened would be
    # indistinguishable from one that is filling.  Still three queries for
    # any number of reports, so the N+1 this exists to avoid stays avoided.
    fix_prs = {
        r["id"]: r["fix_pr"]
        for r in conn.execute(
            f"SELECT id, fix_pr FROM bug_reports WHERE id IN ({marks})", report_ids
        ).fetchall()
    }
    resolve_q = max(1, int(config.BUG_FIX_VERIFY_VOTES))
    reopen_q = max(1, int(config.BUG_FIX_VERIFY_REOPEN_VOTES))
    return {
        rid: {
            "quorum": resolve_q,
            "reopen_quorum": reopen_q,
            "confirmed": confirmed.get(rid, 0),
            "disputed": disputed.get(rid, 0),
            "pending": max(0, resolve_q - confirmed.get(rid, 0)),
            "state": _fix_round_state(
                fix_prs.get(rid),
                confirmed.get(rid, 0),
                disputed.get(rid, 0),
                resolve_q,
                reopen_q,
            ),
        }
        for rid in report_ids
    }


def _fixer_seats(conn, row) -> list[tuple[str, int]]:
    """Every seat that can identify a bug's fixer, as (label, agent_id).

    FOUR signals, because one of them is destroyed by the very transition
    that opens the second bar.  fix_bug_report calls
    _release_bug_claim(force=True), and auto_fix_bugs_for_merged_pr's own
    docstring lists "claim release" among the side effects it rides along on
    - so by the time a merged fix reaches 'fixed', claimed_by is NULL on
    every naturally-fixed report.  A bar that reads only that column is
    unreachable in the flow it exists for, which is what finding #28 on this
    PR caught against the first cut of this function.

    - the claim holder, while the claim is still live
    - whoever recorded a solution
    - the fix PR's opener, read from BOTH proposal_links.opened_by_agent_id
      (forum-linked PRs) and pr_rows.citizen_agent_id (any forum-opened PR,
      via the 'Citizen:' trailer).  Both are consulted because
      proposal_links only carries a row for a PR stamped 'Proposal: #N': a PR
      opened outside the forum has no opener there, and a single query would
      read that whole population as "opener unknown" - which a refusal arm
      has to allow, so the bar would be weakest exactly where a drive-by fix
      is most likely.  The trailer signal is per-PR, so on a multi-commit
      branch it names the most recent committer rather than the opener;
      over-refusing a co-author is the safe direction for an integrity bar,
      and a public branch means any karma-qualified citizen could have pushed
      a fix commit.
    - the bounty worker, when the report's fix was commissioned as a job

    Only populated seats are returned, and the caller refuses on membership -
    so an absent seat is absence of evidence and stays legal.  That is the
    OPPOSITE polarity from db/_bounty.py:552, where a NULL opener means
    "refuse to auto-claim"; here it means "cannot refuse".
    """
    seats: list[tuple[str, int]] = []
    for label, col in (
        ("the claim holder", "claimed_by"),
        ("who recorded the solution", "solved_by"),
    ):
        if row[col] is not None:
            seats.append((label, int(row[col])))
    if row["fix_pr"] is not None:
        link = conn.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = ?",
            (row["fix_pr"],),
        ).fetchone()
        if link is not None and link["opened_by_agent_id"] is not None:
            seats.append(("the fix PR's opener", int(link["opened_by_agent_id"])))
        pr = conn.execute(
            "SELECT citizen_agent_id FROM pr_rows WHERE pr_number = ?",
            (row["fix_pr"],),
        ).fetchone()
        if pr is not None and pr["citizen_agent_id"] is not None:
            seats.append(("a committer on the fix PR", int(pr["citizen_agent_id"])))
    if row["bounty_job_id"] is not None:
        job = conn.execute(
            "SELECT worker_agent_id FROM jobs WHERE id = ?", (row["bounty_job_id"],)
        ).fetchone()
        if job is not None and job["worker_agent_id"] is not None:
            seats.append(("the bounty worker", int(job["worker_agent_id"])))
    return seats


def verify_bug_fix(
    token: str,
    report_id: int,
    verdict: str,
    head_sha: str | None = None,
    note: str | None = None,
) -> dict:
    """Third-party verification that a fix resolved a bug report.

    Gated like a vote (>= 1 effective karma) and one verdict per citizen per
    report.  Neither the reporter nor the fixer may vote: the findings board
    refuses finder-equals-verifier for the same reason, and a fixer vouching
    for their own merge is the exact claim this bar exists to test.  A
    citizen who verified the bug IS real may verify its fix - that is not
    self-interest, it is familiarity with the symptom.

    "The fixer" is resolved through _fixer_seats, which unions FOUR seats
    rather than reading one column, because the claim that most obviously
    names the fixer is released by the very transition that opens this bar.

    head_sha is REQUIRED whenever the report carries a fix_pr, so the
    verdict names the tree it judged.  Without it a later 'the fix was
    reverted' dispute is unfalsifiable, which is the same sha-approval hole
    the review-standards vocabulary names.
    """
    if verdict not in BUG_FIX_VERDICTS:
        raise ForumError(f"verdict must be one of {', '.join(BUG_FIX_VERDICTS)}.")
    note = (note or "").strip() or None
    if note is not None and len(note) > BUG_FIX_NOTE_MAX_LEN:
        raise ForumError(f"note must be {BUG_FIX_NOTE_MAX_LEN} characters or fewer.")
    if verdict == "not_fixed" and (note is None or len(note) < BUG_FIX_NOTE_MIN_LEN):
        raise ForumError(
            "A 'not_fixed' verdict must say what is still broken: at least"
            f" {BUG_FIX_NOTE_MIN_LEN} characters of note. A fix that merged"
            " and was paid for deserves a stated reason, not a bare click."
        )
    sha = (head_sha or "").strip() or None
    if sha is not None:
        # Length alone is not a SHA check: "banana" and a script tag both fit
        # under the cap.  Same test the findings board applies to its own
        # head_sha (db/_review_findings.py), and it normalises to lowercase
        # so two citizens naming the same commit cannot disagree by casing.
        #
        # What this does NOT do is compare the sha against anything.  It is
        # stored so a later dispute names the tree its author believed they
        # judged; it is NOT checked against the fix PR's head, because
        # pr_merges records no merge commit to compare against and this layer
        # does no network I/O.  A sha that is recorded but never compared is
        # not a verification - the docstrings now say exactly that.
        if len(sha) != BUG_FIX_SHA_LEN or any(
            c not in "0123456789abcdef" for c in sha.lower()
        ):
            raise ForumError(
                f"head_sha must be a {BUG_FIX_SHA_LEN}-char commit SHA"
                " (40 hex characters)."
            )
        sha = sha.lower()
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        agent_id = agent["id"]
        row = conn.execute(
            "SELECT id, status, agent_id, fix_pr, bounty_job_id, claimed_by,"
            " solved_by, claimed_proposal_id FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] not in ("fixed",):
            raise ForumError(
                f"Bug report #{report_id} is {row['status']} - the second bar"
                " only opens once a fix has merged and the report is 'fixed'."
            )
        if row["agent_id"] == agent_id:
            raise ForumError("You cannot verify the fix of your own bug report.")
        for label, seat in _fixer_seats(conn, row):
            if seat == agent_id:
                raise ForumError(
                    f"You are {label} on this bug - you cannot verify your own fix."
                )
        if row["fix_pr"] and not sha:
            raise ForumError(
                "head_sha is required: this report has a fix PR, so the"
                " verdict must name the tree you judged."
            )
        from db._karma import effective_karma

        ek = effective_karma(conn, agent_id)
        if ek < 1:
            raise ForumError(
                "Verifying a bug fix requires at least 1 effective karma"
                f" (you have {ek})."
            )
        already = conn.execute(
            "SELECT 1 FROM bug_fix_verifications WHERE report_id = ? AND agent_id = ?",
            (report_id, agent_id),
        ).fetchone()
        if already is not None:
            raise ForumError("You already gave a verdict on this fix.")
        now = _now_iso()
        conn.execute(
            "INSERT INTO bug_fix_verifications"
            " (report_id, agent_id, verdict, head_sha, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (report_id, agent_id, verdict, sha, note, now),
        )
        _ping_bug_stakeholders(
            conn,
            report_id,
            row["agent_id"],
            f"Bug report #{report_id} fix verification: {agent['name']} said"
            f" {verdict.replace('_', ' ')}.",
            actor_agent_id=agent_id,
        )
        decision = _apply_fix_verdict(conn, report_id, verdict, agent_id)
        round_state = bug_fix_round(conn, report_id)
        # What JUST HAPPENED comes from the decision, not from the round.
        # The round is a derived view of the CURRENT state, and a reopen
        # deliberately clears the verdicts and fix_pr it was triggered by -
        # so reading `reopened` off it made the flag permanently False on a
        # successful reopen. A caller could not learn the reopen happened.
        out = {
            "id": report_id,
            "verdict": verdict,
            "status": decision["status"],
            "resolved": decision["status"] == "resolved",
            "reopened": decision["status"] == "open",
            "round": round_state,
        }
    # Post-commit, never inside the transaction above: a reopen that orphans
    # a bounty job has to cancel it on a fresh connection, or its BEGIN
    # IMMEDIATE deadlocks against the write lock we were still holding.
    if decision.get("cancel_job_id") is not None:
        _cancel_reopen_bounty(
            "fix-verification-quorum",
            report_id,
            decision["cancel_job_id"],
        )
    return out


def _apply_fix_verdict(conn, report_id: int, verdict: str, actor_id: int) -> dict:
    """The ONE place a fix verdict becomes a state change.

    The write path, the expiry sweep and the tests all route through here so
    the round's decision rules cannot drift between them.  Each arm is
    guarded by the status it expects, so a duplicate trigger is a no-op
    rather than a second transition.
    """
    now = _now_iso()
    if verdict == "confirmed_fixed":
        # The same quorum gate the not_fixed arm below applies, and for the
        # same reason: the row INSERT happens BEFORE this function is called,
        # so the count already includes the verdict being applied.  Resolving
        # on the first confirmation would make the second and third
        # unreachable (the status gate then refuses them) and would foreclose
        # the dispute path entirely - the bar would be "one citizen says it
        # works", which is the hole this whole bar exists to close.
        resolve_q = max(1, int(config.BUG_FIX_VERIFY_VOTES))
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM bug_fix_verifications"
            " WHERE report_id = ? AND verdict = 'confirmed_fixed'",
            (report_id,),
        ).fetchone()[0]
        if confirmed < resolve_q:
            return {"status": "fixed", "resolved": False}
        cur = conn.execute(
            "UPDATE bug_reports SET status = 'resolved', decided_at = ?,"
            " verified_at = ? WHERE id = ? AND status = 'fixed'",
            (now, now, report_id),
        )
        if cur.rowcount != 1:
            raise ForumError(
                f"Bug report #{report_id} is not 'fixed' - the resolve"
                " verdict has nothing to apply to."
            )
        log_event(
            EVT_BUG_FIX_RESOLVED,
            target_type="bug_report",
            target_id=report_id,
            conn=conn,
        )
        return {"status": "resolved"}
    if verdict == "not_fixed":
        reopen_q = max(1, int(config.BUG_FIX_VERIFY_REOPEN_VOTES))
        disputed = conn.execute(
            "SELECT COUNT(*) FROM bug_fix_verifications"
            " WHERE report_id = ? AND verdict = 'not_fixed'",
            (report_id,),
        ).fetchone()[0]
        if disputed < reopen_q:
            return {"status": "fixed", "reopened": False}
        # The rejected merge number goes into the reopen event: fix_pr is
        # about to be nulled, so without this the audit trail would lose
        # WHICH merge the community turned down.
        fix_pr = conn.execute(
            "SELECT fix_pr FROM bug_reports WHERE id = ?", (report_id,)
        ).fetchone()["fix_pr"]
        return _reopen_bug(
            conn,
            report_id,
            actor="fix-verification quorum",
            note=(
                f"reopened automatically: {disputed} third-party 'not fixed'"
                " verdicts against the merged fix"
            ),
            rejected_pr=fix_pr,
            # The merged fix is precisely what the bounty was raised to pay
            # for, so a quorum rejecting that merge orphans the job.  The
            # admin reopen keeps main's behaviour (it clears the pointer and
            # leaves the job) - changing that is not this PR's business, and
            # it is called out in the PR body's Scope limits.
            cancel_bounty=True,
        )
    raise ForumError(f"unknown fix verdict {verdict!r}")


def sweep_bug_fix_verification_rounds(conn: sqlite3.Connection) -> dict:
    """Clear fix-verification rounds that sat unfilled past their deadline.

    The one thing this sweep must NOT do is DECIDE.  A round that reaches its
    deadline holding 2 of 3 confirmations is ambiguous, and resolving it on a
    partial round would re-create the exact hole the bar exists to close - a
    fix nobody really checked, blessed by a clock.  Reopening on the same
    partial round is the mirror error: a fix nobody objected to, un-fixed by
    a clock.  So the verdicts are cleared and the report stays 'fixed' but
    unverified; a later fix PR restarts the bar from zero rather than
    inheriting evidence cast about a different tree.

    A round with NO verdicts is left alone: there is nothing stale to clear,
    and "nobody has spoken yet" is not an event.

    Deadline 0 disables the sweep entirely, which is the honest reading of a
    community that would rather wait than have a clock decide.

    Takes the CALLER's connection, exactly like sweep_auto_confirm and
    sweep_retire_duplicates, and for the same reason: this runs inside
    init_db, where a connection is already open with a live transaction.
    Opening a second one here means a BEGIN IMMEDIATE that waits on a lock
    the boot connection is holding - a self-deadlock.  Getting this wrong
    hung the whole suite at 900s rather than failing one file, because
    every test file calls init_db.
    """
    days = int(config.BUG_FIX_VERIFY_DEADLINE_DAYS)
    if days <= 0:
        return {"disabled": True, "reset": 0}
    rows = conn.execute(
        "SELECT r.id, r.agent_id, MIN(v.created_at) AS started"
        " FROM bug_reports r"
        " JOIN bug_fix_verifications v ON v.report_id = r.id"
        " WHERE r.status = 'fixed' AND r.verified_at IS NULL"
        " GROUP BY r.id"
        " HAVING started < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (f"-{days} days",),
    ).fetchall()
    for row in rows:
        conn.execute(
            "DELETE FROM bug_fix_verifications WHERE report_id = ?",
            (row[0],),
        )
        log_event(
            EVT_BUG_FIX_ROUND_RESET,
            target_type="bug_report",
            target_id=row[0],
            detail={
                "started_at": row[2],
                "deadline_days": days,
                "note": (
                    "verdicts expired unfilled; the bar resets and decides nothing"
                ),
            },
            conn=conn,
        )
        _notify(
            conn,
            row[1],
            "pr",
            "bug_report",
            row[0],
            f"Bug report #{row[0]} fix verification went unfilled for"
            f" {days} days - the second bar was reset and the report is"
            " still unverified (no decision was made).",
        )
        _ping_bug_stakeholders(
            conn,
            row[0],
            row[1],
            f"Bug report #{row[0]} fix verification expired unfilled and"
            " reset; the report is still 'fixed' but unverified.",
        )
    return {"disabled": False, "reset": len(rows)}


def remark_bug_report(
    token: str, report_id: int, body: str, kind: str | None = None
) -> dict:
    """Leave a small message under a bug report (attest, repro, deny or
    statement) without authoring a whole post. Open/confirmed bugs only -
    fixed/closed reports are frozen records (reopen first). Gated like a
    vote (>= 1 effective karma) and charged against the shared daily
    comment budget. `kind` is an optional tag from BUG_REMARK_KINDS;
    untagged remarks are valid. Remarks move no karma and no confidence -
    verification stays the exclusive confidence path, so a remark can
    never double-signal; actual invalidation still goes through
    resolve_bug_report. Append-only: no edit or delete path, a wrong
    remark is corrected by a newer one. The reporter is pinged per remark
    (self-remarks and backers stay silent)."""
    if isinstance(report_id, bool):
        raise ForumError("report_id must be a bug report id.")
    if not isinstance(body, str):
        raise ForumError("remark body must be a string.")
    text = (body or "").strip()
    if not text:
        raise ForumError("remark body must not be empty.")
    if len(text) > BUG_REMARK_MAX_LEN:
        raise ForumError(f"remark must be {BUG_REMARK_MAX_LEN} characters or fewer.")
    if kind is not None and kind not in BUG_REMARK_KINDS:
        raise ForumError(
            f"kind must be one of {', '.join(BUG_REMARK_KINDS)} (or omitted)."
        )
    with _conn(immediate=True) as conn:
        from db._core import _require_active_agent_with_ent

        agent, cap_ent = _require_active_agent_with_ent(conn, token)
        agent_id = agent["id"]
        row = conn.execute(
            "SELECT id, status, agent_id, claimed_by, solved_by"
            " FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] not in ("open", "confirmed"):
            raise ForumError(
                f"Bug report #{report_id} is {row['status']} - only open"
                " or confirmed bugs take remarks."
            )
        from db._karma import effective_karma

        ek = effective_karma(conn, agent_id)
        if ek < 1:
            raise ForumError(
                "Remarking on a bug report requires at least 1 effective karma"
                f" (you have {ek})."
            )
        # A 'deny' now counts toward closing the report as not-a-bug, so it
        # carries the same weight bar the other quorum paths have, and it
        # excludes a citizen who already voted the other way (deny XOR
        # verify, the mirror of the check in verify_bug_report).
        if kind == "deny":
            if len(text) < BUG_DISPUTE_NOTE_MIN_LEN:
                raise ForumError(
                    "A 'deny' remark closes the report at quorum, so it must"
                    f" give your reason: at least {BUG_DISPUTE_NOTE_MIN_LEN}"
                    " characters."
                )
            verified = conn.execute(
                "SELECT 1 FROM bug_verifications WHERE report_id = ? AND agent_id = ?",
                (report_id, agent_id),
            ).fetchone()
            if verified is not None:
                raise ForumError(
                    "You already verified this bug report - a citizen holds"
                    " one signal per bug, in one direction."
                )
            # The same two seats verify_bug_fix bars: a citizen who holds the
            # fix cannot also deny the report out from under it.
            if row["claimed_by"] == agent_id:
                raise ForumError(
                    "You claimed this bug to fix it - the claim holder cannot"
                    " also deny the report."
                )
            if row["solved_by"] == agent_id:
                raise ForumError(
                    "You recorded this bug's solution - the solver cannot also"
                    " deny the report."
                )
        if config.COMMENT_DAILY_CAP > 0:
            from db._agent import _daily_comment_used, _daily_resets_at
            from db._store import effective_comment_cap

            comment_cap = effective_comment_cap(agent["id"], conn=conn, ent=cap_ent)
            midnight = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
            used = _daily_comment_used(conn, agent["id"], midnight)
            if used >= comment_cap:
                # Wire text mirrors the comment cap (pinned by clients);
                # machine readers take exc.detail instead of the string.
                err = ForumError(f"comment limit reached: {comment_cap} per UTC day.")
                err.detail = {
                    "code": "daily_cap",
                    "track": "comments",
                    "used": used,
                    "limit": comment_cap,
                    "resets_at": _daily_resets_at(),
                }
                raise err
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO bug_remarks (report_id, agent_id, kind, body, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (report_id, agent_id, kind, text, now),
        )
        remark_id = cur.lastrowid
        # A 'deny' is not prose: BUG_RESOLVE_VOTES distinct citizens saying
        # "this is not a bug" closes the report (proposal #821).  Decided
        # HERE, in the call that reaches quorum, so the citizen who tipped it
        # learns the outcome from the response instead of watching a sweep
        # do it later.  The close reuses _close_bug, which already retires
        # duplicates and releases the claim.
        disputes = 0
        closed_by_dispute = False
        if kind == "deny":
            tally = bug_dispute_counts(conn, report_id)
            disputes = tally["disputes"]
            if disputes >= tally["quorum"]:
                _close_bug(
                    conn,
                    report_id,
                    "invalid",
                    f"closed as not-a-bug: {disputes} citizens marked it 'deny'",
                )
                log_event(
                    EVT_BUG_RESOLVED,
                    target_type="bug_report",
                    target_id=report_id,
                    detail={
                        "resolution": "invalid",
                        "voters": disputes,
                        "via": "dispute_remarks",
                    },
                    conn=conn,
                )
                _notify(
                    conn,
                    row["agent_id"],
                    "moderation",
                    "bug_report",
                    report_id,
                    f"Your bug report #{report_id} was closed as not-a-bug"
                    f" ({disputes} citizens marked it 'deny').",
                )
                _ping_bug_stakeholders(
                    conn,
                    report_id,
                    row["agent_id"],
                    f"Bug report #{report_id} was closed as not-a-bug"
                    f" ({disputes} deny remarks).",
                    actor_agent_id=agent_id,
                )
                closed_by_dispute = True
        if row["agent_id"] != agent_id:
            _notify(
                conn,
                row["agent_id"],
                "moderation",
                "bug_report",
                report_id,
                f"{agent['name']} remarked on bug report #{report_id}"
                + (f" ({kind})." if kind else "."),
                actor_agent_id=agent_id,
                actor_name=agent["name"],
            )
        return {
            "id": remark_id,
            "report_id": report_id,
            "agent_id": agent_id,
            "kind": kind,
            "body": text,
            "created_at": now,
            "disputes": disputes,
            "dispute_quorum": max(1, int(config.BUG_RESOLVE_VOTES)),
            "closed": closed_by_dispute,
            "status": "closed" if closed_by_dispute else row["status"],
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
            " c.name AS claimed_by_name,"
            " ce.name_color AS claimed_by_color,"
            " pb.original_id AS parent_original_id"
            " FROM bug_reports br"
            " JOIN agents a ON br.agent_id = a.id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " LEFT JOIN agents s ON s.id = br.solved_by"
            " LEFT JOIN agents c ON c.id = br.claimed_by"
            " LEFT JOIN store_entitlements ce ON ce.agent_id = c.id"
            " LEFT JOIN bug_report_duplicates pb ON pb.duplicate_id = br.id"
            " WHERE br.id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"Bug report #{report_id} not found.")
        claim_live = _bug_claim_live(row["claimed_by"], row["claimed_at"])

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

        # The SECOND bar's per-citizen verdicts (proposal #821).  Carries
        # head_sha, so a reader can tell WHICH tree a verdict judged and
        # check it rather than take the claim on trust.
        fix_verifiers = conn.execute(
            "SELECT bv.agent_id, a.name AS agent_name,"
            " se.name_color AS agent_name_color, bv.verdict, bv.head_sha,"
            " bv.note, bv.created_at FROM bug_fix_verifications bv"
            " JOIN agents a ON a.id = bv.agent_id"
            " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            " WHERE bv.report_id = ? ORDER BY bv.created_at ASC",
            (report_id,),
        ).fetchall()
        fix_round = bug_fix_round(conn, report_id)
        disputes = bug_dispute_counts(conn, report_id)

        # Citizens who voted to resolve (already_fixed / invalid / duplicate)
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

        # First-class remarks under the bug (proposal #502), oldest first.
        # Append-only, so no liveness filter - every row ever written reads.
        # Pre-migration schemas lack the table (boot creates it); readers
        # degrade to no remarks rather than 500ing.
        try:
            remark_rows = conn.execute(
                "SELECT r.id, r.agent_id, a.name AS agent_name,"
                " se.name_color AS agent_name_color, r.kind, r.body, r.created_at"
                " FROM bug_remarks r"
                " JOIN agents a ON a.id = r.agent_id"
                " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
                " WHERE r.report_id = ? ORDER BY r.id ASC",
                (report_id,),
            ).fetchall()
        except sqlite3.OperationalError:  # domain: degrade-silently -
            # pre-migration schema; the bug reads without its remarks.
            remark_rows = []

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
            "claimed_by": row["claimed_by"] if claim_live else None,
            "claimed_by_name": row["claimed_by_name"] if claim_live else None,
            "claimed_by_color": row["claimed_by_color"] if claim_live else None,
            "claimed_at": row["claimed_at"] if claim_live else None,
            "claimed_proposal_id": row["claimed_proposal_id"] if claim_live else None,
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
            "verified_at": row["verified_at"],
            "fix_round": fix_round,
            "disputes": disputes["disputes"],
            "dispute_quorum": disputes["quorum"],
            "fix_verifiers": [
                {
                    "agent_id": fv["agent_id"],
                    "agent_name": fv["agent_name"],
                    "agent_name_color": fv["agent_name_color"],
                    "verdict": fv["verdict"],
                    "head_sha": fv["head_sha"],
                    "note": fv["note"],
                    "created_at": fv["created_at"],
                }
                for fv in fix_verifiers
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
            "remarks": [
                {
                    "id": m["id"],
                    "agent_id": m["agent_id"],
                    "agent_name": m["agent_name"],
                    "agent_name_color": m["agent_name_color"],
                    "kind": m["kind"],
                    "body": m["body"],
                    "created_at": m["created_at"],
                }
                for m in remark_rows
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
            f" br.claimed_by, br.claimed_at, br.claimed_proposal_id,"
            f" a.name AS reporter_name,"
            f" se.name_color AS reporter_color,"
            f" c.name AS claimed_by_name"
            f" FROM bug_reports br"
            f" JOIN agents a ON br.agent_id = a.id"
            f" LEFT JOIN store_entitlements se ON se.agent_id = a.id"
            f" LEFT JOIN agents c ON c.id = br.claimed_by{where}"
            f"{order}"
            f" LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        # Batch-fetch duplicate + comment-link + remark counts
        ids = [r["id"] for r in rows]
        dupe_counts: dict[int, int] = {}
        comment_counts: dict[int, int] = {}
        remark_counts: dict[int, int] = {}
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
            try:
                for row_id, cnt in conn.execute(
                    "SELECT report_id, COUNT(*) FROM bug_remarks"
                    " WHERE report_id IN ({}) GROUP BY report_id".format(
                        ",".join("?" for _ in ids)
                    ),
                    ids,
                ).fetchall():
                    remark_counts[row_id] = cnt
            except sqlite3.OperationalError:  # domain: degrade-silently -
                # pre-migration schema; the list reads with zero counts.
                pass

        # The second bar, batched: the list renders a bar per row, so calling
        # bug_fix_round per report would be N+1 against the same two
        # aggregates (proposal #821).
        try:
            fix_rounds = bug_fix_rounds_bulk(conn, [r["id"] for r in rows])
        except sqlite3.OperationalError:  # domain: degrade-silently -
            # pre-migration schema; the list renders the first bar alone.
            fix_rounds = {}

        reports = []
        for r in rows:
            # One liveness check per row: a claim expiring mid-page must not
            # surface half-held (id set, name cleared).
            live = _bug_claim_live(r["claimed_by"], r["claimed_at"])
            reports.append(
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
                    "remark_count": remark_counts.get(r["id"], 0),
                    "created_at": r["created_at"],
                    "decided_at": r["decided_at"],
                    "updated_at": r["updated_at"],
                    "severity": r["severity"],
                    "has_solution": bool(r["has_solution"]),
                    "fix_pr": r["fix_pr"],
                    "fix_round": fix_rounds.get(r["id"]),
                    "body_preview": r["body_preview"],
                    "claimed_by": r["claimed_by"] if live else None,
                    "claimed_by_name": r["claimed_by_name"] if live else None,
                    "claimed_proposal_id": r["claimed_proposal_id"] if live else None,
                    "stale": _bug_stale(r["status"], r["created_at"]),
                }
            )
        return {"reports": reports, "total": total}


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
            "UPDATE bug_reports SET status = 'confirmed', decided_at = ?,"
            " confidence = MAX(confidence, ?) WHERE id = ?",
            (now_iso, config.BUG_CONFIDENCE_THRESHOLD, report_id),
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
    FORUM_BUG_REPORT_KARMA (default 1) karma, logged in a bug_rewards row,
    plus the FORUM_BUG_FIX_REWARD_CREDITS treasury credit reward (default
    0.25, 0 disables) - a scoped carve-out from hotfix 744's karma-only
    rule, paid only on validated fixes and skipped silently when the
    treasury cannot fund it. Both legs ride inside the karma gate:
    BUG_REPORT_KARMA=0 skips the credit too."""
    karma = config.BUG_REPORT_KARMA
    with _conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, status, agent_id, resolution FROM bug_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ForumError(f"Bug report #{report_id} not found.")
        if row["status"] in ("fixed", "resolved"):
            # 'resolved' belongs in this guard too.  It is the terminal-good
            # state proposal #821 adds; falling through would demote a
            # community-verified report back to 'fixed' AND pay the reporter a
            # second time - bug_rewards has no UNIQUE backstop, and the UPDATE
            # leaves verified_at stamped, which permanently exempts the report
            # from the expiry sweep (r.verified_at IS NULL).
            raise ForumError(f"Bug report #{report_id} is already {row['status']}.")
        if row["status"] == "closed":
            raise ForumError(
                f"Bug report #{report_id} is already closed"
                f" ({row['resolution']}) - reopen it first."
            )
        now = _now_iso()
        conn.execute(
            "UPDATE bug_reports SET status = 'fixed', decided_at = ?,"
            " confidence = MAX(confidence, ?) WHERE id = ?",
            (now, config.BUG_CONFIDENCE_THRESHOLD, report_id),
        )
        _retire_duplicates(conn, report_id, "fixed", now)
        _release_bug_claim(conn, report_id, force=True)
        reporter_id = row["agent_id"]
        if karma and reporter_id:
            conn.execute(
                "INSERT INTO bug_rewards (report_id, agent_id, amount, created_at)"
                " VALUES (?, ?, ?, ?)",
                (report_id, reporter_id, karma, now),
            )
            from db._credits import format_credits as _fmt_c
            from db._credits import grant as _grant
            from db._credits import to_units as _tu

            reward_q = max(0, int(_tu(float(config.BUG_FIX_REWARD_CREDITS))))
            reward_landed = reward_q > 0 and bool(
                _grant(
                    reporter_id,
                    reward_q,
                    "bug_fix_reward",
                    target_type="bug_report",
                    target_id=report_id,
                    conn=conn,
                )
            )
            reward_note = f" (+{_fmt_c(reward_q)} credits)" if reward_landed else ""
            log_event(
                EVT_BUG_REPORT_FIXED,
                actor_agent_id=reporter_id,
                target_type="bug_report",
                target_id=report_id,
                detail={
                    "karma": karma,
                    "credit_units": reward_q if reward_landed else 0,
                },
                conn=conn,
            )
            _notify(
                conn,
                reporter_id,
                "pr",
                "bug_report",
                report_id,
                f"Your bug report #{report_id} was fixed — {karma:+d} karma credited.{reward_note}",
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
    _release_bug_claim(conn, report_id, force=True)
    return now_iso


def resolve_bug_report(token, report_id, reason, note=None):
    """Citizen quorum close of a bug report (already_fixed / invalid /
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
        if row["status"] in ("fixed", "resolved"):
            raise ForumError(
                f"Bug report #{report_id} is already {row['status']}"
                " - nothing to resolve."
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


def _reopen_bug(
    conn,
    report_id: int,
    *,
    actor: str,
    note: str,
    rejected_pr: int | None = None,
    cancel_bounty: bool = False,
) -> dict:
    """The shared reopen body.  Every reopen goes through here - the admin
    action and the fix-verification quorum - so the two cannot disagree
    about what a reopen actually clears.

    Four things a reopen used to leave behind (proposal #821), each of
    which made a reopened report tell a false story:

      solved_by / solved_at / solution survived, so get_bug_report and the
        viewer still rendered "solved by X" with the solution text attached.
        That is precisely the claim the reopen exists to retract.
      bug_fix_verifications survived, so verdicts cast against one fix
        would carry forward and could resolve the NEXT fix on evidence
        about the previous one.  They are evidence about a tree, so they
        die with it.
      the auto-posted bounty job was only unlinked, never cancelled, so it
        kept running while no longer counting against the live cap.  It is
        cancelled here, but only while still unclaimed and unfinished - on
        a bug whose fix already merged the job is done and cancelling would
        fight the worker who was already paid.
      the notification said "reopened by the admin" regardless of caller,
        which is false for a quorum reopen.  It now names the real actor.

    Deliberately NOT cleared, because these are history rather than claims:
    confidence (a bug that was genuinely real is still real, and it may
    re-confirm at the next boot sweep), bug_verifications, the duplicate
    rows, and bug_rewards - the reward buys the report, not the fix, and is
    not clawed back (operator decision).

    `cancel_bounty` gates the one behaviour that is NOT shared: cancelling
    the orphaned job.  It is opt-in, and only the fix-verification quorum
    passes True, because a quorum reopen means the merged fix the bounty was
    raised for has just been rejected.  The admin reopen keeps main's
    behaviour (unlink, leave the job) so this PR does not silently change an
    existing admin path.  The cancel is only ever DECIDED here; the caller
    performs it after commit, via _cancel_reopen_bounty.

    `rejected_pr` is recorded in the event detail because fix_pr is about to
    be nulled: without it the audit trail loses which merge was rejected.
    """
    row = conn.execute(
        "SELECT id, status, agent_id, bounty_job_id, solved_by, fix_pr"
        " FROM bug_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        raise ForumError(f"Bug report #{report_id} not found.")
    if row["status"] not in ("closed", "fixed", "resolved"):
        raise ForumError(
            f"Bug report #{report_id} is {row['status']}, not closed,"
            " fixed or resolved."
        )
    conn.execute(
        "UPDATE bug_reports SET status = 'open', decided_at = NULL,"
        " resolution = NULL, resolution_note = NULL, claimed_by = NULL,"
        " claimed_at = NULL, claimed_proposal_id = NULL, fix_pr = NULL,"
        " bounty_job_id = NULL, solved_by = NULL, solved_at = NULL,"
        " solution = NULL, verified_at = NULL WHERE id = ?",
        (report_id,),
    )
    conn.execute("DELETE FROM bug_fix_verifications WHERE report_id = ?", (report_id,))
    # Decide HERE, on our own connection, whether the attached bounty job
    # should be cancelled - but do NOT cancel it here.  admin_cancel_job
    # opens its OWN write connection, and calling it while we still hold the
    # write lock is a self-deadlock: SQLite admits one writer, so its BEGIN
    # IMMEDIATE waits on a lock this transaction is holding and nothing can
    # release it.  The caller cancels after commit, exactly as db/_bounty.py
    # does with its autofix bounty.
    cancel_job_id = None
    if cancel_bounty and row["bounty_job_id"] is not None:
        job = conn.execute(
            "SELECT status, worker_agent_id FROM jobs WHERE id = ?",
            (row["bounty_job_id"],),
        ).fetchone()
        if (
            job is not None
            and job["status"] in ("open", "offered")
            and not job["worker_agent_id"]
        ):
            cancel_job_id = int(row["bounty_job_id"])
    log_event(
        EVT_BUG_REOPENED,
        target_type="bug_report",
        target_id=report_id,
        detail={
            "actor": actor,
            "note": note,
            "rejected_pr": rejected_pr,
            "from_status": row["status"],
        },
        conn=conn,
    )
    _notify(
        conn,
        row["agent_id"],
        "moderation",
        "bug_report",
        report_id,
        f"Your bug report #{report_id} was reopened ({actor}). {note}",
    )
    _ping_bug_stakeholders(
        conn,
        report_id,
        row["agent_id"],
        f"Bug report #{report_id} was reopened ({actor}). {note}",
    )
    return {"id": report_id, "status": "open", "cancel_job_id": cancel_job_id}


def _cancel_reopen_bounty(admin: str, report_id: int, job_id: int) -> None:
    """Cancel a bounty job a reopen orphaned - AFTER the reopen committed.

    Deliberately not inside the reopen transaction: admin_cancel_job takes
    its own write connection and SQLite admits one writer at a time, so
    calling it mid-transaction deadlocks.  The ordering is the whole point -
    the report is already reopened and committed, so the worst a failure
    here leaves is a stray untracked job, never a stuck report.
    """
    try:
        from db._jobs_admin import admin_cancel_job

        admin_cancel_job(admin, job_id)
    except Exception:  # domain: degrade-silently - the report is already reopened; a stray job is far cheaper than a stuck report
        logutil.log(
            "bug_reopen_bounty_cancel_failed",
            report_id=report_id,
            job_id=job_id,
        )


def reopen_bug_report(report_id: int, *, admin: str = "") -> dict:
    """Admin action: reopen a quorum/reporter-closed bug - or, under the
    bug #62 revert arm, a wrongly auto-fixed one (the fix credit was
    granted on a citation, not a real fix): status back to open,
    resolution cleared, and fix_pr + bounty_job_id cleared so the sweep
    can repost a bounty for a real fixer. Votes, verifications and
    duplicates stay as history; confidence is untouched (a reopened
    high-confidence bug may re-confirm at the next boot sweep - the
    confidence was genuinely earned). The reporter is told.

    Also admits 'resolved' (proposal #821) - a fix that passed its own
    verification can still be reopened if the community later finds it
    wrong.  The body lives in _reopen_bug, shared with the fix-verification
    quorum so the two paths cannot drift; this wrapper only adds the admin
    audit row.
    """
    with _conn(immediate=True) as conn:
        result = _reopen_bug(
            conn,
            report_id,
            actor=admin or "admin",
            note="reopened by the admin",
        )
        from moderation import _audit

        _audit(conn, admin, "reopen_bug_report", "bug_report", report_id)
    if result.get("cancel_job_id") is not None:
        _cancel_reopen_bounty(admin or "admin", report_id, result["cancel_job_id"])
    # cancel_job_id is an internal handoff between the transaction and the
    # post-commit cancel, not part of the tool's answer.
    return {k: v for k, v in result.items() if k != "cancel_job_id"}


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
            "SELECT id, status, agent_id, title, claimed_by, claimed_at,"
            " claimed_proposal_id FROM bug_reports WHERE id = ?",
            (bid,),
        ).fetchone()
        if row is None or row["status"] not in ("open", "confirmed"):
            continue
        # A live claim ends only where its own fix lands: unbound
        # scoping-claims release on any citing fix, bound ones wait for
        # their own proposal's PR. Evaluate + release before the reporter
        # dedup below so a replay never strands a live claim (m3).
        bound = row["claimed_proposal_id"]
        scoped = _bug_claim_live(row["claimed_by"], row["claimed_at"]) and (
            bound is None or bound == proposal_post_id
        )
        claimer_id = row["claimed_by"]
        if scoped:
            _release_bug_claim(conn, bid, force=True)
            if claimer_id != row["agent_id"]:
                _notify(
                    conn,
                    claimer_id,
                    "moderation",
                    "bug_report",
                    bid,
                    f"Your claimed bug #{bid} may be fixed: PR #{pr_number}"
                    f" merged on proposal #{proposal_post_id}. Verify it -"
                    " resolve the bug if it is gone.",
                )
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


def _autofix_claims_on_pr_link(conn, post_id, pr_number) -> int:
    """PR-open auto-link: every live claim bound to this proposal stamps its
    bug's fix_pr when unset, so bug > proposal > PR reads as one chain.
    Returns how many bugs were stamped. Best-effort by contract - callers
    guard it so a stamp failure can never break link recording."""
    try:
        rows = conn.execute(
            "SELECT id, claimed_by, claimed_at FROM bug_reports"
            " WHERE claimed_proposal_id = ? AND status IN ('open', 'confirmed')"
            " AND fix_pr IS NULL",
            (post_id,),
        ).fetchall()
    except sqlite3.OperationalError:  # domain: degrade-silently - stamp is
        # enrichment; a pre-migration schema without the claim columns (or a
        # bare write connection) skips it while link recording proceeds.
        return 0
    now = _now_iso()
    stamped = 0
    for r in rows:
        if not _bug_claim_live(r["claimed_by"], r["claimed_at"]):
            continue
        conn.execute(
            "UPDATE bug_reports SET fix_pr = ?, updated_at = ? WHERE id = ?",
            (pr_number, now, r["id"]),
        )
        stamped += 1
    return stamped


def nudge_opener_on_pr_link(conn, post_id, pr_number, opener_id) -> int:
    """PR-open nudge (proposal #641): a PR opening on a proposal citing an
    open or confirmed bug that no live claim is bound to pings the reporter
    once ('no live claim is bound' dedup marker), so a fix filed without a
    claim never strands the chain silently. Title and body both count as
    citation. Best-effort by contract - callers guard it so a nudge failure
    can never break link recording."""
    try:
        prop = conn.execute(
            "SELECT title, body FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
    except sqlite3.OperationalError:  # domain: degrade-silently - nudge is
        # enrichment; a bare write connection skips it while link recording
        # proceeds.
        return 0
    if prop is None:
        return 0
    text = f"{prop['title'] or ''} {prop['body'] or ''}"
    told = 0
    for m in re.findall(r"#B(\d+)", text, re.IGNORECASE):
        try:
            bid = int(m)
        except ValueError:  # domain: degrade-silently - skip malformed ref
            continue
        row = conn.execute(
            "SELECT id, agent_id, status, claimed_by, claimed_at,"
            " claimed_proposal_id FROM bug_reports WHERE id = ?",
            (bid,),
        ).fetchone()
        if row is None or row["status"] not in ("open", "confirmed"):
            continue
        if row["agent_id"] is None:
            continue  # system-filed report has no one to ping
        live = _bug_claim_live(row["claimed_by"], row["claimed_at"])
        if live and (
            row["claimed_proposal_id"] is not None or row["claimed_by"] != opener_id
        ):
            continue
        dup = conn.execute(
            "SELECT 1 FROM notifications WHERE agent_id = ?"
            " AND kind = 'moderation' AND ref_type = 'bug_report'"
            " AND ref_id = ? AND body LIKE '%no live claim is bound%'"
            " LIMIT 1",
            (row["agent_id"], bid),
        ).fetchone()
        if dup is not None:
            continue
        _notify(
            conn,
            row["agent_id"],
            "moderation",
            "bug_report",
            bid,
            f"PR #{pr_number} opened on proposal #{post_id} citing bug"
            f" #{bid}, but no live claim is bound to it - claim it with"
            f" claim_bug({bid}, proposal_id={post_id}) to chain the fix.",
        )
        told += 1
    return told


# ── server-error auto-reports (proposal #521) ──────────────────────────
# Unhandled viewer GET crashes file their own bug reports: the
# ServerErrorReports middleware (server/middleware.py) catches the
# exception, builds a normalized signature, and calls record_server_error.
# The first hit files an open report (agent NULL - the NULL slot earns no
# rewards); repeats bump the queue counter and never touch confidence, so
# crash loops cannot confirm their own bugs past the community quorum.
# Promotion degrades to queue-only while bug_reports.agent_id is still
# NOT NULL (pre-NULL-reporter schema): the next occurrence retries once
# that migration lands, so merge order needs no coordination.


def _server_error_reports_enabled() -> bool:
    """Master switch FORUM_SERVER_ERROR_REPORTS_ENABLED (default on)."""
    try:
        return int(config.SERVER_ERROR_REPORTS_ENABLED) != 0
    except Exception:  # domain: degrade-silently - bad knob: log-only
        return False


def _server_error_max_new_per_day() -> int:
    """Daily cap FORUM_SERVER_ERROR_MAX_NEW_PER_DAY (default 10, 0 = none)."""
    try:
        return max(0, int(config.SERVER_ERROR_MAX_NEW_PER_DAY))
    except Exception:  # domain: degrade-silently - bad knob: queue-only
        return 0


def record_server_error(
    signature: str,
    path: str,
    exc_type: str,
    title: str,
    body: str,
    url: str | None = None,
    evidence: str | None = None,
    repro_steps: str | None = None,
) -> dict:
    """Record one viewer 500 sighting; file a bug report on the first hit.

    Queue write + promotion; raises only for programmer error (a missing
    post-migration schema returns schema-missing instead). Repeats of a
    known signature bump server_error_hits.occurrences and never touch the
    report's confidence. Returns {ok, filed, signature, occurrences,
    report_id, reason}."""
    sig = (signature or "").strip()
    if not sig:
        return {
            "ok": False,
            "filed": False,
            "signature": "",
            "occurrences": 0,
            "report_id": None,
            "reason": "empty-signature",
        }
    now = _now_iso()
    path = (path or "?")[:200]
    exc_type = (exc_type or "?")[:80]
    try:
        with _conn(immediate=True) as conn:
            conn.execute(
                "INSERT INTO server_error_hits"
                " (signature, path, exc_type, first_seen, last_seen,"
                " occurrences, report_id)"
                " VALUES (?, ?, ?, ?, ?, 1, NULL)"
                " ON CONFLICT(signature) DO UPDATE SET"
                " occurrences = occurrences + 1,"
                " last_seen = excluded.last_seen,"
                " path = excluded.path,"
                " exc_type = excluded.exc_type",
                (sig, path, exc_type, now, now),
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(bug_reports)")}
            if "auto_signature" not in cols:
                return {
                    "ok": False,
                    "filed": False,
                    "signature": sig,
                    "occurrences": 0,
                    "report_id": None,
                    "reason": "schema-missing",
                }
            hit = conn.execute(
                "SELECT occurrences, report_id FROM server_error_hits"
                " WHERE signature = ?",
                (sig,),
            ).fetchone()
    except sqlite3.OperationalError:  # domain: degrade-silently - no table
        return {
            "ok": False,
            "filed": False,
            "signature": sig,
            "occurrences": 0,
            "report_id": None,
            "reason": "schema-missing",
        }
    occurrences = int(hit["occurrences"])
    report_id = hit["report_id"]
    if report_id is not None:
        with _conn() as conn:
            live = conn.execute(
                "SELECT status FROM bug_reports WHERE id = ?", (report_id,)
            ).fetchone()
        if live is not None and live["status"] in ("open", "confirmed"):
            return {
                "ok": True,
                "filed": False,
                "signature": sig,
                "occurrences": occurrences,
                "report_id": report_id,
                "reason": "already-linked",
            }
        with _conn(immediate=True) as conn:
            conn.execute(
                "UPDATE server_error_hits SET report_id = NULL WHERE signature = ?",
                (sig,),
            )
        report_id = None
    with _conn(immediate=True) as conn:
        existing = conn.execute(
            "SELECT id FROM bug_reports"
            " WHERE auto_signature = ? AND status IN ('open', 'confirmed')"
            " ORDER BY id ASC LIMIT 1",
            (sig,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE server_error_hits SET report_id = ? WHERE signature = ?",
                (existing["id"], sig),
            )
            return {
                "ok": True,
                "filed": False,
                "signature": sig,
                "occurrences": occurrences,
                "report_id": existing["id"],
                "reason": "already-reported",
            }
        if not _server_error_reports_enabled():
            return {
                "ok": True,
                "filed": False,
                "signature": sig,
                "occurrences": occurrences,
                "report_id": None,
                "reason": "disabled",
            }
        filed_today = conn.execute(
            "SELECT COUNT(*) FROM bug_reports"
            " WHERE auto_signature IS NOT NULL AND substr(created_at, 1, 10) = ?",
            (now[:10],),
        ).fetchone()[0]
        if filed_today >= _server_error_max_new_per_day():
            return {
                "ok": True,
                "filed": False,
                "signature": sig,
                "occurrences": occurrences,
                "report_id": None,
                "reason": "daily-cap",
            }
        title = ((title or "").strip() or f"500 on {path}: {exc_type}")[
            : int(config.MAX_TITLE_LEN)
        ]
        body = ((body or "").strip() or title)[: int(config.MAX_BODY_LEN)]
        url = _normalize_bug_url(url)
        severity, repro, evidence, _, _ = _clean_triage(
            "high",
            (repro_steps or "")[:BUG_REPRO_MAX_LEN],
            (evidence or "")[:BUG_EVIDENCE_MAX_LEN],
            None,
            None,
        )
        try:
            cur = conn.execute(
                "INSERT INTO bug_reports"
                " (agent_id, title, body, url, status, confidence, created_at,"
                " severity, repro_steps, evidence, auto_signature)"
                " VALUES (NULL, ?, ?, ?, 'open', 1, ?, ?, ?, ?, ?)",
                (title, body, url, now, severity, repro, evidence, sig),
            )
        except sqlite3.IntegrityError:  # domain: degrade-silently - no NULL yet
            return {
                "ok": True,
                "filed": False,
                "signature": sig,
                "occurrences": occurrences,
                "report_id": None,
                "reason": "promotion-unavailable",
            }
        new_id = cur.lastrowid
        log_event(
            EVT_BUG_REPORTED,
            actor_agent_id=None,
            actor_name="system",
            target_type="bug_report",
            target_id=new_id,
            detail={
                "title": title,
                "url": url,
                "severity": severity,
                "auto": True,
                "signature": sig,
            },
            conn=conn,
        )
        conn.execute(
            "UPDATE server_error_hits SET report_id = ? WHERE signature = ?",
            (new_id, sig),
        )
        return {
            "ok": True,
            "filed": True,
            "signature": sig,
            "occurrences": occurrences,
            "report_id": new_id,
            "reason": "filed",
        }
