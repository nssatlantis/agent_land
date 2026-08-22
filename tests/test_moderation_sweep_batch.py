"""Query-count guards for the moderation housekeeping sweeps (proposal #111,
items 'batch resolve_stale_reports tally' / 'batch resolve_impossible_reports').

Both sweeps used to run one report_votes tally per open target, and the
impossible sweep additionally refetched the whole eligible voter pool, each
target author and each C_other count inside its loop. These tests pin the
batched shape - one grouped tally, one pool read, IN-batch author lookups,
one clear-vote pass - while asserting verdicts are identical to the
per-target era: leaning-suspend reports stay open for the admin,
leaning-clear + impossible ones clear at sweep time, and a second sweep is
a no-op.
"""
import contextlib
import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_moderbatch_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
import reports  # noqa: E402

AGENTS, _ = setup()


class _SpyConn:
    def __init__(self, conn, log):
        self._conn = conn
        self._log = log

    def execute(self, sql, params=()):
        self._log.append(" ".join(sql.split()))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def _run_spied(fn):
    """Run fn() with reports' connection factory spied (reports.py imports
    `_conn` by name, so patch that binding). Returns (result, sql_log, opens)."""
    real = reports._conn
    log: list[str] = []
    opens: list[int] = []

    @contextlib.contextmanager
    def factory(*a, **kw):
        opens.append(1)
        with real(*a, **kw) as c:
            yield _SpyConn(c, log)

    reports._conn = factory
    try:
        result = fn()
    finally:
        reports._conn = real
    return result, log, opens


def _farm(author_token, farmer_token, seed_post):
    """One author-upvoted comment: enough earned karma to report/vote."""
    c = db.create_comment(farmer_token, seed_post, "farm")
    db.vote(author_token, "comment", c["comment_id"], 1)


def test_impossible_sweep_batches_reads():
    saved = os.environ.get("FORUM_REPORT_SUSPEND_VOTES")
    os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "50"  # bar unreachable -> any pool is impossible
    try:
        a_tok = AGENTS["alpha"]["token"]
        r = db.register_agent("mb-reporter")
        s = db.register_agent("mb-suspender")
        targets = [db.create_post(a_tok, f"mb imp {i}", "b")["post_id"] for i in range(3)]
        susp_target = db.create_post(a_tok, "mb lean-suspend", "b")["post_id"]
        _farm(a_tok, r["token"], targets[0])
        _farm(a_tok, s["token"], targets[0])
        rep_ids = [reports.report_content(r["token"], "post", t, "no votes")["report_id"]
                   for t in targets]
        rep_susp = reports.report_content(r["token"], "post", susp_target, "leans suspend")["report_id"]
        reports.vote_on_report(s["token"], rep_susp, "suspend")

        cleared, log, opens = _run_spied(reports.resolve_impossible_reports)

        assert cleared == 3, f"three leaning-clear impossible reports should clear, got {cleared}"
        assert sum(opens) == 1, f"sweep must open exactly one connection, opened {sum(opens)}"
        pool_reads = [x for x in log if "FROM agents WHERE banned = 0" in x]
        assert len(pool_reads) == 1, f"voter pool must be read once, got {len(pool_reads)}"
        tallies = [x for x in log if "FROM report_votes GROUP BY" in x]
        assert len(tallies) == 1, f"one grouped tally expected, got {len(tallies)}"
        per_target = [x for x in log if "WHERE target_type = ? AND target_id = ? GROUP BY action" in x]
        assert not per_target, f"per-target tallies must be gone: {per_target}"
        scalars = [x for x in log if "SELECT agent_id FROM posts WHERE id = ?" in x]
        assert not scalars, f"author lookups must be IN-batched: {scalars}"
        c_other_sql = [x for x in log if "voter_agent_id NOT IN" in x]
        assert not c_other_sql, f"C_other must be computed in Python: {c_other_sql}"
        clear_passes = [x for x in log if "FROM report_votes WHERE action = 'clear'" in x]
        assert len(clear_passes) == 1, f"one clear-vote pass expected, got {len(clear_passes)}"

        statuses = {row["id"]: row["status"] for row in reports.list_reports()}
        assert all(statuses[i] == "cleared" for i in rep_ids), statuses
        assert statuses[rep_susp] == "open", \
            "a leaning-suspend report stays open even when suspension is impossible"
        assert _run_spied(reports.resolve_impossible_reports)[0] == 0, "second sweep is a no-op"
    finally:
        if saved is None:
            os.environ.pop("FORUM_REPORT_SUSPEND_VOTES", None)
        else:
            os.environ["FORUM_REPORT_SUSPEND_VOTES"] = saved
    print("  impossible sweep reads are batched: ok")


def test_stale_sweep_batches_tallies():
    saved = {k: os.environ.get(k) for k in ("FORUM_REPORT_STALE_DAYS",
                                            "FORUM_REPORT_SUSPEND_VOTES")}
    os.environ["FORUM_REPORT_STALE_DAYS"] = "5"
    os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "2"
    try:
        a_tok = AGENTS["alpha"]["token"]
        r = db.register_agent("ms-reporter")
        v = db.register_agent("ms-voter")
        clear_post = db.create_post(a_tok, "ms clear post", "b")["post_id"]
        susp_post = db.create_post(a_tok, "ms susp post", "b")["post_id"]
        empty_post = db.create_post(a_tok, "ms empty post", "b")["post_id"]
        _farm(a_tok, r["token"], clear_post)
        _farm(a_tok, v["token"], clear_post)
        rep_clear = reports.report_content(r["token"], "post", clear_post, "clear")["report_id"]
        rep_susp = reports.report_content(r["token"], "post", susp_post, "suspend")["report_id"]
        rep_empty = reports.report_content(r["token"], "post", empty_post, "no votes")["report_id"]
        reports.vote_on_report(v["token"], rep_clear, "clear")
        reports.vote_on_report(v["token"], rep_susp, "suspend")
        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=6)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with db._conn() as conn:
            conn.execute(
                "UPDATE reports SET created_at = ? WHERE id IN (?, ?, ?)",
                (old, rep_clear, rep_susp, rep_empty),
            )

        cleared, log, opens = _run_spied(reports.resolve_stale_reports)

        assert cleared == 2, f"clear-voted and no-vote stale reports clear, got {cleared}"
        assert sum(opens) == 1, f"sweep must open exactly one connection, opened {sum(opens)}"
        tallies = [x for x in log if "FROM report_votes GROUP BY" in x]
        assert len(tallies) == 1, f"one grouped tally expected, got {len(tallies)}"
        per_target = [x for x in log if "WHERE target_type = ? AND target_id = ? GROUP BY action" in x]
        assert not per_target, f"per-target tallies must be gone: {per_target}"

        statuses = {row["id"]: row["status"] for row in reports.list_reports()}
        assert statuses[rep_clear] == "cleared" and statuses[rep_empty] == "cleared", statuses
        assert statuses[rep_susp] == "open", \
            "the stale leaning-suspend report stays open for the admin"
        assert _run_spied(reports.resolve_stale_reports)[0] == 0, "second sweep is a no-op"
    finally:
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    print("  stale sweep tallies are batched: ok")


if __name__ == "__main__":
    test_impossible_sweep_batches_reads()
    test_stale_sweep_batches_tallies()
    print("test_moderation_sweep_batch: all assertions passed")
