"""server.poller._autolink — similarity retro-link ticker for missed PRs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import config
import db
import github
import logutil
import search
from events import (
    EVT_PROPOSAL_AUTO_LINKED,
    log_event,
)

from ._outcome import _process_closed_pr

# -- Similarity auto-link (retro-link merged PRs to their proposal) --------


def _auto_link_candidates(since_iso: str) -> list[dict]:
    """Closed pulls newest-by-updated at or after `since_iso`, raw GitHub
    rows (so the body stamp and labels survive for classification). The
    listing is sorted by updated descending, so the first row older than
    the floor ends the scan; bounded by _PR_PAGE_CAP so a busy repo never
    turns the sweep into an unbounded crawl."""
    # Facade lookup: tests patch server.poller._closed_pulls_page, so read
    # it via the package namespace; a submodule global would not see the
    # patch (same pattern as the wall-clock gates in _outcome).
    import server.poller as _pkg

    out: list[dict] = []
    page = 1
    while True:
        batch = _pkg._closed_pulls_page("closed", config.GITHUB_PRS_PER_PAGE, page)
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
    # Membership only matters for this window's candidates, so scope both
    # lookups with WHERE pr_number IN (candidates): O(window), not O(total).
    candidates = _auto_link_candidates(since_iso)
    numbers = [p["number"] for p in candidates if p.get("merged_at")]
    touched: set[int] = set()
    if numbers:
        marks = ",".join("?" * len(numbers))
        with db._conn() as conn:
            touched = {
                r["pr_number"]
                for r in conn.execute(
                    "SELECT pr_number FROM proposal_links"
                    f" WHERE pr_number IN ({marks})",
                    numbers,
                )
            } | {
                r["pr_number"]
                for r in conn.execute(
                    "SELECT pr_number FROM proposal_outcomes"
                    f" WHERE pr_number IN ({marks})",
                    numbers,
                )
            }
    matches = 0
    for p in candidates:
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
