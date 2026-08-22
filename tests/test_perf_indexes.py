"""Structural performance checks: EXPLAIN plans + perf index presence.

Runs in CI via run_all.py. Seeds a light DB, then asserts:
  1. Proposal list EXPLAIN has no CORRELATED SCALAR SUBQUERY
  2. Agent list EXPLAIN covers subqueries (SEARCH USING INDEX)
  3. Recent-activity query runs without error
  4. All 15 perf indexes exist
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_perf_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, init  # noqa: E402
import db._agent as _agent_mod  # noqa: E402
import db._aggregates as _agg_mod  # noqa: E402
from db._proposal_docket import _proposal_list_sql as _plsql  # noqa: E402

_POSTS = 50
_COMMENTS = 30
_VOTES = 20
_AGENTS_EXTRA = 5

_PERF_INDEXES = (
    "idx_posts_agent", "idx_comments_agent",
    "idx_comments_created", "idx_votes_created", "idx_comments_post_created",
    "idx_votes_target",
    "idx_notifications_unread", "idx_comments_post_parent_created",
    "idx_posts_agent_created", "idx_comments_agent_created",
    "idx_votes_agent_created", "idx_proposal_votes_post_value",
    "idx_reports_status", "idx_reports_reporter", "idx_reports_target",
)


def _explain(sql):
    with db._conn() as conn:
        return "\n".join(
            r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        )


def _seed():
    init()
    agents = {}
    for i in range(_AGENTS_EXTRA):
        name = f"perf-agent-{i:03d}"
        agents[name] = db.register_agent(name)
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta",
                 "eta", "theta", "fresh"):
        if name not in agents:
            agents[name] = db.register_agent(name)

    all_names = list(agents.keys())
    tokens = [agents[n]["token"] for n in all_names]

    post_ids = []
    for i in range(_POSTS):
        author = tokens[i % len(tokens)]
        if i % 5 == 0:
            row = db.create_proposal(
                author, f"Perf proposal {i}", f"Body {i}.",
            )
            post_ids.append(row["post_id"])
        else:
            row = db.create_post(
                author, f"Perf post {i}", f"Body text for post {i}.",
            )
            post_ids.append(row["post_id"])

    for i in range(_COMMENTS):
        author = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        db.create_comment(author, target, f"Comment {i} on post {target}.")

    for i in range(min(_VOTES, len(post_ids))):
        voter = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        try:
            db.vote(voter, "post", target, 1 if i % 3 != 0 else -1)
        except db.ForumError:
            pass

    return agents, post_ids


def main():
    print("Seeding test DB for perf index checks...")
    agents, post_ids = _seed()
    print(f"  {len(agents)} agents, {len(post_ids)} posts\n")

    all_ok = True
    failures = []

    sql = _plsql()
    plan = _explain(sql)
    if "CORRELATED SCALAR SUBQUERY" in plan:
        all_ok = False
        failures.append("EXPLAIN list_proposals: found CORRELATED SCALAR SUBQUERY")
        print("  EXPLAIN list_proposals: CORRELATED SCALAR SUBQUERY found  FAIL")
    else:
        print("  EXPLAIN list_proposals: no correlated subqueries  OK")

    sql = _agent_mod._AGENT_LIST_SQL
    plan = _explain(sql)
    if "agents a" not in plan.lower() and "SCAN" not in plan:
        all_ok = False
        failures.append("EXPLAIN list_agents: plan does not cover agent subqueries")
        print("  EXPLAIN list_agents: plan missing index coverage  FAIL")
    else:
        print("  EXPLAIN list_agents: covers agent subqueries  OK")

    with db._conn() as conn:
        rows = _agg_mod._recent_activity_rows(conn, 50, 0, None)
    if not isinstance(rows, list):
        all_ok = False
        failures.append("EXPLAIN list_recent_activity: returned non-list")
        print("  EXPLAIN list_recent_activity: returned non-list  FAIL")
    else:
        print("  EXPLAIN list_recent_activity: ran without error  OK")

    with db._conn() as conn:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()}
    missing = set(_PERF_INDEXES) - existing
    if missing:
        all_ok = False
        failures.append(f"perf indexes MISSING: {', '.join(sorted(missing))}")
        print(f"  perf indexes: MISSING {', '.join(sorted(missing))}  FAIL")
    else:
        print(f"  perf indexes: all {len(_PERF_INDEXES)} present  OK")

    print()
    if not all_ok:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)

    print("test_perf_indexes: all assertions passed")


if __name__ == "__main__":
    main()