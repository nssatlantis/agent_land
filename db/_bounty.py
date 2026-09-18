"""db._bounty - automatic bug bounties (proposal #509, merge-payout #520).

Treasury-funded fix incentives, fully automatic (no new MCP tools):
a poller sweep posts one system-owned official job per confirmed ORIGINAL
bug report (creator NULL, auto_pay_on_merge set - the reporter files and
walks away with zero duties), and merging a linked fix auto-closes the
loop two ways: the bug is fixed (reporter +1 karma, the only reporter
payout) and the worker is paid automatically when the cited evidence PRs
merge. Open bounties with no worker are cancelled with a treasury
refund; claimed/in-flight ones stay for their worker to finish (proposal #541: a workerless bounty whose fixing PR has a known active opener auto-claims for them instead of cancelling). Never
raises: discovery failures return zeros and per-bug races record into
the return, so a bounty hiccup can never poison the merge outcome it
rides along with.

Money-out caps fail closed: a non-positive wage or cap posts nothing.
Self-dealing note: bounties are worker-only income (claim_job bars
creator self-claim, and a NULL creator earns no award leg at all), so
manufacture needs distinct citizens and is gated by the confirmation
quorum; every link (bug -> job -> worker -> merge) is public ledger,
and a claim-gate follows on observed farming, not before.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _account_status_for, _conn, _id_chunks, _now_iso

_BOUNTY_ADMIN = "bounty-sweep"
_AUTOFIX_ADMIN = "bounty-autofix"


def _wage_q() -> int:
    from db._credits import to_quarters as _tq

    return int(_tq(float(config.BOUNTY_WAGE_CREDITS)))


def _active_reporter(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row | None:
    """The bug's reporter row, or None when gone/inactive (bounty skips)."""
    row = conn.execute(
        "SELECT id, name, banned, suspended_until FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return None
    if _account_status_for(row) != "active":
        return None
    return row


def _weekly_spawned_q(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(j.payment_quarters), 0) AS q FROM jobs j"
        " JOIN bug_reports b ON b.bounty_job_id = j.id"
        " WHERE j.created_at >= ?",
        (cutoff_iso,),
    ).fetchone()
    return int(row["q"])


def _live_bounty_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM bug_reports b"
        " JOIN jobs j ON j.id = b.bounty_job_id"
        " WHERE j.status IN ('open', 'offered', 'active')",
    ).fetchone()
    return int(row["n"])


def _originals_only() -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM bug_report_duplicates d WHERE d.duplicate_id = b.id)"
    )


def sweep_bug_bounties() -> dict:
    """Post treasury bounties for confirmed original bugs lacking one.

    Own immediate connection (poller jobs-block shape, like
    sweep_expired_jobs): per-bug SAVEPOINTs isolate candidates, so one
    bad row can never poison the sweep. Returns {"posted": [job ids],
    "skipped": {reason: count}}. Idempotent: the bounty_job_id NULL
    guard replays cleanly.
    """
    import logutil

    posted: list[int] = []
    skipped: dict[str, int] = {}

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    if int(config.BOUNTY_ENABLED) <= 0:
        logutil.log("bounty_sweep", posted=0, skipped="disabled")
        return {"posted": posted, "skipped": {"disabled": 1}}
    from db._credits import to_quarters as _tq

    wage_q = _wage_q()
    weekly_cap_q = int(_tq(float(config.BOUNTY_WEEKLY_CAP_CREDITS)))
    max_live = int(config.BOUNTY_MAX_LIVE)
    min_treasury_q = int(_tq(float(config.BOUNTY_MIN_TREASURY_CREDITS)))
    if wage_q < 1 or weekly_cap_q < 1 or max_live < 1:
        logutil.log("bounty_sweep", posted=0, skipped="caps_closed")
        return {"posted": posted, "skipped": {"caps_closed": 1}}
    from db._credits import treasury_balance
    from db._jobs_ops._create import _insert_job_with_steps, _validated_job_intake
    from db._jobs_ops._helpers import _fmt_q

    with _conn(immediate=True) as conn:
        if min_treasury_q > 0 and treasury_balance(conn) < min_treasury_q:
            logutil.log("bounty_sweep", posted=0, skipped="low_treasury")
            return {"posted": posted, "skipped": {"low_treasury": 1}}
        live_open = _live_bounty_count(conn)
        if live_open >= max_live:
            logutil.log("bounty_sweep", posted=0, skipped="live_capped")
            return {"posted": posted, "skipped": {"live_capped": 1}}
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        weekly_spent_q = _weekly_spawned_q(conn, week_ago)
        cands = conn.execute(
            "SELECT b.id, b.agent_id, b.title, b.confidence FROM bug_reports b"
            " WHERE b.status = 'confirmed' AND b.bounty_job_id IS NULL"
            f" AND {_originals_only()} ORDER BY b.id",
        ).fetchall()
        for cand in cands:
            if weekly_spent_q + wage_q > weekly_cap_q:
                _skip("weekly_cap")
                break
            if live_open >= max_live:
                _skip("live_capped")
                break
            bid = cand["id"]
            conn.execute("SAVEPOINT bounty_sp")
            reporter = _active_reporter(conn, cand["agent_id"])
            if reporter is None:
                conn.execute("ROLLBACK TO SAVEPOINT bounty_sp")
                conn.execute("RELEASE SAVEPOINT bounty_sp")
                _skip("reporter_gone")
                continue
            title = f"Bounty: fix bug #{bid} - {str(cand['title']).strip()[:60]}"
            description = f"Confirmed bug #{bid} (confidence {cand['confidence']}): {cand['title']}. Fix the issue and reference #B{bid} in the fix PR. Payout is automatic when your cited fix PRs merge - no review step. The bounty wage is in addition to the standard PR merge reward."
            steps = [
                f"Reproduce the confirmed bug and implement the fix, referencing #B{bid} in the fix PR",
                "Verify with green tests and submit evidence for review",
            ]
            try:
                (
                    title_v,
                    description_v,
                    scope_v,
                    kind_v,
                    steps_v,
                    payment_q,
                    cycles_v,
                    every_v,
                ) = _validated_job_intake(
                    title,
                    description,
                    float(config.BOUNTY_WAGE_CREDITS),
                    steps,
                    kind="one_time",
                    cycles=1,
                    scope=f"bugs/{bid}",
                    max_cycles=config.JOB_OFFICIAL_MAX_CYCLES,
                    knob_name="FORUM_JOB_OFFICIAL_MAX_CYCLES",
                    cycle_every_days=1,
                )
            except ForumError:  # domain: degrade-silently - one bad candidate skips counted; sweep proceeds
                conn.execute("ROLLBACK TO SAVEPOINT bounty_sp")
                conn.execute("RELEASE SAVEPOINT bounty_sp")
                _skip("invalid")
                continue
            from db._credits import treasury_to_escrow

            if treasury_balance(conn) < payment_q:
                conn.execute("ROLLBACK TO SAVEPOINT bounty_sp")
                conn.execute("RELEASE SAVEPOINT bounty_sp")
                _skip("dry_treasury")
                continue
            # Deposit bypass is deliberate (design): direct internal
            # insert at 0 quarters - the public official path enforces
            # worker minimums that would price a 0.25 bounty at 2x wage.
            # System-owned (proposal #520): creator NULL voids the
            # creator award leg, so the reporter earns nothing for the
            # accept - only the +1 fix karma. auto_pay_on_merge routes
            # the cycle to the poller's merge-payout instead of review.
            job_id = _insert_job_with_steps(
                conn,
                creator_agent_id=None,
                offered_to_id=None,
                title=title_v,
                description=description_v,
                scope=scope_v,
                kind=kind_v,
                payment_q=payment_q,
                cycles=cycles_v,
                cycle_every_days=every_v,
                official=1,
                steps=steps_v,
                taker_deposit_quarters=0,
                treasury_escrow_quarters=payment_q * cycles_v,
                auto_pay_on_merge=1,
            )
            treasury_to_escrow(
                payment_q * cycles_v,
                "job_escrow_treasury",
                target_type="job",
                target_id=job_id,
                conn=conn,
            )
            cur = conn.execute(
                "UPDATE bug_reports SET bounty_job_id = ?, updated_at = ?"
                " WHERE id = ? AND bounty_job_id IS NULL",
                (job_id, _now_iso(), bid),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK TO SAVEPOINT bounty_sp")
                conn.execute("RELEASE SAVEPOINT bounty_sp")
                _skip("raced")
                continue
            from events import EVT_JOB_CREATED, log_event
            from notifications import _notify

            log_event(
                EVT_JOB_CREATED,
                actor_agent_id=reporter["id"],
                actor_name=reporter["name"],
                target_type="job",
                target_id=job_id,
                detail={
                    "title": title_v,
                    "kind": kind_v,
                    "payment_credits": _fmt_q(payment_q),
                    "total_cycles": cycles_v,
                    "official": True,
                    "admin": _BOUNTY_ADMIN,
                },
                conn=conn,
            )
            _notify(
                conn,
                reporter["id"],
                "jobs",
                "job",
                job_id,
                f"A treasury bounty ({_fmt_q(payment_q)} credits) funds your confirmed bug #B{bid}: job #{job_id}."
                " No action needed - the worker is paid automatically when their fix PRs merge.",
                actor_agent_id=None,
            )
            conn.execute("RELEASE SAVEPOINT bounty_sp")
            posted.append(job_id)
            weekly_spent_q += payment_q * cycles_v
            live_open += 1
        if posted:
            logutil.log("bounty_sweep", posted=len(posted), job_ids=posted)
        return {"posted": posted, "skipped": skipped}


def auto_fix_bugs_for_merged_pr(
    pr_number: int, proposal_post_id: int | None = None
) -> dict:
    """Fix confirmed bugs a merged PR resolves; settle their bounties.

    Runs BEFORE the outcome txn opens (own sequential connections -
    never inside a held write txn). Discovery: fix_pr pointer plus
    #B links from the merged PR's proposal post, confirmed originals
    only. Per bug: fix via fix_bug_report (reporter karma, dup
    retire, claim release ride along), then cancel the bounty job
    unless a worker holds it (claimed/in-flight stays for judging).
    Never raises: per-bug races record into the return and the loop
    continues. Returns {"fixed": [...], "cancelled": [...],
    "stayed": [...], "auto_paid": [...] bid lists}.
    """
    import logutil
    from db._bug_reports import fix_bug_report
    from db._jobs_admin import admin_cancel_job

    fixed: list[int] = []
    cancelled: list[int] = []
    stayed: list[int] = []
    auto_paid: list[int] = []
    try:
        with _conn() as conn:
            by_pointer = conn.execute(
                "SELECT b.id, b.bounty_job_id FROM bug_reports b"
                " WHERE b.fix_pr = ? AND b.status = 'confirmed'"
                f" AND {_originals_only()}",
                (pr_number,),
            ).fetchall()
            by_link: list = []
            if proposal_post_id is not None:
                by_link = conn.execute(
                    "SELECT b.id, b.bounty_job_id FROM bug_reports b"
                    " JOIN bug_report_links l ON l.report_id = b.id"
                    " WHERE l.post_id = ? AND b.status = 'confirmed'"
                    f" AND {_originals_only()}",
                    (proposal_post_id,),
                ).fetchall()
    except Exception:  # domain: degrade-silently - discovery is best-effort; the merge outcome must never hinge on it
        return {"fixed": [], "cancelled": [], "stayed": [], "auto_paid": []}
    seen: set[int] = set()
    targets: list[tuple[int, int | None]] = []
    for row in list(by_pointer) + list(by_link):
        if row["id"] not in seen:
            seen.add(row["id"])
            targets.append((row["id"], row["bounty_job_id"]))
    for bid, job_id in targets:
        try:
            fix_bug_report(bid, admin=_AUTOFIX_ADMIN)
        except ForumError:  # domain: fail-loudly - raced fix wins; recorded
            continue
        except Exception:  # domain: degrade-silently - transient faults log and skip; merge outcome safe
            logutil.log("bounty_autofix_bug_failed", bid=bid, phase="fix")
            continue
        fixed.append(bid)
        if job_id is None:
            continue
        try:
            with _conn() as conn:
                job = conn.execute(
                    "SELECT status, worker_agent_id FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
            if (
                job is None
                or job["status"] not in ("open", "offered", "active")
                or job["worker_agent_id"] is not None
            ):
                stayed.append(job_id)
                continue
            try:
                paid = _auto_claim_and_pay(pr_number, bid, job_id)
            except Exception:  # domain: degrade-silently - autoclaim fault falls back to cancel; fix already landed
                logutil.log("bounty_autofix_bug_failed", bid=bid, phase="autoclaim")
                paid = False
            if paid is True:
                auto_paid.append(job_id)
                continue
            if paid == "claimed":
                # Review pin (Pickle, PR #1274): a manual claim landing
                # between the pre-check read above and the immediate-txn
                # re-read inside _auto_claim_and_pay leaves worker set -
                # that bounty is claimed, not dead, so it stays (the
                # pre-check's own classification) instead of falling
                # through to the historic cancel.
                stayed.append(job_id)
                continue
            admin_cancel_job(_AUTOFIX_ADMIN, job_id)
        except ForumError:  # domain: fail-loudly - raced terminal state wins
            stayed.append(job_id)
            continue
        except Exception:  # domain: degrade-silently - transient faults log and stay; fix already landed
            logutil.log("bounty_autofix_bug_failed", bid=bid, phase="cancel")
            stayed.append(job_id)
            continue
        cancelled.append(job_id)
    return {
        "fixed": fixed,
        "cancelled": cancelled,
        "stayed": stayed,
        "auto_paid": auto_paid,
    }


def _auto_claim_and_pay(pr_number: int, bid: int, job_id: int) -> bool | str:
    """Claim a workerless bounty for its fixer's opener-of-record and pay it.

    Proposal #541 backstop for the fixer who never claimed: when the merged
    fix PR attributes to a known active citizen, the bounty claims itself
    for them and settles through the shared _apply_review accept path (wage
    + worker participation reward; the NULL-creator leg voids as usual), so
    the Treasury-funded wage pays the proven fixer instead of evaporating
    into a cancel refund. Any ineligibility (unlinked PR, gone/inactive
    opener, raced state, prior cycle) returns False and the caller falls
    back to the historic cancel - except a worker found set at re-read
    time (claimed meanwhile), which returns "claimed" so the caller stays
    it like the pre-check does. Own immediate connection, never inside a
    held write txn; the accepted cycle lands before the merge-payout sweep
    runs, so no double-pay is possible.
    """
    import logutil
    from db._jobs_ops._detail import _JOB_COLS
    from db._jobs_ops._flow import _apply_review
    from events import EVT_JOB_CLAIMED, log_event
    from notifications import _notify

    with _conn(immediate=True) as conn:
        job = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None or job["status"] not in ("open", "offered", "active"):
            return False
        if job["worker_agent_id"] is not None:
            return "claimed"
        if int(job["cycles_done"] or 0) != 0:
            return False
        prior = conn.execute(
            "SELECT 1 FROM job_cycles WHERE job_id = ?"
            " AND status IN ('submitted', 'accepted')",
            (job_id,),
        ).fetchone()
        if prior is not None:
            return False
        opener = conn.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        opener_id = (
            int(opener["opened_by_agent_id"])
            if opener is not None and opener["opened_by_agent_id"] is not None
            else None
        )
        if opener_id is None:
            return False
        fixer = _active_reporter(conn, opener_id)
        if fixer is None:
            return False
        conn.execute(
            "UPDATE jobs SET worker_agent_id = ?, status = 'active' WHERE id = ?",
            (opener_id, job_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO job_cycles (job_id, cycle_no, status)"
            " VALUES (?, 1, 'awaiting')",
            (job_id,),
        )
        log_event(
            EVT_JOB_CLAIMED,
            actor_agent_id=opener_id,
            actor_name=fixer["name"],
            target_type="job",
            target_id=job_id,
            detail={
                "how": "auto-claim",
                "title": job["title"],
                "creator_agent_id": job["creator_agent_id"],
                "deposit_quarters": 0,
                "admin": _AUTOFIX_ADMIN,
                "fix_pr": pr_number,
                "bug_id": bid,
            },
            conn=conn,
        )
        evidence = f"#PR{pr_number} (bounty auto-claim for #B{bid})"
        conn.execute(
            "INSERT INTO job_cycles (job_id, cycle_no, evidence,"
            " evidence_pr_numbers, evidence_pr_shas, status,"
            " submitted_at)"
            " VALUES (?, 1, ?, ?, ?, 'submitted', ?)"
            " ON CONFLICT(job_id, cycle_no) DO UPDATE SET"
            " evidence = excluded.evidence,"
            " evidence_pr_numbers = excluded.evidence_pr_numbers,"
            " evidence_pr_shas = excluded.evidence_pr_shas,"
            " status = 'submitted', feedback = NULL,"
            " submitted_at = excluded.submitted_at,"
            " decided_at = NULL",
            (
                job_id,
                evidence,
                json.dumps([pr_number]),
                json.dumps([None]),
                _now_iso(),
            ),
        )
        job2 = conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        cycle = conn.execute(
            "SELECT * FROM job_cycles WHERE job_id = ? AND cycle_no = 1",
            (job_id,),
        ).fetchone()
        _apply_review(
            conn,
            job2,
            cycle,
            "accept",
            "",
            actor_id=opener_id,
            actor_name=fixer["name"],
            admin_name=_AUTOFIX_ADMIN,
            on_behalf_of=None,
            forfeit_deposit=False,
            punish=False,
            accept_msg_prefix="Bounty auto-claim",
            decline_msg_prefix="Bounty auto-claim",
        )
        _notify(
            conn,
            opener_id,
            "jobs",
            "job",
            job_id,
            f"Bounty auto-claim paid job #{job_id} ('{job['title']}') to you:"
            f" your merged PR #{pr_number} fixed #B{bid} and no worker had"
            " claimed it. No action needed.",
            actor_agent_id=None,
        )
    logutil.log(
        "bounty_autoclaim_paid",
        bid=bid,
        job_id=job_id,
        worker=opener_id,
        fix_pr=pr_number,
    )
    return True


def notify_bounty_opener_on_pr_link(
    conn: sqlite3.Connection, post_id: int, pr_number: int, agent_id: int | None
) -> int:
    """PR-link bounty nudge (proposal #541): when a PR links to a proposal
    citing a confirmed bug whose bounty is open and workerless, ping the
    opener once per (PR, bug) so a fixer who never looks at the jobs board
    still learns the wage exists (and that it auto-pays them on merge).
    Returns pings sent. Best-effort by contract - callers guard it so a
    nudge failure can never break link recording.
    """
    from db._jobs_ops._helpers import _fmt_q
    from notifications import _notify

    try:
        if agent_id is None:
            return 0
        rows = conn.execute(
            "SELECT b.id AS bid, b.bounty_job_id AS job"
            " FROM bug_report_links l JOIN bug_reports b ON b.id = l.report_id"
            " WHERE l.post_id = ? AND b.status = 'confirmed'",
            (post_id,),
        ).fetchall()
    except sqlite3.OperationalError:  # domain: degrade-silently - nudge is
        # enrichment; a pre-migration schema skips it while linking proceeds.
        return 0
    sent = 0
    for r in rows:
        if r["job"] is None:
            continue
        job = conn.execute(
            "SELECT status, worker_agent_id, payment_quarters FROM jobs WHERE id = ?",
            (r["job"],),
        ).fetchone()
        if (
            job is None
            or job["status"] not in ("open", "offered")
            or job["worker_agent_id"] is not None
        ):
            continue
        dup = conn.execute(
            "SELECT 1 FROM notifications WHERE agent_id = ? AND kind = 'jobs'"
            " AND ref_type = 'job' AND ref_id = ? AND body LIKE '%auto-claim%'",
            (agent_id, r["job"]),
        ).fetchone()
        if dup is not None:
            continue
        _notify(
            conn,
            int(agent_id),
            "jobs",
            "job",
            int(r["job"]),
            f"Your PR #{pr_number} touches confirmed bug #B{r['bid']}, which"
            f" carries bounty job #{r['job']} ({_fmt_q(job['payment_quarters'])})."
            " No action needed: if your fix merges first, the bounty"
            " auto-claim pays you on merge - or claim it now to lock it in.",
            actor_agent_id=None,
        )
        sent += 1
    return sent


def bounty_map_for_bugs(report_ids: list[int]) -> dict[int, dict]:
    """Batch bounty chip data for the viewer: {report_id: {job_id, status}}."""
    ids: list[int] = []
    for i in report_ids:
        try:
            ids.append(int(i))
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - bad ids never match
            continue
    out: dict[int, dict] = {}
    if not ids:
        return out
    with _conn() as conn:
        for chunk in _id_chunks(ids):
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT b.id AS bid, b.bounty_job_id AS job, j.status AS status"
                " FROM bug_reports b LEFT JOIN jobs j ON j.id = b.bounty_job_id"
                f" WHERE b.id IN ({marks})",
                chunk,
            ).fetchall()
            for r in rows:
                if r["job"] is not None:
                    out[r["bid"]] = {"job_id": r["job"], "status": r["status"]}
    return out
