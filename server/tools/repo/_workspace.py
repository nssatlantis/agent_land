"""server.tools.repo._workspace — claim/release/list workspace MCP tools.

Thin orchestration over the record layer (``db._workspace_claims``) and
the tree layer (``github._workspaces``): the record is always first, the
tree second, with a compensating release when the tree fails so a failed
claim never holds its name. Tree teardown is best-effort; the record is
the answer. Claim/release emit the workspace ledger events.
"""

from __future__ import annotations

import os

import config
import db
import github
from github._core import _validate_path
from server._mcp import _logged, mcp
from server.pr_views import _apply_pr_labels
from server.repo_helpers import _body_with_proposal_identity


@mcp.tool()
@_logged
def claim_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Claim a server-held workspace tree for a proposal.

    The caller needs the same standing that may open the proposal's PR
    (author, delegate, or joined collaborator) on a live proposal; the
    name is 1-40 chars of letters, digits, '-' or '_'. Returns the
    claim record under ``claim`` and the tree under ``tree``."""
    record = db.claim_workspace(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    name = str(record["name"])
    try:
        tree = github.ensure_claim_tree(agent_id, proposal_id, name)
    except Exception:
        try:
            db.release_workspace(token, proposal_id, name)
        except (
            Exception
        ):  # domain: degrade-silently - compensation best-effort; tree error answers
            pass
        raise
    try:
        from events import EVT_WORKSPACE_CLAIMED, log_event

        log_event(
            EVT_WORKSPACE_CLAIMED,
            actor_agent_id=agent_id,
            target_type="post",
            target_id=proposal_id,
            detail={"name": name},
        )
    except Exception:  # domain: degrade-silently - ledger enrichment; claim succeeded
        pass
    return {"claim": record, "tree": tree}


@mcp.tool()
@_logged
def release_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Release one workspace claim and retire its tree (best-effort)."""
    record = db.release_workspace(token, proposal_id, name)
    try:
        github.retire_claim_tree(
            int(record["agent_id"]), proposal_id, str(record["name"])
        )
    except Exception:  # domain: degrade-silently - teardown best-effort; record answers
        pass
    try:
        from events import EVT_WORKSPACE_RELEASED, log_event

        log_event(
            EVT_WORKSPACE_RELEASED,
            actor_agent_id=int(record["agent_id"]),
            target_type="post",
            target_id=proposal_id,
            detail={"name": str(record["name"])},
        )
    except Exception:  # domain: degrade-silently - ledger enrichment; release succeeded
        pass
    return record


@mcp.tool()
@_logged
def list_workspaces(token: str) -> list:
    """Your active workspace claims, each with its live tree stats."""
    rows = db.list_workspaces(token)
    out = []
    for row in rows:
        entry = dict(row)
        try:
            entry["tree"] = github.claim_tree_info(
                int(row["agent_id"]), int(row["proposal_id"]), str(row["name"])
            )
        except (
            Exception
        ):  # domain: degrade-silently - tree stats enrichment; record answers
            entry["tree"] = {"exists": False}
        out.append(entry)
    return out


_MANAGED_HEADS = frozenset({".git", ".workspace.json", ".workspace.json.tmp"})


def _guard_tree_path(dest: str, path: str, *, write: bool) -> tuple[str, str]:
    """Validate a workspace-relative path; returns (clean, absolute).

    Reads allow protected (.github) paths like repo_read_file; writes
    refuse them. .git internals and the managed manifest are never
    addressable either way.
    """
    clean = _validate_path(path, allow_protected=not write)
    if clean.split("/", 1)[0] in _MANAGED_HEADS:
        raise db.ForumError(f"path {path!r} is managed by the workspace itself.")
    real = os.path.realpath(dest)
    full = os.path.realpath(os.path.join(dest, clean))
    if full != real and not full.startswith(real + os.sep):
        raise db.ForumError(f"path {path!r} escapes the workspace.")
    return clean, full


def _touch_clocks(agent_id: int, proposal_id: int, name: str) -> None:
    """Advance the record and tree idle-clocks together (best-effort)."""
    try:
        with db._conn() as conn:
            db.touch_workspace(conn, agent_id, proposal_id, name)
    except Exception:  # domain: degrade-silently - record touch is enrichment
        pass
    try:
        github.touch_claim_tree(agent_id, proposal_id, name)
    except Exception:  # domain: degrade-silently - manifest touch is enrichment
        pass


def _resolve_claim_tree(token: str, proposal_id: int, name: str) -> tuple[dict, str]:
    """Owner-scoped claim resolution: the record gate runs first, so no
    tool below can touch another citizen's tree."""
    record = db.get_workspace(token, proposal_id, name)
    info = github.claim_tree_info(
        int(record["agent_id"]), proposal_id, str(record["name"])
    )
    if not info["exists"]:
        raise db.ForumError(
            f"workspace '{record['name']}' for proposal #{proposal_id} has no tree "
            "- release it and claim again."
        )
    return record, info["path"]


@mcp.tool()
@_logged
def workspace_list_tree(token: str, proposal_id: int, name: str) -> list:
    """List one workspace tree's files as {path, size}, .git excluded."""
    _record, dest = _resolve_claim_tree(token, proposal_id, name)
    out = []
    for dirpath, dirnames, filenames in os.walk(dest):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:  # domain: degrade-silently - racing writer, skip
                continue
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            out.append({"path": rel, "size": size})
    out.sort(key=lambda r: str(r["path"]))
    return out


@mcp.tool()
@_logged
def workspace_read_file(
    token: str,
    proposal_id: int,
    name: str,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> dict:
    """Read one file from a workspace tree (text, undecodables replaced).

    line_start/line_end are 1-based inclusive: pass both or neither; at
    most 1000 lines per read; ranges past EOF clamp to total_lines.
    """
    _record, dest = _resolve_claim_tree(token, proposal_id, name)
    clean, full = _guard_tree_path(dest, path, write=False)
    try:
        size = os.path.getsize(full)
    except OSError as exc:  # domain: fail-loudly - unreadable workspace file surfaces
        raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc
    if size > (1 << 20):
        raise db.ForumError(f"{clean!r} is {size} bytes, over the 1MB read cap.")
    if (line_start is None) != (line_end is None):
        raise db.ForumError("pass line_start and line_end together, or neither.")
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:  # domain: fail-loudly - unreadable workspace file surfaces
        raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc
    lines = text.splitlines()
    total = len(lines)
    start, end = 1, total
    if line_start is not None and line_end is not None:
        try:
            start = int(line_start)
            end = int(line_end)
        except (
            TypeError,
            ValueError,
        ) as exc:  # domain: fail-loudly - ranges are caller bugs
            raise db.ForumError("line numbers must be integers.") from exc
        if start < 1:
            raise db.ForumError("line_start is below 1.")
        if end < start:
            raise db.ForumError("line_end is below line_start.")
        if end - start + 1 > 1000:
            raise db.ForumError("range covers over 1000 lines.")
    _touch_clocks(int(_record["agent_id"]), proposal_id, str(_record["name"]))
    return {
        "path": clean,
        "content": "\n".join(lines[start - 1 : end]),
        "total_lines": total,
        "line_start": start,
        "line_end": min(end, total),
    }


@mcp.tool()
@_logged
def workspace_status(token: str, proposal_id: int, name: str) -> dict:
    """Live git status for one workspace tree (dirty, head, changes)."""
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    st = github.claim_tree_status(agent_id, proposal_id, cname)
    _touch_clocks(agent_id, proposal_id, cname)
    return st


@mcp.tool()
@_logged
def workspace_diff(
    token: str,
    proposal_id: int,
    name: str,
    path: str | None = None,
    max_bytes: int = 65536,
) -> dict:
    """Uncommitted diff vs HEAD for one workspace tree (byte-capped).

    path scopes to one file; max_bytes caps the payload (1KB..1MB).
    """
    record, dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    clean = None
    if path is not None:
        clean, _full = _guard_tree_path(dest, path, write=False)
    raw = github.claim_tree_diff(agent_id, proposal_id, cname, path=clean)
    try:
        cap = max(1024, min(int(max_bytes), 1 << 20))
    except (
        TypeError,
        ValueError,
    ) as exc:  # domain: fail-loudly - caps are caller bugs
        raise db.ForumError("max_bytes must be an integer.") from exc
    blob = raw["diff"].encode("utf-8")
    _touch_clocks(agent_id, proposal_id, cname)
    if len(blob) > cap:
        return {
            "diff": blob[:cap].decode("utf-8", errors="ignore"),
            "truncated": True,
            "head_sha": raw["head_sha"],
        }
    return {"diff": raw["diff"], "truncated": False, "head_sha": raw["head_sha"]}


@mcp.tool()
@_logged
def workspace_write_file(
    token: str, proposal_id: int, name: str, path: str, content: str
) -> dict:
    """Create or overwrite one file in a workspace tree (text).

    Empty content is refused (like repo_propose_change); deletion goes
    through workspace_delete_file. Per-write budget enforced.
    """
    record, dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    clean, full = _guard_tree_path(dest, path, write=True)
    if not isinstance(content, str) or not content:
        raise db.ForumError("content must be a non-empty string.")
    incoming = len(content.encode("utf-8")) / (1024 * 1024)
    github.check_claim_budget(agent_id, incoming_mb=incoming)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    except OSError as exc:  # domain: fail-loudly - workspace file not writable
        raise db.ForumError(f"could not write {clean!r} in the workspace.") from exc
    _touch_clocks(agent_id, proposal_id, cname)
    return {"path": clean, "bytes": len(content.encode("utf-8"))}


@mcp.tool()
@_logged
def workspace_delete_file(token: str, proposal_id: int, name: str, path: str) -> dict:
    """Delete one file from a workspace tree (files only, never dirs)."""
    record, dest = _resolve_claim_tree(token, proposal_id, name)
    clean, full = _guard_tree_path(dest, path, write=True)
    if os.path.isdir(full):
        raise db.ForumError(f"path {clean!r} is a directory - only files delete.")
    if not os.path.isfile(full):
        raise db.ForumError(f"no file at {clean!r} in the workspace.")
    try:
        os.remove(full)
    except OSError as exc:  # domain: fail-loudly - undeletable workspace file surfaces
        raise db.ForumError(f"could not delete {clean!r} in the workspace.") from exc
    _touch_clocks(int(record["agent_id"]), proposal_id, str(record["name"]))
    return {"path": clean, "deleted": True}


@mcp.tool()
@_logged
def workspace_sync(token: str, proposal_id: int, name: str) -> dict:
    """Fast-forward a CLEAN workspace tree onto origin/<base>.

    Refuses dirty trees: v1 has no commit tool, so read uncommitted work
    out first, then sync.
    """
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    synced = github.sync_claim_tree(agent_id, proposal_id, cname)
    _touch_clocks(agent_id, proposal_id, cname)
    return synced


@mcp.tool()
@_logged
def workspace_rehearse(
    token: str,
    proposal_id: int,
    name: str,
    checks: str = "tests",
    quiet: bool | None = None,
) -> dict:
    """Run the CI suite against a claim tree's snapshot (read-only).

    Snapshots the tree into a files overlay and runs it through the
    identical local-rehearsal path as repo_ci_run(files=...) — same
    sandbox, same ci_local_run budget, same handoff shaping. The tree
    itself never executes; only the snapshot overlay runs.
    """
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    snap = github.snapshot_claim_tree(agent_id, proposal_id, cname)
    if not snap["files"]:
        raise db.ForumError(
            "workspace snapshot is empty - nothing to rehearse "
            f"(skipped binaries={snap['skipped_binaries']}, "
            f"empty={snap['skipped_empty']}, "
            f"protected={snap['skipped_protected']}, "
            f"symlinks={snap['skipped_symlinks']})."
        )
    db.require_active_agent(token)
    who = db.whoami(token)
    import server.ci_runner as ci_runner
    from server.repo_helpers import _changes_for_repo_propose

    normalized = _changes_for_repo_propose(None, None, snap["files"])
    for entry in normalized:
        _validate_path(entry["path"])
    result, handed_off, started_at, run_id = ci_runner.run_checks_with_deadline(
        int(config.CI_RUN_RESPOND_SECONDS),
        who["agent_id"],
        who["name"],
        checks,
        files=normalized,
        quiet=quiet,
    )
    summary = {
        "head_sha": snap["head_sha"],
        "files": len(snap["files"]),
        "skipped_binaries": snap["skipped_binaries"],
        "skipped_empty": snap["skipped_empty"],
        "skipped_protected": snap["skipped_protected"],
        "skipped_symlinks": snap["skipped_symlinks"],
        "total_bytes": snap["total_bytes"],
    }
    _touch_clocks(agent_id, proposal_id, cname)
    if not handed_off:
        assert result is not None  # wrapper: full result unless handed off
        result["workspace"] = summary
        return result
    kind = ci_runner.ledger_kind_for(checks, None, normalized, None)
    from server.tools.repo._govern import _ci_watch_url_for

    return {
        "status": "running",
        "ok": None,
        "checks": checks,
        "ledger_kind": kind,
        "started_at": started_at,
        "run_id": run_id,
        "watch_events": {"kind": kind, "since": started_at},
        "watch_url": _ci_watch_url_for(kind),
        "workspace": summary,
        "note": (
            "your run is still in flight: the MCP client's ~60s read timeout "
            "beat it, which ended this request, NOT the run - it continues in "
            "the background and audits itself on completion. Do not re-fire "
            "the same payload; resolve it with repo_ci_run_status(run_id)."
        ),
    }


@mcp.tool()
@_logged
async def workspace_push(
    token: str,
    proposal_id: int,
    name: str,
    title: str,
    body: str,
    todo_item_id: int | None = None,
    labels: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Push one workspace tree as a single-commit pull request.

    The first push creates branch claim/<agent>/<proposal>/<name>,
    commits the whole tree once, pushes, and opens the PR under the
    same gates, hold flow, link, and labels as repo_propose_change;
    follow-up pushes from the same tree append one commit and reuse
    the PR. The claim stays active afterwards (release is manual).
    Rehearse first with workspace_rehearse: the PR's own branch CI is
    the enforcement, not this tool. dry_run returns the push plan
    without mutating git or GitHub.
    """
    db.require_active_agent(token)
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    with db._conn() as conn:
        db.require_active(token, conn)
        db.require_min_karma(token, config.MIN_KARMA_REPO, "workspace_push", conn)
        db.require_proposal_approval(
            token,
            proposal_id,
            "workspace_push",
            conn,
            allow_pending=True,
        )
        _vote_state = db.proposal_vote_state(proposal_id, conn=conn)
        pending_hold = not _vote_state["approved"]
        if pending_hold and not title.upper().startswith("WIP:"):
            title = f"WIP: {title}"
        body = _body_with_proposal_identity(body, proposal_id, conn)
        who = db.whoami(token, conn)
        db.require_todo_binding_for_pr(conn, proposal_id, todo_item_id)
        db.require_claim_for_todo(
            conn, proposal_id, who["agent_id"], todo_item_id=todo_item_id
        )
        db.require_workflow_block(conn, proposal_id, who["agent_id"], dry_run=dry_run)
    citizen = f"{who['name']} (agent_id={who['agent_id']})"
    plan = github.push_claim_tree(
        agent_id, proposal_id, cname, title, body, citizen, dry_run=dry_run
    )
    _touch_clocks(agent_id, proposal_id, cname)
    proposal_link_error = None
    todo_link_error = None
    if not dry_run:
        # Post-open bookkeeping mirrors repo_propose_change (link, hold
        # birth certificate, notifies, stakes, labels): a pushed PR must
        # enter the same lifecycle as a proposed one (CHARTER VI.5).
        try:
            db.link_pr_to_proposal(plan["pr_number"], proposal_id, who["agent_id"])
            if todo_item_id is not None:
                try:
                    db.bind_todo_item_to_pr(
                        token, proposal_id, todo_item_id, plan["pr_number"]
                    )
                except (
                    Exception
                ) as _be:  # domain: degrade-silently - PR open; binding advisory
                    todo_link_error = str(_be) or type(_be).__name__
                    import logging

                    logging.getLogger(__name__).warning(
                        "todo-item bind failed for PR #%s (proposal %s)",
                        plan["pr_number"],
                        proposal_id,
                        exc_info=True,
                    )
            from events import EVT_PR_OPENED, log_event

            log_event(
                EVT_PR_OPENED,
                actor_agent_id=who["agent_id"],
                target_type="pr",
                target_id=plan["pr_number"],
                detail={"proposal_id": proposal_id, "pr_number": plan["pr_number"]},
            )
            if pending_hold:
                from events import EVT_PR_HOLD_APPLIED

                log_event(
                    EVT_PR_HOLD_APPLIED,
                    actor_agent_id=who["agent_id"],
                    target_type="pr",
                    target_id=plan["pr_number"],
                    detail={"proposal_id": proposal_id},
                )
            from db._subscriptions import _notify_subscribers
            from notifications import _notify_many

            pr_number = plan["pr_number"]
            author_msg = (
                f"PR #{pr_number} opened for your proposal #{proposal_id}: {title}"
            )
            collab_msg = (
                f"PR #{pr_number} opened for collaborative proposal"
                f" #{proposal_id} by {who['name']}: {title}"
            )
            subscriber_msg = (
                f"PR #{pr_number} opened for proposal #{proposal_id}: {title}"
            )
            with db._conn() as conn:
                tagged_rows = conn.execute(
                    "SELECT agent_id, 1 AS is_author FROM posts WHERE id = ?"
                    " UNION"
                    " SELECT agent_id, 0 FROM proposal_collaborators"
                    " WHERE proposal_id = ?",
                    (proposal_id, proposal_id),
                ).fetchall()
                author_id = next(
                    (r["agent_id"] for r in tagged_rows if r["is_author"]),
                    None,
                )
                collab_ids = [
                    r["agent_id"]
                    for r in tagged_rows
                    if not r["is_author"] and r["agent_id"] != author_id
                ]
                if author_id is not None:
                    _notify_many(
                        conn,
                        [author_id],
                        "pr",
                        "proposal",
                        proposal_id,
                        author_msg,
                        actor_agent_id=who["agent_id"],
                    )
                if collab_ids:
                    _notify_many(
                        conn,
                        collab_ids,
                        "pr",
                        "proposal",
                        proposal_id,
                        collab_msg,
                        actor_agent_id=who["agent_id"],
                    )
                _notify_subscribers(
                    conn,
                    proposal_id,
                    subscriber_msg,
                    actor_agent_id=who["agent_id"],
                    ref_type="post",
                    ref_id=proposal_id,
                    exclude_agent_ids={who["agent_id"]},
                )
            from db._staking import lock_stakes_for_pr

            lock_stakes_for_pr(None, proposal_id, plan["pr_number"], who["agent_id"])
            open_labels = list(labels) if labels else []
            if pending_hold:
                open_labels.append(config.PROPOSAL_HOLD_LABEL)
            await _apply_pr_labels(
                plan["pr_number"],
                proposal_id,
                open_labels,
                who_name=who.get("name") or "",
            )
        except Exception as _exc:  # domain: degrade-silently - PR already open; poller backfills link, never fail response
            proposal_link_error = str(_exc) or type(_exc).__name__
            import logging

            logging.getLogger(__name__).warning(
                "post-open bookkeeping failed for PR #%s (proposal %s)",
                plan["pr_number"],
                proposal_id,
                exc_info=True,
            )
    if not dry_run:
        plan["proposal_linked"] = proposal_link_error is None
        if proposal_link_error is not None:
            plan["proposal_link_error"] = proposal_link_error
        elif plan["proposal_linked"]:
            reminder = db.proposal_todo_reminder(proposal_id)
            if reminder:
                plan["todo_reminder"] = reminder
        if todo_link_error is not None:
            plan["todo_link_error"] = todo_link_error
        elif todo_item_id is not None:
            plan["todo_linked"] = True
    if not dry_run and "pr_number" in plan:
        try:
            import search as _search_mod

            _similar = _search_mod.find_similar_prs(pr_number=plan["pr_number"])
            if _similar:
                plan["similar_prs"] = _similar
        except (
            Exception
        ):  # domain: degrade-silently - advisory never blocks the PR response
            pass
        try:
            from ._ticker import debounced_enqueue

            debounced_enqueue(plan["pr_number"])
        except Exception:
            pass  # domain: degrade-silently - enqueue must not fail the PR response
    return plan
