"""Parity + count pins for the trimmed perf bundle (post-#1173).

Keeps only what #1169-#1173 did not subsume: the counts-scan NULL preview,
the ci entitlements threading, the workflow-steps batch, and the single
merge probe (+ its merge-path mention dedup). Items dropped at the merge:
shared docket rows/tuple, ek threading, review-ids note, todo-rows
passthrough (all in #1173), held-conn FTS (decided the other way in #433),
actor threading (in #432), digest batching (in #433). A pin that cannot
fail is decoration - each test here names the old shape it guards.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_perf_bundle_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

from db._ci_usage import ci_usage_for  # noqa: E402, I001
from db._proposal_docket import (  # noqa: E402, I001
    _PROPOSAL_VIEWS,
    _proposal_list_sql,
    _proposal_matches_view,
    _proposal_rows,
)
from db._store import effective_ci_cap  # noqa: E402, I001
from db._workflow import (  # noqa: E402, I001
    start_personal_workflow,
    workflow_steps_for_run,
    workflow_steps_for_runs,
)

db.init_db()

AGENTS, BASE_POST = setup()


def _aid(name):
    return AGENTS[name]["agent_id"]


def _tok(name):
    return AGENTS[name]["token"]


def main():
    alpha = _aid("alpha")
    db.create_proposal(_tok("alpha"), "Bundle regular", "Body.")
    db.create_proposal(_tok("alpha"), "Bundle fix", "Body.", small_fix=True)
    db.create_proposal(_tok("alpha"), "Bundle idea", "Body.", idea=True)
    with db._conn() as conn:
        threshold = db._proposal_vote_threshold(conn)

        # --- 1. light scan: narrow shape, same predicate outcomes ----
        light_sql = _proposal_list_sql(for_counts=True)
        assert "NULL AS body_preview" in light_sql
        assert "substr(p.body" not in light_sql, "counts must skip the substr()"
        assert "LEFT JOIN agents" not in light_sql, "counts drop display joins"
        assert "LEFT JOIN posts" not in light_sql, "counts drop lineage join"
        assert "proposal_claims pc" in light_sql, "unclaimed tab needs claim id"
        assert "ORDER BY" not in light_sql, "counts callers re-sort survivors"
        assert "substr(p.body" in _proposal_list_sql()
        light = _proposal_rows(conn, "", (), for_counts=True, threshold=threshold)
        full = _proposal_rows(conn, "", (), threshold=threshold)
        assert light and full and len(light) == len(full)
        assert all(p["body_preview"] is None for p in light), (
            "counts rows must skip the substr() preview"
        )
        assert all(p["body_preview"] for p in full), "display rows keep the preview"
        for v in _PROPOSAL_VIEWS:
            a = sorted(p["id"] for p in light if _proposal_matches_view(p, v))
            b = sorted(p["id"] for p in full if _proposal_matches_view(p, v))
            assert a == b, f"view {v}: light/full predicate disagree"
        print("  light scan NULL-preview + view parity: ok")

        # --- 2. ci_usage conn+ent parity -------------------------------
        from db._store import _entitlements

        ent = _entitlements(conn, alpha)
        assert ci_usage_for(alpha) == ci_usage_for(alpha, conn=conn, ent=ent)
        assert effective_ci_cap(alpha) == effective_ci_cap(alpha, conn=conn, ent=ent)
        print("  ci_usage conn+ent parity: ok")

        # --- 3. workflow steps batch -----------------------------------
        r1 = start_personal_workflow(conn, "full-visit", alpha)
        r2 = start_personal_workflow(conn, "full-visit", _aid("beta"))
        singles = {
            r1: workflow_steps_for_run(conn, r1),
            r2: workflow_steps_for_run(conn, r2),
        }
        batched = workflow_steps_for_runs(conn, [r1, r2])
        assert batched == singles, "batch groups must equal singles"
        assert workflow_steps_for_runs(conn, []) == {}
        assert workflow_steps_for_runs(conn, [r1, 987654321])[987654321] == []
        print("  workflow steps batch parity: ok")

    # --- 4. single merge probe: merges, splits, tracks ------------------
    post = db.create_post(_tok("gamma"), "Merge probe post", "Body.")
    pid = post["post_id"]
    c1 = db.create_comment(_tok("delta"), pid, "first piece")
    assert c1.get("merged") is not True
    c2 = db.create_comment(_tok("delta"), pid, "second piece")
    assert c2.get("merged") is True and c2["comment_id"] == c1["comment_id"], (
        "back-to-back self comments must merge (single probe)"
    )
    # Interleaving breaks the merge: newest row is another citizen's.
    d1 = db.create_comment(_tok("epsilon"), pid, "eps one")
    db.create_comment(_tok("zeta"), pid, "zeta cuts in")
    d2 = db.create_comment(_tok("epsilon"), pid, "eps two")
    assert d2.get("merged") is not True, "interleaved track must not merge"
    assert d2["comment_id"] != d1["comment_id"]
    # Reply track is separate from the top-level track.
    r1 = db.create_comment(
        _tok("delta"), pid, "reply piece", parent_comment_id=c1["comment_id"]
    )
    r2 = db.create_comment(
        _tok("delta"), pid, "reply piece two", parent_comment_id=c1["comment_id"]
    )
    assert r2.get("merged") is True and r2["comment_id"] == r1["comment_id"]
    print("  single merge probe (merge/split/tracks): ok")

    # --- 5. merge-path mentions use the shared map, ping once ----------
    mpost = db.create_post(_tok("gamma"), "Mention merge post", "Body.")
    mpid = mpost["post_id"]
    db.create_comment(_tok("theta"), mpid, "hello @beta")
    m2 = db.create_comment(_tok("theta"), mpid, "and hello @delta")
    assert m2.get("merged") is True
    assert sorted(m["agent_id"] for m in m2["mentioned"]) == [_aid("delta")], (
        "only NEW mentions ping on merge"
    )
    with db._conn() as conn:
        n_beta = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'mention' AND ref_id = ?",
            (_aid("beta"), m2["comment_id"]),
        ).fetchone()[0]
    assert n_beta == 1, "beta pinged once, not re-pinged on merge"
    print("  merge-path mention dedup: ok")

    print("test_perf_bundle: all assertions passed")


if __name__ == "__main__":
    main()
