"""server.poller._batches — open-PR batch workers and the unified CI ticker."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
import github
import logutil
import notifications

from ._vote import _pr_vote_sweep

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
    db.maybe_checkpoint(). Also ticks the conservation watch (edge-
    triggered escrow audit events, loud but never load-bearing)."""
    db.maybe_checkpoint()
    try:
        db.conservation_watch_tick()
    except Exception:  # domain: degrade-silently - watch never breaks a poll tick
        pass


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
