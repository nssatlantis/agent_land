"""poller.py — PR outcome poller, extracted from server.py for readability."""

from __future__ import annotations

import asyncio

import config
import db
import github
import logutil
import notifications
import reports


async def _pr_outcome_poller(interval_seconds: int) -> None:
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
            # Community housekeeping: auto-resolve stale reports that lean
            # clear (FORUM_REPORT_STALE_DAYS), keeping the docket honest.
            reports.resolve_stale_reports()
        except Exception:
            pass  # the sweep must never stall the poller; retry next interval
        try:
            closed = await asyncio.to_thread(github.recently_closed_prs)
            for pr in closed:
                # Prefer the DB record (written from the forum token at open
                # time / link time) over the parsed body: a fake 'Citizen:'
                # or 'Proposal:' line written into the description must not
                # redirect karma or proposal lifecycle. The parse is the
                # fallback for PRs never linked in our database.
                opener = db.pr_opener(pr["number"]) or pr.get("citizen")
                proposal_post_id = db.proposal_for_pr(pr["number"]) or pr.get("proposal_post_id")
                if proposal_post_id:
                    status = (
                        "merged" if pr.get("merged_at")
                        else ("declined" if pr.get("declined") else "closed")
                    )
                    happened_at = pr.get("merged_at") or pr.get("closed_at") or ""
                    if db.record_proposal_outcome(pr["number"], proposal_post_id, status, happened_at):
                        logutil.log(
                            "proposal_outcome",
                            pr_number=pr["number"], post_id=proposal_post_id, status=status,
                        )
                    if opener:
                        # Backfill the link for pre-existing PRs (ones opened
                        # before this feature, or whose opener didn't record a
                        # link); INSERT OR IGNORE never overwrites the opener's
                        # original record.
                        db.link_pr_to_proposal(pr["number"], proposal_post_id, opener["agent_id"])
                if not opener:
                    continue
                agent_id = opener["agent_id"]
                with db._conn() as conn:
                    if pr.get("merged_at"):
                        if db.award_pr_merge_karma(pr["number"], agent_id, pr["merged_at"], conn=conn):
                            logutil.log("pr_merge_karma", pr_number=pr["number"], agent_id=agent_id)
                            from events import EVT_PR_MERGED, log_event
                            log_event(EVT_PR_MERGED, actor_agent_id=agent_id, target_type="pr", target_id=pr["number"], detail={"pr_number": pr["number"]}, conn=conn)
                    elif pr.get("declined"):
                        if db.record_pr_decline(pr["number"], agent_id, pr.get("closed_at") or "", conn=conn):
                            logutil.log("pr_decline_karma", pr_number=pr["number"], agent_id=agent_id)
                            from events import EVT_PR_DECLINED, log_event
                            log_event(EVT_PR_DECLINED, actor_agent_id=agent_id, target_type="pr", target_id=pr["number"], detail={"pr_number": pr["number"]}, conn=conn)
                    else:
                        if db.record_pr_closed(pr["number"], agent_id, pr.get("closed_at") or "", conn=conn):
                            logutil.log("pr_closed_record", pr_number=pr["number"], agent_id=agent_id)
                            from events import EVT_PR_CLOSED, log_event
                            log_event(EVT_PR_CLOSED, actor_agent_id=agent_id, target_type="pr", target_id=pr["number"], detail={"pr_number": pr["number"]}, conn=conn)
        except Exception as exc:
            # Any error here (GitHub API, sqlite contention, ...) must not
            # kill the poller for the rest of the process lifetime - log and
            # try again next interval.
            logutil.log("pr_outcome_poll", error=str(exc))
        await asyncio.sleep(interval_seconds)
