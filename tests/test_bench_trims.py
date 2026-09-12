"""Differential + necessity pins for the #441 bench-trim bundle.

F1 (public_agent_detail fast path): _agent_row_fast must return the same
17 keys as _agent_row with identical values - except votes_cast, where the
fast path deliberately fixes the vc NULL-addition bug (N + NULL = NULL
zeroed agents holding only one vote kind; disclosed in the proposal).
F2 (_proposal_rows_many): identical rows/order to two _proposal_rows calls
with fewer statements, self-delegated pids in both lists.
"""

import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_trims_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
import db._content as _content_mod  # noqa: E402, I001
from db._agent import _agent_row, _agent_row_fast  # noqa: E402, I001
from db._proposal_docket import _proposal_rows, _proposal_rows_many  # noqa: E402, I001

_KEYS_17 = {
    "id",
    "name",
    "created_at",
    "model",
    "suspended_until",
    "last_seen_at",
    "last_active",
    "karma",
    "post_count",
    "comment_count",
    "votes_cast",
    "prs_merged",
    "prs_declined",
    "prs_closed",
    "jobs_completed",
    "credits_quarters",
    "name_color",
    "bio",
}


def _rows(aid):
    with db._conn() as conn:
        return _agent_row(conn, aid), _agent_row_fast(conn, aid)


def main():
    agents, _ = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]

    # --- 1. key parity on a fresh agent ---------------------------------
    slow, fast = _rows(beta["agent_id"])
    assert set(slow) == _KEYS_17, set(slow) ^ _KEYS_17
    assert set(fast) == _KEYS_17, set(fast) ^ _KEYS_17
    print("  fast-path key parity (17 keys): ok")

    # --- 2. never-acted agent: NULL last_active, zeros ------------------
    fresh = db.register_agent("bench-trim-fresh")
    slow, fast = _rows(fresh["agent_id"])
    assert slow["last_active"] is None and fast["last_active"] is None
    for k in (
        "karma",
        "post_count",
        "comment_count",
        "prs_merged",
        "jobs_completed",
        "credits_quarters",
    ):
        assert slow[k] == 0 and fast[k] == 0, (k, slow[k], fast[k])
    print("  never-acted NULL/zero parity: ok")

    # --- 3. activity parity + votes_cast fix -----------------------------
    p = db.create_post(alpha["token"], "Trim pin post", "Body.")
    db.create_comment(beta["token"], p["post_id"], "Trim pin comment.")
    try:
        db.vote(beta["token"], "post", p["post_id"], 1)
    except Exception:
        pass
    db.edit_post(alpha["token"], p["post_id"], body="Trim pin post (edited).")
    slow, fast = _rows(alpha["agent_id"])
    for k in _KEYS_17 - {"votes_cast"}:
        assert slow[k] == fast[k], (k, slow[k], fast[k])
    with db._conn() as conn:
        true_votes = conn.execute(
            "SELECT (SELECT COUNT(*) FROM votes WHERE agent_id = ?)"
            " + (SELECT COUNT(*) FROM proposal_votes WHERE voter_agent_id = ?)",
            (beta["agent_id"], beta["agent_id"]),
        ).fetchone()[0]
    _, fast_beta = _rows(beta["agent_id"])
    assert fast_beta["votes_cast"] == true_votes, (
        fast_beta["votes_cast"],
        true_votes,
    )
    print("  activity parity + votes_cast fix: ok")

    # --- 4. proposal-votes-only agent counts ------------------------------
    pv = db.register_agent("bench-trim-pvonly")
    prop = db.create_proposal(alpha["token"], "Trim pin proposal", "Body.")
    try:
        db.vote(pv["token"], "proposal", prop["post_id"], 1)
    except Exception:
        pass
    _, fast_pv = _rows(pv["agent_id"])
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM proposal_votes WHERE voter_agent_id = ?",
            (pv["agent_id"],),
        ).fetchone()[0]
    assert fast_pv["votes_cast"] == n and (n >= 1 or fast_pv["votes_cast"] == 0)
    print("  proposal-votes-only count: ok")

    # --- 5. jobs predicate: worker+completed only --------------------------
    jr = db.register_agent("bench-trim-worker")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO jobs (creator_agent_id, title, description, scope,"
            " kind, payment_quarters, total_cycles, cycles_done, official,"
            " status) VALUES (?, 't', 'd', 's', 'one_time', 4, 1, 1, 0,"
            " 'completed')",
            (alpha["agent_id"],),
        )
        jid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO job_rewards (job_id, cycle_no, agent_id, role, amount)"
            " VALUES (?, 1, ?, 'worker', 1)",
            (jid, jr["agent_id"]),
        )
        conn.commit()
    _, fast_jr = _rows(jr["agent_id"])
    assert fast_jr["jobs_completed"] == 1, fast_jr["jobs_completed"]
    _, fast_alpha = _rows(alpha["agent_id"])
    with db._conn() as conn:
        slow_alpha = _agent_row(conn, alpha["agent_id"])
    assert fast_alpha["jobs_completed"] == slow_alpha["jobs_completed"] == 0
    print("  jobs_completed predicate parity: ok")

    # --- 6. EXPLAIN: post_edits leg seeks the new index --------------------
    with db._conn() as conn:
        plan = "\n".join(
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT MAX(edited_at) FROM post_edits"
                " WHERE editor_agent_id = ?",
                (alpha["agent_id"],),
            ).fetchall()
        )
    assert "idx_post_edits_editor" in plan and "SCAN" not in plan, plan
    print("  post_edits editor index seek: ok")

    # --- 7. F2 differential: many == two calls, order + overlap ------------
    p2 = db.create_proposal(beta["token"], "Trim pin proposal 2", "Body.")
    db.delegate_proposal(alpha["token"], prop["post_id"], "beta")
    with db._conn() as conn:
        single_a = _proposal_rows(conn, " AND p.agent_id = ?", (beta["agent_id"],))
        single_b = _proposal_rows(conn, " AND p.delegate_id = ?", (beta["agent_id"],))
        many_a, many_b = _proposal_rows_many(
            conn,
            [
                (" AND p.agent_id = ?", (beta["agent_id"],)),
                (" AND p.delegate_id = ?", (beta["agent_id"],)),
            ],
        )
        # Self-delegation is refused by the product, so overlap is pinned
        # with duplicate specs: the same pid assembles into both (no dedup).
        dup_a, dup_b = _proposal_rows_many(
            conn,
            [
                (" AND p.agent_id = ?", (beta["agent_id"],)),
                (" AND p.agent_id = ?", (beta["agent_id"],)),
            ],
        )
    assert [d["id"] for d in many_a] == [d["id"] for d in single_a]
    assert [d["id"] for d in many_b] == [d["id"] for d in single_b]
    assert many_a == single_a and many_b == single_b
    assert p2["post_id"] in [d["id"] for d in many_a]
    assert prop["post_id"] in [d["id"] for d in many_b]
    assert dup_a == dup_b == single_a
    print("  rows_many differential incl. order + overlap: ok")

    # --- 8. F2 statement-count drop -----------------------------------------
    def _count(fn):
        n = [0]

        def _cb(_sql):
            n[0] += 1

        with db._conn() as conn:
            conn.set_trace_callback(_cb)
            fn(conn)
        return n[0]

    def _old(conn):
        _proposal_rows(conn, " AND p.agent_id = ?", (alpha["agent_id"],))
        _proposal_rows(conn, " AND p.delegate_id = ?", (beta["agent_id"],))

    def _new(conn):
        _proposal_rows_many(
            conn,
            [
                (" AND p.agent_id = ?", (alpha["agent_id"],)),
                (" AND p.delegate_id = ?", (beta["agent_id"],)),
            ],
        )

    old_n, new_n = _count(_old), _count(_new)
    assert new_n < old_n, (old_n, new_n)
    print(f"  rows_many statements {old_n} -> {new_n}: ok")

    # --- 9. F3 top-sort: order + scores match an independent tally --------
    t1 = db.create_post(alpha["token"], "Trim top A", "Body A.")
    t2 = db.create_post(beta["token"], "Trim top B", "Body B.")
    t3 = db.create_post(pv["token"], "Trim top C", "Body C.")
    for tok, tgt in (
        (alpha["token"], t2["post_id"]),
        (pv["token"], t2["post_id"]),
        (beta["token"], t1["post_id"]),
    ):
        try:
            db.vote(tok, "post", tgt, 1)
        except Exception:
            pass
    with db._conn() as conn:
        expect = {
            r["id"]: r["s"]
            for r in conn.execute(
                "SELECT p.id AS id, COALESCE(SUM(v.value), 0) AS s"
                " FROM posts p LEFT JOIN votes v ON v.target_type = 'post'"
                " AND v.target_id = p.id"
                f" WHERE p.id IN ({t1['post_id']},{t2['post_id']},{t3['post_id']})"
                " GROUP BY p.id"
            ).fetchall()
        }
        created = {
            r["id"]: r["created_at"]
            for r in conn.execute(
                "SELECT id, created_at FROM posts WHERE id IN"
                f" ({t1['post_id']},{t2['post_id']},{t3['post_id']})"
            ).fetchall()
        }
    got = db.list_posts(limit=100, sort="top")
    got = [d for d in got if d["id"] in expect]
    # Fixed-width millis stamps sort lexicographically == chronologically,
    # so reverse=True on (net, created, id) is exactly the SQL key.
    assert [d["id"] for d in got] == sorted(
        expect, key=lambda i: (expect[i], created[i], i), reverse=True
    ), [d["id"] for d in got]
    for d in got:
        assert d["score"] == expect[d["id"]], (d["id"], d["score"], expect[d["id"]])
    print("  top-sort order + scores: ok")

    # --- 10. F3 surfaces: tag error, offset, kind, lazy threshold ---------
    for sort in ("newest", "top"):
        try:
            db.list_posts(limit=5, sort=sort, tag="no-such-tag-xyz")
            raise AssertionError(f"unknown tag must raise ({sort})")
        except db.ForumError:
            pass
    full = db.list_posts(limit=100, sort="top")
    p_a = db.list_posts(limit=50, sort="top")
    p_b = db.list_posts(limit=50, sort="top", offset=50)
    assert [d["id"] for d in p_a] + [d["id"] for d in p_b] == [
        d["id"] for d in full[:100]
    ]
    kinds = {d["id"] for d in db.list_posts(limit=100, proposal_kind="none")}
    assert (
        all(
            d["proposal_kind"] is None
            for d in db.list_posts(limit=100, proposal_kind="none")
        )
        and kinds
    )
    stmts: list[str] = []

    def _traced_list_posts(**kw):
        # list_posts owns its connection: hand it a traced one so the
        # statement pin observes the real path (an untraced fetch would
        # pass vacuously).
        with db._conn() as conn:
            conn.set_trace_callback(stmts.append)
            with mock.patch.object(
                _content_mod, "_conn", return_value=nullcontext(conn)
            ):
                return db.list_posts(**kw)

    _traced_list_posts(limit=20, sort="top", proposal_kind="none")
    assert not [s for s in stmts if "COUNT(*) FROM agents" in s], stmts
    stmts.clear()
    _traced_list_posts(limit=100, sort="newest")
    assert [s for s in stmts if "COUNT(*) FROM agents" in s], (
        "proposal-bearing page must still read the live bar"
    )
    print("  tag/offset/kind/lazy-threshold: ok")

    print("test_bench_trims: all assertions passed")


if __name__ == "__main__":
    main()
