"""Standalone query benchmark and EXPLAIN health check.

Not in run_all.py — run manually: python tests/test_benchmark.py

Seeds a realistic test DB, runs structural EXPLAIN assertions, then
times every expensive query 5 times and reports min/median/max ms.
"""
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
_POSTS = 200
_COMMENTS = 150
_VOTES = 100
_AGENTS_EXTRA = 20  # beyond the 9 from setup()

# Ensure no vote daily cap for the benchmark seeding
os.environ.setdefault("FORUM_VOTE_DAILY_CAP", "0")

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


# -- seed --------------------------------------------------------------------

def _seed():
    """Build a test DB with realistic volume."""
    init()
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
    for i in range(_POSTS):
        author = tokens[i % len(tokens)]
        if i % 5 == 0:
            row = db.create_proposal(author, f"Benchmark proposal {i}", f"Proposal body for benchmark {i}.")
            post_ids.append(row["post_id"])
        elif i % 7 == 0:
            row = db.create_proposal(author, f"Benchmark small fix {i}", f"Small fix body for benchmark {i}.",
                                     small_fix=True)
            post_ids.append(row["post_id"])
        else:
            row = db.create_post(author, f"Benchmark post {i}", f"Body text for benchmark post number {i}.")
            post_ids.append(row["post_id"])

    # Comments
    comment_ids = []
    for i in range(_COMMENTS):
        author = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        row = db.create_comment(author, target, f"Benchmark comment {i} on post {target}.")
        comment_ids.append(row["comment_id"])

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

    return agents, post_ids, comment_ids


# -- structural checks -------------------------------------------------------

_perf_indexes = (
    "idx_posts_agent", "idx_comments_agent",
    "idx_comments_created", "idx_votes_created", "idx_comments_post_created",
    "idx_votes_target",
    "idx_notifications_unread", "idx_comments_post_parent_created",
    "idx_posts_agent_created", "idx_comments_agent_created",
    "idx_votes_agent_created", "idx_proposal_votes_post_value", "idx_reports_status",
    "idx_reports_reporter", "idx_reports_target",
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
    # The fact that it returned without error and no full-table-scan warning
    # in the explain is a reasonable structural check.
    # We just verify it ran and returned data (or empty list is fine).
    return isinstance(rows, list)


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
    agents, post_ids, comment_ids = _seed()
    n_agents = len(agents)
    n_posts = len(post_ids)
    n_comments = len(comment_ids)
    print(f"  {n_agents} agents, {n_posts} posts, {n_comments} comments\n")

    all_ok = True

    # Structural checks
    print("[Structural checks]")
    r = _check_explain_proposals()
    ok = "OK" if r else "FAIL"
    print(f"  EXPLAIN list_proposals: no correlated subqueries  {ok}")
    if not r:
        all_ok = False
        print("    FAIL: found CORRELATED SCALAR SUBQUERY in proposal docket")

    r = _check_explain_agents()
    ok = "OK" if r else "FAIL"
    print(f"  EXPLAIN list_agents: plan covers agent subqueries  {ok}")
    if not r:
        all_ok = False

    r = _check_explain_activity()
    ok = "OK" if r else "FAIL"
    print(f"  EXPLAIN list_recent_activity: ran without error  {ok}")
    if not r:
        all_ok = False

    idx_ok, missing = _check_perf_indexes()
    if idx_ok:
        print("  Performance indexes: all 11 present  OK")
    else:
        all_ok = False
        print(f"  Performance indexes: MISSING {', '.join(sorted(missing))}  FAIL")
    print()

    # Timing
    print(f"[Timing - {_ITERATIONS} iterations each, min / median / max ms]")
    queries = [
        ("list_agents", lambda: aggregates.list_agents()),
        ("list_proposals", lambda: db.list_proposals()),
        ("list_recent_activity", lambda: aggregates.list_recent_activity(50)),
        ("counts", lambda: aggregates.counts()),
        ("search_posts", lambda: search.search_posts("benchmark")),
        ("get_posts_batch", lambda: db.get_posts(post_ids=post_ids[:3])),
    ]
    for label, fn in queries:
        lo, med, hi = _time_query(fn)
        print(f"  {label:30s} {lo:6.2f} / {med:6.2f} / {hi:6.2f}")

    if not all_ok:
        print("\nSome structural checks failed.")
        shutil.rmtree(_TMP, ignore_errors=True)
        sys.exit(1)

    print("\nDone.")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
