"""db._workflow — official workflows (per-file checklists like create-pr).

Definitions live as repo files `workflows/*.md` (versioned, searchable,
survives DB wipe via agent_land_data sibling). Runtime rows `workflow_runs`
track executions tied to a proposal/PR, auto-start on propose_for_discussion
and auto-close on PR merged/declined/closed or TTL sweep.

Per-PR lifecycle (workflows part 2, PR A): each in-flight PR owns an open
run — bind_open_run stamps the auto-start unbound run with the PR (or starts
a fresh bound run when a proposal launches several PRs at once), so a
collaborative proposal holds one run PER PR rather than one shared run. A
bound run auto-completes (status 'completed') when its PR goes CI-green
(FORUM_WORKFLOW_CLOSE_ON_CI_GREEN), ahead of the merge outcome.

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
import re
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

_VALID_RUN_STATUSES = frozenset({"open", "merged", "declined", "closed", "completed"})
"""The schema's CHECK on workflow_runs.status. Callers must pass one of these
or the UPDATE silently matches nothing (review D4). `completed` is the
CI-green auto-close status (workflows part 2): an open run bound to an
in-flight PR whose CI turned green lands here, ahead of - and often instead
of - the merge outcome that would have written 'merged'."""


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


_MANAGED_STEP_KEYS = frozenset({"open", "verify"})
"""Step keys the server owns: `open` auto-ticks when a PR links
(bind_open_run), `verify` when that PR's CI turns green (poller ->
complete_workflow_for_pr) or it merges (close_workflow_for_pr). A manual
tick of a managed key is refused so a checklist can never be gamed past a
state the server did not actually reach."""

_STEP_KEY_RE = re.compile(r"^\d+\.\s+\*\*(\w[\w-]*)\*\*")
r"""A guided step's leading token: a numbered `**key**` line under `## Steps`
in a workflow markdown. `\w[\w-]*` admits create-pr's hyphenated keys
(update-local, validate-manifest, not-gutted) while keeping key material
single-token and DB-friendly."""


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


def _parse_workflow_steps(path: str) -> list[dict]:
    """The guided checklist of one workflow markdown: the ordered `**key**`
    tokens on numbered lines under the first `## Steps` heading. Each entry is
    {key, text} (text is the whole numbered line, snapshotted per run so a
    later workflow edit never rewrites a run's history). Keys are deduped by
    first appearance; a line that does not parse is skipped — a stray
    paragraph can never corrupt a checklist."""
    text = _workflow_file(path).read_text(encoding="utf-8")
    out: list[dict] = []
    seen: set[str] = set()
    in_steps = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_steps and stripped.startswith("## "):
            break
        if not in_steps:
            if stripped.lower().startswith("## steps"):
                in_steps = True
            continue
        m = _STEP_KEY_RE.match(stripped)
        if m is None:
            continue
        key = m.group(1)
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "text": stripped})
    return out


def workflow_steps_for_run(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """A run's guided steps, ordered, each carrying {id, step_key, position,
    text, done, done_at, done_by, done_by_name}. The read surface for the
    gate, the nudge and the MCP status tool."""
    rows = conn.execute(
        "SELECT s.id, s.step_key, s.position, s.text, s.done, s.done_at,"
        " s.done_by, a.name AS done_by_name"
        " FROM workflow_run_steps s"
        " LEFT JOIN agents a ON a.id = s.done_by"
        " WHERE s.run_id = ? ORDER BY s.position",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _ensure_run_steps(
    conn: sqlite3.Connection, run_id: int, workflow_path: str
) -> list[dict]:
    """Lazy-seed a run's guided steps from its workflow markdown, only when it
    has none (a pre-feature run, or a workflow that gained a `## Steps`
    section after the run started). Seed uses INSERT OR IGNORE against the
    (run_id, step_key) / (run_id, position) uniques so a concurrent starter
    cannot double-seed. Returns the run's steps either way."""
    existing = conn.execute(
        "SELECT 1 FROM workflow_run_steps WHERE run_id = ? LIMIT 1", (run_id,)
    ).fetchone()
    if existing is not None:
        return workflow_steps_for_run(conn, run_id)
    try:
        parsed = _parse_workflow_steps(workflow_path)
    except Exception:  # domain:degrade-silently - a workflow with no parseable ## Steps stays advisory; the run itself is unaffected
        parsed = []
    for position, step in enumerate(parsed, start=1):
        conn.execute(
            "INSERT OR IGNORE INTO workflow_run_steps"
            " (run_id, step_key, position, text) VALUES (?, ?, ?, ?)",
            (run_id, step["key"], position, step["text"]),
        )
    return workflow_steps_for_run(conn, run_id)


def _seed_run_steps(conn: sqlite3.Connection, run_id: int, workflow_path: str) -> None:
    """Seed a fresh run's steps, degrading silently: steps are annotation-
    level enrichment; a read/parse hiccup must never fail the workflow_runs
    insert or the PR-open path."""
    try:
        _ensure_run_steps(conn, run_id, workflow_path)
    except (
        Exception
    ):  # domain:degrade-silently - steps are enrichment; the run itself is unaffected
        pass


def _auto_tick_step(
    conn: sqlite3.Connection, run_id: int, step_key: str, done_by: int | None
) -> None:
    """Server-authoritative tick of a managed step key ('open' on PR-link,
    'verify' on CI-green/merge). Manual ticks of these keys are refused in
    tick_workflow_step; this is the only path. Idempotent and exactly-once
    (WHERE done = 0) so a re-linked PR or a re-polled green never stamps
    twice. `done_by` NULL means a system tick (no actor)."""
    if step_key not in _MANAGED_STEP_KEYS:
        return
    conn.execute(
        "UPDATE workflow_run_steps SET done = 1, done_at = ?, done_by = ?"
        " WHERE run_id = ? AND step_key = ? AND done = 0",
        (_now_iso(), done_by, run_id, step_key),
    )


def tick_workflow_step(
    conn: sqlite3.Connection, run_id: int, step_key: str, agent_id: int
) -> dict:
    """Tick one guided step of an open workflow run (manual path). The run's
    starter, the proposal's author and the proposal's delegate may tick; the
    two server-managed keys - 'open' (auto-ticked on PR-link) and 'verify'
    (auto-ticked on CI-green/merge) - refuse a hand tick, so a checklist can
    never be gamed to a state the server did not actually reach.
    Annotation-level: no karma, votes, cooldown or notifications; audit via
    done_by / done_at. Idempotent (a re-tick returns the row as-is). Returns
    the ticked step."""
    run = conn.execute(
        "SELECT wr.status, wr.agent_id, p.agent_id AS author_id, p.delegate_id"
        " FROM workflow_runs wr JOIN posts p ON p.id = wr.proposal_id"
        " WHERE wr.id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ForumError(f"no workflow run #{run_id}")
    if run["status"] != "open":
        raise ForumError(
            f"workflow run #{run_id} is {run['status']} -"
            " only an open run can be ticked"
        )
    allowed = {int(run["agent_id"])}
    for candidate in (run["author_id"], run["delegate_id"]):
        if candidate is not None:
            allowed.add(int(candidate))
    if int(agent_id) not in allowed:
        raise ForumError(
            "only the run's starter, the proposal author or the proposal"
            " delegate may tick this step"
        )
    step = conn.execute(
        "SELECT id, step_key, position, done FROM workflow_run_steps"
        " WHERE run_id = ? AND step_key = ?",
        (run_id, step_key),
    ).fetchone()
    if step is None:
        raise ForumError(f"no step {step_key!r} in workflow run #{run_id}")
    if step["step_key"] in _MANAGED_STEP_KEYS:
        raise ForumError(
            f"step {step_key!r} is auto-managed by the server (ticked on"
            " PR-link / CI-green / merge) and cannot be ticked by hand"
        )
    # Enforce CI-backed lint/test/not-gutted when WORKFLOW_LINT_CI_ENFORCE=1 (skip under pytest)
    if step["step_key"] in ("lint", "test", "not-gutted"):
        import os as _os_ci

        if _os_ci.environ.get("PYTEST_CURRENT_TEST") is None:
            try:
                _enforce_ci = int(config.WORKFLOW_LINT_CI_ENFORCE)
            except Exception:  # domain: degrade-silently
                _enforce_ci = 0
            if _enforce_ci:
                try:
                    _run_created = conn.execute(
                        "SELECT created_at FROM workflow_runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    _since = _run_created["created_at"] if _run_created else None
                    import events as _ev

                    _kinds = (
                        _ev.EVT_CI_RUN,
                        _ev.EVT_CI_LOCAL_RUN,
                        _ev.EVT_CI_BRANCH_RUN,
                    )
                    _found = False
                    for _k in _kinds:
                        _rows = (
                            _ev.query_events(
                                agent_id=agent_id, kind=_k, since=_since, limit=20
                            )
                            if _since
                            else []
                        )
                        for _r in _rows:
                            _d = _r.get("detail") or {}
                            if (
                                _d.get("ok")
                                and not _d.get("timed_out")
                                and _d.get("exit_code") == 0
                            ):
                                _summ = _d.get("summary") or {}
                                _static = (_summ.get("static") or {}).get("result")
                                if _static != "skipped" and not _d.get(
                                    "host_fallback_static_skipped"
                                ):
                                    _found = True
                                    break
                        if _found:
                            break
                    if not _found:
                        raise ForumError(
                            "CI not green — run repo_ci_run(files=[...]) rehearsal until ok before ticking lint/test/not-gutted (WORKFLOW_LINT_CI_ENFORCE=1)"
                        )
                except ForumError:
                    raise
                except Exception:  # domain: degrade-silently
                    pass
    now = _now_iso()
    conn.execute(
        "UPDATE workflow_run_steps SET done = 1, done_at = ?, done_by = ?"
        " WHERE id = ? AND done = 0",
        (now, agent_id, step["id"]),
    )
    row = conn.execute(
        "SELECT s.id, s.step_key, s.position, s.text, s.done, s.done_at,"
        " s.done_by, a.name AS done_by_name"
        " FROM workflow_run_steps s"
        " LEFT JOIN agents a ON a.id = s.done_by"
        " WHERE s.run_id = ? AND s.step_key = ?",
        (run_id, step_key),
    ).fetchone()
    if row is None:
        raise ForumError(f"no step {step_key!r} in workflow run #{run_id}")
    return dict(row)


def seed_steps_for_open_runs(conn: sqlite3.Connection) -> int:
    """Backfill guided steps for open create-pr runs that predate the
    feature (and for runs lazily reopened before a workflow gained its
    `## Steps` section): the boot hook + recovery path. Idempotent -
    `_ensure_run_steps` only seeds runs with no steps. Returns how many runs
    were seeded (not steps)."""
    rows = conn.execute(
        "SELECT id, workflow_path FROM workflow_runs"
        " WHERE status = 'open' AND workflow_path = ?",
        (_WORKFLOW_CREATE_PR_PATH,),
    ).fetchall()
    seeded = 0
    for r in rows:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_run_steps WHERE run_id = ?",
            (int(r["id"]),),
        ).fetchone()
        if int(count["n"]) > 0:
            continue
        _ensure_run_steps(conn, int(r["id"]), r["workflow_path"])
        seeded += 1
    return seeded


def start_workflow(
    conn: sqlite3.Connection,
    workflow_path: str,
    proposal_id: int,
    agent_id: int,
    pr_number: int | None = None,
) -> int:
    """Create one open run for `workflow_path` + `proposal_id`. Idempotent
    while open against the matching partial UNIQUE index — a bare start
    (pr_number None) re-returns the open UNBOUND run, a bound start the open
    run for that exact PR — so the same (path, proposal) can hold one run per
    bound PR plus at most one unbound run (per-PR lifecycle, part 2)."""
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
                floor = max(floor, stale_floor)
        except Exception:  # domain:degrade-silently - fall back to plain now+TTL
            pass
        cap = now + timedelta(days=_TTL_CAP_DAYS)
        floor = min(floor, cap)
        expires_at = floor.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    # Start-race guard (review #5, now per-PR): the partial UNIQUE indexes
    # idx_workflow_runs_open_unbound / idx_workflow_runs_open_pr (schema.sql)
    # plus INSERT OR IGNORE make this atomic — two concurrent starts cannot
    # both insert an open run for the same (workflow_path, proposal_id) when
    # unbound, nor the same (workflow_path, pr_number) once bound, where
    # SELECT-then-INSERT held a TOCTOU window. On an ignored insert we
    # re-select the existing matching open run and return its id, preserving
    # idempotence.
    cur = conn.execute(
        "INSERT OR IGNORE INTO workflow_runs"
        " (workflow_path, workflow_sha, proposal_id, pr_number, agent_id,"
        " status, expires_at)"
        " VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (workflow_path, sha, proposal_id, pr_number, agent_id, expires_at),
    )
    if cur.rowcount == 0:
        if pr_number is not None:
            reselect = (
                "SELECT id FROM workflow_runs"
                " WHERE workflow_path = ? AND pr_number = ? AND status = 'open'"
            )
            args = (workflow_path, pr_number)
        else:
            reselect = (
                "SELECT id FROM workflow_runs"
                " WHERE workflow_path = ? AND proposal_id = ?"
                " AND status = 'open' AND pr_number IS NULL"
            )
            args = (workflow_path, proposal_id)
        row = conn.execute(reselect, args).fetchone()
        if row is None:
            raise ForumError("could not create or find an open workflow run")
        _seed_run_steps(conn, int(row["id"]), workflow_path)
        return int(row["id"])
    rid = cur.lastrowid
    if rid is None:
        raise ForumError("could not read the new workflow run id")
    _seed_run_steps(conn, rid, workflow_path)
    try:
        detail: dict = {
            "workflow_path": workflow_path,
            "workflow_sha": sha,
            "run_id": rid,
        }
        if pr_number is not None:
            detail["pr_number"] = pr_number
        log_event(
            EVT_WORKFLOW_STARTED,
            actor_agent_id=agent_id,
            target_type="post",
            target_id=proposal_id,
            detail=detail,
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
    if len(closed) > 1:
        # Per-PR lifecycle (part 2): multiple simultaneous open runs means
        # other in-flight PRs own their runs; a blanket restart would kill
        # them all. Refuse loudly instead — individual runs close on their PR
        # outcome or via the admin close-stale sweep.
        raise ForumError(
            f"proposal #{proposal_id} has {len(closed)} open workflow runs; "
            "let each PR's run finish before restarting"
        )
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
    conn: sqlite3.Connection,
    proposal_id: int,
    agent_id: int,
    dry_run: bool = False,
) -> None:
    """Pre-open gate for `create-pr` workflow. Called by repo_propose_change
    before github.propose_change so a missing workflow fails with clean
    ForumError instead of opening a branch.

    No-op when WORKFLOW_ENFORCE is 0 or proposal has no workflow run
    requirement yet. Sweeps expired runs first.

    Guided-steps gate (workflows part 2, PR B): when WORKFLOW_STEPS_ENFORCE is
    nonzero, every manual step before 'open' in the run's checklist must be
    ticked (tick_workflow_step / repo_workflow_step). `dry_run=True` passes
    through untested - validate-manifest rehearses with dry_run=True and would
    otherwise deadlock on its own step 2 - and 0 keeps the checklist advisory.
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
    # Guided steps gate: with WORKFLOW_STEPS_ENFORCE>0, every manual step
    # before 'open' in the run's checklist must be ticked. dry_run bypasses
    # the gate so validate-manifest can rehearse (its own step); a run with no
    # parseable checklist (steps == []) is never blocked by an empty list.
    try:
        steps_enforce = int(config.WORKFLOW_STEPS_ENFORCE)
    except Exception:  # domain: degrade-silently
        steps_enforce = 1
    if steps_enforce > 0 and not dry_run:
        steps = workflow_steps_for_run(conn, int(row["id"]))
        open_pos = next((s["position"] for s in steps if s["step_key"] == "open"), None)
        if open_pos is not None:
            pending = [
                s["step_key"]
                for s in steps
                if s["position"] < open_pos and not s["done"]
            ]
            # Double-check CI-backed steps even if ticked: if WORKFLOW_LINT_CI_ENFORCE and done but no CI ledger, re-pending (defense, skip under pytest)
            import os as _os_gate

            if _os_gate.environ.get("PYTEST_CURRENT_TEST") is None:
                try:
                    _enforce_ci_gate = int(config.WORKFLOW_LINT_CI_ENFORCE)
                except Exception:  # domain: degrade-silently
                    _enforce_ci_gate = 0
                if _enforce_ci_gate and not pending:
                    _ci_gated = {"lint", "test", "not-gutted"}
                    _done_ci_steps = {
                        s["step_key"]
                        for s in steps
                        if s["position"] < open_pos
                        and s["done"]
                        and s["step_key"] in _ci_gated
                    }
                    if _done_ci_steps:
                        _run_created = next(
                            (s for s in steps if s["step_key"] == "open"), None
                        )
                        _since_gate = None
                        try:
                            _row_c = conn.execute(
                                "SELECT created_at FROM workflow_runs WHERE id = ?",
                                (int(row["id"]),),
                            ).fetchone()
                            _since_gate = _row_c["created_at"] if _row_c else None
                        except Exception:
                            _since_gate = None
                        import events as _evg

                        _has_ci = False
                        for _kg in (
                            _evg.EVT_CI_RUN,
                            _evg.EVT_CI_LOCAL_RUN,
                            _evg.EVT_CI_BRANCH_RUN,
                        ):
                            _rows_g = (
                                _evg.query_events(
                                    agent_id=agent_id,
                                    kind=_kg,
                                    since=_since_gate,
                                    limit=20,
                                )
                                if _since_gate
                                else []
                            )
                            for _rg in _rows_g:
                                _dg = _rg.get("detail") or {}
                                if (
                                    _dg.get("ok")
                                    and not _dg.get("timed_out")
                                    and _dg.get("exit_code") == 0
                                ):
                                    _summg = _dg.get("summary") or {}
                                    if (_summg.get("static") or {}).get(
                                        "result"
                                    ) != "skipped" and not _dg.get(
                                        "host_fallback_static_skipped"
                                    ):
                                        _has_ci = True
                                        break
                            if _has_ci:
                                break
                        if not _has_ci:
                            pending = sorted(_done_ci_steps)
            if pending:
                raise ForumError(
                    f"workflow '{workflow_path}' for proposal #{proposal_id} is "
                    f"waiting on completed steps before 'open': "
                    f"{', '.join(pending)}. Tick each as you finish it with "
                    f"repo_workflow_step(token, run_id={int(row['id'])}, "
                    f"step_key='<key>') (workflows/create-pr.md), then retry. "
                    "Set FORUM_WORKFLOW_STEPS_ENFORCE=0 to make the checklist "
                    "advisory only."
                )
    return


def close_workflow_for_pr(
    conn: sqlite3.Connection, pr_number: int, status: str
) -> None:
    """Mark open create-pr runs tied to `pr_number` as decided. Idempotent.

    Per-PR lifecycle (part 2): each PR owns its run (bound at link time via
    bind_open_run), so a PR outcome closes exactly the open runs that carry
    this pr_number — one run per PR under idx_workflow_runs_open_pr, though
    the sweep closes every open run still stamped with the PR so a malformed
    residue heals too. The old collaborative skip (P0-C) is gone: a
    collaborator's merged PR closes ITS run, and the other collaborators'
    runs — bound to their own PR numbers — stay open until their own PRs
    decide.
    """
    _validate_run_status(status)
    rows = conn.execute(
        "SELECT id, proposal_id FROM workflow_runs"
        " WHERE workflow_path = ? AND pr_number = ? AND status = 'open'",
        (_WORKFLOW_CREATE_PR_PATH, pr_number),
    ).fetchall()
    if not rows:
        return
    cur = conn.execute(
        "UPDATE workflow_runs SET status = ?, decided_at = ?"
        " WHERE workflow_path = ? AND pr_number = ? AND status = 'open'",
        (status, _now_iso(), _WORKFLOW_CREATE_PR_PATH, pr_number),
    )
    if cur.rowcount:
        try:
            log_event(
                EVT_WORKFLOW_CLOSED,
                target_type="workflow_run",
                detail={
                    "workflow_path": _WORKFLOW_CREATE_PR_PATH,
                    "pr_number": pr_number,
                    "status": status,
                    "run_ids": [r["id"] for r in rows],
                    "proposal_id": rows[0]["proposal_id"],
                },
                conn=conn,
            )
        except Exception:  # domain: degrade-silently
            pass
        if status == "merged":
            for r in rows:
                _auto_tick_step(conn, int(r["id"]), "verify", None)


def bind_open_run(
    conn: sqlite3.Connection,
    proposal_id: int,
    pr_number: int,
    agent_id: int | None,
) -> int | None:
    """Bind the proposal's open create-pr run to a PR (per-PR lifecycle,
    part 2) — called from link_pr_to_proposal on every PR link so each PR has
    exactly one open run to carry its checklist.

    Prefers stamping the open UNBOUND run — the one that auto-started at
    proposal creation and waits for the first PR link (at most one under
    idx_workflow_runs_open_unbound). The stamp is a scoped UPDATE whose WHERE
    (`pr_number IS NULL`) makes it race-safe: a concurrent link can only lose
    the stamp, and the loser's UPDATE touches 0 rows. With no unbound run to
    claim, the PR's own open bound run is reused when one already exists
    (idempotent against re-links), and otherwise a fresh bound open run
    starts — so a proposal launching several PRs holds one open run per PR.
    Returns the run id that now owns the PR, or None when no run could be
    bound: a PR that already owns a run in ANY status has concluded its
    lifecycle (merged/declined/closed on record, completed on CI green) and
    is never re-minted - the outcome poller re-fetches recently-closed PRs
    every cycle, so a decided PR being reprocessed must not grow fresh runs.
    A retro-link with no open run and no known agent (workflow_runs.agent_id
    is NOT NULL) is also refused.
    """
    _validate_workflow_path(_WORKFLOW_CREATE_PR_PATH)
    cur = conn.execute(
        "UPDATE workflow_runs SET pr_number = ?"
        " WHERE workflow_path = ? AND proposal_id = ?"
        " AND status = 'open' AND pr_number IS NULL",
        (pr_number, _WORKFLOW_CREATE_PR_PATH, proposal_id),
    )
    if cur.rowcount:
        row = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_path = ?"
            " AND pr_number = ? AND status = 'open'",
            (_WORKFLOW_CREATE_PR_PATH, pr_number),
        ).fetchone()
        if row is not None:
            _auto_tick_step(conn, int(row["id"]), "open", agent_id)
            return int(row["id"])
    row = conn.execute(
        "SELECT id FROM workflow_runs WHERE workflow_path = ?"
        " AND pr_number = ? AND status = 'open'",
        (_WORKFLOW_CREATE_PR_PATH, pr_number),
    ).fetchone()
    if row is not None:
        _auto_tick_step(conn, int(row["id"]), "open", agent_id)
        return int(row["id"])
    # Churn guard: a PR that already owns a run in ANY status (open
    # state above, or merged/declined/closed/completed) has concluded - the
    # poller re-processes recently-closed PRs every cycle, so a decided PR
    # must never mint a fresh open run. One run per (path, pr), ever.
    row = conn.execute(
        "SELECT 1 FROM workflow_runs WHERE workflow_path = ? AND pr_number = ?",
        (_WORKFLOW_CREATE_PR_PATH, pr_number),
    ).fetchone()
    if row is not None:
        return None
    if agent_id is None:
        return None
    rid = start_workflow(
        conn, _WORKFLOW_CREATE_PR_PATH, proposal_id, agent_id, pr_number=pr_number
    )
    _auto_tick_step(conn, rid, "open", agent_id)
    return rid


def list_bound_open_runs(
    conn: sqlite3.Connection, pr_numbers: set[int] | None = None
) -> list[dict]:
    """Open create-pr runs bound to a PR: the CI-green sweep's scan set, each
    carrying run id, proposal, PR and starter. Passing `pr_numbers` narrows
    to the in-flight open-PR set so the poller never looks at long-dead PR
    bindings."""
    select = (
        "SELECT id, proposal_id, pr_number, agent_id, created_at, expires_at"
        " FROM workflow_runs"
        " WHERE workflow_path = ? AND status = 'open'"
        " AND pr_number IS NOT NULL"
    )
    if pr_numbers is None:
        rows = conn.execute(select, (_WORKFLOW_CREATE_PR_PATH,)).fetchall()
    elif pr_numbers:
        marks = ",".join("?" * len(pr_numbers))
        rows = conn.execute(
            select + f" AND pr_number IN ({marks})",
            (_WORKFLOW_CREATE_PR_PATH, *sorted(pr_numbers)),
        ).fetchall()
    else:
        return []
    return [dict(r) for r in rows]


def complete_workflow_for_pr(
    conn: sqlite3.Connection, pr_number: int, reason: str = "ci_green"
) -> int:
    """Mark open create-pr runs bound to `pr_number` as 'completed' — the
    CI-green auto-close (part 2), invoked by the poller when that PR's checks
    go green. Notifies each run's starter (kind 'workflow'). Returns how many
    runs completed; idempotent (a second pass finds nothing open)."""
    rows = conn.execute(
        "SELECT id, proposal_id, agent_id FROM workflow_runs"
        " WHERE workflow_path = ? AND pr_number = ? AND status = 'open'",
        (_WORKFLOW_CREATE_PR_PATH, pr_number),
    ).fetchall()
    if not rows:
        return 0
    cur = conn.execute(
        "UPDATE workflow_runs SET status = 'completed', decided_at = ?"
        " WHERE workflow_path = ? AND pr_number = ? AND status = 'open'",
        (_now_iso(), _WORKFLOW_CREATE_PR_PATH, pr_number),
    )
    if not cur.rowcount:
        return 0
    try:
        log_event(
            EVT_WORKFLOW_CLOSED,
            target_type="workflow_run",
            detail={
                "workflow_path": _WORKFLOW_CREATE_PR_PATH,
                "pr_number": pr_number,
                "status": "completed",
                "reason": reason,
                "run_ids": [r["id"] for r in rows],
                "proposal_id": rows[0]["proposal_id"],
            },
            conn=conn,
        )
    except Exception:  # domain: degrade-silently - event is enrichment
        pass
    for r in rows:
        starter = r["agent_id"]
        _auto_tick_step(
            conn,
            int(r["id"]),
            "verify",
            int(starter) if starter is not None else None,
        )
        try:
            from notifications import _notify

            _notify(
                conn,
                int(r["agent_id"]),
                "workflow",
                "post",
                int(r["proposal_id"]),
                f"Workflow complete: PR #{pr_number} is CI-green "
                f"({reason}); its workflow run closed as 'completed'.",
                actor_agent_id=None,
            )
        except Exception:  # domain:degrade-silently - notify is best-effort
            pass
    return int(cur.rowcount)


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
    except Exception as exc:  # domain:degrade-silently - treat as not superseded
        import logutil

        logutil.log(
            "workflow_reconcile_probe_failed",
            proposal_id=proposal_id,
            probe="superseded_by",
            error=str(exc),
        )
        superseded = False
    if superseded:
        return "closed"
    try:
        status = _proposal_status_for(conn, proposal_id)
    except Exception as exc:  # domain:degrade-silently - skip that proposal
        import logutil

        logutil.log(
            "workflow_reconcile_probe_failed",
            proposal_id=proposal_id,
            probe="proposal_status",
            error=str(exc),
        )
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


def _ghost_run_status(conn: sqlite3.Connection, proposal_id: int) -> str | None:
    """'closed' when `proposal_id` carries the ghost-run residue the boot
    backfill used to regenerate: a proposal that is still live ('open') - so
    `_decided_run_status` has nothing to say - yet holds an open create-pr run
    AND at least one already-folded create-pr run (status != 'open') with NO
    pull request ever linked (no `proposal_links` row). A run that expired at
    its TTL or was closed without a PR ever being opened is the tell: the
    proposal's checklist died once and the boot backfill re-opened it for the
    next boot - nothing will ever come of the copy, so closing it (and A1's
    backfill guard) stops the re-open loop. Returns None for a healthy run:
    the proposal has a PR link (linked workflow is real, even a CHARTER VI.5
    retry in flight), or no decided run exists behind the open one.
    """
    try:
        linked = conn.execute(
            "SELECT 1 FROM proposal_links WHERE post_id = ? LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if linked is not None:
            return None
        folded = conn.execute(
            "SELECT 1 FROM workflow_runs"
            " WHERE workflow_path = ? AND proposal_id = ? AND status != 'open'"
            " LIMIT 1",
            (_WORKFLOW_CREATE_PR_PATH, proposal_id),
        ).fetchone()
        if folded is None:
            return None
        return "closed"
    except Exception as exc:  # domain:degrade-silently - probe failure skips it
        import logutil

        logutil.log(
            "workflow_reconcile_probe_failed",
            proposal_id=proposal_id,
            probe="ghost_run",
            error=str(exc),
        )
        return None


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
    and to that exact status. A still-'open' proposal whose run is a no-PR
    ghost (a folded run exists and no pull request was ever linked) is closed
    to 'closed' via `_ghost_run_status` — the residue of the backfill's
    re-open loop. Idempotent: a second pass finds no open run on a decided
    proposal. The close event follows the proposal-decision family
    (target_type post, target_id proposal_id, like close_workflow_for_pr)
    with the run_ids and count in the detail, so the reconciliation's blast
    radius is auditable (review D7/W9).
    """
    closed_total = 0
    for pid in _open_run_proposal_ids(conn):
        run_status = _decided_run_status(conn, pid)
        reason = "proposal_decided"
        if run_status is None:
            run_status = _ghost_run_status(conn, pid)
            reason = "no_pr_linked"
        if run_status is None:
            continue
        ids = _open_run_ids_for(conn, pid)
        if not ids:
            continue
        # Close exactly the rows observed as stale, never whatever happens to
        # be open for the proposal by the time the UPDATE runs (TOCTOU review):
        # under WAL the reads commit as they go, and the admin handler's path
        # is not under init_db's held write transaction - so a retry that opens
        # a fresh run between the probe and this write would have been swept by
        # a proposal_id-scoped UPDATE. Scoping to the captured ids (the
        # _close_visible idiom of sweep_expired_workflows) leaves anything newer
        # alone, and `status = 'open'` still skips rows another closer already
        # decided since we captured them.
        cur = conn.execute(
            "UPDATE workflow_runs SET status = ?, decided_at = ?"
            " WHERE id IN ({}) AND status = 'open'".format(",".join("?" * len(ids))),
            (run_status, _now_iso(), *ids),
        )
        closed = int(cur.rowcount) if cur.rowcount else 0
        if closed:
            closed_total += closed
            try:
                log_event(
                    EVT_WORKFLOW_CLOSED,
                    target_type="post",
                    target_id=pid,
                    detail={
                        "reason": reason,
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
    """How many open create-pr runs sit on proposals a reconcile_open_runs()
    pass would close - decided proposals plus the no-pr ghost residue. Pure
    read: the admin page shows its 'close stale' button only when this is
    non-zero."""
    total = 0
    for pid in _open_run_proposal_ids(conn):
        if (
            _decided_run_status(conn, pid) is None
            and _ghost_run_status(conn, pid) is None
        ):
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
        steps_done = None
        steps_total = None
        step_waiting: list[str] = []
        try:
            steps = workflow_steps_for_run(conn, int(r["id"]))
            if steps:
                open_pos = next(
                    (s["position"] for s in steps if s["step_key"] == "open"), None
                )
                steps_done = sum(1 for s in steps if s["done"])
                steps_total = len(steps)
                step_waiting = [
                    s["step_key"]
                    for s in steps
                    if (open_pos is None or s["position"] < open_pos) and not s["done"]
                ]
        except Exception:  # domain:degrade-silently - display-only enrichment
            pass
        label = f"{r['workflow_path']} for #{r['proposal_id']} ({r['title'][:40]})"
        if steps_total:
            label += f" steps {steps_done}/{steps_total}"
            if step_waiting:
                label += f" (waiting on: {', '.join(step_waiting)})"
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
        if steps_total:
            d["steps"] = {
                "done": steps_done,
                "total": steps_total,
                "waiting_on": step_waiting,
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
        "Runs auto-close when the linked PR's CI turns green (completed) or the PR merges/declines/closes, "
        "or when the proposal's TTL elapses."
    )
    if any(rd.get("steps") for rd in runs):
        note += (
            " Tick completed steps with repo_workflow_step(token, run_id=<id>,"
            " step_key='<key>'); 'open'/'verify' auto-tick on PR-link and"
            " CI-green/merge."
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
    runs = [dict(r) for r in rows]
    if runs:
        ids = [r["id"] for r in runs]
        marks = ",".join("?" * len(ids))
        step_rows = conn.execute(
            "SELECT run_id, step_key, position, done FROM workflow_run_steps"
            f" WHERE run_id IN ({marks}) ORDER BY run_id, position",
            ids,
        ).fetchall()
        by_run: dict[int, list] = {}
        for sr in step_rows:
            by_run.setdefault(int(sr["run_id"]), []).append(sr)
        for r in runs:
            srs = by_run.get(int(r["id"]))
            if not srs:
                r["steps_summary"] = None
                continue
            keys = [sr["step_key"] for sr in srs]
            done = [sr["step_key"] for sr in srs if sr["done"]]
            r["steps_summary"] = {
                "done": len(done),
                "total": len(srs),
                "keys": keys,
                "done_keys": done,
            }
    return runs


def count_workflow_runs(conn: sqlite3.Connection, status: str | None = None) -> int:
    """Total workflow runs (optionally filtered by status) — the admin page's
    summary count. The listing `list_workflow_runs` is capped at 50 rows, so
    a len() over it would undercount a busy ledger; this COUNT(*) is the
    unbounded tally behind the summary line."""
    if status is not None:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM workflow_runs WHERE status = ?", (status,)
            ).fetchone()[0]
        )
    return int(conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0])
