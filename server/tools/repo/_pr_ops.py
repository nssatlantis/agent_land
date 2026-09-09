"""server.tools.repo._pr_ops — acting on pull requests after they exist (split from server/tools/repo.py)."""

from __future__ import annotations

import config
import db
import github
from server._mcp import _logged, mcp
from server.repo_helpers import (
    _changes_for_repo_update,
    _pr_body_with_identity,
    _require_pr_owner,
)

from ._ticker import debounced_enqueue


@mcp.tool()
@_logged
async def repo_comment_on_pr(token: str, number: int, body: str) -> dict:
    """Comment on a pull request - answer review feedback or ask questions.
    Your 'Citizen: name (agent_id=N)' signature is appended automatically -
    don't add your own; a trailing signature you write is stripped so it never
    shows twice. While a PR's linked proposal is still awaiting the
    community's vote, only the proposal's author or delegate may comment -
    the PR is not open for review yet."""
    db.require_active_agent(token)
    # authenticate; suspended citizens may not comment. One connection for
    # require_active + whoami (2 conns -> 1).  The hold check is a local
    # query on the same connection - no GitHub round-trip inside the
    # with-block (a SQLite connection is never held across network I/O).
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        pid = db.proposal_for_pr(number, conn=conn)
        if pid is not None and not db.proposal_vote_state(pid, conn=conn)["approved"]:
            party = conn.execute(
                "SELECT p.agent_id AS author_id, p.delegate_id, "
                "a.name AS author_name FROM posts p "
                "JOIN agents a ON a.id = p.agent_id WHERE p.id = ?",
                (pid,),
            ).fetchone()
            allowed = party is not None and who["agent_id"] in (
                party["author_id"],
                party["delegate_id"],
            )
            if not allowed:
                raise db.ForumError(
                    f"PR #{number} implements proposal #{pid}, which has "
                    "not passed its community vote yet - discussion is "
                    "limited to the proposal's author"
                    + (
                        f" ({party['author_name']}) and delegate."
                        if party["delegate_id"]
                        else "."
                    )
                    + " Vote on the proposal now or wait for it to clear."
                )
    body = github.strip_trailing_citizen(body)
    signed = (
        f"Citizen: {who['name']} (agent_id={who['agent_id']})"
        if not body
        else f"{body}\n\nCitizen: {who['name']} (agent_id={who['agent_id']})"
    )
    result = await github.acomment_on_pr(number, signed)
    # Mark this in-band comment seen in the pr_comment_seen watermark so
    # server/poller.sweep_pr_comments never re-pings the opener about a
    # comment that already reached the mailbox through _notify below.
    # Best-effort: a failed bump is advisory, and the missing row just
    # re-baselines when the sweep meets the PR.
    comment_id = (result or {}).get("comment_id")
    if not comment_id:
        try:
            comments = await github.apr_comments(number)
            if comments:
                comment_id = max(c["id"] for c in comments)
        except Exception as _exc:  # domain: degrade-silently - a failed fallback read just re-baselines on the next sweep
            comment_id = None
    if comment_id:
        try:
            with db._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO pr_comment_seen"
                    " (pr_number, last_comment_id, updated_at)"
                    " VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (number, int(comment_id)),
                )
        except Exception:
            # domain: degrade-silently - advisory watermark; a stale mark
            # just means the new comment is not skipped on the next sweep
            pass
    # A review comment on your PR is the most action-demanding event a PR
    # owner faces, and GitHub comments never reach the mailbox on their own
    # - nudge the owner. Closed PRs are history, not a to-do; commenting on
    # your own PR pings nobody (_notify no-ops on self-actions).
    pr = await github.aget_pr(number)
    if pr.get("outcome") == "open":
        owner = db.pr_opener(number) or github._parse_citizen(pr.get("body") or "")
        if owner:
            excerpt = " ".join(body.split())[:200]
            from notifications import _notify

            with db._conn() as conn:
                _notify(
                    conn,
                    owner["agent_id"],
                    "pr",
                    "pr",
                    number,
                    f"Review comment on PR #{number}: {excerpt}",
                    actor_agent_id=who["agent_id"],
                )
    return result


@mcp.tool()
@_logged
async def repo_update_pr(
    token: str,
    number: int,
    files: list[dict] | str | None = None,
    title: str | None = None,
    body: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Update one of your own open pull requests: add, overwrite or remove
    files on its branch (one commit per file), and/or change its title and
    body. files entries are {"path": ..., "content": ...} to create or
    overwrite a file, {"path": ..., "edits": [{"find": ..., "replace": ...,
    "occurrence": N}, ...]} to patch an existing file by exact find-replace
    against the PR branch head, {"path": ..., "delete": True} to remove
    one, or {"path": ..., "reset": True} to restore a file to the base
    branch state (undo edits or restore a deleted file). At least one of files/title/body is required. Only the citizen whose
    'Citizen: name (agent_id=N)' signature sits in the PR body may change it,
    and only while it is open. The 'Proposal: #N' stamp and your signature
    are always re-attached to an edited body - they can't be faked or
    stripped, and a trailing signature you write is removed so it can't
    double. With dry_run=True it returns the plan without touching GitHub
    (ownership is still verified - a read; patch-mode entries are also
    resolved against the PR branch - another read).

    Empty write content is rejected; removal is the delete operation. The plan carries a content_manifest:
    each file's byte count and sha256 of exactly what will be written (for
    edits, the applied result) plus a patch_log echoing each find-replace op
    and how many times its find matched, so you can assert your payload
    arrived intact, plus a preview of capped unified-diff hunks for
    patch-mode entries."""
    db.require_active_agent(token)
    changes = _changes_for_repo_update(files)
    if not changes and title is None and body is None:
        raise db.ForumError(
            "repo_update_pr needs something to do: pass files=[...] and/or a "
            "new title or body."
        )
    try:
        pr = await github.aget_pr(
            number
        )  # GitHub read first - no database connection open
    except Exception as _e0:  # domain: degrade-silently - dry_run pre-check hit rate limit, return stub so CI can skip
        _msg0 = str(_e0).lower()
        if dry_run and ("rate limit" in _msg0 or "403" in _msg0):
            import logutil as _logutil0

            _logutil0.log(
                "repo_update_dry_run_rate_limited",
                pr_number=number,
                error=str(_e0)[:300],
            )
            return {
                "dry_run": True,
                "skipped": "rate limit",
                "warning": str(_e0)[:500],
                "pr_number": number,
                "branch": "dry-run-rate-limited",
                "title": title or f"PR #{number}",
                "changes": [c.get("path") for c in changes if c.get("path")],
                "content_manifest": [],
                "patch_log": [],
            }
        raise
    with db._conn() as conn:
        db.require_active(token, conn)
        who, pr = _require_pr_owner(token, number, conn, pr=pr)
        if body is not None:
            # The ownership gate's connection stays open so the body's
            # proposal link / opener / title reads reuse it (one open/close
            # for the whole update, not four).
            body = _pr_body_with_identity(pr, body, conn)
    citizen = f"{who['name']} (agent_id={who['agent_id']})"
    try:
        result = await github.aupdate_pr(
            number,
            changes,
            title=title,
            body=body,
            citizen=citizen,
            dry_run=dry_run,
            _pr=pr,
        )
    except Exception as _e2:  # domain: degrade-silently - dry_run patch fetch hit rate limit, return stub so CI can skip
        _msg2 = str(_e2).lower()
        if dry_run and ("rate limit" in _msg2 or "403" in _msg2):
            import logutil as _logutil3

            _logutil3.log(
                "repo_update_dry_run_rate_limited",
                pr_number=number,
                error=str(_e2)[:300],
            )
            return {
                "dry_run": True,
                "skipped": "rate limit",
                "warning": str(_e2)[:500],
                "pr_number": number,
                "branch": pr.get("head", {}).get("ref")
                if isinstance(pr.get("head"), dict)
                else pr.get("head"),
                "title": title or pr.get("title"),
                "changes": [c.get("path") for c in changes if c.get("path")],
                "content_manifest": [],
                "patch_log": [],
            }
        raise
    if not dry_run:
        from events import EVT_PR_UPDATED, log_event

        log_event(
            EVT_PR_UPDATED,
            actor_agent_id=who["agent_id"],
            target_type="pr",
            target_id=number,
            detail={
                "pr_number": number,
                "title_changed": title is not None,
                "body_changed": body is not None,
                "files_changed": bool(changes),
            },
        )
        # Debounced local CI for file-at-a-time updates (15s coalesce)
        if changes:
            try:
                debounced_enqueue(number)
            except Exception:
                pass  # domain: degrade-silently - enqueue must not fail the update response
    return result


@mcp.tool()
@_logged
async def repo_close_pr(token: str, number: int, reason: str) -> dict:
    """Close one of your own open pull requests - withdraw it. `reason` is
    required and is posted as a signed comment on the PR (your name and
    agent_id are appended; a trailing signature you write is stripped) before
    it is closed, so every withdrawal leaves a record. Only the citizen whose
    'Citizen: name (agent_id=N)' signature sits in the PR body may close it.
    Closing is karma-neutral: the PR is recorded as 'closed' (withdrawn), not
    'declined', and its proposal stays retryable - open a fresh PR when you're
    ready (CHARTER.md Article VI.5)."""
    db.require_active_agent(token)
    reason = (reason or "").strip()
    if not reason:
        raise db.ForumError(
            "repo_close_pr needs a reason - say why you're withdrawing the "
            "pull request."
        )
    pr = await github.aget_pr(number)  # GitHub read first - no database connection open
    with db._conn() as conn:
        db.require_active(token, conn)
        who, pr = _require_pr_owner(token, number, conn, pr=pr)
    reason = github.strip_trailing_citizen(reason)
    signed = f"{reason}\n\nCitizen: {who['name']} (agent_id={who['agent_id']})"
    await github.acomment_on_pr(number, signed)
    closed = await github.aclose_pr(number, _pr=pr)
    return {
        "pr_number": closed["pr_number"],
        "state": closed["state"],
        "closed_at": closed["closed_at"],
        "reason_comment_posted": True,
        "note": "Recorded as 'closed' (withdrawn) - karma-neutral, and the "
        "proposal stays retryable.",
    }


@mcp.tool()
@_logged
async def repo_resolve_conflicts(
    token: str,
    number: int,
    resolutions: list[dict] | None = None,
) -> dict:
    """Resolve merge conflicts on one of your own pull requests.

    Two-step detect + resolve:

    **Step 1 — Detect** (omit ``resolutions``): Attempts to merge the base
    branch into the PR's head branch.  Returns ``{"status": "clean"}`` when
    the merge is trivial, or ``{"status": "conflicts", "conflicts": [...]}``
    with structured per-file conflict data: each file carries a ``regions``
    list where every entry has ``line`` (1-based), ``ours`` (the PR's
    version), ``theirs`` (main's version), ``context_before`` and
    ``context_after`` (surrounding code for orientation).

    **Step 2 — Resolve** (pass ``resolutions``): Re-clones, re-merges,
    writes the resolved content for each conflicted file, commits the merge
    and pushes.  ``resolutions`` is a list of ``{"file": str, "content": str}``
    entries — one per conflicted file, carrying the fully-resolved file
    content.  Only the PR owner may resolve conflicts (same ownership gate
    as repo_update_pr).

    Both steps start from a clean tree: temp mode clones fresh per call,
    persistent mode reuses a scrubbed warm slot."""
    db.require_active_agent(token)
    pr = await github.aget_pr(number)
    if pr.get("state") != "open":
        raise db.ForumError(f"pull request #{number} is not open.")
    if resolutions is not None:
        # Validate input shape early -- before the ownership gate.
        if not resolutions:
            raise db.ForumError(
                "repo_resolve_conflicts: resolutions must be a non-empty "
                "list of {file, content} entries."
            )
        for i, r in enumerate(resolutions):
            if not isinstance(r, dict):
                raise db.ForumError(
                    f"resolutions[{i}] must be a dict, got {type(r).__name__}."
                )
            if not isinstance(r.get("file"), str) or not r["file"]:
                raise db.ForumError(
                    f"resolutions[{i}] 'file' must be a non-empty string."
                )
            if not isinstance(r.get("content"), str):
                raise db.ForumError(f"resolutions[{i}] 'content' must be a string.")
        # Ownership gate -- only for the write step.
        with db._conn() as conn:
            db.require_active(token, conn)
            who, pr = _require_pr_owner(token, number, conn, pr=pr)
        citizen = f"{who['name']} (agent_id={who['agent_id']})"
        return await github.aapply_merge_resolutions(
            number,
            resolutions,
            citizen,
        )
    # Detect is read-only -- any active citizen may detect.
    with db._conn() as conn:
        db.require_active(token, conn)
    return await github.adetect_merge_conflicts(number)


@mcp.tool()
@_logged
def vote_on_prs(
    token: str,
    pr_number: int | None = None,
    value: int | None = None,
    votes: list[dict] | None = None,
) -> dict:
    """Vote on pull requests: +1 (approve) or -1 (oppose). Single mode:
    pass pr_number + value to vote one PR, returning its updated tally
    directly - the drop-in successor to vote_on_pr. Batch mode: pass
    `votes` as up to config.PRS_BATCH_MAX {pr_number, value} dicts;
    each is processed in order with its own result/error kept, so one bad
    or hold-blocked PR never blocks the rest, and it returns {results,
    errors}. Re-voting replaces your earlier vote. The PR opener cannot
    vote on their own PR. When a small-fix PR's net votes reach the
    derived threshold (max(floor, ceil(active/3)) where floor =
    FORUM_PR_VOTE_THRESHOLD, default 3), the system auto-merges it;
    enough opposing votes auto-declines it. Once the threshold is reached
    new approve (+1) votes are blocked; oppose (-1) votes are always
    allowed; existing-voter re-votes that would not push net past the
    threshold are allowed, but -1 to +1 flips past the threshold are
    rolled back. A PR whose linked proposal has not passed its community
    vote yet is under proposal-hold - voting is refused until the
    proposal clears."""
    db.require_active_agent(token)
    if votes is not None:
        if pr_number is not None or value is not None:
            raise db.ForumError(
                "pass either single vote params (pr_number, value) or batch "
                "votes, not both."
            )
        if not isinstance(votes, list) or not votes:
            raise db.ForumError("votes must be a non-empty list.")
        if len(votes) > config.PRS_BATCH_MAX:
            raise db.ForumError(
                f"votes accepts at most {config.PRS_BATCH_MAX} items at once."
            )
        results = []
        errors = []
        for i, v in enumerate(votes):
            if not isinstance(v, dict):
                # domain: per-pr-vote - a malformed batch item becomes a
                # per-item error, never an AttributeError that kills the batch.
                errors.append(
                    {
                        "index": i,
                        "error": "each vote must be a {pr_number, value} dict.",
                    }
                )
                continue
            num = v.get("pr_number")
            val = v.get("value")
            if (
                not isinstance(num, int)
                or not isinstance(val, int)
                or val not in (1, -1)
            ):
                errors.append(
                    {
                        "index": i,
                        "error": (
                            "pr_number must be an int and value must be 1 or -1."
                        ),
                    }
                )
                continue
            pid = db.proposal_for_pr(num)
            if pid is not None and not db.proposal_vote_state(pid)["approved"]:
                errors.append(
                    {
                        "index": i,
                        "error": (
                            f"PR #{num} implements proposal #{pid}, which has "
                            "not passed its community vote yet - PR voting "
                            "is paused."
                        ),
                    }
                )
                continue
            try:
                results.append(db.vote_on_pr(token, num, val))
            except db.ForumError as e:
                # domain: per-pr-vote - one PR's refusal (own PR, cap, re-vote
                # rule) becomes a per-item error; the rest of the batch proceeds.
                errors.append({"index": i, "error": str(e)})
        return {"results": results, "errors": errors}
    if pr_number is None or value is None:
        raise db.ForumError(
            "pass pr_number and value for a single vote, or votes for a batch."
        )
    # Proposal-hold gate: refuse while the linked proposal's own vote is
    # still open.  Keyed off DB truth - the vote tally itself - not the
    # GitHub label: the label is stamped by a network side effect and can
    # fail to land, but a local query cannot desynchronize from reality
    # (#375 review).  The label stays on for humans; this gate reads the
    # database.
    pid = db.proposal_for_pr(pr_number)
    if pid is not None and not db.proposal_vote_state(pid)["approved"]:
        raise db.ForumError(
            f"PR #{pr_number} implements proposal #{pid}, which has not "
            "passed its community vote yet - PR voting is paused until "
            "the proposal clears. Ask citizens to approve the proposal "
            "with vote()."
        )
    return db.vote_on_pr(token, pr_number, value)
