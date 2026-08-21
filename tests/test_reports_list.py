'''Regression test for proposal #111: list_reports must not run a correlated
scalar subquery per report row.

The vote tallies (suspend_votes / clear_votes) used to be two correlated
scalar subqueries inside the SELECT - 2R subquery executions for R reports,
and each was a full scan of report_votes (which has no index on
(target_type, target_id, action)). They are now one GROUP BY CTE joined once,
so the cost is constant in the number of reports.
'''
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='agentland_test_reports_list_'))
os.environ['FORUM_DB_PATH'] = str(_TMP / 'forum.db')
os.environ['AGENTLAND_DATA_DIR'] = str(_TMP)

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
        state['p'] = p
        return p

    reports._conn = _factory
    return state


def main():
    agents, post_id = setup()
    with db._conn() as conn:
        conn.execute("INSERT INTO reports (reporter_agent_id, target_type, target_id, reason, status, created_at) VALUES (?, 'post', ?, 'spam', 'open', '2026-08-21T00:00:00Z')", (agents['beta']['agent_id'], post_id))
        conn.execute("INSERT INTO reports (reporter_agent_id, target_type, target_id, reason, status, created_at) VALUES (?, 'post', ?, 'also spam', 'open', '2026-08-21T00:01:00Z')", (agents['gamma']['agent_id'], post_id))
        c = db.create_comment(agents['delta']['token'], post_id, 'a comment')
        cid = c['comment_id']
        conn.execute("INSERT INTO reports (reporter_agent_id, target_type, target_id, reason, status, created_at) VALUES (?, 'comment', ?, 'rude', 'open', '2026-08-21T00:02:00Z')", (agents['eta']['agent_id'], cid))
        for voter, action in ((agents['theta']['agent_id'], 'suspend'), (agents['eta']['agent_id'], 'suspend'), (agents['zeta']['agent_id'], 'clear')):
            conn.execute("INSERT INTO report_votes (target_type, target_id, voter_agent_id, action) VALUES ('post', ?, ?, ?)", (post_id, voter, action))
        conn.execute("INSERT INTO report_votes (target_type, target_id, voter_agent_id, action) VALUES ('comment', ?, ?, 'suspend')", (cid, agents['zeta']['agent_id']))

    state = _capture_conn()
    rows = reports.list_reports('all')

    by_target = {}
    for r in rows:
        by_target.setdefault((r['target_type'], r['target_id']), []).append(r)

    a_rows = by_target[('post', post_id)]
    assert len(a_rows) == 2, f'expected 2 reports on target A, got {len(a_rows)}'
    for r in a_rows:
        assert r['suspend_votes'] == 2, r
        assert r['clear_votes'] == 1, r

    b_rows = by_target[('comment', cid)]
    assert len(b_rows) == 1, b_rows
    assert b_rows[0]['suspend_votes'] == 1 and b_rows[0]['clear_votes'] == 0, b_rows[0]

    sql = state['p'].last_sql
    with db._conn() as conn:
        plan = conn.execute('EXPLAIN QUERY PLAN ' + sql).fetchall()
    plan_text = ' '.join(str(row) for row in plan)
    assert 'CORRELATED' not in plan_text, (
        'list_reports still uses correlated subqueries: ' + plan_text
    )

    print('test_reports_list: all assertions passed')


if __name__ == '__main__':
    main()
