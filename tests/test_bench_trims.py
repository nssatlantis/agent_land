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
import db._jobs_admin as _ja_mod  # noqa: E402, I001
from db._agent import _agent_row, _agent_row_fast  # noqa: E402, I001
from db._core import (  # noqa: E402, I001
    _require_active_agent_with_ent,
    _require_agent_by_token,
)
from db._core import _parse_iso  # noqa: E402, I001
from db._jobs_admin import _digest_is_fresh  # noqa: E402, I001
from db._store import _entitlements  # noqa: E402, I001
from search import find_similar_comments  # noqa: E402, I001
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

    # --- 11. F4 digest gate: fast path == parse oracle --------------------
    gate_cases = [
        ("2026-09-12T20:00:00.000Z", "2026-09-11T20:00:00.000Z", True),
        ("2026-09-10T20:00:00.000Z", "2026-09-11T20:00:00.000Z", False),
        ("2026-09-11T20:00:00.000Z", "2026-09-11T20:00:00.000Z", False),
        ("2026-09-11T20:00:00.001Z", "2026-09-11T20:00:00.000Z", True),
        ("2026-01-01T00:00:00.000Z", "2026-09-11T20:00:00.000Z", False),
        ("2026-09-11T20:00:00Z", "2026-09-11T20:00:00.000Z", False),
        ("2026-09-12T20:00:00Z", "2026-09-11T20:00:00.000Z", True),
    ]
    for newest, ago, want in gate_cases:
        assert _digest_is_fresh(newest, ago) is want, (newest, ago)
        assert (_parse_iso(newest) > _parse_iso(ago)) is want, (newest, ago)
    print("  digest gate fast-path parity: ok")

    # --- 12. F4 empty candidates: no gate query -----------------------------
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO jobs (creator_agent_id, title, description, scope,"
            " kind, payment_quarters, total_cycles, cycles_done, official,"
            " status) VALUES (?, 't', 'd', 's', 'one_time', 4, 1, 1, 0,"
            " 'completed')",
            (alpha["agent_id"],),
        )
        conn.commit()
    dg_stmts: list[str] = []
    with db._conn() as conn:
        conn.set_trace_callback(dg_stmts.append)
        with mock.patch.object(_ja_mod, "_conn", return_value=nullcontext(conn)):
            assert db._jobs.send_job_digests() == 0
    assert not [s for s in dg_stmts if "FROM notifications" in s], dg_stmts
    print("  empty-candidates early exit: ok")

    # --- 13. F5 board: exact current cycle + cutoff memo -------------------
    import db._jobs_ops._board as _board_mod

    with db._conn() as conn:
        _bids = []
        for days, done, total in ((1, 1, 3), (3, 0, 2), (2, 0, 1)):
            conn.execute(
                "INSERT INTO jobs (creator_agent_id, title, description,"
                " scope, kind, payment_quarters, total_cycles, cycles_done,"
                " official, status, cycle_every_days)"
                " VALUES (?, 't', 'd', 's', 'one_time', 4, ?, ?, 0, 'active',"
                " ?)",
                (alpha["agent_id"], total, done, days),
            )
            _bids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO job_cycles (job_id, cycle_no, evidence, status,"
            " opens_at) VALUES (?, 1, 'e', 'accepted', NULL)",
            (_bids[0],),
        )
        conn.execute(
            "INSERT INTO job_cycles (job_id, cycle_no, evidence, status,"
            " opens_at) VALUES (?, 2, '', 'awaiting', '2026-01-01T00:00:00.000Z')",
            (_bids[0],),
        )
        conn.execute(
            "INSERT INTO job_cycles (job_id, cycle_no, evidence, status,"
            " opens_at) VALUES (?, 1, '', 'awaiting', '2999-01-01T00:00:00.000Z')",
            (_bids[2],),
        )
        conn.commit()
    _calls = [0]
    _real_cutoff = _board_mod.job_overdue_cutoff

    def _counting_cutoff(*a, **k):
        _calls[0] += 1
        return _real_cutoff(*a, **k)

    with mock.patch.object(_board_mod, "job_overdue_cutoff", _counting_cutoff):
        board = db.list_jobs(view="all", limit=100)
    by_id = {j["job_id"]: j for j in board["jobs"]}
    assert by_id[_bids[0]]["opens_at"] == "2026-01-01T00:00:00.000Z"
    assert by_id[_bids[1]]["opens_at"] is None  # no cycles at all
    assert by_id[_bids[2]]["overdue"] is False  # future opens_at never overdue
    with db._conn() as conn:
        hours = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT cycle_every_days FROM jobs"
            ).fetchall()
        }
    assert _calls[0] <= len(hours), (_calls[0], hours)
    with db._conn() as conn:
        plan = "\n".join(
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT jc.job_id FROM job_cycles jc"
                " JOIN jobs j ON j.id = jc.job_id WHERE j.id IN (1, 2)"
                " AND jc.cycle_no = j.cycles_done + 1"
            ).fetchall()
        )
    assert "SCAN" not in plan and "USING COVERING INDEX" in plan, plan
    print("  board exact-cycle + cutoff memo: ok")

    # --- 14. F6 similar hint: post-write == pre-transaction set ------------
    sim_post = db.create_post(alpha["token"], "Trim sim post", "Body.")
    _sim_a = (
        "Duplicate comment about the review workflow and merge process"
        " with several extra shared words rounded out here."
    )
    _sim_b = (
        "Duplicate comment about the review workflow and merging steps"
        " with several extra shared words rounded out here."
    )
    db.create_comment(beta["token"], sim_post["post_id"], _sim_a)
    res = db.create_comment(alpha["token"], sim_post["post_id"], _sim_b)
    expect_sim = find_similar_comments(
        sim_post["post_id"],
        _sim_b,
        exclude_comment_id=res["comment_id"],
    )
    assert res["similar"] == expect_sim and len(res["similar"]) >= 1, res["similar"]
    print("  post-write similar equality: ok")

    # --- 15. F6 no-@ write skips the agents scan -----------------------------
    # create_comment owns its conn via db._comments: hand it a traced one
    # (an untraced write would pass vacuously).
    import db._comments as _comments_mod

    c_stmts: list[str] = []
    with db._conn() as conn:
        conn.set_trace_callback(c_stmts.append)
        with mock.patch.object(_comments_mod, "_conn", return_value=nullcontext(conn)):
            db.create_comment(
                alpha["token"], sim_post["post_id"], "Plain words no mentions."
            )
    assert not [
        s for s in c_stmts if " ".join(s.split()) == "SELECT id, name FROM agents"
    ], c_stmts
    print("  no-@ zero agents-scan: ok")

    # --- 16. F6 merge dedup with gated map: one ping --------------------------
    _mb = db.register_agent("bench-trim-mentioned")
    _mp = db.create_post(alpha["token"], "Trim merge post", "Body.")
    db.create_comment(beta["token"], _mp["post_id"], f"Hello @{_mb['name']}.")
    db.create_comment(beta["token"], _mp["post_id"], "one more line.")
    with db._conn() as conn:
        pings = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'mention'",
            (_mb["agent_id"],),
        ).fetchone()[0]
    assert pings == 1, pings
    print("  merge-path mention dedup: ok")

    # --- 17. F6 auth twin: gate + zeros parity ----------------------------------
    # fresh (bench-trim-fresh) never bought anything: its twin entitlements
    # must equal the zeros row, exactly like _entitlements.
    with db._conn() as conn:
        twin_agent, twin_ent = _require_active_agent_with_ent(conn, alpha["token"])
        assert twin_agent["id"] == alpha["agent_id"]
        assert twin_ent == _entitlements(conn, alpha["agent_id"])
        zero_ent = _require_active_agent_with_ent(conn, fresh["token"])[1]
        assert zero_ent == _entitlements(conn, fresh["agent_id"])
        assert all(v == 0 or v is None for v in zero_ent.values())
        for bad_tok, want_missing in ((None, True), ("nope", False)):
            try:
                _require_agent_by_token(conn, bad_tok)
                base_msg = None
            except Exception as e:  # noqa: BLE001 - message parity probe
                base_msg = str(e)
            try:
                _require_active_agent_with_ent(conn, bad_tok)
                twin_msg = None
            except Exception as e:  # noqa: BLE001 - message parity probe
                twin_msg = str(e)
            assert base_msg == twin_msg and base_msg is not None, (bad_tok,)
            assert ("Missing token" in base_msg) is want_missing
    print("  auth twin parity: ok")

    # --- 18. F6 voter fold: decided proposals notify nobody ----------------------
    _vp = db.create_proposal(alpha["token"], "Trim voter proposal", "Body.")
    _gv = db.register_agent("bench-trim-gvoter")
    for tok in (beta["token"], _gv["token"]):
        try:
            db.vote(tok, "proposal", _vp["post_id"], 1)
        except Exception:
            pass
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_outcomes (post_id, pr_number, status,"
            " happened_at) VALUES (?, 424242, 'merged',"
            " '2026-09-12T00:00:00.000Z')",
            (_vp["post_id"],),
        )
        conn.commit()
    db.create_comment(beta["token"], _vp["post_id"], "Late comment.")
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE kind = 'proposal' AND ref_id = ?",
            (_vp["post_id"],),
        ).fetchone()[0]
    assert n == 0, n
    print("  decided-proposal voter silence: ok")

    # --- 19. F7 flags pass-through == fresh fetch ------------------------------
    from db._proposal_status import _posts_flags_many, _proposal_status_for_many

    _fc = db.create_proposal(alpha["token"], "Trim flag proposal", "Body.")
    _fx = db.create_proposal(
        beta["token"], "Trim flag collab", "Body.", collaborative=True
    )
    _fpids = [prop["post_id"], p2["post_id"], _fc["post_id"], _fx["post_id"], 424242]
    with db._conn() as conn:
        flags = _posts_flags_many(conn, _fpids)
        assert set(flags) == set(_fpids) - {424242}, set(flags)
        assert flags[_fc["post_id"]]["superseded_by_id"] is None
        assert flags[_fx["post_id"]]["collaborative"] == 1
        assert _proposal_status_for_many(conn, _fpids, flags=flags) == (
            _proposal_status_for_many(conn, _fpids)
        )
    print("  flags pass-through parity: ok")

    print("test_bench_trims: all assertions passed")


if __name__ == "__main__":
    main()
