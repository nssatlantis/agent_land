"""db._core._boot_final — init_db phase: treasury genesis, workflow/bug sweeps, pr_rows index, hygiene (moved verbatim from db/_core.py:1730-1988)."""

from __future__ import annotations

import sqlite3

import config

from ._errors import ForumError


def run(conn) -> None:
    if config.CREDITS_ENABLED:
        from db._credits import (
            exact_from_credits,
            format_credits,
        )
        from db._credits import (
            quarters_per_karma as _qpk_boot,
        )

        # Fail VISIBLY at boot if the earn-rate knob is misconfigured.
        # Runtime degrades to earning-disabled (voting must never
        # break over a credits knob), but a human watching the deploy
        # should see this line immediately, not hunt it later.
        if config.KARMA_TO_CREDIT_RATIO and _qpk_boot() == 0:
            import logutil

            logutil.log(
                "economy_ratio_invalid_boot",
                level="ERROR",
                value=config.KARMA_TO_CREDIT_RATIO,
                hint="FORUM_KARMA_TO_CREDIT_RATIO must be whole/half/"
                "quarter - credit earning is DISABLED.",
            )
        genesis_q = 0
        try:
            genesis_q = exact_from_credits(
                config.TREASURY_GENESIS_CREDITS,
                what="FORUM_TREASURY_GENESIS_CREDITS",
            )
        except ForumError as exc:
            # domain: degrade-silently - a mis-set price must not keep
            # the forum's database from opening; seeding retries on
            # the first boot after the knob is fixed.
            # Same degrade philosophy as the ratio knob: a mis-set
            # price must not keep the forum's database from opening.
            # Skip genesis loudly; the marker-free ledger seeds
            # normally on the first boot after the knob is fixed
            # (review H2).
            import logutil

            logutil.log(
                "economy_genesis_invalid",
                level="ERROR",
                value=config.TREASURY_GENESIS_CREDITS,
                error=str(exc),
            )
        if (
            genesis_q > 0
            and not conn.execute(
                "SELECT 1 FROM credit_entries"
                " WHERE account = 'treasury' AND reason = 'genesis' LIMIT 1"
            ).fetchone()
        ):
            conn.execute(
                "INSERT INTO credit_entries"
                " (agent_id, delta_quarters, reason, target_type,"
                "  target_id, account, tx_id)"
                " VALUES (NULL, ?, 'genesis', 'economy', NULL,"
                "  'treasury',"
                "  (SELECT COALESCE(MAX(tx_id), 0) + 1"
                "     FROM credit_entries))",
                (genesis_q,),
            )
            from events import EVT_CREDIT_MINTED, log_event

            log_event(
                EVT_CREDIT_MINTED,
                actor_agent_id=None,
                target_type="economy",
                target_id=None,
                detail={
                    "reason": "genesis",
                    "credits": format_credits(genesis_q),
                    "delta_quarters": genesis_q,
                    "admin": "system",
                },
                conn=conn,
            )
    # Workflow create-pr backfill (review #7): a database that predates the
    # workflows feature has open, PR-openable proposals with no create-pr
    # run yet, and with FORUM_WORKFLOW_ENFORCE=1 their next
    # repo_propose_change would be hard-blocked until the gate's lazy
    # restart re-opens a run on first attempt. Opening those runs here -
    # once, at boot - makes the feature seamless for pre-existing
    # proposals. Idempotent: start_workflow's "no open run" guard means a
    # proposal that already has a run (or that _insert_post already
    # started) is never double-started, so this is a no-op on fresh DBs
    # and on every later boot. The candidate set is every proposal with NO
    # create-pr run of any status: a proposal that already folded a run
    # (expired at TTL, or decided) without ever linking a pull request is
    # the ghost pattern the reconcile sweep below cleans up, and re-seeding
    # one here would regenerate it forever across boots. Only still-openable
    # proposals qualify: a proposal whose live status is anything but
    # 'open' - merged (terminal), or declined/closed (retryable per
    # CHARTER VI.5, but not currently open) - plus superseded (locked by a
    # newer version) cannot open a PR today and are skipped. A
    # DECLINED/CLOSED proposal would otherwise gain a fresh, forever-open
    # run on every boot, because it is still retryable and nothing ever
    # closes runs for decisions that predate the feature.
    # reconcile_open_runs() below heals the runs that leaked through that
    # old "skip only merged" gate: it closes any open run whose proposal
    # is decided (or is a no-link ghost), mirroring what
    # close_workflow_for_pr does for poller-processed outcomes, so this
    # backfill and the reconciliation cannot fight each other across
    # boots.
    # Per-PR lifecycle (part 2): the candidate gate is "no create-pr run
    # of ANY status" (the old `status = 'open'` filter could resurrect a
    # spurious unbound open run on the next boot for an in-flight PR whose
    # bound run had already CI-completed), and start_workflow's partial
    # UNIQUE guard keeps at most one open run per unbound proposal.
    try:
        # This conn has no row_factory (plain tuples) - every other query
        # in init_db keys by index. start_workflow and our reads need
        # sqlite3.Row keyed access, so switch it on for this last block
        # and restore it in a finally (review D3). A separate connection
        # would be a dead end: init_db still holds this conn's write
        # transaction open while backfilling, so a second writer would
        # busy-timeout (~5s) and silently no-op the backfill - precisely
        # the failure this backfill exists to prevent.
        _previous_factory = conn.row_factory
        try:
            conn.row_factory = sqlite3.Row
            from db._proposal_status import (
                _proposal_status_for,
                _proposal_superseded_by,
            )
            from db._workflow import start_workflow as _start_workflow

            _candidates = conn.execute(
                "SELECT id, agent_id FROM posts"
                " WHERE proposal_kind IN ('proposal', 'small_fix')"
                " AND id NOT IN ("
                "   SELECT proposal_id FROM workflow_runs"
                "   WHERE workflow_path = 'workflows/create-pr.md'"
                " )"
            ).fetchall()
            for _row in _candidates:
                _pid = int(_row["id"])
                _author = _row["agent_id"]
                if _author is None:
                    continue
                try:
                    if _proposal_status_for(conn, _pid) != "open":
                        continue
                except Exception:  # domain: degrade-silently - treat as openable
                    pass
                try:
                    if _proposal_superseded_by(conn, _pid) is not None:
                        continue
                except Exception:  # domain: degrade-silently - treat as openable
                    pass
                try:
                    _start_workflow(conn, "workflows/create-pr.md", _pid, int(_author))
                except (
                    Exception
                ):  # domain: degrade-silently - one bad proposal must not block boot
                    pass
            # Reconciliation sweep (not the backfill): close any open
            # create-pr run whose proposal is already decided or
            # superseded - the residue that leaked through the old
            # "skip only merged" backfill gate on pre-feature decisions.
            # Idempotent, so harmless on every later boot. A failure here -
            # even of the lazy import itself - is logged, never silently
            # dropped: an invisible break would leave stale runs piling up
            # until _workflow_nudge starts pinging authors about them.
            try:
                from db._workflow import reconcile_open_runs as _reconcile_open_runs

                _reconcile_open_runs(conn)
            except Exception as exc:  # domain: degrade-silently - workflow is enrichment; boot must not fail
                logutil.log("workflow_reconcile_failed", error=str(exc))
            # Guided-steps backfill (workflows part 2, PR B): seed the
            # checklist for open create-pr runs that predate the feature
            # (and for lazy restarts before a workflow gained its
            # `## Steps` section). Idempotent - only runs with no steps are
            # seeded. Steps are annotation-level enrichment; a failure here
            # is logged and the run lazy-seeds on its first read anyway.
            try:
                from db._workflow import (
                    seed_steps_for_open_runs as _seed_steps_for_open_runs,
                )

                _seed_steps_for_open_runs(conn)
            except Exception as exc:  # domain:degrade-silently - steps are enrichment; runs lazy-seed on first read
                logutil.log("workflow_steps_seed_failed", error=str(exc))
            # Bug-report auto-confirm sweep: open reports whose confidence
            # already reached BUG_CONFIDENCE_THRESHOLD (crossed under a
            # higher config, or before the decided_at + EVT_BUG_CONFIRMED
            # stamping existed) are promoted to confirmed on boot, with the
            # same side effects as a live threshold crossing.  Idempotent,
            # so harmless on every later boot.  A failure here - even of
            # the lazy import itself - is logged, never silently dropped:
            # an invisible break would leave over-threshold reports open
            # and stale.
            try:
                from db._bug_reports import (
                    sweep_auto_confirm as _sweep_auto_confirm,
                )
                from db._bug_reports import (
                    sweep_retire_duplicates as _sweep_retire_duplicates,
                )

                _sweep_auto_confirm(conn)
                _sweep_retire_duplicates(conn)
            except Exception as exc:  # domain: degrade-silently - bug sweep is enrichment; boot must not fail
                logutil.log("bug_sweep_confirm_failed", error=str(exc))
        finally:
            conn.row_factory = _previous_factory
    except (
        Exception
    ):  # domain: degrade-silently - workflows are enrichment; boot must not fail
        pass

    # PR-cache index: lives here rather than schema.sql because
    # schema.sql's CREATE INDEX statements run before migrations and
    # would crash an upgraded (pre-feature) database - the
    # AGENTS.md schema-migration rule. The cache is optional
    # enrichment, so a broken index never blocks boot.
    try:
        _has_pr_rows = (
            conn.execute(
                "SELECT 1 FROM sqlite_master"
                " WHERE type = 'table' AND name = 'pr_rows' LIMIT 1"
            ).fetchone()
            is not None
        )
        if _has_pr_rows:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pr_rows_state_updated"
                " ON pr_rows(state, updated_at)"
            )
    except Exception:  # domain: degrade-silently - cache index is best-effort
        pass

    # Index hygiene (proposal #270, item 4771):
    # 1. events.category index for /events?category= filter
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_category ON events(category)")
    # 2. Simplify idx_credit_entries_agent - drop redundant PK leading col
    try:
        conn.execute("DROP INDEX IF EXISTS idx_credit_entries_agent")
        conn.execute(
            "CREATE INDEX idx_credit_entries_agent ON credit_entries(agent_id)"
        )
    except Exception:  # domain: degrade-silently - index rebuild is best-effort
        pass
    # 3. Replace low-cardinality idx_job_cycles_status with composite
    try:
        conn.execute("DROP INDEX IF EXISTS idx_job_cycles_status")
        conn.execute(
            "CREATE INDEX idx_job_cycles_job_status ON job_cycles(job_id, status)"
        )
    except Exception:  # domain: degrade-silently - index rebuild is best-effort
        pass
    # 4. Drop the legacy 3-col events index (PR #409 superseded it with the
    # covering idx_events_kind_target_created; schema.sql only adds indexes,
    # so upgraded databases kept the redundant one).
    conn.execute("DROP INDEX IF EXISTS idx_events_kind_target")
    # 5. Jobs overdue-anchor index (perf bundle 2): target-first
    # (target_type, target_id, kind, created_at) serves the per-job MAX
    # anchor probes as one range each; the kind-first covering index
    # cannot. Strictly subsumes idx_events_target, which is dropped.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_job_anchor"
        " ON events(target_type, target_id, kind, created_at)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_events_target")
    # 6. Offered-to index (perf bundle 2): fresh databases carry
    # idx_jobs_offered_to(status, offered_to_agent_id) from schema.sql;
    # databases upgraded through the legacy rebuild carry the
    # single-column variant under the same name, which still serves the
    # offered_to probe - CREATE IF NOT EXISTS keeps whichever exists.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_offered_to"
        " ON jobs(status, offered_to_agent_id)"
    )
