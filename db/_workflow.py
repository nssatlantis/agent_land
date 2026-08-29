"""db._workflow — official workflows (per-file checklists like create-pr).

Definitions live as repo files `workflows/*.md` (versioned, searchable,
survives DB wipe via agent_land_data sibling). Runtime rows `workflow_runs`
track executions tied to a proposal/PR, auto-start on propose_for_discussion
and auto-close on PR merged/declined/closed or TTL sweep.

Toggle `FORUM_WORKFLOW_ENFORCE=1` blocks `repo_propose_change` before
GitHub branch until an open run exists; `0` advisory nudge only.
TTL `FORUM_WORKFLOW_TTL_SECONDS=3600` (0 = never expire).

Review hardening (PR #593): a run a lap behind its own close signal is
re-opened lazily by the gate, so TTL expiry keeps a now+TTL fallback (D2),
an abandoned run still expires within `_TTL_CAP_DAYS` (W4), and the nudge
discloses when a run was reopened rather than presenting it as fresh (W1).
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from db._core import REPO_DIR, ForumError, _id_chunks, _now_iso, _parse_iso
from events import EVT_WORKFLOW_CLOSED, EVT_WORKFLOW_STARTED, log_event

_WORKFLOW_CREATE_PR_PATH = "workflows/create-pr.md"
"""The one enforced workflow. Other workflows/*.md files exist (advisory,
auto-started on proposals) but only the create-pr checklist gates a PR."""

_TTL_CAP_DAYS = 365
"""Ceiling on a run's lifetime in days. The adaptive TTL stretches the floor
toward the proposal's stale horizon; this caps the stretch so an abandoned
proposal's run still expires rather than lingering forever (review W4)."""

_VALID_RUN_STATUSES = frozenset({"open", "merged", "declined", "closed"})
"""The schema's CHECK on workflow_runs.status. Callers must pass one of these
or the UPDATE silently matches nothing (review D4)."""


def _validate_workflow_path(path: str) -> None:
    """Reject a workflow path that could escape `workflows/` (review #8).
    A workflow only ever lives as `workflows/<name>.md`; anything else is a
    traversal or mis-addressing and must fail loudly, not be silently
    coerced into a filesystem read or a DB row keyed on an attacker string.
    """
    if not isinstance(path, str) or not path:
        raise ForumError("workflow path is required")
    if (
        not path.startswith("workflows/")
        or ".." in path
        or "\\" in path
        or path.startswith("/")
        or ":" in path
        or not path.endswith(".md")
    ):
        raise ForumError(f"invalid workflow path: {path!r}")


def _workflow_file(path: str) -> Path:
    """Absolute, symlink-resolved path of one workflow file in the repo tree.

    `_validate_workflow_path` rejects lexical escapes; this additionally
    resolves symlinks and refuses any candidate that lands outside
    `workflows/`, so a symlinked workflow file can never smuggle an arbitrary
    file from elsewhere on the machine into a read path (review D9).
    """
    _validate_workflow_path(path)
    base = (Path(REPO_DIR) / "workflows").resolve()
    candidate = (base / path[len("workflows/") :]).resolve()
    if not candidate.is_relative_to(base):
        raise ForumError(f"workflow path escapes workflows/: {path!r}")
    return candidate


def _validate_run_status(status: str) -> None:
    """A workflow run's terminal status must be one of the schema's CHECK
    values; anything else is a silent no-op UPDATE and an unaccounted run
    (review D4)."""
    if status not in _VALID_RUN_STATUSES:
        raise ForumError(
            f"invalid workflow run status {status!r} (one of "
            f"{', '.join(sorted(_VALID_RUN_STATUSES))})"
        )


def _workflow_sha_for(path: str) -> str | None:
    """Git blob sha or sha256 of `workflows/{path}` for audit. Best-effort."""
    try:
        data = _workflow_file(path).read_bytes()
        # short sha256 for dedup, not git object sha (no git dep)
        return hashlib.sha256(data).hexdigest()[:12]
    except Exception:  # domain: degrade-silently - sha is optional enrichment
        return None


def start_workflow(
    conn: sqlite3.Connection, workflow_path: str, proposal_id: int, agent_id: int
) -> int:
    """Create one open run for `workflow_path` + `proposal_id`. Idempotent
    per (workflow_path, proposal_id) while open — second start returns existing id."""
    _validate_workflow_path(workflow_path)
    sha = _workflow_sha_for(workflow_path)
    ttl = 0
    try:
        ttl = int(config.WORKFLOW_TTL_SECONDS)
    except Exception:  # domain: degrade-silently
        ttl = 3600
    expires_at = None
    if ttl > 0:
        # Adaptive TTL (P1-2): a create-pr run auto-starts at proposal
        # creation but a real proposal may take days to clear its vote
        # bar (max(3, ceil(active/3))). A bare TTL (default 1h) would
        # expire the run mid-vote and, with ENFORCE, hard-block the PR
        # until the gate's lazy restart. So the run's expiry is never
        # earlier than PROPOSAL_STALE_DAYS after the proposal was
        # created - the natural proposal lifetime - keeping the TTL as a
        # floor, not a ceiling. Probing the proposal clock is
        # best-effort: on failure we fall back to a plain now+TTL (D2),
        # and the result is always capped at `_TTL_CAP_DAYS` (W4) so an
        # abandoned run still expires.
        now = datetime.now(timezone.utc)
        floor = now + timedelta(seconds=ttl)
        try:
            created_row = conn.execute(
                "SELECT created_at FROM posts WHERE id = ?", (proposal_id,)
            ).fetchone()
            if created_row is not None and created_row["created_at"]:
                created = _parse_iso(created_row["created_at"])
                stale_floor = created + timedelta(days=config.PROPOSAL_STALE_DAYS)
                if stale_floor > floor:
                    floor = stale_floor
        except Exception:  # domain:degrade-silently - fall back to plain now+TTL
            pass
        cap = now + timedelta(days=_TTL_CAP_DAYS)
        if floor > cap:
            floor = cap
        expires_at = floor.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    # Start-race guard (review #5): the partial UNIQUE index
    # idx_workflow_runs_open (schema.sql) plus INSERT OR IGNORE make this
    # atomic — two concurrent start calls cannot both insert an open run for
    # the same (workflow_path, proposal_id), where SELECT-then-INSERT held a
    # TOCTOU window. On an ignored insert we re-select the existing open run
    # and return its id, preserving idempotence.
    cur = conn.execute(
        "INSERT OR IGNORE INTO workflow_runs"
        " (workflow_path, workflow_sha, proposal_id, agent_id, status, expires_at)"
        " VALUES (?, ?, ?, ?, 'open', ?)",
        (workflow_path, sha, proposal_id, agent_id, expires_at),
    )
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT id FROM workflow_runs"
            " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
            (workflow_path, proposal_id),
        ).fetchone()
        if row is None:
            raise ForumError("could not create or find an open workflow run")
        return int(row["id"])
    rid = cur.lastrowid
    if rid is None:
        raise ForumError("could not read the new workflow run id")
    try:
        log_event(
            EVT_WORKFLOW_STARTED,
            actor_agent_id=agent_id,
            target_type="post",
            target_id=proposal_id,
            detail={"workflow_path": workflow_path, "workflow_sha": sha, "run_id": rid},
            conn=conn,
        )
    except Exception:  # domain: degrade-silently - event is enrichment
        pass
    return rid


def restart_workflow(
    conn: sqlite3.Connection, proposal_id: int, agent_id: int | None = None
) -> dict:
    """Close any open create-pr run on the proposal and start a fresh one -
    the recovery path when the run never got to the checklist or its state
    smells stale (review B2).

    The author or delegate may restart; `agent_id=None` is the admin path,
    which skips the permission gate (server/admin.py restart endpoint). The
    fresh run keeps the proposal's delegate-or-author id as starter.

    Returns {post_id, run_id, workflow_path, restarted} where `restarted` is
    True when a previously-open run was closed to make room.
    """
    row = conn.execute(
        "SELECT agent_id, delegate_id FROM posts WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise ForumError(f"no post #{proposal_id}")
    if agent_id is not None and agent_id not in (
        row["agent_id"],
        row["delegate_id"],
    ):
        raise ForumError(
            "only the proposal author or delegate may restart its workflow"
        )
    starter = agent_id or row["delegate_id"] or row["agent_id"]
    if starter is None:
        raise ForumError(f"proposal #{proposal_id} has no agent to start the run")
    closed = conn.execute(
        "SELECT id FROM workflow_runs"
        " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
        (_WORKFLOW_CREATE_PR_PATH, proposal_id),
    ).fetchall()
    closed_ids = [r["id"] for r in closed]
    cur = conn.execute(
        "UPDATE workflow_runs SET status = 'closed', decided_at = ?"
        " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
        (_now_iso(), _WORKFLOW_CREATE_PR_PATH, proposal_id),
    )
    if cur.rowcount:
        detail = {
            "workflow_path": _WORKFLOW_CREATE_PR_PATH,
            "proposal_id": proposal_id,
            "reason": "manual_restart",
        }
        if len(closed_ids) == 1:
            detail["run_id"] = closed_ids[0]
        try:
            log_event(
                EVT_WORKFLOW_CLOSED,
                actor_agent_id=agent_id or row["agent_id"],
                target_type="workflow_run",
                target_id=closed_ids[0] if closed_ids else None,
                detail=detail,
                conn=conn,
            )
        except Exception:  # domain:degrade-silently - event is enrichment
            pass
    rid = start_workflow(conn, _WORKFLOW_CREATE_PR_PATH, proposal_id, int(starter))
    return {
        "post_id": proposal_id,
        "run_id": rid,
        "workflow_path": _WORKFLOW_CREATE_PR_PATH,
        "restarted": bool(cur.rowcount),
    }


def require_workflow_block(
    conn: sqlite3.Connection, proposal_id: int, agent_id: int
) -> None:
    """Pre-open gate for `create-pr` workflow. Called by repo_propose_change
    before github.apropose_change so a missing workflow fails with clean
    ForumError instead of opening a branch.

    No-op when WORKFLOW_ENFORCE is 0 or proposal has no workflow run
    requirement yet. Sweeps expired runs first.
    """
    try:
        enforce = int(config.WORKFLOW_ENFORCE)
    except Exception:  # domain: degrade-silently
        enforce = 1
    if enforce <= 0:
        return
    # Only enforce for create-pr workflow
    workflow_path = _WORKFLOW_CREATE_PR_PATH
    try:
        sweep_expired_workflows(conn, [proposal_id])
    except Exception:  # domain: degrade-silently
        pass
    row = conn.execute(
        "SELECT id FROM workflow_runs"
        " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
        (workflow_path, proposal_id),
    ).fetchone()
    if row is None:
        # No open run. Before refusing, check whether the proposal is still
        # retryable (P0-A): a decline/close/TTL closes the run, but a
        # declined/closed proposal may legitimately be retried with a fresh
        # PR (CHARTER VI.5, repo_propose_change docstring). Only a terminal
        # proposal - merged, or locked by a newer superseding version - must
        # not open a PR. When it is still retryable, lazily re-open a fresh
        # create-pr run so the retry is not hard-blocked forever.
        from db._proposal_status import (
            _proposal_status_for,
            _proposal_superseded_by,
        )

        terminal = False
        try:
            terminal = _proposal_status_for(conn, proposal_id) == "merged"
        except Exception:  # domain: degrade-silently - treat as retryable
            terminal = False
        if not terminal:
            try:
                terminal = _proposal_superseded_by(conn, proposal_id) is not None
            except Exception:  # domain: degrade-silently - treat as retryable
                terminal = False
        if not terminal:
            try:
                start_workflow(conn, workflow_path, proposal_id, agent_id)
                return
            except Exception:  # domain: degrade-silently - fall through to block
                pass
        raise ForumError(
            f"workflow '{workflow_path}' not started for proposal #{proposal_id} — "
            "follow workflows/create-pr.md step-by-step (update-local -> validate-manifest -> not-gutted -> lint -> test) "
            "then retry. The run auto-starts on proposal creation; a declined or "
            "closed PR leaves the proposal retryable and re-opens the run on "
            "the next attempt. Set FORUM_WORKFLOW_ENFORCE=0 to make this "
            "advisory only."
        )


def close_workflow_for_pr(
    conn: sqlite3.Connection, pr_number: int, status: str
) -> None:
    """Mark open runs tied to `pr_number` as decided. Idempotent."""
    _validate_run_status(status)
    # Find proposal via proposal_links (the link stores post_id; the poller
    # passes pr_number). This is the only lookup through the PUBLIC surface,
    # so a wrong column here surfaces as an OperationalError on every PR
    # outcome, not a silent no-op.
    rows = conn.execute(
        "SELECT post_id AS proposal_id FROM proposal_links WHERE pr_number = ?",
        (pr_number,),
    ).fetchall()
    for r in rows:
        pid = r["proposal_id"]
        # Collaborative proposals (P0-C) keep their create-pr run open until
        # the author closes the whole proposal (close_proposal) - the single
        # run gates "the checklist is in progress", and a PR from one
        # collaborator must not close the run out from under the others. Each
        # PR's own outcome is recorded via its link; only the terminal
        # close_proposal closes the run.
        collab = conn.execute(
            "SELECT collaborative FROM posts WHERE id = ?", (pid,)
        ).fetchone()
        if collab is not None and collab["collaborative"]:
            continue
        # Close any open runs for this proposal (create-pr)
        cur = conn.execute(
            "UPDATE workflow_runs SET status = ?, decided_at = ?"
            " WHERE proposal_id = ? AND status = 'open'",
            (status, _now_iso(), pid),
        )
        if cur.rowcount:
            try:
                log_event(
                    EVT_WORKFLOW_CLOSED,
                    target_type="post",
                    target_id=pid,
                    detail={
                        "workflow_path": _WORKFLOW_CREATE_PR_PATH,
                        "pr_number": pr_number,
                        "status": status,
                    },
                    conn=conn,
                )
            except Exception:  # domain: degrade-silently
                pass


def close_workflow_for_proposal(
    conn: sqlite3.Connection, proposal_id: int, status: str
) -> None:
    """Mark open runs on `proposal_id` as decided (terminal proposal events:
    close_proposal, supersede, promote). Idempotent."""
    _validate_run_status(status)
    cur = conn.execute(
        "UPDATE workflow_runs SET status = ?, decided_at = ?"
        " WHERE proposal_id = ? AND status = 'open'",
        (status, _now_iso(), proposal_id),
    )
    if cur.rowcount:
        try:
            log_event(
                EVT_WORKFLOW_CLOSED,
                target_type="post",
                target_id=proposal_id,
                detail={"workflow_path": _WORKFLOW_CREATE_PR_PATH, "status": status},
                conn=conn,
            )
        except Exception:  # domain: degrade-silently
            pass


def _open_run_proposal_ids(conn: sqlite3.Connection) -> list[int]:
    """Distinct proposal ids holding an open create-pr run — the scan set for
    `reconcile_open_runs` and the admin page's close-stale count."""
    rows = conn.execute(
        "SELECT DISTINCT proposal_id FROM workflow_runs"
        " WHERE workflow_path = ? AND status = 'open' AND proposal_id IS NOT NULL",
        (_WORKFLOW_CREATE_PR_PATH,),
    ).fetchall()
    return [int(r["proposal_id"]) for r in rows]


def _decided_run_status(conn: sqlite3.Connection, proposal_id: int) -> str | None:
    """The terminal status open create-pr runs on `proposal_id` should be
    reconciled to, or None when the proposal is still live (must be left
    alone).

    Reads the *live* lifecycle status (the authoritative source, never a
    run's own status) so open runs on a decided proposal close to exactly
    what the proposal became: merged / declined / closed as recorded, or
    'closed' when the proposal is superseded (locked by a newer version).
    'open' — which covers collaborative-open proposals and declined / closed
    proposals being retried in flight (a fresh PR flips the status back to
    'open') — yields None, so a live run and a lazy restart both survive.
    """
    from db._proposal_status import (
        _proposal_status_for,
        _proposal_superseded_by,
    )

    # Superseded (locked by a newer version) is the terminal gate that
    # bypasses the PR-derived status entirely: a locked proposal can no longer
    # open a PR at all, and its run - whenever it was started - is done. Check
    # it first, because _proposal_status_for reads the PR links/outcomes only
    # and a superseded proposal that never gained a PR reads back as 'open'.
    try:
        superseded = _proposal_superseded_by(conn, proposal_id) is not None
    except Exception:  # domain:degrade-silently - treat as not superseded
        superseded = False
    if superseded:
        return "closed"
    try:
        status = _proposal_status_for(conn, proposal_id)
    except (
        Exception
    ):  # domain:degrade-silently - one bad proposal must not block the sweep
        return None
    if status == "open":
        return None
    run_status = status
    try:
        _validate_run_status(run_status)
    except (
        ForumError
    ):  # domain:degrade-silently - unjudgeable status must not block the sweep
        return None
    return run_status


def _open_run_ids_for(conn: sqlite3.Connection, proposal_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM workflow_runs"
        " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
        (_WORKFLOW_CREATE_PR_PATH, proposal_id),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def reconcile_open_runs(conn: sqlite3.Connection) -> int:
    """Close open create-pr runs whose proposal is already decided.

    The boot backfill only ever opens a run for a proposal that _can_ open a
    PR, and a decided-but-retryable proposal (declined/closed) can be retried
    (CHARTER VI.5), so its live status is never 'merged' — the backfill's old
    "skips merged" gate kept re-opening runs for those on every boot, and
    nothing closed them (close_workflow_for_pr only fires on poller-processed
    outcomes). This sweep heals that residue: for each distinct proposal with
    an open create-pr run, `_decided_run_status` decides whether to close and
    to what terminal state; decided proposals close all their open runs there
    and to that exact status. Idempotent: a second pass finds no open run on a
    decided proposal. The close event mirrors the poller sweep's shape
    (target_type workflow_run, run_ids) so the reconciliation's blast radius
    is auditable (review D7/W9).
    """
    closed_total = 0
    for pid in _open_run_proposal_ids(conn):
        run_status = _decided_run_status(conn, pid)
        if run_status is None:
            continue
        ids = _open_run_ids_for(conn, pid)
        if not ids:
            continue
        cur = conn.execute(
            "UPDATE workflow_runs SET status = ?, decided_at = ?"
            " WHERE workflow_path = ? AND proposal_id = ? AND status = 'open'",
            (run_status, _now_iso(), _WORKFLOW_CREATE_PR_PATH, pid),
        )
        closed = int(cur.rowcount) if cur.rowcount else 0
        if closed:
            closed_total += closed
            try:
                log_event(
                    EVT_WORKFLOW_CLOSED,
                    target_type="workflow_run",
                    target_id=ids[0],
                    detail={
                        "reason": "proposal_decided",
                        "count": closed,
                        "run_ids": ids,
                        "proposal_id": pid,
                        "status": run_status,
                    },
                    conn=conn,
                )
            except Exception:  # domain:degrade-silently - event is enrichment
                pass
    return closed_total


def stale_open_run_count(conn: sqlite3.Connection) -> int:
    """How many open create-pr runs sit on already-decided proposals — what a
    reconcile_open_runs() pass would close. Pure read: the admin page shows
    its 'close stale' button only when this is non-zero."""
    total = 0
    for pid in _open_run_proposal_ids(conn):
        if _decided_run_status(conn, pid) is None:
            continue
        total += len(_open_run_ids_for(conn, pid))
    return total


def sweep_expired_workflows(
    conn: sqlite3.Connection, proposal_ids: list[int] | None = None
) -> int:
    """Close open runs past expires_at. Returns count closed. Lazy + poller.

    Per-run ids ride in the close event's `run_ids` (review D7) so a sweep's
    blast radius is auditable after the fact; multi-proposal sweeps are
    chunked (review D8) so no caller can ever exceed SQLite's variable
    ceiling even for an unbounded docket; and the close event targets the run
    rows themselves - target_type "workflow_run" (review W9) - rather than
    the proposals, which are only incidental to an expiry.
    """
    try:
        now_iso = _now_iso()
    except Exception:  # domain: degrade-silently
        return 0

    def _close_visible(where_tail: str, params: list[object]) -> int:
        """Select ids first, then close exactly those, and log them."""
        rows = conn.execute(
            "SELECT id FROM workflow_runs WHERE " + where_tail, params
        ).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            return 0
        cur = conn.execute(
            "UPDATE workflow_runs SET status = 'closed', decided_at = ?"
            " WHERE id IN ({})".format(",".join("?" * len(ids))),
            [now_iso, *ids],
        )
        closed = int(cur.rowcount) if cur.rowcount else 0
        if closed:
            detail = {"reason": "ttl_expired", "count": closed, "run_ids": ids}
            if proposal_ids is not None and len(ids) == 1:
                detail["proposal_id"] = proposal_ids[0]
            try:
                log_event(
                    EVT_WORKFLOW_CLOSED,
                    target_type="workflow_run",
                    detail=detail,
                    conn=conn,
                )
            except Exception:  # domain:degrade-silently - event is enrichment
                pass
        return closed

    if proposal_ids is None:
        return _close_visible(
            "status = 'open' AND expires_at IS NOT NULL AND expires_at <= ?",
            [now_iso],
        )
    if not proposal_ids:
        return 0
    # Sweep per chunk of proposal ids (review D8); each chunk logs its own
    # run_ids so the audit trail is exact whether the caller passed one id
    # (require_workflow_block) or a whole docket.
    total = 0
    for chunk in _id_chunks([int(p) for p in proposal_ids]):
        total += _close_visible(
            "proposal_id IN ({}) AND status = 'open'".format(",".join("?" * len(chunk)))
            + " AND expires_at IS NOT NULL AND expires_at <= ?",
            [*chunk, now_iso],
        )
    return total


def _open_workflow_runs_for(conn: sqlite3.Connection, agent_id: int) -> list:
    """Open workflow runs awaiting `agent_id`: runs on proposals where the
    agent is author or delegate, else runs the agent started. Each row is
    enriched with `prior_closes` - how many earlier decided runs exist for
    the same (workflow_path, proposal_id), which flags a lazily re-opened
    run (review W1) - and `collabs`, the collaborator names on a
    collaborative proposal (review W10)."""
    base = (
        "SELECT wr.id, wr.workflow_path, wr.proposal_id, wr.expires_at, p.title,"
        " (SELECT COUNT(*) FROM workflow_runs pr"
        "   WHERE pr.workflow_path = wr.workflow_path"
        "   AND pr.proposal_id = wr.proposal_id"
        "   AND pr.status != 'open' AND pr.created_at <= wr.created_at)"
        "   AS prior_closes,"
        " (SELECT group_concat(a.name, ', ') FROM proposal_collaborators c"
        "   JOIN agents a ON a.id = c.agent_id"
        "   WHERE c.proposal_id = wr.proposal_id) AS collabs"
        " FROM workflow_runs wr JOIN posts p ON p.id = wr.proposal_id"
    )
    rows = conn.execute(
        base + " WHERE wr.status = 'open' AND (p.agent_id = ? OR p.delegate_id = ?)"
        " ORDER BY wr.created_at DESC LIMIT ?",
        (agent_id, agent_id, 3),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            base + " WHERE wr.status = 'open' AND wr.agent_id = ?"
            " ORDER BY wr.created_at DESC LIMIT ?",
            (agent_id, 3),
        ).fetchall()
    return rows


def _workflow_nudge_impl(conn: sqlite3.Connection, agent_id: int) -> dict:
    """The actual nudge builder: data-driven while a workflow run awaits the
    agent. Quiet when none. Wrapped by `_workflow_nudge` (review D1) so a
    defect here can never break profile reads."""
    try:
        enforce = int(config.WORKFLOW_ENFORCE)
    except Exception:  # domain: degrade-silently
        enforce = 1
    # Sweep first so stale never nudges
    try:
        sweep_expired_workflows(conn)
    except Exception:  # domain: degrade-silently
        pass
    rows = _open_workflow_runs_for(conn, agent_id)
    if not rows:
        return {}
    now = datetime.now(timezone.utc)
    summaries = []
    runs = []
    for r in rows:
        action = "reopened" if int(r["prior_closes"] or 0) > 0 else "open"
        expires_in = None
        if r["expires_at"]:
            try:
                expires_in = max(
                    0, int((_parse_iso(r["expires_at"]) - now).total_seconds())
                )
            except Exception:  # domain:degrade-silently - display-only enrichment
                pass
        label = f"{r['workflow_path']} for #{r['proposal_id']} ({r['title'][:40]})"
        if expires_in is not None:
            label += f" (expires in {expires_in // 60}m)"
        if action == "reopened":
            label += " [reopened after a close]"
        summaries.append(label)
        d = {
            "id": r["id"],
            "workflow_path": r["workflow_path"],
            "proposal_id": r["proposal_id"],
            "title": r["title"],
            "workflow_action": action,
            "expires_in_seconds": expires_in,
        }
        if r["collabs"]:
            d["collaborators"] = r["collabs"]
        runs.append(d)
    joined = ", ".join(summaries)
    if len(rows) > 3:
        joined += f" and {len(rows) - 3} more"
    mode = "blocking" if enforce else "advisory"
    note = (
        f"You have {len(rows)} workflow(s) open ({mode}) — {joined}. "
        "Follow the checklist in workflows/*.md (create-pr: update-local -> validate-manifest -> not-gutted -> lint -> test -> open). "
        "Runs auto-close when the linked PR is merged/declined/closed or when the proposal's TTL elapses."
    )
    if any(rd["workflow_action"] == "reopened" for rd in runs):
        note += (
            " A [reopened] run was lazily re-opened after a prior close "
            "(declined/closed PR or TTL) - the checklist starts fresh."
        )
    return {"workflow_note": note, "workflow_runs": runs}


def _workflow_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Data-driven nudge while a workflow run awaits the agent. Quiet when none.

    The whole body is guarded: this runs inside every profile-ish read
    (my_profile / whoami / check_in), so a workflow-maintenance defect must
    never 500 the profile (review D1)."""
    try:
        return _workflow_nudge_impl(conn, agent_id)
    except (
        Exception
    ):  # domain:degrade-silently - the profile always answers; the nudge is enrichment
        return {}


def list_workflow_runs(
    conn: sqlite3.Connection,
    agent_id: int | None = None,
    status: str | None = None,
    proposal_id: int | None = None,
) -> list[dict]:
    """Recent workflow_runs, newest first. Filters: `agent_id` (as starter,
    proposal author or delegate), `status`, `proposal_id`."""
    clauses: list[str] = []
    params: list[object] = []
    if agent_id is not None:
        clauses.append("(wr.agent_id = ? OR p.agent_id = ? OR p.delegate_id = ?)")
        params.extend([agent_id, agent_id, agent_id])
    if status is not None:
        clauses.append("wr.status = ?")
        params.append(status)
    if proposal_id is not None:
        clauses.append("wr.proposal_id = ?")
        params.append(proposal_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT wr.id, wr.workflow_path, wr.workflow_sha, wr.proposal_id, wr.pr_number,"
        f" wr.agent_id, a.name AS agent_name, wr.status, wr.created_at, wr.decided_at,"
        f" wr.expires_at, p.title"
        f" FROM workflow_runs wr"
        f" JOIN posts p ON p.id = wr.proposal_id"
        f" LEFT JOIN agents a ON a.id = wr.agent_id{where}"
        f" ORDER BY wr.created_at DESC LIMIT 50",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
