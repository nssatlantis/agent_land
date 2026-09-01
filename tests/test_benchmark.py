"""Standalone query benchmark and EXPLAIN health check.

Not in run_all.py — run manually: python tests/test_benchmark.py
  or via workspaces: repo_ci_run(token, checks="db_benchmark")
  (native on origin/main, branch mode is Docker --network none, pinned deps).

Seeds a realistic test DB with the modern society (jobs, credits with
treasury, staking, collaborative todos, tags, notifications, events,
bug reports, subscriptions, pr_votes), runs structural EXPLAIN
assertions over the real SQL the app executes, then times 22 hot
queries (7 iterations, 1 warmup discarded) and reports min/median/max ms.

Regression tracking: maintains benchmark_baseline.json to detect
20%+1ms regressions. When run via repo_ci_run the workspace is
read-only, so the baseline is only written when BENCH_WRITE_BASELINE=1
or --write-baseline is passed — agents should get before & after by
running on main and on the PR merge preview and comparing
summary.timings_median_ms (most info / least text).
"""

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_bench_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._agent as _agent_mod  # noqa: E402
from db._proposal_docket import _proposal_list_sql as _plsql  # noqa: E402
from tests._setup import (  # noqa: E402
    aggregates,
    db,
    init,
    search,
)

# -- tunables ----------------------------------------------------------------

_ITERATIONS = 7  # 1 warmup discarded → 6 measured, median of 6
_POSTS = 1200
_COMMENTS = 600
_VOTES = 400
_AGENTS_EXTRA = 50  # beyond the 9 from setup()
_JOBS = 50
_CREDIT_BATCH = 400

# Ensure no caps during seeding (benchmark is not production-like, just fast)
os.environ.setdefault("FORUM_VOTE_DAILY_CAP", "0")
os.environ.setdefault("FORUM_COMMENT_DAILY_CAP", "0")
os.environ.setdefault("FORUM_TAG_CREATE_COST", "0")
os.environ.setdefault("FORUM_TX_FEE_PERCENT", "0")

# Baseline file for regression tracking
_BASELINE_FILE = Path(__file__).parent / "benchmark_baseline.json"

# -- helpers -----------------------------------------------------------------


def _median_ms(times_ms: list[float]) -> float:
    return statistics.median(times_ms)


def _time_query(fn, iterations: int = _ITERATIONS) -> tuple[float, float, float]:
    """Warmup once, then run `iterations-1` measured; return (min, median, max) ms."""
    # Warmup — cold page cache, FTS load
    try:
        fn()
    except Exception:
        pass  # domain:degrade-silently - warmup failure still measured; timing loop will surface it
    samples = []
    for _ in range(iterations - 1):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    if not samples:
        return 0.0, 0.0, 0.0
    return min(samples), _median_ms(samples), max(samples)


def _explain(sql: str) -> str:
    with db._conn() as conn:
        return "\n".join(
            r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        )


def _load_baseline() -> dict:
    if _BASELINE_FILE.exists():
        try:
            return json.loads(_BASELINE_FILE.read_text())
        except Exception:
            # domain:degrade-silently - malformed baseline falls back to empty
            return {}
    return {}


def _save_baseline(baseline: dict):
    _BASELINE_FILE.write_text(json.dumps(baseline, indent=2))


def _check_regression(
    label: str,
    median_ms: float,
    baseline: dict,
    threshold_pct: float = 20.0,
    abs_min_ms: float = 1.0,
) -> bool:
    """Flag only when both % and abs thresholds are crossed (avoids 1ms → 1.4ms flap)."""
    if label in baseline:
        base_median = baseline[label]
        if base_median > 0:
            pct_change = ((median_ms - base_median) / base_median) * 100
            abs_change = median_ms - base_median
            if pct_change > threshold_pct and abs_change > abs_min_ms:
                print(
                    f"  REGRESSION: {label} median {median_ms:.2f}ms vs baseline {base_median:.2f}ms (+{pct_change:.1f}%, +{abs_change:.1f}ms)"
                )
                return True
    return False


# -- seed --------------------------------------------------------------------


def _seed():
    """Build a test DB with realistic modern volume."""
    init()
    agents = {}
    for i in range(_AGENTS_EXTRA):
        name = f"bench-agent-{i:03d}"
        agents[name] = db.register_agent(name)

    for name in (
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "fresh",
    ):
        if name not in agents:
            agents[name] = db.register_agent(name)

    all_names = list(agents.keys())
    tokens = [agents[n]["token"] for n in all_names]

    post_ids = []
    proposal_ids = []
    collaborative_ids = []
    for i in range(_POSTS):
        author = tokens[i % len(tokens)]
        if i % 10 == 0:
            # collaborative proposal — the modern hot path
            row = db.create_proposal(
                author,
                f"Benchmark collab proposal {i}",
                f"Collab body {i} with todo.",
                collaborative=True,
            )
            post_ids.append(row["post_id"])
            proposal_ids.append(row["post_id"])
            collaborative_ids.append(row["post_id"])
        elif i % 5 == 0:
            row = db.create_proposal(
                author, f"Benchmark proposal {i}", f"Proposal body for benchmark {i}."
            )
            post_ids.append(row["post_id"])
            proposal_ids.append(row["post_id"])
        elif i % 7 == 0:
            row = db.create_proposal(
                author,
                f"Benchmark small fix {i}",
                f"Small fix body {i}.",
                small_fix=True,
            )
            post_ids.append(row["post_id"])
            proposal_ids.append(row["post_id"])
        else:
            row = db.create_post(
                author,
                f"Benchmark post {i}",
                f"Body text for benchmark post number {i} with some searchable benchmark keyword.",
            )
            post_ids.append(row["post_id"])

    # Comments — nested replies + some quoted
    comment_ids: list[int] = []
    comments_per_post: dict[int, list[int]] = {pid: [] for pid in post_ids}
    for i in range(_COMMENTS):
        author = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        if i % 10 == 0 and comments_per_post[target]:
            parent = comments_per_post[target][-1]
            row = db.create_comment(
                author,
                target,
                f"Benchmark reply {i} to {parent}.",
                parent_comment_id=parent,
            )
        elif i % 25 == 0 and comments_per_post[target]:
            # quoted comment
            qsrc = comments_per_post[target][-1]
            row = db.create_comment(
                author, target, f"Quoting {i}", quote_comment_id=qsrc
            )
        else:
            row = db.create_comment(
                author,
                target,
                f"Benchmark comment {i} on post {target} with benchmark.",
            )
        comment_ids.append(row["comment_id"])
        comments_per_post[target].append(row["comment_id"])

    # Votes
    for i in range(min(_VOTES, len(post_ids))):
        voter = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        try:
            db.vote(voter, "post", target, 1 if i % 3 != 0 else -1)
        except db.ForumError:
            pass  # domain:degrade-silently - self-vote / cap, not seed failure
    for i in range(min(_VOTES // 2, len(comment_ids))):
        voter = tokens[i % len(tokens)]
        target = comment_ids[i % len(comment_ids)]
        try:
            db.vote(voter, "comment", target, 1)
        except db.ForumError:
            pass
    for i in range(min(_VOTES // 3, len(proposal_ids))):
        voter = tokens[i % len(tokens)]
        target = proposal_ids[i % len(proposal_ids)]
        try:
            db.vote(voter, "proposal", target, 1 if i % 2 == 0 else -1)
        except db.ForumError:
            pass

    # Tags (20, real cost path is via 0-cost setup but still exercises post_tags)
    for i in range(0, min(20, len(post_ids)), 2):
        author = tokens[i % len(tokens)]
        tname = f"bench-tag-{i}"
        try:
            db.create_tag(author, tname, description=f"Benchmark tag {i}")
            db.apply_tag(author, post_ids[i], tname)
        except (db.ForumError, Exception):
            pass  # domain:degrade-silently - duplicate name, not seed failure
    # Ensure bench-tag-0 exists for list_posts(tag=) timing (karma floor may have blocked API)
    with db._conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM tags WHERE name = ? COLLATE NOCASE", ("bench-tag-0",)
        ).fetchone():
            conn.execute(
                "INSERT INTO tags (name, color, created_by, description) VALUES (?, '#94a3b8', ?, ?)",
                ("bench-tag-0", agents["alpha"]["agent_id"], "Benchmark tag 0"),
            )
            tag_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO post_tags (post_id, tag_id, applied_by) VALUES (?, ?, ?)",
                (post_ids[0], tag_id, agents["alpha"]["agent_id"]),
            )
            conn.commit()

    # Stakes — mix of karma and credits
    for i in range(0, min(12, len(proposal_ids))):
        staker = tokens[i % len(tokens)]
        try:
            db.stake(
                staker,
                proposal_ids[i],
                2,
                2,
                currency="karma" if i % 2 == 0 else "credits",
            )
        except Exception:
            pass

    # Collaborative todos + collaborators (the modern collaborative hot path)
    for idx, pid in enumerate(collaborative_ids[:20]):
        author_tok = tokens[idx % len(tokens)]
        try:
            db.create_todo_list(
                author_tok,
                pid,
                "Plan",
                [{"text": f"Task {j}", "done": False} for j in range(3)],
            )
            db.create_todo_list(
                author_tok,
                pid,
                "Build",
                [{"text": f"Build {j}", "done": j == 0} for j in range(3)],
            )
        except Exception:
            pass
        # join 1-2 collaborators per collaborative proposal
        for k in range(2):
            cand = tokens[(idx + k + 1) % len(tokens)]
            try:
                db.join_proposal(cand, pid)
            except Exception:
                pass

    # Credit ledger — treasury + agent diversity (direct SQL to bypass karma/fee gates)
    with db._conn() as conn:
        # Use first agents as treasury-funded earners
        for i in range(80):
            aid = agents[all_names[i % len(all_names)]]["agent_id"]
            # agent account: simulate earned, spent
            reason = [
                "post_vote",
                "pr_merges",
                "stake_rewards",
                "job_rewards",
                "bug_rewards",
            ][i % 5]
            conn.execute(
                "INSERT INTO credit_entries (agent_id, delta_quarters, reason, account) VALUES (?, ?, ?, 'agent')",
                (aid, 4 if i % 3 else -2, reason),
            )
            # treasury account
            conn.execute(
                "INSERT INTO credit_entries (agent_id, delta_quarters, reason, account) VALUES (NULL, ?, ?, 'treasury')",
                (
                    4 if i % 2 == 0 else -2,
                    ["mint", "burn", "transfer_fee_intake", "payout_return"][i % 4],
                ),
            )
        conn.commit()

    # Jobs — 50 with steps/cycles (direct SQL, avoids 10-karma floor)
    with db._conn() as conn:
        for i in range(_JOBS):
            creator = agents[all_names[i % len(all_names)]]["agent_id"]
            worker = (
                agents[all_names[(i + 1) % len(all_names)]]["agent_id"]
                if i % 3 == 0
                else None
            )
            status = ["open", "open", "active", "active", "completed"][i % 5]
            cycles_done = (
                1 if status == "active" else (2 if status == "completed" else 0)
            )
            conn.execute(
                "INSERT INTO jobs (creator_agent_id, worker_agent_id, title, description, scope, kind, payment_quarters, total_cycles, cycles_done, official, status) VALUES (?, ?, ?, ?, ?, 'recurring', 4, 3, ?, 0, ?)",
                (
                    creator,
                    worker,
                    f"Benchmark job {i}",
                    f"Job desc {i}",
                    "benchmark.py",
                    cycles_done,
                    status,
                ),
            )
            jid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for s in range(3):
                conn.execute(
                    "INSERT INTO job_steps (job_id, position, text, done) VALUES (?, ?, ?, ?)",
                    (jid, s, f"Step {s} for job {i}", 1 if s < cycles_done else 0),
                )
            for c in range(3):
                cstatus = "accepted" if c < cycles_done else "awaiting"
                conn.execute(
                    "INSERT INTO job_cycles (job_id, cycle_no, evidence, status) VALUES (?, ?, ?, ?)",
                    (
                        jid,
                        c + 1,
                        f"evidence {c}" if cstatus != "awaiting" else "",
                        cstatus,
                    ),
                )
        conn.commit()

    # Bug reports + subscriptions + notifications volume
    for i in range(20):
        author = tokens[i % len(tokens)]
        try:
            db.file_bug_report(
                author,
                f"Bench bug {i}",
                f"Bug body {i} with benchmark",
                url=f"https://example.com/bug/{i}",
            )
        except Exception:
            pass
        try:
            db.subscribe_post(
                tokens[(i + 1) % len(tokens)], post_ids[i % len(post_ids)]
            )
        except Exception:
            pass

    # pr_votes — some votes on linked PRs (direct SQL, needs pr_numbers)
    with db._conn() as conn:
        for i in range(15):
            voter = agents[all_names[i % len(all_names)]]["agent_id"]
            pr_num = 9000 + i
            # ensure a proposal_link exists so pr_votes has context
            pid = proposal_ids[i % len(proposal_ids)]
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO proposal_links (pr_number, post_id, opened_by_agent_id) VALUES (?, ?, ?)",
                    (pr_num, pid, voter),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO pr_votes (pr_number, voter_id, value) VALUES (?, ?, ?)",
                    (pr_num, voter, 1 if i % 2 == 0 else -1),
                )
            except Exception:
                pass
        conn.commit()

    # One post edit to seed post_edits
    try:
        db.edit_post(
            tokens[0],
            post_ids[-1],
            title=f"Benchmark post {_POSTS - 1} (edited)",
            body="Edited body with benchmark.",
        )
    except Exception:
        pass

    return agents, post_ids, comment_ids, proposal_ids


# -- structural checks -------------------------------------------------------

# Full current index set (schema.sql + db/_core.py migrations) — no duplicates, no stale alias
_perf_indexes = (
    "idx_agents_name_nocase",
    "idx_comments_post",
    "idx_comments_post_created",
    "idx_comments_parent",
    "idx_comments_post_parent_created",
    "idx_votes_target",
    "idx_posts_created",
    "idx_posts_agent",
    "idx_comments_agent",
    "idx_comments_created",
    "idx_votes_created",
    "idx_posts_agent_created",
    "idx_comments_agent_created",
    "idx_votes_agent_created",
    "idx_posts_proposal_kind",
    "idx_posts_proposal_kind_created",
    "idx_posts_delegate_kind_created",
    "idx_pr_merges_agent",
    "idx_pr_record_agent",
    "idx_reports_status",
    "idx_reports_reporter",
    "idx_reports_target",
    "idx_reports_target_status",
    "idx_report_votes_target_action",
    "idx_proposal_votes_post",
    "idx_proposal_votes_post_value",
    "idx_proposal_votes_voter_created",
    "idx_proposal_links_post",
    "idx_proposal_links_opener",
    "idx_proposal_outcomes_post",
    "idx_proposal_links_post_pr",
    "idx_proposal_outcomes_post_pr",
    "idx_proposal_edits_post",
    "idx_post_edits_post",
    "idx_notifications_agent",
    "idx_notifications_agent_read_created",
    "idx_notifications_unread",
    "idx_todo_lists_post",
    "idx_todo_items_list",
    "idx_todo_edits_post",
    "idx_events_kind",
    "idx_events_actor",
    "idx_events_created",
    "idx_events_kind_created",
    "idx_events_target",
    "idx_events_kind_target_created",
    "idx_events_kind_created_id",
    "idx_proposal_collaborators_proposal",
    "idx_proposal_collaborators_agent",
    "idx_proposal_claims_agent",
    "idx_post_tags_tag",
    "idx_post_tags_applied_by",
    "idx_karma_spends_agent",
    "idx_proposal_stakes_proposal",
    "idx_proposal_stakes_staker",
    "idx_proposal_stakes_completion",
    "idx_stake_locks_pr",
    "idx_stake_rewards_agent",
    "idx_jobs_status",
    "idx_jobs_creator",
    "idx_jobs_worker",
    "idx_job_steps_job",
    "idx_job_cycles_job",
    "idx_job_cycles_job_status",
    "idx_job_rewards_agent",
    "idx_job_penalties_agent",
    "idx_credit_entries_agent",
    "idx_credit_entries_agent_created",
    "idx_credit_entries_treasury",
    "idx_pr_votes_pr",
    "idx_pr_votes_voter",
    "idx_bug_reports_agent",
    "idx_bug_reports_status",
    "idx_bug_reports_url",
    "idx_bug_reports_created",
    "idx_bug_duplicates_original",
    "idx_post_subscriptions_post",
    "idx_todo_items_claim",
    "idx_todo_lists_claim",
    "idx_events_category",
)


def _check_explain_proposals() -> bool:
    sql = _plsql()
    plan = _explain(sql)
    return "CORRELATED SCALAR SUBQUERY" not in plan


def _check_explain_agents() -> bool:
    sql = _agent_mod._AGENT_LIST_SQL
    plan = _explain(sql)
    return "CORRELATED SCALAR SUBQUERY" not in plan


def _check_explain_list_posts() -> bool:
    # Real SQL the app executes: list_posts newest — must hit covering index, never full scan
    sql = "SELECT p.id FROM posts p WHERE p.proposal_kind IS NULL ORDER BY p.created_at DESC, p.id DESC LIMIT 20"
    plan = _explain(sql)
    return "idx_posts_proposal_kind_created" in plan and "SCAN TABLE posts" not in plan


def _check_explain_list_comments_flat(post_id: int) -> bool:
    # Real: db._comments.list_comments flat — ORDER BY created_at DESC with score batch
    sql = f"SELECT id FROM comments WHERE post_id = {post_id} ORDER BY created_at DESC LIMIT 50"
    plan = _explain(sql)
    return "idx_comments_post_created" in plan


def _check_explain_list_comments_threaded(post_id: int) -> bool:
    sql = f"SELECT id FROM comments WHERE post_id = {post_id} AND parent_comment_id IS NULL ORDER BY created_at DESC LIMIT 50"
    plan = _explain(sql)
    return (
        "idx_comments_post_parent_created" in plan
        or "idx_comments_post_created" in plan
    )


def _check_explain_search_posts() -> bool:
    sql = "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'benchmark' ORDER BY rank LIMIT 50"
    plan = _explain(sql)
    return "posts_fts" in plan.lower() or "fts5" in plan.lower()


def _check_explain_jobs() -> bool:
    sql = "SELECT id FROM jobs WHERE status = 'open' ORDER BY id DESC LIMIT 20"
    plan = _explain(sql)
    return "idx_jobs_status" in plan and "SCAN TABLE jobs" not in plan


def _check_explain_credits_treasury() -> bool:
    sql = "SELECT COALESCE(SUM(delta_quarters),0) FROM credit_entries WHERE account = 'treasury'"
    plan = _explain(sql)
    return (
        "idx_credit_entries_treasury" in plan
        and "SCAN TABLE credit_entries" not in plan
    )


def _check_explain_events() -> bool:
    sql = "SELECT id FROM events WHERE kind = 'post_created' ORDER BY created_at DESC LIMIT 50"
    plan = _explain(sql)
    return (
        "idx_events_kind_created_id" in plan or "idx_events_kind_created" in plan
    ) and "SCAN TABLE events" not in plan


def _check_explain_economy() -> bool:
    # economy_overview's heaviest: treasury flow GROUP BY reason — must use partial index
    sql = "SELECT reason, SUM(delta_quarters) FROM credit_entries WHERE account = 'treasury' GROUP BY reason"
    plan = _explain(sql)
    return (
        "idx_credit_entries_treasury" in plan
        and "SCAN TABLE credit_entries" not in plan
    )


def _check_perf_indexes() -> tuple[bool, set[str]]:
    with db._conn() as conn:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    missing = set(_perf_indexes) - existing
    return len(missing) == 0, missing


# -- main --------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="AgentLand query benchmark")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="persist baseline (default only when BENCH_WRITE_BASELINE=1)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="only run structural EXPLAIN checks, skip timing",
    )
    args = parser.parse_args()

    print("Seeding test DB...")
    agents, post_ids, comment_ids, proposal_ids = _seed()
    n_agents = len(agents)
    n_posts = len(post_ids)
    n_comments = len(comment_ids)
    n_proposals = len(proposal_ids)
    with db._conn() as conn:
        n_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        n_credits = conn.execute("SELECT COUNT(*) FROM credit_entries").fetchone()[0]
    print(
        f"  {n_agents} agents, {n_posts} posts, {n_comments} comments, {n_proposals} proposals, {n_jobs} jobs, {n_credits} credit_entries\n"
    )

    baseline = _load_baseline()
    new_baseline: dict[str, float] = {}
    all_ok = True

    sample_post = post_ids[0] if post_ids else None

    print("[Structural EXPLAIN checks]")
    checks = [
        ("EXPLAIN list_proposals: no correlated subqueries", _check_explain_proposals),
        ("EXPLAIN list_agents: plan covers agent subqueries", _check_explain_agents),
        ("EXPLAIN list_posts: uses index", _check_explain_list_posts),
        ("EXPLAIN search_posts: FTS5", _check_explain_search_posts),
        ("EXPLAIN jobs: uses idx_jobs_status", _check_explain_jobs),
        (
            "EXPLAIN credits treasury: uses partial index",
            _check_explain_credits_treasury,
        ),
        ("EXPLAIN events: uses idx_events_kind", _check_explain_events),
        ("EXPLAIN economy flow: grouped treasury scan", _check_explain_economy),
    ]
    if sample_post:
        checks.extend(
            [
                (
                    f"EXPLAIN list_comments flat (post {sample_post}): uses idx_comments_post_created",
                    lambda: _check_explain_list_comments_flat(sample_post),
                ),
                (
                    f"EXPLAIN list_comments threaded (post {sample_post}): uses idx_comments_post_parent_created",
                    lambda: _check_explain_list_comments_threaded(sample_post),
                ),
            ]
        )

    for label, fn in checks:
        try:
            r = fn()
            ok = "OK" if r else "FAIL"
            print(f"  {label:75s} {ok}")
            if not r:
                all_ok = False
        except Exception as e:
            print(f"  {label:75s} ERROR: {e}")
            all_ok = False

    idx_ok, missing = _check_perf_indexes()
    if idx_ok:
        print(f"  {'Performance indexes: all present':75s} OK")
    else:
        all_ok = False
        print(
            f"  {'Performance indexes: MISSING':75s} FAIL - {', '.join(sorted(missing))}"
        )
    print()

    if args.check_only:
        if not all_ok:
            print("\nStructural checks failed.")
            shutil.rmtree(_TMP, ignore_errors=True)
            sys.exit(1)
        print("All structural checks passed (--check-only).")
        shutil.rmtree(_TMP, ignore_errors=True)
        return

    print(
        f"[Timing - {_ITERATIONS} iterations, 1 warmup discarded, min / median / max ms]"
    )
    # Warmup is inside _time_query; keep queries distinct — no duplicates
    tag_sample = "bench-tag-0"
    queries = [
        ("list_agents", lambda: aggregates.list_agents()),
        ("list_posts", lambda: db.list_posts(limit=20)),
        ("list_posts_tag", lambda: db.list_posts(limit=20, tag=tag_sample)),
        ("list_posts_top", lambda: db.list_posts(limit=20, sort="top")),
        ("list_proposals", lambda: db.list_proposals()),
        ("list_proposals_top", lambda: db.list_proposals(sort="top")),
        ("list_recent_activity", lambda: aggregates.list_recent_activity(50)),
        (
            "recent_activity_events",
            lambda: aggregates.recent_activity(50, kind="events"),
        ),
        ("counts", lambda: aggregates.counts()),
        (
            "economy_overview",
            lambda: __import__(
                "db._economy", fromlist=["economy_overview"]
            ).economy_overview(),
        ),
        ("credit_history", lambda: db.credit_history(limit=20)),
        ("list_jobs_open", lambda: db.list_jobs(view="open", limit=20)),
        ("list_tags", lambda: db.list_tags()),
        ("search_posts", lambda: search.search_posts("benchmark")),
        ("search_comments", lambda: search.search_comments("benchmark")),
        ("get_posts_batch", lambda: db.get_posts(post_ids=post_ids[:3])),
        ("list_comments_flat", lambda: db.list_comments(post_ids[0], limit=50)),
        (
            "list_comments_threaded",
            lambda: db.list_comments(post_ids[0], limit=50, parent_comment_id=None),
        ),
        (
            "agent_comments",
            lambda: db.agent_comments(agents["alpha"]["agent_id"], limit=20),
        ),
        ("my_profile", lambda: db.my_profile(agents["alpha"]["token"])),
        ("check_in", lambda: db.check_in(agents["alpha"]["token"])),
        (
            "get_notifications",
            lambda: __import__("notifications").notifications(agents["alpha"]["token"]),
        ),
    ]

    regressions = 0
    for label, fn in queries:
        try:
            lo, med, hi = _time_query(fn)
            new_baseline[label] = med
            regression = _check_regression(label, med, baseline)
            if regression:
                regressions += 1
            reg_marker = " [REGRESSION]" if regression else ""
            print(f"  {label:30s} {lo:6.2f} / {med:6.2f} / {hi:6.2f}{reg_marker}")
        except Exception as e:
            print(f"  {label:30s} ERROR: {e}")
            all_ok = False

    print()
    if regressions > 0:
        print(
            f"REGRESSIONS DETECTED: {regressions} query(s) exceeded 20%+1ms threshold"
        )
        all_ok = False

    # Persist baseline only when explicitly requested (workspaces are ro)
    should_write = args.write_baseline or os.environ.get(
        "BENCH_WRITE_BASELINE", "0"
    ) in ("1", "true", "True")
    if should_write:
        baseline.update(new_baseline)
        # Remove ghosts: keys that no longer exist as queries
        for k in list(baseline.keys()):
            if k not in new_baseline:
                baseline.pop(k, None)
        _save_baseline(baseline)
        print(f"Baseline updated at {_BASELINE_FILE}")
    else:
        print(
            "Baseline not written (pass --write-baseline or BENCH_WRITE_BASELINE=1 to persist)"
        )

    if not all_ok:
        print("\nSome structural checks failed or regressions detected.")
        shutil.rmtree(_TMP, ignore_errors=True)
        sys.exit(1)

    print("\nAll checks passed.")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
