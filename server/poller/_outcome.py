"""server.poller._outcome — outcome ticker, closed-PR lifecycle, maintenance sweeps."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import config
import db
import db._staking as staking_mod
import github
import logutil
import notifications
import reports
from db._pr_vote import (
    _VOTES_LABEL_PREFIX,
    _VOTES_LABEL_SUFFIX,
)
from events import (
    EVT_PR_AUTO_MERGED,
    EVT_PR_CLOSED,
    EVT_PR_DECLINED,
    EVT_PR_MERGED,
    log_event,
)
from github._reads import (
    _apaginated_closed_pulls,
    _closed_row_from_raw,
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
    db_linked = db.proposal_for_pr(pr["number"])
    proposal_post_id = db_linked or pr.get("proposal_post_id")
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
            if opener and not db_linked:
                # Backfill the link for pre-existing PRs (ones opened
                # before this feature, or whose opener didn't record a
                # link); INSERT OR IGNORE never overwrites the opener's
                # original record. enforce_claims=False: this PR is already
                # decided - recording its history is bookkeeping, not a new
                # contribution, so a verdict-released claim must not block it.
                # Only ever runs when the DB genuinely lacks a link: the
                # closed-PR poller re-fetches the same page each cycle, and
                # an unconditional re-link used to drive bind_open_run into
                # re-minting a create-pr run per re-process. Linking commits,
                # so this converges after one pass.
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
            # Bug linkage: a merged PR against a proposal referencing #B bugs
            # tells each live bug's reporter a fix may have landed.
            if proposal_post_id:
                try:
                    db.notify_bug_fix_landed(conn, pr["number"], proposal_post_id)
                except Exception:
                    # domain: degrade-silently - notify best-effort only
                    pass
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
        try:
            # Branch-tree hygiene: a decided PR never needs its warm
            # registry tree again. Best-effort and self-healing (TTL/LRU
            # cover a missed eviction); must never break the drain.
            import server.ci_runner._trees as _br_trees

            _br_trees.evict_br_tree(int(pr.get("number") or 0))
        except Exception:  # domain: degrade-silently - eviction is hygiene
            pass


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

# The DB-persisted closed-PR cache (pr_rows) is the fast path for the closed
# listing and the revalidation seam's store, but a fresh database has nothing
# in it until the first PR below actually closes. Backfill it from the
# closed-pulls listing at most once per this period (watermark-tracked) so
# the cache stands on its own feet; 6 hours is far shorter than the repo's
# close cadence, so the listing stays fresh without a full-page walk every
# interval.
_PR_ROWS_BACKFILL_MAX_AGE_SECONDS = 6 * 3600


async def _maybe_backfill_pr_rows() -> None:
    """Refill the DB-persisted closed-PR cache (pr_rows) from the closed-pulls
    listing, at most once per _PR_ROWS_BACKFILL_MAX_AGE_SECONDS: the stored
    watermark tracks the last completed refill, and an absent or stale
    watermark refills now. Best-effort - the cache is an optimization and
    every reader falls back to live GitHub when it is unpopulated - so a
    failed refill is logged (pr_rows_backfill_failed) and retried next
    interval."""
    try:
        wm = db.pr_rows_watermark()
        if wm is not None:
            try:
                last = datetime.fromisoformat(wm.replace("Z", "+00:00"))
            except ValueError:
                # domain:degrade-silently - an unparseable watermark is
                # treated as stale so the cache refills; no data is lost.
                last = None  # unparseable watermark: treat as stale, refill
            if last is not None and (
                datetime.now(timezone.utc) - last
                < timedelta(seconds=_PR_ROWS_BACKFILL_MAX_AGE_SECONDS)
            ):
                return
        pulls = await _apaginated_closed_pulls("closed", config.GITHUB_PRS_PER_PAGE)
        rows = [_closed_row_from_raw(p) for p in pulls]
        if not rows:
            return
        with db._conn() as conn:
            for row in rows:
                db.pr_rows_upsert(conn, row)
            db.pr_rows_set_watermark(conn)
        logutil.log("pr_rows_backfill", rows=len(rows))
    except Exception as exc:
        # domain: degrade-silently - cache refill is optional enrichment;
        # readers fall back to live GitHub while the cache is unpopulated.
        logutil.log("pr_rows_backfill_failed", error=str(exc))


def _maybe_gc_vote_labels() -> None:
    """Run _sweep_orphan_vote_labels at most once every
    FORUM_PR_MERGE_POLL_SECONDS * _VOTE_LABEL_GC_MULTIPLIER seconds.  The
    wall-clock gate is restart-safe: a freshly booted server clears any
    accumulated orphan definitions on its first pass."""
    # Facade lookup: tests patch server.poller._sweep_orphan_vote_labels
    # and _last_vote_label_gc, so read and write them via the package
    # namespace; submodule globals would not see the patch.
    import server.poller as _pkg

    interval = (config.PR_MERGE_POLL_SECONDS or 300) * _pkg._VOTE_LABEL_GC_MULTIPLIER
    now = time.monotonic()
    if now - _pkg._last_vote_label_gc < interval:
        return
    _pkg._last_vote_label_gc = now
    try:
        _pkg._sweep_orphan_vote_labels()
    except Exception as exc:
        # domain: degrade-silently - an unexpected sweep failure is logged
        # and the whole pass retries on the next cadence.
        logutil.log("vote_label_gc", error=str(exc))


# Notification-prune cadence: read mail only becomes prunable after
# FORUM_NOTIFICATION_RETENTION_DAYS (default 60), so running the DELETE on
# every outcome-poll tick (~300s) is pure waste - once daily is plenty, and
# the prune's partial index keeps each run cheap. Same wall-clock pattern
# as _maybe_gc_vote_labels above: restart-safe, first pass always runs.
_NOTIFICATION_PRUNE_MAX_AGE_SECONDS = 24 * 3600
_last_notification_prune = 0.0


def _maybe_prune_notifications() -> None:
    """Run notifications.prune_notifications at most once per
    _NOTIFICATION_PRUNE_MAX_AGE_SECONDS. Failures propagate to the
    caller's never-stall guard (the poller retries next interval)."""
    # Facade lookup: tests set server.poller._last_notification_prune, so
    # read and write it via the package namespace (see _maybe_gc_vote_labels).
    import server.poller as _pkg

    now = time.monotonic()
    if now - _pkg._last_notification_prune < _pkg._NOTIFICATION_PRUNE_MAX_AGE_SECONDS:
        return
    _pkg._last_notification_prune = now
    notifications.prune_notifications()


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
            # Gated to once daily - retention is measured in days, so a
            # per-tick DELETE is pure waste (see _maybe_prune_notifications).
            _maybe_prune_notifications()
            # Fold aged tool-call ledger rows into the long-term aggregate
            # and prune them (FORUM_TOOL_USAGE_RETENTION_DAYS), keeping the
            # admin /admin/usage drill-down window bounded.
            db.tool_usage_sweep()
            # Polls: conclude any open poll past its FORUM_POLL_MAX_DURATION
            # conclusion time - notify the thread's participants with the
            # tallied results and log EVT_POLL_CONCLUDED. Idempotent (the
            # status flip to 'concluded' is the guard).
            db._sweep_concluded_polls()
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
            # Invoices (small_fix #341): fire the 50/25/10% due-window
            # reminders plus the one-time overdue ping for accepted,
            # unpaid invoices. Flag-guarded and idempotent inside.
            db._invoices.sweep_invoice_reminders()
        except (
            Exception
        ):  # domain: degrade-silently - reminders are advisory; retry next tick
            pass  # the invoice sweep must never stall the poller
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
            # Backfill the DB-persisted closed-PR cache (pr_rows) so the
            # fast closed listing and the revalidation seam have a row set on
            # a fresh database. Watermark-gated inside
            # _maybe_backfill_pr_rows (at most once per
            # _PR_ROWS_BACKFILL_MAX_AGE_SECONDS, failures retried next tick).
            await _maybe_backfill_pr_rows()
        except Exception as exc:
            # domain:degrade-silently - the cache is an optimization; a
            # failed refill is logged and retried next interval, and readers
            # fall back to live GitHub in the meantime.
            logutil.log("pr_rows_backfill_failed", error=str(exc))
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
