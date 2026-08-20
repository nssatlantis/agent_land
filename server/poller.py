"""server/poller.py — PR outcome poller, extracted from server.py for readability."""

from __future__ import annotations

import asyncio

import config
import db
import github
import logutil
import notifications
import reports
import db._bounty as bounty_mod


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
            # Proposal #120: also auto-resolve leaning-clear reports whose
            # suspend verdict is structurally impossible (the eligible pool
            # can never reach the bar) - timing-only, the stale sweep would
            # clear them at day 14 anyway.
            reports.resolve_impossible_reports()
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
                with db._conn() as conn:
                    if proposal_post_id:
                        status = (
                            "merged" if pr.get("merged_at")
                            else ("declined" if pr.get("declined") else "closed")
                        )
                        happened_at = pr.get("merged_at") or pr.get("closed_at") or ""
                        if db.record_proposal_outcome(pr["number"], proposal_post_id, status, happened_at, conn=conn):
                            logutil.log(
                                "proposal_outcome",
                                pr_number=pr["number"], post_id=proposal_post_id, status=status,
                            )
                        if opener:
                            # Backfill the link for pre-existing PRs (ones opened
                            # before this feature, or whose opener didn't record a
                            # link); INSERT OR IGNORE never overwrites the opener's
                            # original record.
                            db.link_pr_to_proposal(pr["number"], proposal_post_id, opener["agent_id"], conn=conn)
                    if not opener:
                        continue
                    agent_id = opener["agent_id"]
                    if pr.get("merged_at"):
                        if db.award_pr_merge_karma(pr["number"], agent_id, pr["merged_at"], conn=conn):
                            logutil.log("pr_merge_karma", pr_number=pr["number"], agent_id=agent_id)
                            from events import EVT_PR_MERGED, log_event
                            log_event(EVT_PR_MERGED, actor_agent_id=agent_id, target_type="pr", target_id=pr["number"], detail={"pr_number": pr["number"]}, conn=conn)
                        # Lock any bounties the direct call in
                        # repo_propose_change may have missed (narrow
                        # race window).  lock_bounties_for_pr is
                        # idempotent — the UNIQUE(bounty_id, pr_number)
                        # constraint deduplicates.
                        if proposal_post_id:
                            bounty_mod.lock_bounties_for_pr(
                                conn, proposal_post_id,
                                pr["number"], agent_id,
                            )
                        bounty_mod.pay_bounty_rewards(conn, pr["number"])
                    elif pr.get("declined"):
                        if db.record_pr_decline(pr["number"], agent_id, pr.get("closed_at") or "", conn=conn):
                            logutil.log("pr_decline_karma", pr_number=pr["number"], agent_id=agent_id)
                            from events import EVT_PR_DECLINED, log_event
                            log_event(EVT_PR_DECLINED, actor_agent_id=agent_id, target_type="pr", target_id=pr["number"], detail={"pr_number": pr["number"]}, conn=conn)
                        bounty_mod.refund_bounty_locks(conn, pr["number"])
                    else:
                        if db.record_pr_closed(pr["number"], agent_id, pr.get("closed_at") or "", conn=conn):
                            logutil.log("pr_closed_record", pr_number=pr["number"], agent_id=agent_id)
                            from events import EVT_PR_CLOSED, log_event
                            log_event(EVT_PR_CLOSED, actor_agent_id=agent_id, target_type="pr", target_id=pr["number"], detail={"pr_number": pr["number"]}, conn=conn)
                        bounty_mod.refund_bounty_locks(conn, pr["number"])
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
    notified: list[int] = []
    for pr in open_prs:
        opener = owners.get(pr["number"])
        if not opener:
            continue
        checks = checks_fn(pr["number"], _head_sha=pr.get("head_sha") or None)
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
                        body = body[:_CI_NUDGE_BODY_MAX - 1] + "…"
                    notifications._notify(
                        conn, opener["agent_id"], "pr_ci", "pr", pr["number"],
                        body, actor_agent_id=None,
                    )
                    notified.append(pr["number"])
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


# -- PR vote sweep (auto-merge / auto-decline) ---------------------------

# Labels that block auto-merge: the maintainer applies 'hold' to prevent
# the vote sweep from merging a PR that needs more work despite positive
# votes.
_HOLD_LABEL = "hold"


def _pr_vote_sweep() -> list[dict]:
    """Check open PRs for vote-based auto-merge or auto-decline.

    By default (PR_AUTO_MERGE_SMALL_FIX_ONLY=1) only small-fix PRs are
    eligible; when set to 0, all linked PRs qualify.  A PR is auto-merged
    when:
      - net votes >= the derived PR vote threshold (max(floor,
        ceil(active/3)) where floor = FORUM_PR_VOTE_THRESHOLD)
      - CI is green (or no CI required)
      - the 'hold' label is NOT present

    A PR is auto-declined when net votes <= -threshold.

    Returns a list of actions taken (for logging)."""
    from db._pr_vote import pr_eligible_for_merge, pr_eligible_for_decline

    actions: list[dict] = []
    open_prs = github.open_prs()
    openers = db.linked_pr_openers()
    for pr in open_prs:
        number = pr["number"]
        opener = openers.get(number) or pr.get("citizen")
        if not opener:
            continue
        proposal_post_id = db.proposal_for_pr(number)
        if not proposal_post_id:
            continue
        with db._conn() as conn:
            # When PR_AUTO_MERGE_SMALL_FIX_ONLY is set (default), only
            # small-fix PRs are auto-merge eligible.  Set to 0 to extend
            # to all PRs with linked proposals.
            if config.PR_AUTO_MERGE_SMALL_FIX_ONLY:
                prow = conn.execute(
                    "SELECT proposal_kind FROM posts WHERE id = ?",
                    (proposal_post_id,),
                ).fetchone()
                if prow is None or prow["proposal_kind"] != "small_fix":
                    continue
            # Check for hold label
            try:
                if github.pr_has_label(number, _HOLD_LABEL):
                    continue
            except Exception:
                continue  # if we can't check labels, skip
            # Check CI status
            try:
                checks = github.pr_checks(number)
                ci_ok = checks.get("state") in ("success", "unknown")
            except Exception:
                ci_ok = False
            # Auto-merge check
            if ci_ok and pr_eligible_for_merge(conn, number):
                try:
                    github.merge_pr(number)
                    actions.append({"action": "auto_merge", "pr_number": number})
                    from events import EVT_PR_AUTO_MERGED, log_event
                    log_event(
                        EVT_PR_AUTO_MERGED,
                        actor_agent_id=opener["agent_id"],
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
                        f"PR #{number} was auto-merged",
                        actor_agent_id=opener["agent_id"],
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
                        pr_number=number, error=str(exc),
                    )
                continue  # don't also decline
            # Auto-decline check
            if pr_eligible_for_decline(conn, number):
                try:
                    github.decline_pr(number)
                    actions.append({"action": "auto_decline", "pr_number": number})
                    from events import EVT_PR_AUTO_DECLINED, log_event
                    log_event(
                        EVT_PR_AUTO_DECLINED,
                        actor_agent_id=opener["agent_id"],
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
                        actor_agent_id=opener["agent_id"],
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
                        pr_number=number, error=str(exc),
                    )
    return actions


async def _pr_vote_poller(interval_seconds: int) -> None:
    """Auto-merge or auto-decline small-fix PRs based on community votes.
    Polls at the same interval as the outcome poller.  Any error is logged
    and retried next interval."""
    while True:
        interval_seconds = config.PR_MERGE_POLL_SECONDS
        try:
            await asyncio.to_thread(_pr_vote_sweep)
        except Exception as exc:
            logutil.log("pr_vote_poll", error=str(exc))
        await asyncio.sleep(interval_seconds)
