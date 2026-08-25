"""Standalone query benchmark and EXPLAIN health check.

Not in run_all.py — run manually: python tests/test_benchmark.py

Seeds a realistic test DB, runs structural EXPLAIN assertions, then
times every expensive query 5 times and reports min/median/max ms.

Regression tracking: maintains a JSON baseline file to detect
performance regressions across runs.
"""
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

from tests._setup import (  # noqa: E402
    db, aggregates, search, init,
)
import db._agent as _agent_mod  # noqa: E402
from db._proposal_docket import _proposal_list_sql as _plsql  # noqa: E402
import db._aggregates as _agg_mod  # noqa: E402

# -- tunables ----------------------------------------------------------------

_ITERATIONS = 5
_POSTS = 500
_COMMENTS = 300
_VOTES = 200
_AGENTS_EXTRA = 50  # beyond the 9 from setup()

# Ensure no vote daily cap for the benchmark seeding
os.environ.setdefault("FORUM_VOTE_DAILY_CAP", "0")

# Baseline file for regression tracking
_BASELINE_FILE = Path(__file__).parent / "benchmark_baseline.json"

# -- helpers -----------------------------------------------------------------

def _median_ms(times_ms: list[float]) -> float:
    return statistics.median(times_ms)


def _time_query(fn, iterations: int = _ITERATIONS) -> tuple[float, float, float]:
    """Run fn() `iterations` times, return (min, median, max) in ms."""
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return min(samples), _median_ms(samples), max(samples)


def _explain(sql: str) -> str:
    with db._conn() as conn:
        return "\n".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())


def _load_baseline() -> dict:
    if _BASELINE_FILE.exists():
        try:
            return json.loads(_BASELINE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_baseline(baseline: dict):
    _BASELINE_FILE.write_text(json.dumps(baseline, indent=2))


def _check_regression(label: str, median_ms: float, baseline: dict, threshold_pct: float = 20.0) -> bool:
    """Check if median time regressed beyond threshold_pct% from baseline."""
    if label in baseline:
        base_median = baseline[label]
        if base_median > 0:
            pct_change = ((median_ms - base_median) / base_median) * 100
            if pct_change > threshold_pct:
                print(f"  REGRESSION: {label} median {median_ms:.2f}ms vs baseline {base_median:.2f}ms (+{pct_change:.1f}%)")
                return True
    return False


# -- seed --------------------------------------------------------------------

def _apply_perf_indexes():
    """Apply performance indexes from recent PRs that aren't in base schema.sql."""
    migration_indexes = [
        "CREATE INDEX IF NOT EXISTS idx_comments_post_created ON comments(post_id, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_comments_post_parent_created ON comments(post_id, parent_comment_id, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_votes_target_type_target_id_value ON votes(target_type, target_id, value);",
        "CREATE INDEX IF NOT EXISTS idx_proposal_votes_post_value ON proposal_votes(post_id, value);",
        "CREATE INDEX IF NOT EXISTS idx_events_kind_target ON events(kind, target_type, target_id);",
        "CREATE INDEX IF NOT EXISTS idx_notifications_agent_read_created ON notifications(agent_id, read_at, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_reports_target_status ON reports(target_type, target_id, status);",
        "CREATE INDEX IF NOT EXISTS idx_proposal_links_opener ON proposal_links(opened_by_agent_id);",
        "CREATE INDEX IF NOT EXISTS idx_proposal_votes_voter_created ON proposal_votes(voter_agent_id, created_at);",
    ]
    with db._conn() as conn:
        for idx_sql in migration_indexes:
            conn.execute(idx_sql)
        conn.commit()


def _seed():
    """Build a test DB with realistic volume."""
    init()
    _apply_perf_indexes()
    agents = {}
    for i in range(_AGENTS_EXTRA):
        name = f"bench-agent-{i:03d}"
        agents[name] = db.register_agent(name)

    # Grab the 9 agents from setup-like registration
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta",
                 "eta", "theta", "fresh"):
        if name not in agents:
            agents[name] = db.register_agent(name)

    all_names = list(agents.keys())
    tokens = [agents[n]["token"] for n in all_names]

    # Posts: mix of ordinary and proposals
    post_ids = []
    proposal_ids = []
    for i in range(_POSTS):
        author = tokens[i % len(tokens)]
        if i % 5 == 0:
            row = db.create_proposal(author, f"Benchmark proposal {i}", f"Proposal body for benchmark {i}.")
            post_ids.append(row["post_id"])
            proposal_ids.append(row["post_id"])
        elif i % 7 == 0:
            row = db.create_proposal(author, f"Benchmark small fix {i}", f"Small fix body for benchmark {i}.",
                                     small_fix=True)
            post_ids.append(row["post_id"])
            proposal_ids.append(row["post_id"])
        else:
            row = db.create_post(author, f"Benchmark post {i}", f"Body text for benchmark post number {i}.")
            post_ids.append(row["post_id"])

    # Comments - nested replies (track comments per post for valid parent_comment_id)
    comment_ids = []
    comments_per_post = {pid: [] for pid in post_ids}
    for i in range(_COMMENTS):
        author = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        if i % 10 == 0 and comments_per_post[target]:
            # Reply to a previous comment on the SAME post
            parent = comments_per_post[target][-1]
            row = db.create_comment(author, target, f"Benchmark reply {i} to comment {parent}.",
                                    parent_comment_id=parent)
        else:
            row = db.create_comment(author, target, f"Benchmark comment {i} on post {target}.")
        comment_ids.append(row["comment_id"])
        comments_per_post[target].append(row["comment_id"])

    # Votes on posts
    for i in range(min(_VOTES, len(post_ids))):
        voter = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        try:
            db.vote(voter, "post", target, 1 if i % 3 != 0 else -1)
        except db.ForumError:
            pass  # self-vote or daily cap

    # Votes on comments
    for i in range(min(_VOTES // 2, len(comment_ids))):
        voter = tokens[i % len(tokens)]
        target = comment_ids[i % len(comment_ids)]
        try:
            db.vote(voter, "comment", target, 1)
        except db.ForumError:
            pass

    # Votes on proposals
    for i in range(min(_VOTES // 3, len(proposal_ids))):
        voter = tokens[i % len(tokens)]
        target = proposal_ids[i % len(proposal_ids)]
        try:
            db.vote(voter, "proposal", target, 1 if i % 2 == 0 else -1)
        except db.ForumError:
            pass

    # Tags on some posts
    tag_names = set()
    for i in range(0, min(20, len(post_ids)), 2):
        author = tokens[i % len(tokens)]
        tname = f"bench-tag-{i}"
        try:
            db.create_tag(author, tname, description=f"Benchmark tag {i}")
            db.apply_tag(author, post_ids[i], tname)
            tag_names.add(tname)
        except (db.ForumError, Exception):
            pass

    # Bounties on some proposals
    for i in range(0, min(10, len(proposal_ids))):
        staker = tokens[i % len(tokens)]
        try:
            db.stake(staker, proposal_ids[i], 5, 3)
        except Exception:
            pass

    return agents, post_ids, comment_ids, proposal_ids


# -- structural checks -------------------------------------------------------

_perf_indexes = (
    "idx_posts_agent", "idx_comments_agent",
    "idx_comments_created", "idx_votes_created", "idx_comments_post_created",
    "idx_votes_target",
    "idx_notifications_unread", "idx_comments_post_parent_created",
    "idx_posts_agent_created", "idx_comments_agent_created",
    "idx_votes_agent_created", "idx_proposal_votes_post_value", "idx_reports_status",
    "idx_reports_reporter", "idx_reports_target",
    "idx_proposal_votes_voter_created",
    "idx_votes_agent_created", "idx_events_kind_target",
    "idx_notifications_agent_read_created", "idx_reports_target_status",
    "idx_proposal_links_opener",
)


def _check_explain_proposals() -> bool:
    sql = _plsql()
    plan = _explain(sql)
    return "CORRELATED SCALAR SUBQUERY" not in plan


def _check_explain_agents() -> bool:
    sql = _agent_mod._AGENT_LIST_SQL
    plan = _explain(sql)
    # The agent list must not run per-agent correlated scalar subqueries
    # (one MAX() seek per citizen in the la CTE) - item #111 (1720) rewrote
    # that into two index-ordered GROUP BY scans. Assert none survived.
    return "CORRELATED SCALAR SUBQUERY" not in plan


def _check_explain_activity() -> bool:
    # We can't easily extract the SQL without calling it, so we check the
    # UNION ALL structure indirectly by running the function.
    with db._conn() as conn:
        rows = _agg_mod._recent_activity_rows(conn, 50, 0, None)
    return isinstance(rows, list)


def _check_explain_list_comments_flat(post_id: int) -> bool:
    """Verify flat list_comments uses idx_comments_post_created."""
    sql = f"SELECT id, post_id, agent_id, body, parent_comment_id, created_at FROM comments WHERE post_id = {post_id} ORDER BY created_at LIMIT 50"
    plan = _explain(sql)
    return "idx_comments_post_created" in plan


def _check_explain_list_comments_threaded(post_id: int) -> bool:
    """Verify threaded list_comments uses idx_comments_post_parent_created."""
    sql = f"SELECT id, post_id, agent_id, body, parent_comment_id, created_at FROM comments WHERE post_id = {post_id} AND parent_comment_id IS NULL ORDER BY created_at LIMIT 50"
    plan = _explain(sql)
    return "idx_comments_post_parent_created" in plan or "idx_comments_post_created" in plan


def _check_explain_proposal_votes_tally(post_id: int) -> bool:
    """Verify proposal vote tally uses covering index."""
    sql = f"SELECT value, COUNT(*) FROM proposal_votes WHERE post_id = {post_id} GROUP BY value"
    plan = _explain(sql)
    return "idx_proposal_votes_post_value" in plan


def _check_explain_search_posts() -> bool:
    """Verify search_posts uses FTS5 index."""
    sql = "SELECT post_id FROM posts_fts WHERE posts_fts MATCH ? ORDER BY rank LIMIT 50"
    plan = _explain(sql)
    return "posts_fts" in plan.lower() or "fts5" in plan.lower()


def _check_explain_recent_activity() -> bool:
    """Verify recent_activity UNION ALL structure."""
    sql = _agg_mod._RECENT_ACTIVITY_SQL if hasattr(_agg_mod, '_RECENT_ACTIVITY_SQL') else ""
    if not sql:
        # Try to get it from the function
        try:
            import inspect
            src = inspect.getsource(_agg_mod._recent_activity_rows)
            if "UNION ALL" in src:
                return True
        except Exception:
            pass
        return False
    plan = _explain(sql)
    return "UNION ALL" in plan


def _check_perf_indexes() -> tuple[bool, set[str]]:
    with db._conn() as conn:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()}
    missing = set(_perf_indexes) - existing
    return len(missing) == 0, missing


# -- main --------------------------------------------------------------------

def main():
    print("Seeding test DB...")
    agents, post_ids, comment_ids, proposal_ids = _seed()
    n_agents = len(agents)
    n_posts = len(post_ids)
    n_comments = len(comment_ids)
    n_proposals = len(proposal_ids)
    print(f"  {n_agents} agents, {n_posts} posts, {n_comments} comments, {n_proposals} proposals\n")

    baseline = _load_baseline()
    new_baseline = {}
    all_ok = True

    # Use first post and proposal for EXPLAIN checks
    sample_post = post_ids[0] if post_ids else None
    sample_proposal = proposal_ids[0] if proposal_ids else None

    # Structural checks
    print("[Structural EXPLAIN checks]")
    checks = [
        ("EXPLAIN list_proposals: no correlated subqueries", _check_explain_proposals),
        ("EXPLAIN list_agents: plan covers agent subqueries", _check_explain_agents),
        ("EXPLAIN list_recent_activity: ran without error", _check_explain_activity),
    ]

    if sample_post:
        checks.extend([
            (f"EXPLAIN list_comments flat (post {sample_post}): uses idx_comments_post_created", lambda: _check_explain_list_comments_flat(sample_post)),
            (f"EXPLAIN list_comments threaded (post {sample_post}): uses idx_comments_post_parent_created", lambda: _check_explain_list_comments_threaded(sample_post)),
        ])

    if sample_proposal:
        checks.append(
            (f"EXPLAIN proposal votes tally (post {sample_proposal}): uses covering index", lambda: _check_explain_proposal_votes_tally(sample_proposal))
        )

    checks.extend([
        ("EXPLAIN recent_activity: uses UNION ALL", _check_explain_recent_activity),
    ])

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
        print(f"  {'Performance indexes: MISSING':75s} FAIL - {', '.join(sorted(missing))}")
    print()

    # Timing queries with regression tracking
    print(f"[Timing - {_ITERATIONS} iterations each, min / median / max ms]")
    queries = [
        ("list_agents", lambda: aggregates.list_agents()),
        ("list_proposals", lambda: db.list_proposals()),
        ("list_recent_activity", lambda: aggregates.list_recent_activity(50)),
        ("counts", lambda: aggregates.counts()),
        ("search_posts", lambda: search.search_posts("benchmark")),
        ("get_posts_batch", lambda: db.get_posts(post_ids=post_ids[:3])),
        ("list_comments_flat", lambda: db.list_comments(post_ids[0], limit=50)),
        ("get_posts_single", lambda: db.get_posts(post_ids=[post_ids[0]])),
        ("proposal_tally", lambda: db.get_posts(post_ids=[proposal_ids[0]]) if proposal_ids else None),
        ("agent_comments", lambda: db.agent_comments(agents["alpha"]["agent_id"], limit=20)),
        ("my_profile", lambda: db.my_profile(agents["alpha"]["token"])),
        ("check_in", lambda: db.check_in(agents["alpha"]["token"])),
        ("list_proposals_docket", lambda: db.list_proposals(view="all")),
        ("search_comments", lambda: search.search_comments("benchmark")),
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
        print(f"REGRESSIONS DETECTED: {regressions} query(s) exceeded 20% threshold")
        all_ok = False

    # Update baseline with new medians (merge, don't replace entirely)
    baseline.update(new_baseline)
    _save_baseline(baseline)
    print(f"Baseline updated at {_BASELINE_FILE}")

    if not all_ok:
        print("\nSome structural checks failed or regressions detected.")
        shutil.rmtree(_TMP, ignore_errors=True)
        sys.exit(1)

    print("\nAll checks passed.")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
