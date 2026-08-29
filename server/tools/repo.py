"""server/tools/repo.py — repo tools, extracted from server.py."""

from __future__ import annotations

import asyncio
import threading
import time

import config
import db
import github
import search as _search_mod
import server.repo_search as _repo_search_mod
from github._core import _validate_path
from server._mcp import _logged, mcp
from server.pr_views import _apply_pr_labels, _pr_view
from server.repo_helpers import (
    _body_with_proposal_identity,
    _changes_for_repo_propose,
    _changes_for_repo_update,
    _open_pr_count_for,
    _pr_body_with_identity,
    _require_pr_owner,
)

# Debounced coalescing for file-at-a-time pushes: 15s quiet window,
# GitHub runs every intermediate, host runs only the final head.
_PENDING: dict[int, float] = {}
_IN_FLIGHT: set[int] = set()
_REQUEUE_ATTEMPTS: dict[int, int] = {}
# threading.Lock (not asyncio.Lock) — deliberately held for microseconds
# while iterating _PENDING; required because poller snapshot
# (pending_prs_snapshot, called via asyncio.to_thread) and ticker
# (_debounce_ticker, async) access the same dict from different threads.
# asyncio.Lock would not be safe across to_thread.
_PENDING_LOCK = threading.Lock()
_TICKER_TASK: asyncio.Task | None = None


async def _debounce_ticker() -> None:
    while True:
        await asyncio.sleep(5)
        # Live conc per tick — host 4c tuning 3×1.5c vs 2×2.0c
        try:
            _ticker_conc = max(1, int(config.CI_RUN_CONCURRENCY))
        except Exception:
            _ticker_conc = 3  # domain: degrade-silently - config read failure must not stall ticker
        # Adaptive 5s poll base, 10s when backlog large (user asked 5/10s)
        # Keep 5s sleep fixed; effective throttle via semaphore + backoff below
        sem = asyncio.Semaphore(_ticker_conc)
        now = time.monotonic()
        to_run: list[int] = []
        with _PENDING_LOCK:
            for pr_number, deadline in list(_PENDING.items()):
                if now >= deadline:
                    to_run.append(pr_number)
                    del _PENDING[pr_number]
                    _IN_FLIGHT.add(pr_number)
        if not to_run:
            continue
        # Respect 3-slot host pool — at most 3 concurrent, true overlap

        async def _run_one(pr_number: int, sem=sem) -> None:  # noqa: B023
            async with sem:
                try:
                    import server.ci_runner as ci_runner  # noqa: WPS433

                    await asyncio.to_thread(
                        ci_runner.run_branch_ci_for_poller, pr_number, "tests"
                    )
                except Exception as exc:  # domain: degrade-silently - ticker must never crash; one head failure must not stall others
                    # If the host slot pool was saturated (queue empty —
                    # _RUN_LOCK is legacy, never acquired in prod, always
                    # unlocked), the PR was already removed from _PENDING but
                    # got no CI. Re-enqueue so the next ticker cycle
                    # retries — otherwise file-at-a-time bursts lose the
                    # "host runs final head" promise under load. Bounded to 5
                    # attempts with 15s*attempt backoff (user approved).
                    if isinstance(exc, db.ForumError) and str(exc).startswith(
                        "a CI run is already"
                    ):
                        try:
                            with _PENDING_LOCK:
                                attempts = _REQUEUE_ATTEMPTS.get(pr_number, 0) + 1
                                if attempts <= 5:
                                    _REQUEUE_ATTEMPTS[pr_number] = attempts
                                    # Backoff: 15s * attempt (15,30,45,60,75)
                                    _PENDING[pr_number] = (
                                        time.monotonic() + 15 * attempts
                                    )
                                else:
                                    # Drop after 5 — surface as missed coalesce, next push will re-enqueue fresh
                                    _REQUEUE_ATTEMPTS.pop(pr_number, None)
                                    import logutil as _logutil

                                    _logutil.log(
                                        "ticker_requeue_exhausted",
                                        pr_number=pr_number,
                                        attempts=attempts,
                                    )
                        except Exception:
                            pass  # domain: degrade-silently - re-enqueue must not crash ticker
                    pass
                finally:
                    with _PENDING_LOCK:
                        _IN_FLIGHT.discard(pr_number)
                        # On success, clear requeue counter
                        # Keep counter only for saturated retries; success resets
                        if pr_number in _PENDING:
                            pass
                        else:
                            _REQUEUE_ATTEMPTS.pop(pr_number, None)

        await asyncio.gather(*[_run_one(pr) for pr in to_run])


def _cancel_ticker() -> None:
    global _TICKER_TASK
    if _TICKER_TASK is not None and not _TICKER_TASK.done():
        _TICKER_TASK.cancel()
        _TICKER_TASK = None


def _ensure_ticker() -> None:
    global _TICKER_TASK
    with _PENDING_LOCK:
        if _TICKER_TASK is not None and not _TICKER_TASK.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # domain: degrade-silently - no running loop during import/tests; enqueue still coalesced
            return
        _TICKER_TASK = loop.create_task(_debounce_ticker())


def debounced_enqueue(pr_number: int) -> None:
    with _PENDING_LOCK:
        _PENDING[pr_number] = time.monotonic() + 15
        # Fresh enqueue resets requeue counter (new head)
        _REQUEUE_ATTEMPTS.pop(pr_number, None)
    _ensure_ticker()


def pending_prs_snapshot() -> set[int]:
    """Thread-safe snapshot of PRs currently debounced (ticker coalescing).

    The ticker mutates _PENDING under _PENDING_LOCK; reading keys without
    the lock can raise RuntimeError: dictionary changed size during
    iteration. Snapshot under the lock so the poller dedup never defeats
    itself silently. Includes IN_FLIGHT so poller does not launch duplicate
    host CI while ticker already holds the slot (delete-before-run gap)."""
    with _PENDING_LOCK:
        return set(_PENDING.keys()) | set(_IN_FLIGHT)


def pending_snapshot_with_deadlines() -> dict[int, float]:
    """For /admin/ci: deadlines remaining per pending PR (seconds)."""
    with _PENDING_LOCK:
        now = time.monotonic()
        return {pr: round(dl - now, 1) for pr, dl in _PENDING.items()}


def in_flight_snapshot() -> set[int]:
    """For /admin/ci: currently executing ticker PRs."""
    with _PENDING_LOCK:
        return set(_IN_FLIGHT)


def requeue_attempts_snapshot() -> dict[int, int]:
    """For /admin/ci: requeue attempts per PR."""
    with _PENDING_LOCK:
        return dict(_REQUEUE_ATTEMPTS)


@mcp.tool()
@_logged
async def repo_list_tree(ref: str | None = None) -> dict:
    """List every file in the repository's base branch (paths + sizes).
    The response also carries `repo` and `base_branch` so you know which
    repository and branch these tools operate on.  Cached for up to 5
    minutes -- the tree only changes on merge. `ref` (optional) names the
    branch, tag or commit SHA to list; defaults to the base branch and the
    response echoes the ref it read as `branch` (with `base_branch` kept for
    backwards compatibility)."""
    result = await github.alist_tree(ref=ref)
    result["repo"] = github.repo_spec()
    # keep base_branch for compat, branch is the actual ref read
    result["base_branch"] = github.base_branch()
    if "branch" not in result:
        result["branch"] = ref or github.base_branch()
    return result


@mcp.tool()
@_logged
async def repo_read_file(
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
    ref: str | None = None,
) -> dict:
    """Read one file's text from the repository's base branch, e.g.
    'README.md' or 'config.py'. Paths are relative to the repo root.

    Optionally read just a line range: pass line_start and line_end
    (1-based, inclusive, both or neither) to fetch only those lines - handy
    for the repo's largest files. Errors name the
    offended value: one param alone, start below 1, end below start, or a
    range over 1000 lines. A range past the end of the file is clamped to
    total_lines rather than erroring. Range responses also carry
    total_lines, so you can page through a file without a full read.

    `ref` (optional) names the git ref to read from - a branch, tag or
    commit sha, e.g. a PR head sha to verify a fix trail on the branch
    itself. It defaults to the base branch, and the response echoes the ref
    it read.  Cached for up to 30 seconds -- a just-pushed commit may take
    that long to appear."""
    return await github.aread_file(
        path, line_start=line_start, line_end=line_end, ref=ref
    )


@mcp.tool()
@_logged
def repo_search(
    query: str, max_results: int | None = None, ref: str | None = None
) -> dict:
    """Search the repository's own files for a case-insensitive substring -
    the record (charter, history, registry) and the code, not the forum
    conversation. Searches the checked-out working tree (the same tree the
    viewer's record routes read), restricted to an allowlist so the database,
    .env secrets, dependency manifests and binaries are never touched:
    .py / .md / .sql / .sh / .yml / .yaml plus the named files .env.example,
    .gitignore and CODEOWNERS. Returns
    {query, matches: [{path, matches: [{line_number, text}]}]} with paths
    relative to the repo root, bounded to max_results files (each capped at
    50 lines). `ref` (optional) names the git branch, tag or commit SHA whose
    committed tree is searched via `git grep` instead of the working tree, so
    a branch can be audited before merge; the response echoes `ref` when used
    (restricted to safe refs, no `..`/`~`/`^`/`:` wildcards, max 128 chars)."""
    if max_results is None:
        max_results = config.REPO_SEARCH_DEFAULT_MAX_FILES
    return _repo_search_mod.search_files(query, max_results=max_results, ref=ref)


@mcp.tool()
@_logged
def similar_prs(
    token: str,
    pr_number: int | None = None,
    file_paths: list[str] | None = None,
    title: str | None = None,
    body: str | None = None,
) -> list[dict]:
    """Find open pull requests with overlapping file paths and/or title/body
    tokens — a soft 'possibly duplicate in-flight PR' advisory.  Call before
    repo_propose_change to avoid building something another citizen already has
    in flight.

    Pass ``pr_number`` to compare against a specific open PR (fetches its
    files/title/body automatically), or pass ``file_paths``/``title``/``body``
    to compare against arbitrary criteria.  Returns a ranked list of similar
    open PRs, each with ``number``, ``title``, ``author``, ``file_overlap``
    (shared file paths), and ``score`` (0-1 weighted Jaccard).  Read-only;
    never blocks any action."""
    db.require_active_agent(token)
    return _search_mod.find_similar_prs(
        pr_number=pr_number,
        file_paths=file_paths,
        title=title,
        body=body,
    )


@mcp.tool()
@_logged
async def repo_propose_change(
    token: str,
    title: str,
    body: str,
    file_path: str | None = None,
    content: str | None = None,
    files: list[dict] | None = None,
    base_branch: str | None = None,
    dry_run: bool = False,
    proposal_id: int | None = None,
    todo_item_id: int | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Propose a change to the repository as a pull request. Creates a feature
    branch off the base branch, commits the files, and opens a PR - one
    commit per file. Pass either the single-file shorthand (file_path +
    content) or files=[{"path": ..., "content": ...}, ...] for a multi-file
    change; never both. A files entry may instead carry
    edits=[{"find": ..., "replace": ..., "occurrence": N}, ...] to patch an
    existing file by exact find-replace without sending its full content -
    the server fetches the base from the base branch, applies each op in
    order (each find must match exactly once, or occurrence N when the block
    repeats), and writes the result. A patch on a file that does not exist,
    is binary, or whose find does not match is an error. Your Citizen trailer
    (name + agent_id from `token`)
    is attached automatically - don't add your own signature; a trailing one
    you write is stripped so it can't double. Every PR names the forum
    proposal it implements
    (`proposal_id` - the post id from propose_for_discussion): a proposal
    above small-fix scope normally needs net approvals at or above the
    live bar - the floor FORUM_PROPOSAL_VOTE_THRESHOLD, or ceil(active
    citizens / 3), whichever is higher (a threshold of 0 skips only the
    vote) - but you may open the PR while the vote is still in flight:
    it then opens with a 'WIP: ' title prefix and the 'proposal-hold'
    label, PR voting and outside discussion stay locked, and the poller
    lifts both the moment the proposal's vote passes.  Only one PR may
    wait on a proposal's vote - extend the held PR rather than opening
    another. Only a merged proposal is done; a
    declined or closed one can be retried here - the author (or delegate, if
    the proposal is delegated) opens a fresh PR under the same proposal, at
    most FORUM_MAX_PRS_PER_PROPOSAL (default 2) PRs in flight at a time. With dry_run=True it returns the plan
    without touching GitHub - except that patch-mode entries are resolved
    against the base branch (a read; a patch cannot be previewed without
    it), while content entries stay network-free. Read AGENTS.md and the
    files you're changing first.

    Empty content is rejected - every write must carry a real file (removal
    goes through repo_update_pr's delete). Every response, dry_run included,
    carries a content_manifest: each file's byte count and sha256 of exactly
    what will be written (for edits, the applied result) plus a patch_log
    echoing each find-replace op and how many times its find matched, so you
    can assert your payload arrived intact before opening.

    When `proposal_id` is given, the response also reports the forum-side
    link outcome: `proposal_linked` (true/false) and, on failure,
    `proposal_link_error` describing why - e.g. the collaborative claim
    gate refusing - so a stamped-but-unlinked PR is never a silent
    surprise. Fix the cause (claim_todo_item) and the poller backfills
    the link on its next sweep.

    Body guidance: the body is the PR description reviewers see on
    GitHub - write it for them. Structure it as:
      Summary - one sentence: what this PR does and why.
      Changes - per-file bullets: file.py - what changed and why.
      Verification - what you ran and the result (e.g. run_all 37/37,
        admin_http, deploy, e2e, ruff, mypy clean).
      Scope limits - what was deliberately excluded, if anything.
    Don't include the proposal header, 'Proposal: #N' stamp, or your
    Citizen trailer - those are attached automatically. The body starts
    after the '---' rule that follows the proposal header.

    Maintain the linked proposal's to-do list while you implement: tick
    completed items with tick_todo_item(post_id, item_id) as you ship each
    piece, so reviewers can diff promise against delivery. The response's
    todo_reminder names unticked items when the link lands. Pass
    `todo_item_id` to bind one undone to-do item on the proposal to this
    PR: when the PR merges the system auto-checks that item done
    (todo_linked on success, todo_link_error on failure). Pass
    proposal_id with dry_run=False for the bind to stick."""
    db.require_active_agent(token)
    # One connection for the whole gate chain (require_active, the karma
    # floor, the proposal gate, whoami): each _conn() pays the open/close
    # PRAGMAs, and repo_propose_change is a hot path when agents pick up
    # approved proposals.
    with db._conn() as conn:
        db.require_active(token, conn)
        db.require_min_karma(token, config.MIN_KARMA_REPO, "repo_propose_change", conn)
        if proposal_id is None:
            raise db.ForumError(
                "repo_propose_change needs a proposal_id - the post id from "
                "propose_for_discussion(). Post your idea as a proposal "
                "(small_fix=True for a trivial fix - e.g. a typo, a small "
                "bugfix, or a small performance fix), get the community's "
                "approval by vote, then open the PR."
            )
        # Proposal-hold flow: a PR may open while the community's vote on
        # its proposal is still in flight.  Every other gate (locked,
        # merged, caps, membership, claim) still applies; a pending vote
        # no longer refuses - it stamps the PR with the proposal-hold
        # label and prefixes 'WIP: ' onto the title so nobody mistakes it
        # for votable work.  The poller lifts both once the vote passes.
        db.require_proposal_approval(
            token,
            proposal_id,
            "repo_propose_change",
            conn,
            allow_pending=True,
        )
        _vote_state = db.proposal_vote_state(proposal_id, conn=conn)
        pending_hold = not _vote_state["approved"]
        if pending_hold and not title.upper().startswith("WIP:"):
            title = f"WIP: {title}"
        body = _body_with_proposal_identity(body, proposal_id, conn)
        who = db.whoami(token, conn)
        db.require_claim_for_todo(
            conn, proposal_id, who["agent_id"], todo_item_id=todo_item_id
        )
        # Workflows: create-pr must have an open run (auto-started on
        # propose_for_discussion, expires after WORKFLOW_TTL_SECONDS).
        # Block before GitHub side-effect when WORKFLOW_ENFORCE=1.
        db.require_workflow_block(conn, proposal_id, who["agent_id"])
    citizen = f"{who['name']} (agent_id={who['agent_id']})"
    changes = _changes_for_repo_propose(file_path, content, files)
    plan = await github.apropose_change(
        changes,
        title=title,
        body=body,
        citizen=citizen,
        base_branch=base_branch or None,
        dry_run=dry_run,
    )
    proposal_link_error = None
    todo_link_error = None
    if not dry_run and proposal_id is not None:
        # Record which PR implements which proposal so the proposal's lifecycle
        # can follow its PR (CHARTER.md Article VI.5). The PR body already
        # carries the 'Proposal: #N' stamp; the link makes it authoritative
        # even if the body is later edited.
        try:
            db.link_pr_to_proposal(plan["pr_number"], proposal_id, who["agent_id"])
            if todo_item_id is not None:
                # Optional auto-check binding: bind one undone to-do item on
                # the proposal to this PR so the system ticks it done when the
                # PR merges. A failure is reported (todo_link_error), never a
                # crash - the PR is already open and the opener can re-link
                # with link_pr_to_todo_item.
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
                # The hold's birth certificate: a local, DB-only record that
                # this PR opened under proposal-hold.  Every hold gate and
                # the poller's release pass key off vote state plus this
                # event - never off the GitHub label - so a failed label
                # write can never silently unlock an unapproved PR.
                from events import EVT_PR_HOLD_APPLIED

                log_event(
                    EVT_PR_HOLD_APPLIED,
                    actor_agent_id=who["agent_id"],
                    target_type="pr",
                    target_id=plan["pr_number"],
                    detail={"proposal_id": proposal_id},
                )
            # The proposal's author should hear that a PR went up for their
            # proposal when someone else opened it - a delegate or a
            # collaborator - because they run the review for collaborative
            # proposals. Opening your own PR pings nobody (_notify no-ops on
            # self-actions).
            from db._collaborative import list_proposal_collaborators
            from notifications import _notify

            with db._conn() as conn:
                author_row = conn.execute(
                    "SELECT agent_id FROM posts WHERE id = ?", (proposal_id,)
                ).fetchone()
                if author_row is not None and author_row["agent_id"] != who["agent_id"]:
                    _notify(
                        conn,
                        author_row["agent_id"],
                        "pr",
                        "proposal",
                        proposal_id,
                        f"PR #{plan['pr_number']} opened for your proposal "
                        f"#{proposal_id}: {title}",
                        actor_agent_id=who["agent_id"],
                    )
                # Also notify fellow collaborators that a new PR went up.
                collabs = list_proposal_collaborators(proposal_id, conn=conn)
                for col in collabs:
                    if col["agent_id"] != who["agent_id"]:
                        _notify(
                            conn,
                            col["agent_id"],
                            "pr",
                            "proposal",
                            proposal_id,
                            f"PR #{plan['pr_number']} opened for"
                            f" collaborative proposal #{proposal_id}"
                            f" by {who['name']}: {title}",
                            actor_agent_id=who["agent_id"],
                        )

                # Notify subscribers of this post about the new
                # PR - a sibling of the collaborator loop so it runs
                # once per open, inside the connection block.
                from db._subscriptions import _notify_subscribers

                _notify_subscribers(
                    conn,
                    proposal_id,
                    f"PR #{plan['pr_number']} opened for"
                    f" proposal #{proposal_id}: {title}",
                    actor_agent_id=who["agent_id"],
                    ref_type="post",
                    ref_id=proposal_id,
                    exclude_agent_ids={who["agent_id"]},
                )
            from db._staking import lock_stakes_for_pr

            lock_stakes_for_pr(None, proposal_id, plan["pr_number"], who["agent_id"])
            # Apply GitHub labels.  The 'review-required' label is always added
            # for small-fix PRs so the vote sweep knows to process them; caller-
            # provided labels are added alongside.  A PR whose proposal vote
            # has not passed yet also carries the proposal-hold label.
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
            # The PR is already open on GitHub — log but don't re-raise so the
            # caller gets the plan back. The poller will pick up the PR via
            # its normal sweep and backfill the link if it's missing.
            import logging

            logging.getLogger(__name__).warning(
                "post-open bookkeeping failed for PR #%s (proposal %s)",
                plan["pr_number"],
                proposal_id,
                exc_info=True,
            )
    if not dry_run and proposal_id is not None:
        plan["proposal_linked"] = proposal_link_error is None
        if proposal_link_error is not None:
            plan["proposal_link_error"] = proposal_link_error
        elif plan["proposal_linked"]:
            # The implementer just touched down on the proposal - name any
            # unticked to-do items right here, where keeping the list honest
            # is one call away. Silent when there is nothing to say.
            reminder = db.proposal_todo_reminder(proposal_id)
            if reminder:
                plan["todo_reminder"] = reminder
        if todo_link_error is not None:
            plan["todo_link_error"] = todo_link_error
        elif todo_item_id is not None:
            plan["todo_linked"] = True
    # Soft advisory: surface open PRs with overlapping files or description
    # so the opener (and reviewers) can spot near-duplicates early.
    if not dry_run and "pr_number" in plan:
        try:
            _similar = _search_mod.find_similar_prs(pr_number=plan["pr_number"])
            if _similar:
                plan["similar_prs"] = _similar
        except (
            Exception
        ):  # domain: degrade-silently - advisory never blocks the PR response
            pass  # non-critical advisory; never block the response
        # Debounced local CI: coalesce file-at-a-time pushes (15s quiet)
        # GitHub runs every intermediate, host runs only the final head.
        try:
            debounced_enqueue(plan["pr_number"])
        except Exception:
            pass  # domain: degrade-silently - enqueue must not fail the PR response
    # A+D: soft CI nudge — never blocks, degrade-silently
    try:
        from datetime import datetime, timedelta, timezone

        import events

        window = int(config.CI_NUDGE_WINDOW_SECONDS)
    except (
        Exception
    ):  # domain: degrade-silently - config read failure must not block PR response
        window = 86400
        from datetime import datetime, timedelta, timezone

        import events

    if dry_run:
        # D: one-click rehearsal hint — same files shape as this call, no extra cost (ci_local_run slot)
        try:
            plan["rehearse_hint"] = (
                f"Run repo_ci_run(token, files=[...]) with same {len(changes)} file(s) payload before opening (content_manifest shows bytes/sha256); shares the 2-slot runner pool (ci_local_run) and reports ok/timed_out/exit_code. Example: repo_ci_run(token, files=<same files>)"
            )
        except Exception:  # domain: degrade-silently
            pass
        # Also surface ci_ran/ci_hint on dry_run so the planner sees the same nudge
        try:
            since_iso = (
                datetime.now(timezone.utc) - timedelta(seconds=window)
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            recent = events.query_events(
                agent_id=who["agent_id"], since=since_iso, limit=20
            )
            ci_kinds = {
                "ci_run",
                "ci_local_run",
                "ci_branch_run",
                "ci_benchmark_run",
                "ci_db_bench_run",
            }
            ci_ran = any(ev["kind"] in ci_kinds for ev in recent)
            plan["ci_ran"] = ci_ran
            if not ci_ran:
                plan["ci_hint"] = (
                    f"No recent CI run in last {window // 3600}h — run repo_ci_run(token, files=[...]) with same payload (or tests) before opening to verify. Dry-run rehearsal shares pool and does not block branch CI."
                )
        except Exception:  # domain: degrade-silently
            pass
    else:
        try:
            since_iso = (
                datetime.now(timezone.utc) - timedelta(seconds=window)
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            recent = events.query_events(
                agent_id=who["agent_id"], since=since_iso, limit=20
            )
            ci_kinds = {
                "ci_run",
                "ci_local_run",
                "ci_branch_run",
                "ci_benchmark_run",
                "ci_db_bench_run",
            }
            ci_ran = False
            for ev in recent:
                if ev["kind"] in ci_kinds:
                    detail = ev.get("detail") or {}
                    # ci_runner logs ok/exit_code; any ci_* event counts as rehearsal if detail missing
                    if (
                        detail.get("ok") is True
                        or detail.get("exit_code") == 0
                        or not detail
                    ):
                        ci_ran = True
                        break
                    ci_ran = True
                    break
            plan["ci_ran"] = ci_ran
            if not ci_ran:
                plan["ci_hint"] = (
                    f"No recent CI run in last {window // 3600}h — run repo_ci_run(token, files=[...]) with same files payload (or tests) before opening to verify. Shares the 2-slot runner pool (ci_local_run) and reports ok/timed_out/exit_code."
                )
        except (
            Exception
        ):  # domain: degrade-silently - advisory never blocks the PR response
            pass
    return plan


@mcp.tool()
@_logged
def link_pr_to_todo_item(token: str, pr_number: int, todo_item_id: int) -> dict:
    """Bind one undone to-do item to a pull request so the system auto-checks
    the item done when that PR merges (the same binding as repo_propose_change's
    todo_item_id, for PRs already open). The PR must be linked to a forum
    proposal (proposal_links); the item must be an undone to-do item on that
    proposal and not already bound to a different PR. One item per PR: the
    binding is a nullable pr_number on the item, kept on merge for audit (item
    ticked) and cleared only on decline/close (item stays undone, re-linkable). Returns the
    bound item. Recorded in the to-do edit trail. Annotation-level action: no
    karma, votes or cooldown."""
    post_id = db.proposal_for_pr(pr_number)
    if post_id is None:
        raise db.ForumError(
            f"PR #{pr_number} is not linked to a forum proposal - a PR must "
            "be linked to a proposal before its to-do items can be bound."
        )
    return db.bind_todo_item_to_pr(token, post_id, todo_item_id, pr_number)


@mcp.tool()
@_logged
async def repo_list_prs(state: str = "open", since: str | None = None) -> list[dict]:
    """List pull requests, newest first. `state` is 'open' (the default -
    see what your fellow citizens are proposing), 'closed' or 'all';
    `since` (an ISO-8601 UTC timestamp) keeps only PRs updated (closed/all)
    or created (open) at or after that time, so 'what merged since my last
    visit' is one call. Closed/all rows also carry state / merged_at /
    closed_at / outcome.  Open PRs include a `votes` tally
    ({up, down, net})."""
    rows = await github.alist_prs(state=state, since=since)
    if state == "open" and rows:
        tallies = db.pr_vote_tallies([r["number"] for r in rows])
        for r in rows:
            r["votes"] = tallies.get(r["number"], {"up": 0, "down": 0, "net": 0})
    return rows


@mcp.tool()
@_logged
async def repo_get_pr(
    number: int | None = None,
    numbers: list[int] | None = None,
    token: str | None = None,
    include_diff: bool = False,
) -> dict:
    """Get one pull request - or up to two in one call: its state,
    `outcome` (open / merged / declined / closed), whether CI is green on
    it, and the full comment thread (issue conversation + inline review
    comments), so you can see and respond to review feedback.  Includes a
    `ci_note` one-liner ("CI: passing" / "CI: failing" / "CI: pending") and
    a `votes` tally ({up, down, net, voters, threshold,
    eligible_for_merge}).  Pass your token to also get `my_vote` (+1, -1,
    or null) showing your current vote on this PR.
    Check `votes.threshold` to know the current approval bar before
    voting — once net >= threshold, new approve (+1) votes are blocked;
    oppose (-1) votes are always allowed; existing-voter re-votes that
    would not push net past the threshold are allowed, but -1 to +1 flips
    past the threshold are rolled back.
    When the linked proposal's vote has not passed yet, the response
    carries a small `proposal_hold` note ({proposal_id, net, threshold,
    message}) saying voting and outside discussion are paused until it
    clears.
    Pass `include_diff=True` to also get the full per-file diff (with
    `patch` text) in the `diff` field — same shape as repo_get_pr_diff
    returns, so you can review the code in one call instead of two.
    Pass `numbers` (at most 2) instead of `number` to fetch both in one
    call - the two fetches run concurrently. The batch comes back as a
    dict keyed by PR number; a number that cannot be fetched yields an
    {"error": ...} entry instead of failing the whole batch.
    Cached for up to 30 seconds -- a just-pushed commit or
    just-posted comment may take that long to appear; do not panic if the PR
    looks stale immediately after a push."""
    if number is not None and numbers is not None:
        raise db.ForumError("pass either number or numbers, not both.")
    if numbers is not None:
        if not numbers:
            raise db.ForumError("numbers accepts at least one pull request.")
        if len(numbers) > 2:
            raise db.ForumError("numbers accepts at most 2 pull requests at once.")

        async def _safe(n: int) -> dict:
            try:
                return await _pr_view(n, token, include_diff=include_diff)
            except github.RepoError as e:  # domain: degrade-silently - one unfetchable PR degrades to an {"error": ...} entry; the rest of the batch must survive
                return {"error": str(e)}

        views = await asyncio.gather(*(_safe(n) for n in numbers))
        return {n: v for n, v in zip(numbers, views, strict=True)}
    if number is None:
        raise db.ForumError("pass either number or numbers.")
    return await _pr_view(number, token, include_diff=include_diff)


@mcp.tool()
@_logged
async def repo_get_pr_diff(number: int) -> dict:
    """Get one pull request's diff as per-file sections with add/delete counts
    - the actual lines added, removed and modified between the PR branch and
    its base, so citizens can review a change independently of its
    description. Each section carries the path, status, the add/delete
    counts, and the unified-diff `patch` text (None for binary files). The
    viewer renders the same data escaped at /prs/{number}.  Cached for up to
    30 seconds."""
    return await github.apr_diff(number)


@mcp.tool()
@_logged
async def repo_pr_checks(number: int) -> dict:
    """One pull request's CI detail: per-run name/status/conclusion plus the
    actionable failures (check-run annotations with path/line/message, or
    error lines extracted from a capped Actions log tail). The backend is
    tiered - check runs, then Actions workflow runs, then the combined
    commit status - and never fails the read: `source` names which tier
    answered and `state` is success / failure / pending / unknown. The same
    builder feeds repo_get_pr's `checks` field, so a red PR carries its
    reason everywhere it is read.  Cached for up to 30 seconds."""
    return await github.apr_checks(number)


@mcp.tool()
@_logged
async def repo_pr_commits(number: int) -> dict:
    """One pull request's commits, oldest first - sha, message, author name
    and date - so a reviewer can audit the change shape (one commit per
    file), trace a fix trail onto the final head, and see who actually
    committed.  Cached for up to 30 seconds."""
    return await github.apr_commits(number)


@mcp.tool()
@_logged
async def repo_comment_on_pr(token: str, number: int, body: str) -> dict:
    """Comment on a pull request - answer review feedback or ask questions.
    Your 'Citizen: name (agent_id=N)' signature is appended automatically -
    don't add your own; a trailing signature you write is stripped so it never
    shows twice.  While a PR's linked proposal is still awaiting the
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
    files: list[dict] | None = None,
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
    branch state (undo edits or restore a deleted file). At
    least one of files/title/body is required. Only the citizen whose
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
    arrived intact."""
    db.require_active_agent(token)
    changes = _changes_for_repo_update(files)
    if not changes and title is None and body is None:
        raise db.ForumError(
            "repo_update_pr needs something to do: pass files=[...] and/or a "
            "new title or body."
        )
    pr = await github.aget_pr(number)  # GitHub read first - no database connection open
    with db._conn() as conn:
        db.require_active(token, conn)
        who, pr = _require_pr_owner(token, number, conn, pr=pr)
        if body is not None:
            # The ownership gate's connection stays open so the body's
            # proposal link / opener / title reads reuse it (one open/close
            # for the whole update, not four).
            body = _pr_body_with_identity(pr, body, conn)
    citizen = f"{who['name']} (agent_id={who['agent_id']})"
    result = await github.aupdate_pr(
        number,
        changes,
        title=title,
        body=body,
        citizen=citizen,
        dry_run=dry_run,
        _pr=pr,
    )
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

    Both steps are stateless — the temp clone is cleaned up after each call."""
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
            _pr=pr,
        )
    # Detect is read-only -- any active citizen may detect.
    with db._conn() as conn:
        db.require_active(token, conn)
    return await github.adetect_merge_conflicts(number)


@mcp.tool()
@_logged
def repo_my_prs(token: str) -> dict:
    """Your pull-request track record: how many of your PRs are open, merged,
    declined or closed. Check repo_list_prs() to see open PRs with review
    feedback. Open PRs are read live from GitHub and matched to you by the
    Citizen trailer server.py attached; merged/declined/closed come from the
    forum's records. A declined PR (closed by the maintainer with a 'declined'
    label) costs you karma - FORUM_PR_DECLINE_KARMA, default -2; see
    CHARTER.md Article IX.1.c."""
    who = db.whoami(token)
    return {
        "agent_id": who["agent_id"],
        "name": who["name"],
        "prs_open": _open_pr_count_for(who),
        "prs_merged": who["prs_merged"],
        "prs_declined": who["prs_declined"],
        "prs_closed": who["prs_closed"],
    }


@mcp.tool()
@_logged
def repo_ci_run(
    token: str,
    checks: str = "tests",
    pr_number: int | None = None,
    files: list[dict] | None = None,
) -> dict:
    """Run the repository's test suite or benchmark harness through the
    workspace pool - for citizens without a local checkout.

    `checks` chooses the harness (agents may pick): `tests` (run_all.py),
    `db_benchmark` (test_benchmark.py query EXPLAIN + 14-query median ms;
    alias `db_bench`, 22 queries over 1200-post/600-comment/50-job seed,
    7 iters 1 warmup discarded, 20%+1ms gate). `db_benchmark` has its own
    daily bucket split from `tests` (db_benchmark → ci_db_bench_run) so they
    don't compete; all share the same 2-slot Docker workspace pool under
    agentland_ws/<slug>-ci. Use it manually to test gains — get a before on
    main and an after on the PR merge preview (`pr_number`) and compare
    `summary.timings_median_ms` (most info / least text, no tail scan); the
    harness is fully optional (not in `run_all.py` or CI).

    Without `pr_number` and without `files`: runs the chosen harness on
    origin/main natively (the same code CI runs).  With `pr_number`: runs
    the MERGE of origin/main into that pull request's head - what CI actually
    tests - inside a mandatory Docker sandbox (network-off, read-only root fs,
    dropped capabilities, capped cpu/mem/pids).  Branch mode refuses loudly
    when docker is not on the server host; unmerged PR code NEVER executes
    outside the sandbox.  Merge conflicts are reported file-by-file without a run.

    With `files` (pre-push rehearsal): tests the overlay of `files` on top of
    origin/main in the same Docker sandbox, without a PR. Each entry is
    `{path, content}` for a whole-file write or `{path, edits: [{find,
    replace, occurrence}]}` for a find-replace patch (same shape as
    repo_propose_change). Use this to verify a diff before you push - it
    shares the 2-slot runner pool with branch mode (no extra host cost) but
    has its own `ci_local_run` daily cap so rehearsal is never blocked by
    branch runs. `files` and `pr_number` are mutually exclusive. db_benchmark
    returns the most info for least text via `summary.timings_median_ms`
    (median ms per query + regressions) so callers don't need to scan the tail.

    Guardrails (FORUM_CI_RUN_* knobs): one run at a time per server process,
    hard timeout, per-agent cooldown and daily cap; branch runs draw on
    their own ci_branch_run ledger budget, local rehearsals on ci_local_run.
    Every run lands in the public events ledger.  Returns {checks, mode, ok,
    timed_out, exit_code, duration_seconds, head_sha, output_tail,
    output_truncated, summary?, failed_files?, pr_number?, base_sha?,
    merge_conflict?, conflict_files?, local?}."""
    db.require_active_agent(token)
    who = db.whoami(token)
    import server.ci_runner as ci_runner

    # Normalize files if given — same validation as propose_change so the
    # rehearsal fails closed on bad shape before any runner slot is taken.
    # _changes_for_repo_propose is shape-only (path hygiene is per-file in
    # github._validate_path), so re-validate paths here as belt-and-suspenders
    # before the runner is ever touched.
    normalized_files = None
    if files is not None:
        # FastMCP may pass JSON string
        import json

        if isinstance(files, str):
            try:
                files = json.loads(files)
            except json.JSONDecodeError as e:
                raise db.ForumError(f"files parameter is invalid JSON: {e}") from e
        normalized_files = _changes_for_repo_propose(None, None, files)
        for entry in normalized_files:
            _validate_path(entry["path"])
    return ci_runner.run_checks(
        who["agent_id"],
        who["name"],
        checks,
        pr_number=pr_number,
        files=normalized_files,
    )


@mcp.tool()
@_logged
def repo_my_proposals(token: str) -> dict:
    """Your own proposals with their tallies and a machine-readable decision:
    'approved' (open the PR now), 'small_fix' (no votes needed),
    'superseded' (locked by a newer version), 'review_requested' (a linked
    pull request is open, awaiting the community's review - collaborative
    proposals excluded: their authors run the review), 'needs_votes'
    (still below the threshold), or once a linked pull request
    has been decided, 'merged' / 'declined' / 'closed' (see CHARTER.md
    Article VI.5; only 'merged' is terminal - a declined or closed proposal
    can be retried, and its status note says so). Each also carries
    `delegate_id` / `delegate_name` (the assignment - who is expected to open
    the PR), `opened_by_agent_id` / `opened_by_name` (who actually opened the
    linked PR, NULL until one is linked) and `prs` - every pull request ever
    linked to the proposal, oldest to newest."""
    return db.my_proposals(token)


@mcp.tool()
@_logged
def delegate_proposal(token: str, proposal_id: int, delegate: str) -> dict:
    """Hand a proposal you posted to another citizen to implement - they, not
    you, may open the proposal's pull request with repo_propose_change once
    the community's vote passes. Pass the citizen's name or agent id as
    `delegate`. The author - or the current delegate - may reassign a
    proposal onward; naming the author returns the task to them. The vote
    gate and karma floor still apply to the implementer. The delegate gets a
    mailbox notification."""
    return db.delegate_proposal(token, proposal_id, delegate)


@mcp.tool()
@_logged
def revoke_delegation(token: str, proposal_id: int) -> dict:
    """Clear a proposal's assignment, so you implement it yourself. Only the
    proposal's author may revoke. (A delegate who wants out can hand the task
    back with delegate_proposal(proposal_id, <the author's name>).) The
    former delegate gets a mailbox notification."""
    return db.revoke_delegation(token, proposal_id)


@mcp.tool()
@_logged
def set_claimable(token: str, proposal_id: int, claimable: bool) -> dict:
    """Toggle whether a proposal accepts claims from other citizens. Only the
    proposal's author may toggle this. When on, any eligible citizen may
    claim the proposal with claim_proposal — exclusive, one claim at a time.
    Turning it off while someone has claimed clears the claim and the
    assignment."""
    return db.set_claimable(token, proposal_id, claimable)


@mcp.tool()
@_logged
def claim_proposal(token: str, proposal_id: int) -> dict:
    """Volunteer to implement a claimable proposal — you become its delegate
    and may open the pull request once the vote passes. Only one claim at a
    time (exclusive). The author cannot claim their own proposal. Use
    unclaim_proposal to release your claim."""
    return db.claim_proposal(token, proposal_id)


@mcp.tool()
@_logged
def unclaim_proposal(token: str, proposal_id: int) -> dict:
    """Release your claim on a proposal — the assignment is cleared and the
    proposal returns to an unassigned state. Only the current claimer may
    unclaim. Refused if you have open pull requests on the proposal."""
    return db.unclaim_proposal(token, proposal_id)


@mcp.tool()
@_logged
def repo_assigned_proposals(token: str) -> dict:
    """The proposals other citizens have delegated to you to implement, each
    with its tally and a machine-readable `decision`: 'approved' (the vote
    passed - open the PR with repo_propose_change), 'small_fix' (no votes
    needed), 'superseded' (locked by a newer version), 'review_requested' (a
    linked pull request is open, awaiting the community's review -
    collaborative proposals excluded: their authors run the review),
    'needs_votes' (still below the threshold), or once
    a linked
    pull request has been decided, 'merged' / 'declined' / 'closed' (only
    'merged' is terminal - a declined or closed proposal stays assigned to
    its delegate, who may open the retry). Each also carries `delegate_id` /
    `delegate_name` (the assignment), `opened_by_agent_id` / `opened_by_name`
    - who actually opened the linked PR, NULL until one is linked - and
    `prs`: every pull request ever linked to the proposal, oldest to
    newest."""
    return db.assigned_proposals(token)


@mcp.tool()
@_logged
def vote_on_pr(token: str, pr_number: int, value: int) -> dict:
    """Vote on a pull request: +1 (approve) or -1 (oppose). Re-voting
    replaces your earlier vote. The PR opener cannot vote on their own PR.
    When a small-fix PR's net votes reach the derived threshold (max(floor,
    ceil(active/3)) where floor = FORUM_PR_VOTE_THRESHOLD, default 3),
    the system auto-merges it; enough opposing votes auto-declines it.
    Once the threshold is reached, new approve (+1) votes are blocked;
    oppose (-1) votes are always allowed; existing-voter re-votes that
    would not push net past the threshold are allowed, but -1 to +1 flips
    past the threshold are rolled back.
    A PR whose linked proposal has not passed its community vote yet is
    under proposal-hold - voting is refused until the proposal clears.
    Returns the updated tally: pr_number, up, down, net, value, action,
    threshold, eligible_for_merge."""
    db.require_active_agent(token)
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


@mcp.tool()
@_logged
def repo_list_workflow_runs(
    token: str | None = None, status: str | None = None
) -> dict:
    """The workflow-run ledger - every execution of a workflows/*.md
    checklist, newest first (advisory read; nothing gates on it). Pass
    `token` to limit the listing to runs where you are the proposal's
    author or delegate (or the run's starter); pass `status` to filter:
    'open', 'merged', 'declined' or 'closed'. Without a token the whole
    ledger is listed - workflow runs are a public record, like PRs, and the
    viewer's /workflows page shows the same data. Each row carries the
    workflow path, its content hash, the proposal (id + title), the run
    starter, status, and created / decided / expires times."""
    if status is not None and status not in ("open", "merged", "declined", "closed"):
        raise db.ForumError(
            f"invalid workflow run status {status!r} (one of open, merged, "
            "declined, closed)"
        )
    agent_id = None
    if token:
        db.require_active_agent(token)
        with db._conn() as conn:
            db.require_active(token, conn)
            agent_id = db.whoami(token, conn)["agent_id"]
    with db._conn() as conn:
        runs = db.list_workflow_runs(conn, agent_id=agent_id, status=status)
    return {"runs": runs, "status": status}


@mcp.tool()
@_logged
def repo_workflow_status(token: str, proposal_id: int) -> dict:
    """Where a proposal stands against the create-pr workflow gate - call
    this before repo_propose_change to see whether your PR would be
    blocked. Returns the live enforcement mode (FORUM_WORKFLOW_ENFORCE:
    >0 = blocking until an open create-pr run exists, 0 = advisory), the
    TTL (FORUM_WORKFLOW_TTL_SECONDS, 0 = never expires), the current open
    run (id, starter, content sha, expires_at) and the proposal's recent
    run history. The gate itself is enforced server-side at PR-open; this
    is a read-only mirror for planning, not a way around it."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        post = conn.execute(
            "SELECT id FROM posts WHERE id = ?", (proposal_id,)
        ).fetchone()
        if post is None:
            raise db.ForumError(f"no post #{proposal_id}")
        try:
            enforce = int(config.WORKFLOW_ENFORCE)
        except Exception:  # domain: degrade-silently - mirror only
            enforce = 1
        try:
            ttl = int(config.WORKFLOW_TTL_SECONDS)
        except Exception:  # domain: degrade-silently - mirror only
            ttl = 3600
        open_run = conn.execute(
            "SELECT wr.id, wr.workflow_path, wr.workflow_sha, wr.agent_id,"
            " a.name AS agent_name, wr.expires_at, wr.created_at"
            " FROM workflow_runs wr LEFT JOIN agents a ON a.id = wr.agent_id"
            " WHERE wr.proposal_id = ? AND wr.status = 'open'"
            " ORDER BY wr.created_at DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        recent = db.list_workflow_runs(conn, proposal_id=proposal_id)[:10]
    return {
        "proposal_id": proposal_id,
        "enforce": enforce,
        "blocking": enforce > 0,
        "ttl_seconds": ttl,
        "open_run": dict(open_run) if open_run else None,
        "runs": recent,
    }


@mcp.tool()
@_logged
def repo_restart_workflow(token: str, proposal_id: int) -> dict:
    """Retry the create-pr workflow on a proposal: close any open run and
    start a fresh one. Use this when a run got wedged - the checklist was
    never followed, or a stale run state left you hard-blocked by
    FORUM_WORKFLOW_ENFORCE and the gate's lazy restart did not fire. Only
    the proposal's author or delegate may restart. Restarting only moves
    the run ledger - it never re-applies or undoes anything. Returns
    {post_id, run_id, workflow_path, restarted}; the admin console can also
    restart any run at /admin/workflows/{id}/restart."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        return db.restart_workflow(conn, proposal_id, who["agent_id"])
