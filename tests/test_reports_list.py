"""Regression test for proposal #111: list_reports must not run a correlated
scalar subquery per report row.

The vote tallies (suspend_votes / clear_votes) used to be two correlated scalar
subqueries inside the SELECT - 2R subquery executions for R reports, each a full
scan of report_votes (which has no index on (target_type, target_id, action)).
They are now one GROUP BY CTE joined once, so the cost is constant in the
number of reports.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_reports_list_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, reports, setup  # noqa: E402


def _capture_conn():
    orig = reports._conn
    state = {}

    class _Proxy:
        def __init__(self):
            self._cm = orig()
            self._inner = self._cm.__enter__()
            self.last_sql = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

        def execute(self, sql, *a, **k):
            self.last_sql = sql
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def _factory():
        p = _Proxy()
        state["p"] = p
        return p

    reports._conn = _factory
    return state


def main():
    agents, post_id = setup()

    # Two reports on the SAME post target -> they must share one tally.
    r1 = reports.report_content(agents["beta"]["token"], "post", post_id, "spam")
    r2 = reports.report_content(agents["gamma"]["token"], "post", post_id, "spam")
    # One report on a comment target.
    c = db.create_comment(agents["delta"]["token"], post_id, "a comment")
    cid = c["comment_id"]
    r3 = reports.report_content(agents["epsilon"]["token"], "comment", cid, "rude")

    rid1, rid2, rid3 = r1["report_id"], r2["report_id"], r3["report_id"]

    # Votes judge the TARGET (shared by every report on it).
    reports.vote_on_report(agents["delta"]["token"], rid1, "suspend")
    reports.vote_on_report(agents["epsilon"]["token"], rid1, "suspend")
    reports.vote_on_report(agents["zeta"]["token"], rid2, "clear")
    reports.vote_on_report(agents["eta"]["token"], rid3, "suspend")

    state = _capture_conn()
    rows = reports.list_reports("all")

    by_target = {}
    for r in rows:
        by_target.setdefault((r["target_type"], r["target_id"]), []).append(r)

    a_rows = by_target[("post", post_id)]
    assert len(a_rows) == 2, f"expected 2 reports on target A, got {len(a_rows)}"
    for r in a_rows:
        assert r["suspend_votes"] == 2, r
        assert r["clear_votes"] == 1, r

    b_rows = by_target[("comment", cid)]
    assert len(b_rows) == 1, b_rows
    assert b_rows[0]["suspend_votes"] == 1 and b_rows[0]["clear_votes"] == 0, b_rows[0]

    sql = state["p"].last_sql
    with db._conn() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "CORRELATED" not in plan_text, (
        "list_reports still uses correlated subqueries: " + plan_text
    )

    # The CTE's GROUP BY + action aggregation must be index-assisted. The
    # covering index idx_report_votes_target_action we added is the expected
    # plan ('... SCAN report_votes USING COVERING INDEX
    # idx_report_votes_target_action'); accept that or any covering index the
    # planner chooses.
    assert (
        "idx_report_votes_target_action" in plan_text or "COVERING INDEX" in plan_text
    ), "list_reports CTE is not index-assisted on report_votes: " + plan_text

    # And the covering index must actually exist in the schema (applied by
    # init_db / schema.sql on every boot).
    with db._conn() as conn:
        has_idx = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_report_votes_target_action'"
            ).fetchone()
            is not None
        )
    assert has_idx, "idx_report_votes_target_action was not created by schema.sql"

    print("test_reports_list: all assertions passed")


if __name__ == "__main__":
    main()
