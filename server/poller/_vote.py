"""server.poller._vote — vote sweep (auto-merge / auto-decline) and helpers."""

from __future__ import annotations

import concurrent.futures as _cf  # for TimeoutError robustness across versions
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta, timezone

import config
import db
import github
import logutil
import notifications
from db._pr_vote import (
    _pr_vote_threshold,
    pr_decline_ready_batch,
)
from events import (
    EVT_CI_BRANCH_RUN,
    EVT_PR_AUTO_DECLINED,
    EVT_PR_AUTO_MERGED,
    EVT_PR_HOLD_APPLIED,
    EVT_PR_HOLD_RELEASED,
    log_event,
)

# -- PR vote sweep (auto-merge / auto-decline) ---------------------------

# Labels that block auto-merge: the maintainer applies 'hold' to prevent
# the vote sweep from merging a PR that needs more work despite positive
# votes.
_HOLD_LABEL = "hold"


def _notify_proposal_watchers(
    conn,
    proposal_id: int,
    message: str,
    exclude: set[int],
    actor: int,
) -> None:
    """Ping every subscriber of a proposal (already-notified citizens are
    excluded via *exclude*), ref_type/ref_id pointing at the post so
    mailbox links land on it.  *actor* is a real agent id - notifications
    FK the actor to the agents table, so system events borrow the citizen
    whose action triggered them."""
    from db._subscriptions import _notify_subscribers

    _notify_subscribers(
        conn,
        proposal_id,
        message,
        actor_agent_id=actor,
        ref_type="post",
        ref_id=proposal_id,
        exclude_agent_ids=exclude,
    )


def _pr_created_epoch(pr: dict) -> float | None:
    """Parse a GitHub PR's created_at into epoch seconds, or None if absent
    or unparseable (so a missing timestamp fails open to 'old enough')."""
    ca = pr.get("created_at")
    if not ca:
        return None
    try:
        dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _pr_stall_notices_impl(
    candidates: list[tuple],
    threshold: int,
    tallies: dict,
    *,
    conn,
) -> list[dict]:
    """Tell a PR's opener when their in-flight branch has stalled below
    the merge bar. The community-facing pr_vote_note deliberately excludes
    the opener (they cannot vote on their own PR), so without this nothing
    ever points the author at a stalled branch.

    Fires for open, linked, non-collaborative PRs whose proposal vote has
    passed, that are neither merge- nor decline-eligible, and have been
    open at least FORUM_PR_STALL_HOURS (0 disables). Deduped to at most
    one notice per PR per 24h via the notifications table itself - no new
    state, no steady-state writes while quiet. Runs on the caller's
    connection so the whole sweep shares one threshold derivation."""
    if not candidates or config.PR_STALL_HOURS <= 0:
        return []

    cutoff = time.time() - config.PR_STALL_HOURS * 3600
    actions: list[dict] = []
    for pr, opener, proposal_post_id in candidates:
        number = pr["number"]
        created = _pr_created_epoch(pr)
        if created is None or created > cutoff:
            continue  # unparsable or younger than the stall window
        try:
            if not db.proposal_vote_state(proposal_post_id)["approved"]:
                continue  # held: voting is paused, not stalled
        except Exception:
            # domain: degrade-silently - unknown proposal state must
            # not kill the notice pass; retried on the next sweep.
            continue
        tally = tallies.get(number) or {"net": 0}
        if tally["net"] >= threshold or tally["net"] <= -threshold:
            continue  # merge/decline machinery owns this PR now
        needed = max(1, threshold - tally["net"])
        recent = conn.execute(
            "SELECT 1 FROM notifications WHERE agent_id = ?"
            " AND kind = 'pr' AND ref_type = 'pr' AND ref_id = ?"
            " AND body LIKE '%sits at net %'"
            " AND created_at > ? LIMIT 1",
            (
                opener["agent_id"],
                number,
                db._now_iso(datetime.now(timezone.utc) - timedelta(hours=24)),
            ),
        ).fetchone()
        if recent is not None:
            continue  # already nudged inside the quiet window
        notifications._notify(
            conn,
            opener["agent_id"],
            "pr",
            "pr",
            number,
            f"PR #{number} has been open {config.PR_STALL_HOURS}h+ and "
            f"sits at net {tally['net']} vs bar {threshold} "
            f"({needed} more approving vote(s) needed). Nudge "
            f"reviewers or update the branch.",
        )
        actions.append({"action": "pr_stall_notice", "pr_number": number})
    return actions


def _pr_stall_notices(
    candidates: list[tuple],
    threshold: int,
    tallies: dict,
    *,
    conn=None,
) -> list[dict]:
    """Shim: acquire a connection when called without one."""
    if conn is not None:
        return _pr_stall_notices_impl(candidates, threshold, tallies, conn=conn)
    with db._conn() as owned:
        return _pr_stall_notices_impl(candidates, threshold, tallies, conn=owned)


def _pr_conflict_notice(pr: dict, opener: dict) -> None:
    """Notify the opener that their PR now conflicts with main - the vote
    sweep logs the rebase conflict but would otherwise skip it silently,
    forever, since auto-merge retries every pass and fails every time.

    Re-notifies only when the PR was pushed after the last conflict
    notice (a fresh head deserves a fresh ping); an unchanged red-conflict
    branch stays quiet."""
    from db._core import _parse_iso

    with db._conn() as conn:
        prior = conn.execute(
            "SELECT created_at FROM notifications WHERE agent_id = ?"
            " AND kind = 'pr' AND ref_type = 'pr' AND ref_id = ?"
            " AND body LIKE '%now conflicts with main%'"
            " ORDER BY id DESC LIMIT 1",
            (opener["agent_id"], pr["number"]),
        ).fetchone()
        if prior is not None:
            pushed_at = _parse_iso(pr.get("updated_at") or "")
            noticed_at = _parse_iso(prior["created_at"])
            if pushed_at is None or noticed_at is None or pushed_at <= noticed_at:
                return  # same head already pinged; stay quiet
        notifications._notify(
            conn,
            opener["agent_id"],
            "pr",
            "pr",
            pr["number"],
            f"PR #{pr['number']} now conflicts with main - auto-merge "
            "skipped it this round. Rebase onto main or resolve the "
            "conflicts (repo_resolve_conflicts) and it will re-enter the "
            "merge queue.",
        )


def _local_branch_cached_ok(
    pr_number: int, head_sha: str, memo: dict | None = None
) -> bool | None:
    """Check ledger cache for a recent branch-mode CI run for this head.

    Returns True if the most recent ci_branch_run for this (pr, head) was
    ok/success, False if it was a failure/conflict/timeout, None if no
    record yet. Only 'tests' checks are considered; caller checks
    CI_FALLBACK_ENABLED before consulting.

    ``memo`` is an optional per-sweep cache (a plain dict keyed by
    ``(pr_number, head_sha)``) so one poller sweep's repeated lookups for
    the same head scan the events ledger once instead of five times. It is
    caller-scoped - never shared across sweeps, so a fresh sweep always
    re-reads the ledger (a just-landed run for the same head can change the
    answer)."""
    if not head_sha:
        return None
    if memo is not None:
        key = (pr_number, head_sha)
        if key in memo:
            return memo[key]
    try:
        # Scan recent branch runs — newest first, limit 100 to bound
        # work; filter in Python because detail is JSON.
        rows = __import__("events").query_events(kind=EVT_CI_BRANCH_RUN, limit=100)
    except Exception:
        # domain: degrade-silently - ledger unavailable, treat as no cache
        return None
    for r in rows:
        d = r.get("detail") or {}
        if d.get("pr_number") != pr_number:
            continue
        if d.get("head_sha") != head_sha:
            continue
        if d.get("checks") != "tests":
            continue
        # merge_conflict counts as failure for merge gate
        if d.get("merge_conflict"):
            result: bool | None = False
        elif "ok" in d:
            result = bool(d["ok"])
        else:
            continue
        if memo is not None:
            memo[key] = result
        return result
    if memo is not None:
        memo[key] = None
    return None


def _ensure_local_branch_ok(
    pr_number: int, head_sha: str, memo: dict | None = None
) -> bool:
    """Run local branch CI on demand for fallback, caching via ledger.

    If a recent ledger entry for this head already exists, reuse it.
    Otherwise run the sandboxed branch suite headlessly (no cooldown) and
    return its ok. Any error is a soft failure (local not ok). ``memo`` is
    the per-sweep branch-cache dict forwarded to _local_branch_cached_ok."""
    if not config.CI_FALLBACK_ENABLED or not config.CI_RUN_BRANCH_ENABLED:
        return False
    cached = _local_branch_cached_ok(pr_number, head_sha, memo)
    if cached is not None:
        return cached
    # No cache — run the suite now (respects CI_RUN_CONCURRENCY via slot pool).
    try:
        import server.ci_runner as ci_runner

        res = ci_runner.run_branch_ci_for_poller(pr_number, checks="tests")
        # res carries ok/merge_conflict; treat conflict as not ok for gate
        if res.get("merge_conflict"):
            return False
        return bool(res.get("ok"))
    except Exception as exc:
        # domain: degrade-silently - poller fallback local CI failure skips merge gate
        logutil.log("local_branch_ci_failed", pr_number=pr_number, error=str(exc))
        return False


def _pr_vote_sweep(
    open_prs: list[dict] | None = None,
    checks_cache: dict[int, dict] | None = None,
) -> list[dict]:
    """Check open PRs for vote-based auto-merge or auto-decline.

    By default (PR_AUTO_MERGE_SMALL_FIX_ONLY=1) only small-fix PRs are
    eligible; when set to 0, all linked PRs qualify.  The sweep runs in
    two phases:

    Phase 1 (scan): iterate all open PRs, process auto-declines, and
    identify the single oldest eligible merge candidate.

    Phase 2 (merge): for the candidate, rebase onto main, wait for CI
    to pass on the rebased branch, then merge.  At most one merge per
    sweep — next sweep picks the next PR.  This guarantees every PR is
    tested against the latest main before merge.

    CI gating is GitHub-authoritative by default (CI_FALLBACK_ENABLED=0):
    the sweep checks GitHub Actions CI only, so the shared host CI slot
    stays free for agents' own repo_ci_run rehearsals. Setting
    CI_FALLBACK_ENABLED=1 re-enables the hybrid OR gate, in which the sweep
    may also run a local branch CI (when GitHub's checks stay
    pending/unknown/failure or the API is unreachable) and treats either
    CI as sufficient to merge.

    A PR is auto-merged when:
      - net votes >= the derived PR vote threshold (max(floor,
        ceil(active/3)) where floor = FORUM_PR_VOTE_THRESHOLD)
      - CI is green (or no CI required) before rebase
      - the 'hold' label is NOT present
      - rebase onto main succeeds (no conflicts)
      - CI passes again after rebase

    A PR is auto-declined when net votes <= -threshold, but only after a
    grace window (PR_DECLINE_GRACE_SECONDS) from when it first became
    decline-eligible, so authors can fix and re-request reviews.  A
    passing PR is likewise not auto-merged until it has been open for
    PR_MERGE_MIN_AGE_SECONDS (so even freshly-passing work gets a review
    window).

    ``open_prs`` is an optional pre-fetched list of open PRs from
    ``github.open_prs()``.  When provided the sweep skips its own fetch,
    saving one GitHub API call (the caller and the CI-failure sweep share
    the same list).

    ``checks_cache`` is an optional shared per-tick dict of tiered-checks
    results: numbers already present are reused and fresh fetches are
    stored back, so the poller's three checks consumers share one fetch
    pool per tick (None keeps standalone behavior).

    Returns a list of actions taken (for logging)."""

    actions: list[dict] = []
    if open_prs is None:
        open_prs = github.open_prs()
    # Batched pre-pass (proposal #111 audit item: N+1 in the vote sweep):
    # one connection resolves everything the per-PR gates used to re-derive
    # per number - the linked opener/proposal maps, the small-fix kind
    # gate (one IN fetch), the PR-vote threshold (derived once instead of
    # twice per PR via pr_eligible_for_merge / pr_eligible_for_decline),
    # every candidate's tally (one GROUP BY), and the decline-grace
    # markers.  GitHub I/O below stays per-PR by necessity.
    with db._conn() as conn:
        openers = db.linked_pr_openers(conn=conn)
        proposals_map = db.linked_pr_proposals(conn=conn)

    # Repair pass (proposal #155): an open PR whose body stamps a
    # proposal but whose DB link never landed - e.g. the claim gate
    # refused at open time because an earlier verdict had released the
    # opener's claims - gets retried here on every sweep. The retry
    # succeeds exactly when the opener now holds an undone claim (the
    # remedy the refusal names); decided PRs are handled by the outcome
    # poller's exempt backfill instead.
    for pr in open_prs:
        number = pr["number"]
        if proposals_map.get(number) is not None:
            continue  # already linked
        parsed_pid = github._parse_proposal(pr.get("body") or "")
        opener = openers.get(number) or pr.get("citizen")
        if not (parsed_pid and opener):
            continue
        try:
            db.link_pr_to_proposal(number, parsed_pid, opener["agent_id"])
        except db.ForumError:
            continue  # still claim-less; retried on the next sweep

    candidates = []
    for pr in open_prs:
        opener = openers.get(pr["number"]) or pr.get("citizen")
        if opener and proposals_map.get(pr["number"]):
            candidates.append((pr, opener, proposals_map[pr["number"]]))
    if not candidates:
        return actions

    # Proposal-hold release pass: a PR opened while its linked proposal
    # was still awaiting the community's vote carries the 'proposal-hold'
    # label and a 'WIP: ' title prefix.  The moment that vote passes this
    # pass strips the prefix (first), drops the label (last), and tells
    # the opener, the proposal author, and every subscriber that the PR
    # is open for review and voting.  Hold membership is DB truth - the
    # pr_hold_applied event logged at stamp time plus the vote tally -
    # never the label, which a failed side effect could leave off and
    # thereby silently unlock an unapproved PR (#375 review).  The
    # pr_hold_released event is the commit point, so a crash mid-release
    # converges on the next sweep: the title guard no-ops once stripped,
    # removing an absent label is tolerated (the label is cosmetic now),
    # and notifications fire exactly once.  A held PR cannot orphan-lock:
    # supersede_proposal refuses while any PR is in flight, so the parent
    # can only lock after the PR was closed by hand (karma-neutral).
    # Runs before the small-fix merge filter below so holds on regular
    # (non-small-fix) proposals are lifted too. Hold membership is one
    # batched read: two IN queries on a single connection for the whole
    # candidate list, not two connections per candidate.
    _hold_numbers = [pr["number"] for pr, _, _ in candidates]
    _marks = ",".join("?" * len(_hold_numbers))
    with db._conn() as conn:
        applied = {
            r[0]
            for r in conn.execute(
                "SELECT target_id FROM events WHERE kind = ?"
                f" AND target_type = 'pr' AND target_id IN ({_marks})",
                (EVT_PR_HOLD_APPLIED, *_hold_numbers),
            ).fetchall()
        }
        released = {
            r[0]
            for r in conn.execute(
                "SELECT target_id FROM events WHERE kind = ?"
                f" AND target_type = 'pr' AND target_id IN ({_marks})",
                (EVT_PR_HOLD_RELEASED, *_hold_numbers),
            ).fetchall()
        }
    for pr, opener, proposal_post_id in list(candidates):
        number = pr["number"]
        if number not in applied or number in released:
            continue  # never held, or already released
        try:
            state = db.proposal_vote_state(proposal_post_id)
            if not state["approved"]:
                continue  # still pending; markers stay on
        except Exception:
            continue  # unknown proposal state; retried on the next sweep
        title = pr.get("title") or ""
        if title.upper().startswith("WIP:"):
            # Strip exactly one leading marker - ours or an author's
            # self-applied one; either way the hold is over.  Title
            # first: a failure here retries cleanly on the next sweep.
            try:
                github.update_pr_title(number, title[4:].lstrip())
            except Exception as exc:
                logutil.log(
                    "pr_hold_release_failed",
                    pr_number=number,
                    error=str(exc),
                )
                continue
        try:
            github.remove_pr_label(number, config.PROPOSAL_HOLD_LABEL)
        except Exception as exc:
            # Cosmetic only - every gate keys off vote state, not the
            # label - so a lingering label must not block the release.
            logutil.log(
                "pr_hold_label_remove_failed",
                pr_number=number,
                error=str(exc),
            )
        with db._conn() as conn:
            log_event(
                EVT_PR_HOLD_RELEASED,
                actor_agent_id=opener["agent_id"],
                actor_name=opener.get("name"),
                target_type="pr",
                target_id=number,
                detail={"pr_number": number, "proposal_id": proposal_post_id},
                conn=conn,
            )
            notifications._notify(
                conn,
                opener["agent_id"],
                "pr",
                "pr",
                number,
                f"Proposal #{proposal_post_id} passed its vote - "
                f"PR #{number} is now open for review and voting.",
            )
            exclude = {opener["agent_id"]}
            author_row = conn.execute(
                "SELECT agent_id FROM posts WHERE id = ?",
                (proposal_post_id,),
            ).fetchone()
            if author_row and author_row["agent_id"] not in exclude:
                notifications._notify(
                    conn,
                    author_row["agent_id"],
                    "pr",
                    "proposal",
                    proposal_post_id,
                    f"Proposal #{proposal_post_id} passed its vote - "
                    f"PR #{number} is now open for review.",
                )
                exclude.add(author_row["agent_id"])
            _notify_proposal_watchers(
                conn,
                proposal_post_id,
                f"Proposal #{proposal_post_id} passed its vote - "
                f"PR #{number} is now open for review.",
                exclude,
                actor=opener["agent_id"],
            )
        actions.append({"action": "hold_released", "pr_number": number})

    # Opener stall notices run on the FULL candidate list - deliberately
    # before the small-fix merge filter below, so a regular proposal's PR
    # gets stall signals too even while SMALL_FIX_ONLY gates auto-merge.
    # The threshold is derived ONCE here (the batching guard counts
    # active-citizen reads) and shared by both passes below.
    all_candidates = list(candidates)
    numbers_all = [pr["number"] for (pr, _o, _p) in all_candidates]
    with db._conn() as conn:
        threshold = _pr_vote_threshold(conn)
        tallies = db.pr_vote_tallies(numbers_all, conn=conn)
        actions.extend(_pr_stall_notices(all_candidates, threshold, tallies, conn=conn))
        # When PR_AUTO_MERGE_SMALL_FIX_ONLY is set (default), only
        # small-fix PRs are auto-merge eligible.  Set to 0 to extend
        # to all PRs with linked proposals.  One IN (...) fetch replaces
        # the per-PR posts lookup; non-small-fix candidates drop out
        # exactly as the old early `continue` did (skipping decline too).
        if config.PR_AUTO_MERGE_SMALL_FIX_ONLY:
            pids = [pid for (_pr, _op, pid) in candidates]
            marks = ",".join("?" * len(pids))
            kind_rows = conn.execute(
                f"SELECT id FROM posts WHERE id IN ({marks})"
                " AND proposal_kind = 'small_fix'",
                pids,
            ).fetchall()
            small_fix_ids = {r["id"] for r in kind_rows}
            candidates = [c for c in candidates if c[2] in small_fix_ids]
            numbers = [pr["number"] for (pr, _o, _p) in candidates]
        else:
            numbers = numbers_all
        eligible_merge = {n for n in numbers if tallies[n]["net"] >= threshold}
        eligible_decline = {n for n in numbers if tallies[n]["net"] <= -threshold}
        decline_ready = pr_decline_ready_batch(
            conn,
            numbers,
            eligible_decline,
            config.PR_DECLINE_GRACE_SECONDS,
        )
    # Pre-fetch both CI systems concurrently — local Docker on the host
    # and GitHub Actions on the cloud run at the same time (GH pool up to
    # 8, local pending list; GH and local overlap, locals run in parallel
    # up to CI_RUN_CONCURRENCY via slot pool — _RUN_LOCK is legacy, never
    # acquired in production, only checked for tests).
    gh_results: dict[int, dict] = {}
    gh_errors: dict[int, Exception] = {}
    # Keyed by (pr_number, head_sha) to avoid stale-head reuse
    local_results: dict[tuple[int, str], bool] = {}

    if candidates:
        # Expose debounced pending to poller — skip host launch if already pending.
        # Reads via snapshot helper under _PENDING_LOCK; direct keys() without
        # the lock can raise RuntimeError: dictionary changed size during
        # iteration when the ticker mutates concurrently (caught but silently
        # defeats dedup, launching duplicate host CI).
        try:
            from server.tools.repo import pending_prs_snapshot

            pending_prs = pending_prs_snapshot()
        except Exception:
            pending_prs = (
                set()
            )  # domain: degrade-silently - import or lock failure must not stall poller
        # Prepare local cache checks upfront — head_sha from PR, not GH,
        # so local can start without waiting for GH.
        pending_locals: list[tuple[int, str]] = []
        # Per-sweep memo of _local_branch_cached_ok: the sweep's repeated
        # lookups (initial scan, GH-head refresh, final merge-gate reads)
        # share one ledger scan per (pr, head) instead of five.
        local_ok_memo: dict[tuple[int, str], bool | None] = {}
        if config.CI_FALLBACK_ENABLED and config.CI_RUN_BRANCH_ENABLED:
            for pr, _, _ in candidates:
                num = pr["number"]
                head_sha = pr.get("head_sha") or ""
                if num in pending_prs:
                    continue  # already debounced, poller skips duplicate host run
                cached = _local_branch_cached_ok(num, head_sha, local_ok_memo)
                if cached is not None:
                    local_results[(num, head_sha)] = cached
                    if not head_sha:
                        local_results[(num, "")] = cached
                else:
                    pending_locals.append((num, head_sha))
        # Poller: GitHub authoritative — await GH first so CI slots stay free
        # for agents (repo_ci_run). The local fallback is dormant by default
        # (CI_FALLBACK_ENABLED=0): the pre-pass above and the needs_local
        # pool below run only when that knob re-enables it, and then only for
        # PRs where GH is not success and only after GH has been awaited
        # (keeping the hybrid OR-gate without the one-double-per-head overlap).
        # Numbers already in the shared per-tick cache are reused; only
        # misses fan out a pool.
        if checks_cache is not None:
            for pr, _, _ in candidates:
                if pr["number"] in checks_cache:
                    gh_results[pr["number"]] = checks_cache[pr["number"]]
        missing = [
            pr["number"] for pr, _, _ in candidates if pr["number"] not in gh_results
        ]
        if missing:
            gh_pool_size = min(8, len(missing))
            with ThreadPoolExecutor(max_workers=gh_pool_size) as gh_pool:
                gh_futures = {
                    gh_pool.submit(github.pr_checks, num): num for num in missing
                }
                for fut in as_completed(gh_futures):
                    num = gh_futures[fut]
                    try:
                        gh_results[num] = fut.result()
                    except Exception as exc:  # domain: degrade-silently - per-PR GH failure isolated, local may still pass
                        gh_errors[num] = exc
                        logutil.log(
                            "ci_check_batch_error", pr_number=num, error=str(exc)
                        )
        if checks_cache is not None:
            checks_cache.update(gh_results)
        # Local fallback: only for candidates where GH is not success.
        # Dedup via pending_locals (already excludes pending_prs + ledger cache)
        # plus a second filter after GH: skip locals where GH already success.
        # Dormant by default (CI_FALLBACK_ENABLED=0) - this pool only fills
        # from the guarded pre-pass above, so no local run ever launches
        # unless an operator re-enables the knob.
        needs_local: list[tuple[int, str]] = []
        for num, sha in pending_locals:
            gh = gh_results.get(num)
            if gh is not None and gh.get("state") == "success":
                continue  # GitHub already green — no local needed, slot stays free
            # If GH failed to return (error) or is pending/unknown/failure, fall through to local
            needs_local.append((num, sha))
        if needs_local:
            # Live 3×1.5c: keep 1 slot for user, poller at most N-1 locals (2 when N=3)
            try:
                _poller_local_cap = max(1, int(config.CI_RUN_CONCURRENCY) - 1)
            except Exception:
                _poller_local_cap = 2  # domain: degrade-silently
            local_pool_size = min(_poller_local_cap, len(needs_local))
            with ThreadPoolExecutor(max_workers=local_pool_size) as local_pool:
                local_futures = {
                    local_pool.submit(
                        _ensure_local_branch_ok, num, sha, local_ok_memo
                    ): num
                    for num, sha in needs_local
                }
                for local_fut in as_completed(local_futures):
                    num = local_futures[local_fut]
                    head_sha = next((sha for n, sha in needs_local if n == num), "")
                    try:
                        local_results[(num, head_sha)] = bool(local_fut.result())
                    except Exception as exc:  # domain: degrade-silently - local run failed, treat as not ok
                        logutil.log(
                            "local_branch_ci_failed", pr_number=num, error=str(exc)
                        )
                        local_results[(num, head_sha)] = False
        # Refresh local cache with GH head_sha where GH provided a fresher sha
        if config.CI_FALLBACK_ENABLED and config.CI_RUN_BRANCH_ENABLED:
            for pr, _, _ in candidates:
                num = pr["number"]
                gh = gh_results.get(num)
                gh_sha = gh.get("head_sha") if gh else None
                # Already have result for this head (pr head or GH head) → skip
                if gh_sha and (num, gh_sha) in local_results:
                    continue
                pr_sha = pr.get("head_sha") or ""
                if (num, pr_sha) in local_results:
                    continue
                if gh is not None:
                    gh_sha = gh.get("head_sha") or ""
                    if gh_sha and gh_sha != pr_sha:
                        cached = _local_branch_cached_ok(num, gh_sha, local_ok_memo)
                        if cached is not None:
                            local_results[(num, gh_sha)] = cached

    merge_candidates: list[tuple] = []
    for pr, opener, proposal_post_id in candidates:
        number = pr["number"]
        # Proposal-hold skip by DB truth: a linked proposal whose
        # community vote has not passed blocks auto-merge outright - no
        # label consulted, so a failed label write can never unlock an
        # unapproved implementation (#375 review).  The maintainer's
        # 'hold' label (don't auto-merge despite votes) stays a live
        # GitHub check.
        try:
            if not db.proposal_vote_state(proposal_post_id)["approved"]:
                continue
        except Exception:
            continue  # unknown proposal state; never auto-merge on doubt
        try:
            if github.pr_has_label(number, _HOLD_LABEL, _pr=pr):
                continue
        except Exception:
            continue  # if we can't check labels, skip
        # Check CI status - GitHub-only by default (CI_FALLBACK_ENABLED=0);
        # the hybrid OR (local prioritized, GitHub on the side) runs only when
        # that knob re-enables the local fallback. Both then ran concurrently;
        # either success is sufficient but local is checked first so host
        # pool work is preferred over cloud.
        # Keep pr_head_sha for local lookup; gh_head_sha is GH's view which
        # may be a fresher SHA if a push landed between open_prs and pr_checks.
        pr_head_sha = pr.get("head_sha") or ""
        gh = gh_results.get(number)
        if gh is not None:
            gh_state = gh.get("state")
            gh_head_sha = gh.get("head_sha") or pr_head_sha
            gh_ok = gh_state == "success"
        else:
            gh_ok = False
            gh_state = "unknown"
            gh_head_sha = pr_head_sha
        # Local lookup keyed by (number, pr_head_sha); also check gh_head_sha
        # when it differs so a locally-green head isn't missed after a push.
        local_ok = bool(local_results.get((number, pr_head_sha), False))
        if not local_ok and gh_head_sha != pr_head_sha:
            local_ok = bool(local_results.get((number, gh_head_sha), False))
        # Refresh from cache if we didn't run local for this head — check
        # pr_head_sha first, then gh_head_sha if different.
        if not local_ok and config.CI_FALLBACK_ENABLED:
            cached = _local_branch_cached_ok(number, pr_head_sha, local_ok_memo)
            if cached:
                local_ok = True
                local_results[(number, pr_head_sha)] = True
            elif gh_head_sha != pr_head_sha:
                cached2 = _local_branch_cached_ok(number, gh_head_sha, local_ok_memo)
                if cached2:
                    local_ok = True
                    local_results[(number, gh_head_sha)] = True
        if not config.CI_FALLBACK_ENABLED:
            ci_ok = gh_state == "success"
        else:
            # Local-first OR: host 2c/1024M+256M is primary, GitHub is sidecar
            ci_ok = local_ok or gh_ok
        # Auto-merge eligibility check: collect every candidate; Phase 2
        # runs each through rebase -> CI -> merge in candidate order.
        if ci_ok and number in eligible_merge:
            # Don't auto-merge a brand-new PR: give reviewers a window
            # (PR_MERGE_MIN_AGE_SECONDS) even on freshly-passing work.
            _created = _pr_created_epoch(pr)
            if _created is not None and (
                time.time() - _created < config.PR_MERGE_MIN_AGE_SECONDS
            ):
                pass  # too young; eligible in a future sweep
            else:
                merge_candidates.append((pr, opener, proposal_post_id))
        # Auto-decline check
        if number in decline_ready:
            with db._conn() as conn:
                try:
                    github.decline_pr(number)
                    actions.append({"action": "auto_decline", "pr_number": number})
                    log_event(
                        EVT_PR_AUTO_DECLINED,
                        actor_agent_id=opener["agent_id"],
                        actor_name=opener.get("name"),
                        target_type="pr",
                        target_id=number,
                        detail={"pr_number": number},
                        conn=conn,
                    )
                    # Notify opener + proposal author.
                    notifications._notify(
                        conn,
                        opener["agent_id"],
                        "pr",
                        "pr",
                        number,
                        f"PR #{number} was auto-declined",
                    )
                    if proposal_post_id:
                        author_row = conn.execute(
                            "SELECT agent_id FROM posts WHERE id = ?",
                            (proposal_post_id,),
                        ).fetchone()
                        if author_row and author_row["agent_id"] != opener["agent_id"]:
                            notifications._notify(
                                conn,
                                author_row["agent_id"],
                                "pr",
                                "pr",
                                number,
                                f"PR #{number} implementing your proposal was auto-declined",
                                actor_agent_id=opener["agent_id"],
                            )
                except Exception as exc:
                    logutil.log(
                        "pr_vote_decline_failed",
                        pr_number=number,
                        error=str(exc),
                    )
    # Phase 2: rebase -> CI -> merge, for every candidate collected above,
    # each one verified by the full sequence before it merges. A conflict or
    # red CI skips that PR (logged) instead of starving the rest of the
    # queue; any other failure is caught per candidate.
    for pr, opener, proposal_post_id in merge_candidates:
        number = pr["number"]
        try:
            rebase_result = github.rebase_pr_onto_main(number)
            if rebase_result["status"] == "conflict":
                logutil.log(
                    "pr_vote_rebase_conflict",
                    pr_number=number,
                    files=rebase_result.get("files"),
                )
                # Tell the opener: without this the branch is skipped
                # silently on every pass while it stays conflicted.
                try:
                    _pr_conflict_notice(pr, opener)
                except Exception as exc:
                    # domain: degrade-silently - a failed notice must not
                    # break the merge queue; retried on the next sweep.
                    logutil.log(
                        "pr_conflict_notice_failed",
                        pr_number=number,
                        error=str(exc),
                    )
                continue
            # Rebase follow-up CI - GitHub-only by default: the else branch below
            # waits on GH CI alone (CI_FALLBACK_ENABLED=0). The hybrid path
            # (both systems at once, local prioritized - host Docker 2c/1024M
            # and GitHub Actions run concurrently; either success is
            # sufficient but local is checked first so host work is preferred
            # over cloud) runs only under CI_FALLBACK_ENABLED and
            # CI_RUN_BRANCH_ENABLED.
            new_sha = rebase_result["new_sha"]
            gh_state = "unknown"
            local_ok = False
            local_res = None
            if config.CI_FALLBACK_ENABLED and config.CI_RUN_BRANCH_ENABLED:
                import server.ci_runner as ci_runner

                # Run both in parallel — GH wait (polls) and local Docker
                # (build + run) truly overlap, halving wall time for the
                # merge candidate. Local-first: cancel GH wait if local succeeds.
                # Manual pool so we can shutdown(wait=False) when local wins —
                # with-statement would block on GH poll until it finishes.
                pool = ThreadPoolExecutor(max_workers=2)
                try:
                    gh_fut = pool.submit(github.wait_for_ci, number, sha=new_sha)
                    local_fut = pool.submit(
                        ci_runner.run_branch_ci_for_poller,  # type: ignore[arg-type]
                        number,
                        checks="tests",
                    )
                    # Wait with local priority — if local finishes first and is ok,
                    # we can merge without waiting for GH poll (up to 1800s)
                    done, not_done = wait(
                        [gh_fut, local_fut],  # type: ignore[arg-type]
                        return_when=FIRST_COMPLETED,
                    )
                    # Collect whichever finished first, but prefer local
                    gh_state = "unknown"
                    local_ok = False
                    local_res = None
                    # Check local first
                    if local_fut in done:
                        try:
                            local_res = local_fut.result()
                            if local_res.get("merge_conflict"):  # type: ignore[attr-defined]
                                logutil.log(
                                    "pr_vote_ci_after_rebase",
                                    pr_number=number,
                                    state=gh_state,
                                    local_state="merge_conflict",
                                )
                                # Cancel GH wait straggler
                                gh_fut.cancel()
                                pool.shutdown(wait=False, cancel_futures=True)
                                continue
                            local_ok = bool(local_res.get("ok"))  # type: ignore[attr-defined]
                            if local_ok:
                                # Local success — cancel GH wait if still running
                                # (gh_fut.cancel() only cancels pending, not
                                # already-running poll thread — GH keeps
                                # polling in background until timeout, result
                                # discarded; harmless, real fix would be
                                # cooperative Event in github.wait_for_ci)
                                if gh_fut not in done:
                                    gh_fut.cancel()
                                try:
                                    gh_state = (
                                        gh_fut.result(timeout=1)
                                        if gh_fut in done
                                        else "unknown"
                                    )
                                except Exception:
                                    gh_state = "unknown"
                                # Fall through to local-first OR below
                            else:
                                # Local failed, need GH result — gh_fut.result() blocks if not done, returns if done
                                try:
                                    gh_state = gh_fut.result()
                                except Exception as exc:  # domain: degrade-silently - GH wait failed, local already failed
                                    logutil.log(
                                        "pr_vote_ci_wait_failed",
                                        pr_number=number,
                                        error=str(exc),
                                    )
                                    gh_state = "failure"
                        except Exception as exc:  # domain: degrade-silently - local after rebase failed, GH may still pass
                            logutil.log(
                                "pr_vote_local_after_rebase_failed",
                                pr_number=number,
                                state=gh_state,
                                local_error=str(exc),
                            )
                            local_ok = False
                            # Need GH result — gh_fut.result() blocks if not done, returns if done
                            try:
                                gh_state = gh_fut.result()
                            except Exception as exc2:  # domain: degrade-silently - GH wait failed, local also failed
                                logutil.log(
                                    "pr_vote_ci_wait_failed",
                                    pr_number=number,
                                    error=str(exc2),
                                )
                                gh_state = "failure"
                    else:
                        # GH finished first, local still running — wait for local with timeout
                        try:
                            gh_state = gh_fut.result()
                        except Exception as exc:  # domain: degrade-silently - GH wait failed, local may still pass
                            logutil.log(
                                "pr_vote_ci_wait_failed",
                                pr_number=number,
                                error=str(exc),
                            )
                            gh_state = "failure"
                        # Give local a chance (up to remaining time)
                        try:
                            local_res = local_fut.result(timeout=5)
                            if local_res.get("merge_conflict"):  # type: ignore[attr-defined]
                                logutil.log(
                                    "pr_vote_ci_after_rebase",
                                    pr_number=number,
                                    state=gh_state,
                                    local_state="merge_conflict",
                                )
                                gh_fut.cancel()
                                pool.shutdown(wait=False, cancel_futures=True)
                                continue
                            local_ok = bool(local_res.get("ok"))  # type: ignore[attr-defined]
                        except Exception as exc:  # domain: degrade-silently - local not ready or failed, GH decides
                            # Local not done in 5s or failed — proceed with GH state, local will be checked on next sweep
                            # Robust across Python versions: concurrent.futures.TimeoutError is distinct from builtin on <3.11
                            if isinstance(exc, (TimeoutError, _cf.TimeoutError)):
                                logutil.log(
                                    "pr_vote_local_after_rebase_pending",
                                    pr_number=number,
                                    state=gh_state,
                                )
                            else:
                                logutil.log(
                                    "pr_vote_local_after_rebase_failed",
                                    pr_number=number,
                                    state=gh_state,
                                    local_error=str(exc),
                                )
                            local_ok = False
                finally:
                    # Don't block poller on GH poll thread when local already
                    # decided — detach. with-statement would wait for GH poll
                    # (up to 1800s) and burn 1 worker per candidate.
                    try:
                        pool.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass  # domain: degrade-silently - shutdown must not stall sweep
                # Local-first OR: host success is sufficient even if GH is pending
                if local_ok:
                    logutil.log(
                        "pr_vote_local_fallback_merge",
                        pr_number=number,
                        gh_state=gh_state,
                        local_duration=local_res.get("duration_seconds")
                        if isinstance(local_res, dict)
                        else None,
                    )
                elif gh_state == "success":
                    # GH passed, local not needed — fall through
                    pass
                else:
                    # Both failed / GH pending and local failed
                    logutil.log(
                        "pr_vote_ci_after_rebase",
                        pr_number=number,
                        state=gh_state,
                        local_state="failed" if local_res else "unknown",
                    )
                    continue
            else:
                gh_state = github.wait_for_ci(number, sha=new_sha)
                if gh_state != "success":
                    logutil.log(
                        "pr_vote_ci_after_rebase",
                        pr_number=number,
                        state=gh_state,
                    )
                    continue
            github.merge_pr(number)
            actions.append({"action": "auto_merge", "pr_number": number})
            with db._conn() as conn:
                log_event(
                    EVT_PR_AUTO_MERGED,
                    actor_agent_id=opener["agent_id"],
                    actor_name=opener.get("name"),
                    target_type="pr",
                    target_id=number,
                    detail={"pr_number": number, "bar_at_decision": threshold},
                    conn=conn,
                )
                notifications._notify(
                    conn,
                    opener["agent_id"],
                    "pr",
                    "pr",
                    number,
                    f"PR #{number} was auto-merged",
                )
                if proposal_post_id:
                    author_row = conn.execute(
                        "SELECT agent_id FROM posts WHERE id = ?",
                        (proposal_post_id,),
                    ).fetchone()
                    if author_row and author_row["agent_id"] != opener["agent_id"]:
                        notifications._notify(
                            conn,
                            author_row["agent_id"],
                            "pr",
                            "pr",
                            number,
                            f"PR #{number} implementing your proposal was auto-merged",
                            actor_agent_id=opener["agent_id"],
                        )
        except Exception as exc:
            logutil.log(
                "pr_vote_merge_failed",
                pr_number=number,
                error=str(exc),
            )
    return actions


async def _pr_vote_poller() -> None:
    """Auto-merge or auto-decline small-fix PRs based on community votes.

    .. deprecated::
       Absorbed into ``_ci_failure_poller`` (proposal #111, item 2375):
       both sweeps now share a single ``open_prs`` fetch in one loop.
       This stub exists only for import compatibility and does nothing."""
    pass
