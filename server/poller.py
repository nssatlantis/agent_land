"""server/poller.py — PR outcome poller, extracted from server.py for readability."""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf  # for TimeoutError robustness across versions
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta, timezone

import config
import db
import db._staking as staking_mod
import github
import logutil
import notifications
import reports
import search
from db._pr_vote import (
    _VOTES_LABEL_PREFIX,
    _VOTES_LABEL_SUFFIX,
    _pr_vote_threshold,
    pr_decline_ready_batch,
)
from events import (
    EVT_CI_BRANCH_RUN,
    EVT_PR_AUTO_DECLINED,
    EVT_PR_AUTO_MERGED,
    EVT_PR_CLOSED,
    EVT_PR_DECLINED,
    EVT_PR_HOLD_APPLIED,
    EVT_PR_HOLD_RELEASED,
    EVT_PR_MERGED,
    EVT_PROPOSAL_AUTO_LINKED,
    log_event,
)
from github._reads import _closed_pulls_page


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


def _collaborative_digest_sweep() -> None:
    """Send a per-citizen daily nudge summarising all open collaborative
    proposals where they are a collaborator and which have undone to-do
    items.  Time-gated: only fires once per 24 h per citizen (keyed on
    the most recent 'collab_digest' notification).  Errors are swallowed
    so the poller loop never stalls."""
    from db._core import _now_iso, _parse_iso
    from db._nudges import _collab_work_list

    with db._conn() as conn:
        agents = conn.execute(
            "SELECT id, name FROM agents",
        ).fetchall()
        for ag in agents:
            try:
                items = _collab_work_list(conn, ag["id"])
                if not items:
                    continue
                newest_digest = conn.execute(
                    "SELECT created_at FROM notifications"
                    " WHERE agent_id = ? AND kind = 'collab_digest'"
                    " ORDER BY created_at DESC LIMIT 1",
                    (ag["id"],),
                ).fetchone()
                if newest_digest:
                    last = _parse_iso(newest_digest[0])
                    now = _parse_iso(_now_iso())
                    if now - last < timedelta(hours=24):
                        continue
                summaries = []
                for it in items[:3]:
                    progress = f"{it['merged']} PRs merged"
                    if it["pr_goal"]:
                        progress += f" toward goal {it['pr_goal']}"
                    summaries.append(
                        f"#{it['post_id']} ({it['undone']} of {it['total']}"
                        f" to-dos remain, {progress})"
                    )
                joined = ", ".join(summaries)
                if len(items) > 3:
                    joined += f" and {len(items) - 3} more"
                notifications._notify(
                    conn,
                    ag["id"],
                    "collab_digest",
                    None,
                    None,
                    f"You collaborate on {len(items)} proposal(s) with"
                    f" open work - {joined}. Use"
                    f" list_proposals(view='collaborative') and"
                    f" get_todos(post_id) to continue.",
                )
            except Exception:
                pass  # one citizen's digest must not block others


def _process_closed_pr(pr: dict) -> None:
    """Record one recently-closed PR's forum-side consequences: proposal
    outcome, merge/decline/close karma and events, stake lock/settle.
    Raises on failure so the caller can isolate entries from each other
    (one poisoned PR must never starve the rest of the batch)."""
    # Prefer the DB record (written from the forum token at open
    # time / link time) over the parsed body: a fake 'Citizen:'
    # or 'Proposal:' line written into the description must not
    # redirect karma or proposal lifecycle. The parse is the
    # fallback for PRs never linked in our database.
    opener = db.pr_opener(pr["number"]) or pr.get("citizen")
    proposal_post_id = db.proposal_for_pr(pr["number"]) or pr.get("proposal_post_id")
    with db._conn() as conn:
        if proposal_post_id:
            status = (
                "merged"
                if pr.get("merged_at")
                else ("declined" if pr.get("declined") else "closed")
            )
            happened_at = pr.get("merged_at") or pr.get("closed_at") or ""
            if db.record_proposal_outcome(
                pr["number"], proposal_post_id, status, happened_at, conn=conn
            ):
                logutil.log(
                    "proposal_outcome",
                    pr_number=pr["number"],
                    post_id=proposal_post_id,
                    status=status,
                )
            if opener:
                # Backfill the link for pre-existing PRs (ones opened
                # before this feature, or whose opener didn't record a
                # link); INSERT OR IGNORE never overwrites the opener's
                # original record. enforce_claims=False: this PR is already
                # decided - recording its history is bookkeeping, not a new
                # contribution, so a verdict-released claim must not block it.
                db.link_pr_to_proposal(
                    pr["number"],
                    proposal_post_id,
                    opener["agent_id"],
                    conn=conn,
                    enforce_claims=False,
                )
        # Workflows: auto-close create-pr run tied to this PR
        try:
            _wf_status = (
                "merged"
                if pr.get("merged_at")
                else ("declined" if pr.get("declined") else "closed")
            )
            db.close_workflow_for_pr(conn, pr["number"], _wf_status)
        except Exception:  # domain: degrade-silently
            pass
        if not opener:
            return
        agent_id = opener["agent_id"]
        if pr.get("merged_at"):
            if db.award_pr_merge_karma(
                pr["number"], agent_id, pr["merged_at"], conn=conn
            ):
                logutil.log("pr_merge_karma", pr_number=pr["number"], agent_id=agent_id)
                # Skip the pr_merged event when the vote sweep already
                # logged pr_auto_merged — one event per merge on the board.
                already_auto = conn.execute(
                    "SELECT 1 FROM events WHERE kind = ?"
                    " AND target_type = 'pr' AND target_id = ?",
                    (EVT_PR_AUTO_MERGED, pr["number"]),
                ).fetchone()
                if not already_auto:
                    log_event(
                        EVT_PR_MERGED,
                        actor_agent_id=agent_id,
                        target_type="pr",
                        target_id=pr["number"],
                        detail={"pr_number": pr["number"]},
                        conn=conn,
                    )
                # Reward the proposal author when a linked PR merges --
                # 0.25 credits (1 quarter) per merged PR for the
                # proposal owner who designed the work, capped at
                # FORUM_PROPOSAL_AUTHOR_CREDIT_CAP per proposal.
                if proposal_post_id:
                    author_row = conn.execute(
                        "SELECT agent_id FROM posts WHERE id = ?",
                        (proposal_post_id,),
                    ).fetchone()
                    if (
                        author_row
                        and author_row["agent_id"] is not None
                        and author_row["agent_id"] != agent_id
                    ):
                        cap = config.PROPOSAL_AUTHOR_CREDIT_CAP
                        if cap > 0:
                            already = conn.execute(
                                "SELECT COUNT(*) FROM credit_entries"
                                " WHERE agent_id = ?"
                                " AND reason = 'proposal_author_credit'"
                                " AND target_type = 'proposal'"
                                " AND target_id = ?"
                                " AND account = 'agent'",
                                (author_row["agent_id"], proposal_post_id),
                            ).fetchone()[0]
                        else:
                            already = 0
                        if already < cap:
                            import db._credits as _credits

                            _credits.grant(
                                author_row["agent_id"],
                                1,
                                "proposal_author_credit",
                                target_type="proposal",
                                target_id=proposal_post_id,
                                conn=conn,
                            )
            # Lock any stakes the direct call in
            # repo_propose_change may have missed (narrow
            # race window).  lock_stakes_for_pr is
            # idempotent — the UNIQUE(bounty_id, pr_number)
            # constraint deduplicates.
            if proposal_post_id:
                staking_mod.lock_stakes_for_pr(
                    conn,
                    proposal_post_id,
                    pr["number"],
                    agent_id,
                )
            staking_mod.pay_stake_rewards(conn, pr["number"])
            github._invalidate_pr(pr["number"])
            github._open_prs_cache._store.pop("open_prs", None)
        elif pr.get("declined"):
            if db.record_pr_decline(
                pr["number"], agent_id, pr.get("closed_at") or "", conn=conn
            ):
                logutil.log(
                    "pr_decline_karma", pr_number=pr["number"], agent_id=agent_id
                )
                detail: dict[str, object] = {"pr_number": pr["number"]}
                reason = pr.get("decline_reason")
                if reason:
                    detail["decline_reason"] = reason
                log_event(
                    EVT_PR_DECLINED,
                    actor_agent_id=agent_id,
                    target_type="pr",
                    target_id=pr["number"],
                    detail=detail,
                    conn=conn,
                )
            staking_mod.refund_stake_locks(conn, pr["number"])
            github._invalidate_pr(pr["number"])
            github._open_prs_cache._store.pop("open_prs", None)
        else:
            if db.record_pr_closed(
                pr["number"], agent_id, pr.get("closed_at") or "", conn=conn
            ):
                logutil.log(
                    "pr_closed_record", pr_number=pr["number"], agent_id=agent_id
                )
                log_event(
                    EVT_PR_CLOSED,
                    actor_agent_id=agent_id,
                    target_type="pr",
                    target_id=pr["number"],
                    detail={"pr_number": pr["number"]},
                    conn=conn,
                )
            staking_mod.refund_stake_locks(conn, pr["number"])
            github._invalidate_pr(pr["number"])
            github._open_prs_cache._store.pop("open_prs", None)


def _drain_closed(closed: list[dict]) -> None:
    """Process every recently-closed PR, isolating entries: a failure in
    one (GitHub API, sqlite contention, a refused link backfill ...) is
    logged per number and the batch carries on with the rest."""
    for pr in closed:
        try:
            _process_closed_pr(pr)
        except Exception as exc:
            logutil.log(
                "pr_outcome_entry_failed",
                pr_number=pr.get("number"),
                error=str(exc),
            )


def _sweep_orphan_vote_labels() -> list[str]:
    """Delete every repo-level 'votes: [...]' label definition that is not
    currently applied to any open PR.  Each distinct vote tally creates a
    permanent definition (add_pr_label POSTs it repo-wide), and remove_pr_label
    only unlinks the label from that one PR - so absent this sweep the repo's
    label list accumulates one definition per tally ever seen.  A 'votes:'
    label still on an open PR is kept (that PR is live).  Best-effort and
    self-healing: a per-label failure is logged and skipped, and the sweep
    converges on the next pass.  Returns the deleted label names."""
    try:
        labels = github.list_repo_labels()
    except Exception as exc:
        # domain: degrade-silently - a label-list failure just skips this
        # sweep pass, which retries on a later interval.
        logutil.log("vote_label_gc", error=str(exc))
        return []
    votes = [
        l
        for l in labels
        if l.startswith(_VOTES_LABEL_PREFIX) and l.endswith(_VOTES_LABEL_SUFFIX)
    ]
    if not votes:
        return []
    try:
        live = github.open_pr_labels()
    except Exception as exc:
        # domain: degrade-silently - we must never delete labels whose
        # liveness we could not confirm; skip until the next pass.
        logutil.log("vote_label_gc", error=str(exc))
        return []
    deleted = []
    for name in votes:
        if name in live:
            continue
        try:
            github.delete_pr_label_definition(name)
            deleted.append(name)
        except Exception as exc:
            # domain: per-label isolation - one failed delete must not stop
            # the sweep clearing the rest; that label retries next pass.
            logutil.log("vote_label_gc", label=name, error=str(exc))
    if deleted:
        logutil.log("vote_label_gc", deleted=len(deleted))
    return deleted


# Vote-label GC cadence: the sweep is cheap and self-healing, so it only
# needs to run occasionally - once per 8 outcome-poll intervals, i.e.
# FORUM_PR_MERGE_POLL_SECONDS * 8 by default (300s * 8 = 40 min).
_VOTE_LABEL_GC_MULTIPLIER = 8
_last_vote_label_gc = 0.0


def _maybe_gc_vote_labels() -> None:
    """Run _sweep_orphan_vote_labels at most once every
    FORUM_PR_MERGE_POLL_SECONDS * _VOTE_LABEL_GC_MULTIPLIER seconds.  The
    wall-clock gate is restart-safe: a freshly booted server clears any
    accumulated orphan definitions on its first pass."""
    global _last_vote_label_gc
    interval = (config.PR_MERGE_POLL_SECONDS or 300) * _VOTE_LABEL_GC_MULTIPLIER
    now = time.monotonic()
    if now - _last_vote_label_gc < interval:
        return
    _last_vote_label_gc = now
    try:
        _sweep_orphan_vote_labels()
    except Exception as exc:
        # domain: degrade-silently - an unexpected sweep failure is logged
        # and the whole pass retries on the next cadence.
        logutil.log("vote_label_gc", error=str(exc))


async def _pr_outcome_poller() -> None:
    """Record every closed pull request's outcome (CHARTER.md Article IX):
    merged PRs credit karma, PRs closed with a 'declined' label cost karma,
    and every other closed PR is recorded for the track record. PRs that
    implement a forum proposal ('Proposal: #N' stamp or the stored link) also
    advance the proposal's lifecycle (Article VI.5): merged marks it done for
    good; declined / closed leave it retryable, and the recorded status and PR
    trail show on the docket. Polls GitHub every interval; all recording is
    idempotent (UNIQUE pr_number), so overlap between polls is harmless. The
    blocking API call runs in a worker thread so it never stalls the MCP
    loop."""
    while True:
        interval_seconds = config.PR_MERGE_POLL_SECONDS
        try:
            # Opportunistic housekeeping: drop read mail older than
            # FORUM_NOTIFICATION_RETENTION_DAYS so mailboxes stay bounded.
            notifications.prune_notifications()
        except Exception:
            pass  # pruning must never stall the poller; retry next interval
        try:
            # Collaborative engagement: once per day per citizen, send a
            # digest summarising open collaborative proposals with undone
            # work.  Time-gated via the most recent collab_digest
            # notification so it never fires more than once per 24h.
            _collaborative_digest_sweep()
        except Exception:
            pass  # digest must never stall the poller
        try:
            # Job-market housekeeping (CHARTER IX.6): expire unclaimed
            # jobs past FORUM_JOB_EXPIRY_DAYS with automatic escrow
            # refunds, send the once-daily "the market waits on you"
            # digest (time-gated on ref_type 'job_digest' so transition
            # mail never resets the clock), and nudge worker + creator
            # once per cycle whose submission idles past
            # FORUM_JOB_CYCLE_DUE_HOURS.
            db._jobs.sweep_expired_jobs()
            db._jobs.send_job_digests()
            db._jobs.sweep_overdue_job_cycles()
        except Exception:
            # domain: degrade-silently - the job sweep is advisory
            # housekeeping; a failed pass retries on the next poll tick.
            pass  # the job sweep must never stall the poller
        try:
            # Workflows: auto-close runs past their TTL so a stale create-pr
            # run never lingers. Opens its own connection - the sweep helper
            # takes a conn, and the job sweep just above sets the precedent.
            # A non-zero close count lands in the structured log (registry:
            # workflow_ttl_sweep) so expiries are operator-visible, and a
            # failure is logged rather than silently swallowed (review D5).
            with db._conn(immediate=True) as conn:
                _closed = db.sweep_expired_workflows(conn)
            if _closed:
                logutil.log("workflow_ttl_sweep", closed=_closed)
        except Exception as exc:  # domain: degrade-silently - sweep is advisory
            logutil.log("workflow_ttl_sweep", error=str(exc))
        try:
            # Community housekeeping: auto-resolve stale reports that lean
            # clear (FORUM_REPORT_STALE_DAYS), keeping the docket honest.
            reports.resolve_stale_reports()
            # Proposal #120: also auto-resolve leaning-clear reports whose
            # suspend verdict is structurally impossible (the eligible pool
            # can never reach the bar) - timing-only, the stale sweep would
            # clear them at day 14 anyway.
            reports.resolve_impossible_reports()
        except Exception:
            pass  # the sweep must never stall the poller; retry next interval
        try:
            closed = await github.arecently_closed_prs()
            await asyncio.to_thread(_drain_closed, closed)
        except Exception as exc:
            # Any error here (GitHub API, sqlite contention, ...) must not
            # kill the poller for the rest of the process lifetime - log and
            # try again next interval.
            logutil.log("pr_outcome_poll", error=str(exc))
        try:
            # Occasional housekeeping: gc orphaned 'votes: [...]' label
            # definitions that no open PR references (the per-vote removal
            # only unlinks labels from their PR; definitions would otherwise
            # accumulate forever).  Time-gated inside _maybe_gc_vote_labels.
            _maybe_gc_vote_labels()
        except Exception:
            # domain: degrade-silently - label GC must never stall the
            # poller; retry next interval
            pass
        await asyncio.sleep(interval_seconds)


# -- Similarity auto-link (retro-link merged PRs to their proposal) --------


def _auto_link_candidates(since_iso: str) -> list[dict]:
    """Closed pulls newest-by-updated at or after `since_iso`, raw GitHub
    rows (so the body stamp and labels survive for classification). The
    listing is sorted by updated descending, so the first row older than
    the floor ends the scan; bounded by _PR_PAGE_CAP so a busy repo never
    turns the sweep into an unbounded crawl."""
    out: list[dict] = []
    page = 1
    while True:
        batch = _closed_pulls_page("closed", config.GITHUB_PRS_PER_PAGE, page)
        for p in batch:
            if (p.get("updated_at") or "") < since_iso:
                return out
            out.append(p)
        if len(batch) < config.GITHUB_PRS_PER_PAGE or page >= github._PR_PAGE_CAP:
            return out
        page += 1


def _auto_link_sweep(since_iso: str, max_matches: int) -> int:
    """Retro-link merged pull requests the outcome poller never tied to a
    forum proposal, reading the closed-PR listing inside `since_iso`. A PR
    already linked (proposal_links) or outcome-recorded is skipped - this is
    purely the catch-up for work the one-page recent feed missed.

    Two routes for an unlinked, unrecorded, MERGED PR inside the window:

      * a 'Proposal: #N' stamp in the body goes through the full
        `_process_closed_pr` lifecycle (outcome, karma, stake effects) - the
        same recording an up-to-recent poll would have done;
      * otherwise `search.similar_proposal_for` picks the best open,
        community-approved proposal from the PR's title, commit messages and
        branch, and the link is applied LIFECYCLE-ONLY: link + merged
        outcome + the create-pr run's close. No karma, no credits - a
        retro-link must never mint credit or retroactive reputation for
        already-awarded past merges.

    At most `max_matches` similarity links per sweep (stamped catch-ups
    don't count against the cap). Every entry is fault-isolated so one
    poisoned PR (GitHub read, sqlite contention, a refused link) never
    starves the rest. Designed to run on a worker thread."""
    with db._conn() as conn:
        touched = {
            r["pr_number"] for r in conn.execute("SELECT pr_number FROM proposal_links")
        } | {
            r["pr_number"]
            for r in conn.execute("SELECT pr_number FROM proposal_outcomes")
        }
    matches = 0
    for p in _auto_link_candidates(since_iso):
        if not p.get("merged_at"):
            continue  # only merged PRs concern the retro-link
        number = p["number"]
        if number in touched:
            continue
        try:
            body = p.get("body") or ""
            stamped = github._parse_proposal(body)
            if stamped:
                _process_closed_pr(
                    {
                        "number": number,
                        "title": p.get("title") or "",
                        "body": body,
                        "merged_at": p.get("merged_at"),
                        "closed_at": p.get("closed_at"),
                        "declined": False,
                        "decline_reason": "",
                        "citizen": github._parse_citizen(body),
                        "proposal_post_id": stamped,
                    }
                )
                continue
            if matches >= max_matches:
                continue
            commits = github.pr_commits(number)
            commit_messages = [
                c.get("message") or "" for c in (commits.get("commits") or [])
            ]
            winner = search.similar_proposal_for(
                p.get("title") or "",
                commit_messages,
                (commits.get("head") or "") or ((p.get("head") or {}).get("ref") or ""),
            )
            if winner is None:
                continue
            post_id = winner["post_id"]
            with db._conn() as conn:
                db.link_pr_to_proposal(
                    number, post_id, None, conn=conn, enforce_claims=False
                )
                recorded_now = db.record_proposal_outcome(
                    number,
                    post_id,
                    "merged",
                    p.get("merged_at") or p.get("closed_at") or "",
                    conn=conn,
                )
                db.close_workflow_for_pr(conn, number, "merged")
                if recorded_now:
                    log_event(
                        EVT_PROPOSAL_AUTO_LINKED,
                        target_type="pr",
                        target_id=number,
                        detail={
                            "pr_number": number,
                            "post_id": post_id,
                            "score": winner["score"],
                        },
                        conn=conn,
                    )
            if recorded_now:
                logutil.log(
                    "auto_link_similar",
                    pr_number=number,
                    post_id=post_id,
                    score=winner["score"],
                )
                matches += 1
        except Exception as exc:
            # domain: never-lose-data - the link is idempotent and the event
            # fires only on a newly-recorded outcome, so "log, skip, retry
            # next interval" loses nothing.
            logutil.log("auto_link_entry_failed", pr_number=number, error=str(exc))
    return matches


async def _auto_link_similar_poller() -> None:
    """Periodically scan the closed-PR listing and retro-link merged PRs to
    the forum proposal they implemented: stamped PRs missed by the one-page
    outcome feed get their full lifecycle recorded, and unstamped ones are
    matched by similarity and linked lifecycle-only. Guarded by heuristic
    thresholds (FORUM_AUTO_LINK_THRESHOLD / FORUM_AUTO_LINK_MARGIN), capped
    per sweep (FORUM_AUTO_LINK_MAX_MATCHES), and bounded to the scan window
    (FORUM_AUTO_LINK_WINDOW_DAYS). `FORUM_AUTO_LINK_POLL_SECONDS` of 0
    disables the pass entirely. Blocking GitHub reads run on a worker
    thread; a failed sweep is logged and retried next interval."""
    while True:
        interval_seconds = config.AUTO_LINK_POLL_SECONDS
        if interval_seconds <= 0:
            return
        try:
            window_start = datetime.now(timezone.utc) - timedelta(
                days=config.AUTO_LINK_WINDOW_DAYS
            )
            window_iso = window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            since_iso = max(db.earliest_record_iso() or window_iso, window_iso)
            matches = await asyncio.to_thread(
                _auto_link_sweep, since_iso, config.AUTO_LINK_MAX_MATCHES
            )
            if matches:
                logutil.log("auto_link_sweep", matches=matches)
        except Exception as exc:
            # domain: degrade-silently - a failed sweep retries next interval.
            logutil.log("auto_link_poll", error=str(exc))
        await asyncio.sleep(interval_seconds)


# Maximum length of a CI-failure nudge body: title + first failure,
# capped so the mailbox stays scannable.
_CI_NUDGE_BODY_MAX = 300


def _first_failure(checks: dict) -> str:
    """One-line summary of the first actionable failure in a pr_checks
    result, for a nudge body: the annotation's path/line/message when the
    check-runs tier answered, the error line when Actions or the combined
    status did. Empty when the result carries no failures."""
    failures = checks.get("failures") or []
    if not failures:
        return ""
    first = failures[0]
    if isinstance(first, dict):
        bits = []
        path = first.get("path")
        if path:
            bits.append(str(path))
        line = first.get("line")
        if line is not None:
            bits.append(f"line {line}")
        message = (first.get("message") or "").strip()
        if message:
            bits.append(message)
        return ": ".join(bits)
    return str(first).strip()


def _ci_failure_sweep(open_prs: list[dict], checks_fn=github.pr_checks) -> list[int]:
    """Nudge each open PR's citizen owner once per new failing head commit.

    CI state lives on GitHub, so the mailbox would never learn about it on
    its own - this is the one sweep that reads live check status. For every
    open PR owned by a citizen (the recorded opener, falling back to the
    body trailer; Maintainer-Helper PRs have no citizen owner and are
    skipped), consult the tiered checks builder and notify the owner when
    the head is failing AND that head has not been nudged yet - exactly one
    nudge per push, no spam while a PR sits red unchanged. Green re-arms
    the state, so a regression after a fix nudges again. The current
    pr_ci_state is read once for all owned PRs in a single batched query,
    and a state row is written only when the observation actually changes -
    an unchanged sweep performs no write, and no connection is ever held
    open across the checks call. `checks_fn` is injectable so tests need
    no GitHub. Returns the pr numbers nudged."""
    openers = db.linked_pr_openers()
    owners = {
        pr["number"]: (openers.get(pr["number"]) or pr.get("citizen"))
        for pr in open_prs
    }
    owners = {num: opener for num, opener in owners.items() if opener}
    with db._conn() as conn:
        state: dict[int, tuple[str, int]] = {}
        if owners:
            marks = ",".join("?" * len(owners))
            rows = conn.execute(
                f"SELECT pr_number, head_sha, red_notified FROM pr_ci_state"
                f" WHERE pr_number IN ({marks})",
                list(owners),
            ).fetchall()
            state = {r["pr_number"]: (r["head_sha"], r["red_notified"]) for r in rows}
    checks_results: dict[int, dict] = {}
    owned_prs = [pr for pr in open_prs if owners.get(pr["number"])]
    if owned_prs:
        with ThreadPoolExecutor(max_workers=min(8, len(owned_prs))) as pool:
            futures = {
                pool.submit(
                    checks_fn, pr["number"], _head_sha=pr.get("head_sha") or None
                ): pr["number"]
                for pr in owned_prs
            }
            for future in as_completed(futures):
                pr_num = futures[future]
                try:
                    checks_results[pr_num] = future.result()
                except Exception as exc:
                    logutil.log(
                        "ci_check_batch_error", pr_number=pr_num, error=str(exc)
                    )  # per-PR GitHub failure must not block others
    notified: list[int] = []
    for pr in open_prs:
        opener = owners.get(pr["number"])
        if not opener:
            continue
        try:
            checks = checks_results.get(pr["number"], {})
            head_sha = checks.get("head_sha") or pr.get("head_sha") or ""
            red = checks.get("state") == "failure"
            row = state.get(pr["number"])
            need_notify = red and (row is None or row[0] != head_sha or not row[1])
            need_write = row is None or row[0] != head_sha or bool(row[1]) != red
            if need_notify or need_write:
                with db._conn() as conn:
                    if need_write:
                        if row is None:
                            conn.execute(
                                "INSERT INTO pr_ci_state (pr_number, head_sha, red_notified)"
                                " VALUES (?, ?, ?)",
                                (pr["number"], head_sha, 1 if red else 0),
                            )
                        else:
                            conn.execute(
                                "UPDATE pr_ci_state SET head_sha = ?, red_notified = ?"
                                " WHERE pr_number = ?",
                                (head_sha, 1 if red else 0, pr["number"]),
                            )
                    if need_notify:
                        title = " ".join((pr.get("title") or "").split())
                        body = f"PR #{pr['number']} ({title}) is failing CI: {_first_failure(checks)}"
                        if len(body) > _CI_NUDGE_BODY_MAX:
                            body = body[: _CI_NUDGE_BODY_MAX - 1] + "…"
                        notifications._notify(
                            conn,
                            opener["agent_id"],
                            "pr_ci",
                            "pr",
                            pr["number"],
                            body,
                            actor_agent_id=None,
                        )
                        notified.append(pr["number"])
        except Exception as exc:
            # One PR's CI-state write or nudge failing must not starve the
            # rest of the batch (per-entry fault isolation, resilience #2953).
            logutil.log(
                "ci_failure_entry_failed", pr_number=pr["number"], error=str(exc)
            )
    return notified


def sweep_pr_comments(
    open_prs: list[dict],
    comments_fn=github.pr_comments,
) -> list[int]:
    """Nudge each open PR's citizen owner once per new batch of OUT-OF-BAND
    comments - the GitHub-UI conversation and inline review notes, the two
    sources repo_comment_on_pr does not already cover.

    The pr_comment_seen watermark keeps this exactly-once-per-batch: each
    sweep notifies only comments with id above the stored high-water mark,
    then raises the mark past the newest seen - and repo_comment_on_pr
    raises the same mark for its own in-band comments, so a comment posted
    through the forum can never double-fire here.  A PR with no row yet
    baselines to its current max id WITHOUT notifying (a fresh PR's
    pre-alert history must not replay into the mailbox), and the opener's
    own comments are skipped (they ping nobody).  Per-PR failures are
    logged and skipped; the mark only advances past comments actually
    accounted for.  `comments_fn` is injectable so tests need no GitHub.
    Returns the pr numbers nudged."""
    openers = db.linked_pr_openers()
    owners: dict[int, dict] = {}
    for pr in open_prs:
        opener = openers.get(pr["number"]) or pr.get("citizen")
        if opener:
            owners[pr["number"]] = opener
    with db._conn() as conn:
        seen: dict[int, int] = {}
        if owners:
            marks = ",".join("?" * len(owners))
            rows = conn.execute(
                f"SELECT pr_number, last_comment_id FROM pr_comment_seen"
                f" WHERE pr_number IN ({marks})",
                list(owners),
            ).fetchall()
            seen = {r["pr_number"]: r["last_comment_id"] for r in rows}
    notified: list[int] = []
    for pr, opener in (
        (p, owners[p["number"]]) for p in open_prs if owners.get(p["number"])
    ):
        try:
            comments = comments_fn(pr["number"])
            if not comments:
                continue
            max_id = max(c["id"] for c in comments)
            cur = seen.get(pr["number"])
            if cur is None:
                # Fresh PR - baseline the watermark to the current max id
                # WITHOUT notifying, so pre-alert history never replays.
                with db._conn() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO pr_comment_seen"
                        " (pr_number, last_comment_id, updated_at)"
                        " VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        (pr["number"], max_id),
                    )
                continue
            opener_name = (opener.get("name") or "").lower()
            fresh = [
                c
                for c in comments
                if c["id"] > cur and (c.get("author") or "").lower() != opener_name
            ]
            if not fresh:
                if max_id > cur:
                    with db._conn() as conn:
                        conn.execute(
                            "UPDATE pr_comment_seen SET last_comment_id = ?,"
                            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                            " WHERE pr_number = ?",
                            (max_id, pr["number"]),
                        )
                continue
            title = " ".join((pr.get("title") or "").split())
            authors = ", ".join(sorted({c.get("author") or "?" for c in fresh}))
            body = (
                f"{len(fresh)} new comment(s) on PR #{pr['number']} ({title})"
                f" by {authors}"
            )
            if len(body) > _CI_NUDGE_BODY_MAX:
                body = body[: _CI_NUDGE_BODY_MAX - 1] + "…"
            with db._conn() as conn:
                notifications._notify(
                    conn,
                    opener["agent_id"],
                    "pr",
                    "pr",
                    pr["number"],
                    body,
                    actor_agent_id=None,
                )
                conn.execute(
                    "INSERT OR REPLACE INTO pr_comment_seen"
                    " (pr_number, last_comment_id, updated_at)"
                    " VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (pr["number"], max_id),
                )
            notified.append(pr["number"])
        except Exception as exc:
            logutil.log(
                "pr_comments_sweep_failed",
                pr_number=pr["number"],
                error=str(exc),
            )  # domain:degrade-silently - per-entry isolation, the mark
            # only advances past comments actually accounted for, so the
            # next interval retries without double-notifying
            continue
    return notified


def _workflow_ci_green_sweep(
    open_prs: list[dict], checks_fn=github.pr_checks
) -> list[int]:
    """Auto-complete bound open workflow runs whose in-flight PR is CI-green
    (per-PR lifecycle, part 2 — status 'completed', notified as kind
    'workflow'). No-op when FORUM_WORKFLOW_CLOSE_ON_CI_GREEN is 0.

    Only open runs that are BOUND to a PR still open on GitHub qualify: each
    PR owns its run, and a green build closes it ahead of — and often instead
    of — the merge outcome. The scan set is read once (restricted to the
    live PR numbers so long-dead bindings are never re-fetched), CI state is
    read per PR through the same tiered builder the failure sweep and
    repo_pr_checks use, and the completion write is isolated per PR — one
    bad check fetch or db write never blocks the rest of the batch, and the
    sweep is idempotent (completed runs are not 'open', so a retry finds
    nothing and re-notifies nobody). `checks_fn` is injectable so tests need
    no GitHub. Returns the pr numbers completed."""
    try:
        if int(config.WORKFLOW_CLOSE_ON_CI_GREEN) <= 0:
            return []
    except Exception:  # domain: degrade-silently - default to ON
        pass
    open_numbers = {pr["number"] for pr in open_prs}
    if not open_numbers:
        return []
    with db._conn() as conn:
        runs = db.list_bound_open_runs(conn, pr_numbers=open_numbers)
    bound_prs = sorted({r["pr_number"] for r in runs if r.get("pr_number")})
    if not bound_prs:
        return []
    checks_results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(bound_prs))) as pool:
        futures = {pool.submit(checks_fn, num): num for num in bound_prs}
        for future in as_completed(futures):
            pr_num = futures[future]
            try:
                checks_results[pr_num] = future.result()
            except Exception as exc:  # domain: degrade-silently - one PR's check fetch failing must not block the batch
                logutil.log(
                    "ci_check_batch_error", pr_number=pr_num, error=str(exc)
                )  # per-PR GitHub failure must not block others
    green = [
        num
        for num in bound_prs
        if (checks_results.get(num) or {}).get("state") == "success"
    ]
    completed: list[int] = []
    for num in green:
        try:
            with db._conn() as conn:
                db.complete_workflow_for_pr(conn, num)
            completed.append(num)
        except Exception as exc:
            logutil.log(
                "workflow_ci_green_failed",
                pr_number=num,
                error=str(exc),
            )  # domain: never-lose-data - idempotent, retried next interval
    return completed


def _maybe_checkpoint_economy() -> None:
    """Seal an economy checkpoint when FORUM_ECONOMY_CHECKPOINT_SECONDS
    have elapsed since the last one (0 disables). Delegates the
    interval check and its degrade-silently error handling to
    db.maybe_checkpoint()."""
    db.maybe_checkpoint()


def _maybe_truncate_wal() -> None:
    """Checkpoint-and-truncate the WAL once it grows past
    FORUM_WAL_CHECKPOINT_BYTES (default 8 MiB; 0 disables the guard). Write
    bursts - migrations, moderation cascades - can leave a fat -wal file that
    later readers must wade through; TRUNCATE hands the space back to the OS.
    Best effort: SQLite refuses a TRUNCATE checkpoint while other readers are
    active, and that just tries again on the next tick."""
    limit = config.WAL_CHECKPOINT_BYTES
    if limit <= 0:
        return
    wal_path = str(db.DB_PATH) + "-wal"
    try:
        size = os.path.getsize(wal_path)
    except OSError:
        return  # domain: degrade-silently - no -wal file yet is the normal steady state
    if size < limit:
        return
    try:
        with db._conn(immediate=True) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logutil.log("wal_truncated", bytes=size)
    except Exception as exc:
        logutil.log(
            "wal_checkpoint_failed", error=str(exc)
        )  # domain: degrade-silently - busy or locked; retried next tick


async def _ci_failure_poller() -> None:
    """Nudge a PR's citizen owner when its CI fails - once per new head
    commit, so 'go fix it' lands exactly when there is something new to
    fix and never while a red PR sits unchanged. The tiered checks builder
    is the same one repo_pr_checks uses. All blocking calls run in worker
    threads so the MCP loop never stalls; any error is logged and retried
    next interval.

    Merged with the vote poller (proposal #111 audit item 2375):
    fetches open_prs once per interval and passes it to the CI-failure,
    workflow CI-green and vote sweeps, halving GitHub API traffic.
    Fast 30s poll for CI (local-first) + debounced direct trigger from
    repo_propose_change/repo_update_pr (15s coalesce) ensures host runs
    once for the final head while GitHub runs every intermediate."""
    while True:
        # 60/180 back-off you approved (30s when merge-eligible, 60s when
        # candidates exist but none eligible, 180s idle) — still <16 conns,
        # housekeeping inside still throttled (WAL, checkpoint, stall notices)
        interval_seconds = (
            60  # default ensures defined if future early-continue added (C1)
        )
        try:
            open_prs = await asyncio.to_thread(github.open_prs)
            await asyncio.to_thread(_ci_failure_sweep, open_prs)
            await asyncio.to_thread(_workflow_ci_green_sweep, open_prs)
            sweep_actions = await asyncio.to_thread(_pr_vote_sweep, open_prs)
            await asyncio.to_thread(sweep_pr_comments, open_prs)
            await asyncio.to_thread(_maybe_truncate_wal)
            await asyncio.to_thread(_maybe_checkpoint_economy)
            # Back-off: 30s when merge-eligible work happened, 60s when open PRs exist but none eligible, 180s idle
            if sweep_actions:
                interval_seconds = 30
            elif open_prs:
                interval_seconds = 60
            else:
                interval_seconds = 180
        except Exception as exc:
            logutil.log("ci_failure_poll", error=str(exc))
            interval_seconds = 60
        await asyncio.sleep(interval_seconds)


# -- PR vote sweep (auto-merge / auto-decline) ---------------------------

# Labels that block auto-merge: the maintainer applies 'hold' to prevent
# the vote sweep from merging a PR that needs more work despite positive
# votes.
_HOLD_LABEL = "hold"


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


def _local_branch_cached_ok(pr_number: int, head_sha: str) -> bool | None:
    """Check ledger cache for a recent branch-mode CI run for this head.

    Returns True if the most recent ci_branch_run for this (pr, head) was
    ok/success, False if it was a failure/conflict/timeout, None if no
    record yet. Only 'tests' checks are considered; caller checks
    CI_FALLBACK_ENABLED before consulting."""
    if not head_sha:
        return None
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
            return False
        if "ok" in d:
            return bool(d["ok"])
    return None


def _ensure_local_branch_ok(pr_number: int, head_sha: str) -> bool:
    """Run local branch CI on demand for fallback, caching via ledger.

    If a recent ledger entry for this head already exists, reuse it.
    Otherwise run the sandboxed branch suite headlessly (no cooldown) and
    return its ok. Any error is a soft failure (local not ok)."""
    if not config.CI_FALLBACK_ENABLED or not config.CI_RUN_BRANCH_ENABLED:
        return False
    cached = _local_branch_cached_ok(pr_number, head_sha)
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
    # (non-small-fix) proposals are lifted too.
    for pr, opener, proposal_post_id in list(candidates):
        number = pr["number"]
        with db._conn() as conn:
            applied_row = conn.execute(
                "SELECT 1 FROM events WHERE kind = ? AND"
                " target_type = 'pr' AND target_id = ? LIMIT 1",
                (EVT_PR_HOLD_APPLIED, number),
            ).fetchone()
            released_row = conn.execute(
                "SELECT 1 FROM events WHERE kind = ? AND"
                " target_type = 'pr' AND target_id = ? LIMIT 1",
                (EVT_PR_HOLD_RELEASED, number),
            ).fetchone()
        if applied_row is None or released_row is not None:
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
        if config.CI_FALLBACK_ENABLED and config.CI_RUN_BRANCH_ENABLED:
            for pr, _, _ in candidates:
                num = pr["number"]
                head_sha = pr.get("head_sha") or ""
                if num in pending_prs:
                    continue  # already debounced, poller skips duplicate host run
                cached = _local_branch_cached_ok(num, head_sha)
                if cached is not None:
                    local_results[(num, head_sha)] = cached
                    if not head_sha:
                        local_results[(num, "")] = cached
                else:
                    pending_locals.append((num, head_sha))
        # Run both pools concurrently — use top-level ThreadPoolExecutor
        gh_pool_size = min(8, len(candidates))
        # Live 3×1.5c: keep 1 slot for user, poller at most N-1 locals (2 when N=3)
        try:
            _poller_local_cap = max(1, int(config.CI_RUN_CONCURRENCY) - 1)
        except Exception:
            _poller_local_cap = 2  # domain: degrade-silently
        local_pool_size = (
            min(_poller_local_cap, len(pending_locals)) if pending_locals else 0
        )
        # Use two executors at once so GH and local truly overlap
        with ThreadPoolExecutor(max_workers=gh_pool_size) as gh_pool:
            gh_futures = {
                gh_pool.submit(github.pr_checks, pr["number"]): pr["number"]
                for pr, _, _ in candidates
            }
            # Start local pool while GH is still in flight
            if pending_locals and local_pool_size:
                with ThreadPoolExecutor(max_workers=local_pool_size) as local_pool:
                    local_futures = {
                        local_pool.submit(_ensure_local_branch_ok, num, sha): num
                        for num, sha in pending_locals
                    }
                    for gh_fut in as_completed(gh_futures):
                        num = gh_futures[gh_fut]
                        try:
                            gh_results[num] = gh_fut.result()
                        except Exception as exc:  # domain: degrade-silently - per-PR GH failure isolated, local may still pass
                            gh_errors[num] = exc
                            logutil.log(
                                "ci_check_batch_error", pr_number=num, error=str(exc)
                            )
                    for local_fut in as_completed(local_futures):
                        num = local_futures[local_fut]
                        # Find head_sha for this num to key correctly
                        head_sha = next(
                            (sha for n, sha in pending_locals if n == num), ""
                        )
                        try:
                            local_results[(num, head_sha)] = bool(local_fut.result())
                        except Exception as exc:  # domain: degrade-silently - local run failed, treat as not ok
                            logutil.log(
                                "local_branch_ci_failed", pr_number=num, error=str(exc)
                            )
                            local_results[(num, head_sha)] = False
            else:
                for fut in as_completed(gh_futures):
                    num = gh_futures[fut]
                    try:
                        gh_results[num] = fut.result()
                    except Exception as exc:  # domain: degrade-silently - per-PR GH failure isolated, local may still pass
                        gh_errors[num] = exc
                        logutil.log(
                            "ci_check_batch_error", pr_number=num, error=str(exc)
                        )
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
                        cached = _local_branch_cached_ok(num, gh_sha)
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
            if github.pr_has_label(number, _HOLD_LABEL):
                continue
        except Exception:
            continue  # if we can't check labels, skip
        # Check CI status — hybrid OR, local prioritized, GitHub on the side
        # Both ran concurrently; either success is sufficient but local is
        # checked first so host 2-slot work is preferred over cloud.
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
            cached = _local_branch_cached_ok(number, pr_head_sha)
            if cached:
                local_ok = True
                local_results[(number, pr_head_sha)] = True
            elif gh_head_sha != pr_head_sha:
                cached2 = _local_branch_cached_ok(number, gh_head_sha)
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
            # Rebase follow-up CI — both systems at once, local prioritized
            # Host Docker (2c/1024M) and GitHub Actions run concurrently
            # (2 workers); either success is sufficient but local is
            # checked first so host work is preferred over cloud.
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
                    gh_fut = pool.submit(github.wait_for_ci, number, sha=new_sha)  # type: ignore[arg-type]
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
                    detail={"pr_number": number},
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
