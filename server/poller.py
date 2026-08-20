"""server/poller.py — PR outcome poller, extracted from server.py for readability."""

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


def _ci_failure_sweep(open_prs: list[dict],
                      checks_fn=github.pr_checks) -> list[int]:
    """Nudge each open PR's citizen owner once per new failing head commit.

    CI state lives on GitHub, so the mailbox would never learn about it on
    its own - this is the one sweep that reads live check status. For every
    open PR owned by a citizen (the recorded opener, falling back to the
    body trailer; Maintainer-Helper PRs have no citizen owner and are
    skipped), consult the tiered checks builder and notify the owner when
    the head is failing AND that head has not been nudged yet - exactly one
    nudge per push, no spam while a PR sits red unchanged. Green re-arms
    the state, so a regression after a fix nudges again. `checks_fn` is
    injectable so tests need no GitHub. Returns the pr numbers nudged."""
    openers = db.linked_pr_openers()
    notified: list[int] = []
    for pr in open_prs:
        opener = openers.get(pr["number"]) or pr.get("citizen")
        if not opener:
            continue
        checks = checks_fn(pr["number"], _head_sha=pr.get("head_sha") or None)
        head_sha = checks.get("head_sha") or pr.get("head_sha") or ""
        red = checks.get("state") == "failure"
        with db._conn() as conn:
            row = conn.execute(
                "SELECT head_sha, red_notified FROM pr_ci_state WHERE pr_number = ?",
                (pr["number"],),
            ).fetchone()
            if red and (row is None or row["head_sha"] != head_sha
                        or not row["red_notified"]):
                title = " ".join((pr.get("title") or "").split())
                body = f"PR #{pr['number']} ({title}) is failing CI: {_first_failure(checks)}"
                if len(body) > _CI_NUDGE_BODY_MAX:
                    body = body[:_CI_NUDGE_BODY_MAX - 1] + "…"
                notifications._notify(
                    conn, opener["agent_id"], "pr_ci", "pr", pr["number"],
                    body, actor_agent_id=None,
                )
                notified.append(pr["number"])
            conn.execute(
                "INSERT OR IGNORE INTO pr_ci_state (pr_number, head_sha, red_notified)"
                " VALUES (?, ?, 0)",
                (pr["number"], head_sha),
            )
            conn.execute(
                "UPDATE pr_ci_state SET head_sha = ?, red_notified = ?"
                " WHERE pr_number = ?",
                (head_sha, 1 if red else 0, pr["number"]),
            )
    return notified


async def _ci_failure_poller(interval_seconds: int) -> None:
    """Nudge a PR's citizen owner when its CI fails - once per new head
    commit, so 'go fix it' lands exactly when there is something new to
    fix and never while a red PR sits unchanged. The tiered checks builder
    is the same one repo_pr_checks uses. All blocking calls run in worker
    threads so the MCP loop never stalls; any error is logged and retried
    next interval."""
    while True:
        interval_seconds = config.CI_POLL_SECONDS
        try:
            open_prs = await asyncio.to_thread(github.open_prs)
            await asyncio.to_thread(_ci_failure_sweep, open_prs)
        except Exception as exc:
            logutil.log("ci_failure_poll", error=str(exc))
        await asyncio.sleep(interval_seconds)
