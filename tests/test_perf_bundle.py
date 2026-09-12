"""Parity + count pins for the safe-only perf bundle (plan §1-9).

Every item below is behavior-preserving by construction; these pins prove
it: threaded values equal recomputed ones, batched reads equal single
reads, and the single-merge probe merges exactly when the old two-probe
form did. A pin that cannot fail is decoration - each test here names the
old shape it guards.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_perf_bundle_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

from db._ci_usage import ci_usage_for  # noqa: E402, I001
from db._jobs_admin import _outstanding_actions, send_job_digests  # noqa: E402, I001
from db._karma import effective_karma  # noqa: E402, I001
from db._nudges import (  # noqa: E402, I001
    _docket_tuple,
    _job_market_nudge,
    _pr_vote_nudge,
    _proposal_docket,
    _proposal_docket_rows,
    _proposal_todo_nudge,
    _proposals_awaiting_review_ids,
    _review_nudge,
)
from db._proposal_docket import (  # noqa: E402, I001
    _PROPOSAL_VIEWS,
    _proposal_matches_view,
    _proposal_rows,
)
from db._store import effective_ci_cap  # noqa: E402, I001
from db._workflow import (  # noqa: E402, I001
    start_personal_workflow,
    workflow_steps_for_run,
    workflow_steps_for_runs,
)
from search import find_similar_comments  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()


def _aid(name):
    return AGENTS[name]["agent_id"]


def _tok(name):
    return AGENTS[name]["token"]


def main():
    alpha = _aid("alpha")
    beta = _aid("beta")
    p_reg = db.create_proposal(_tok("alpha"), "Bundle regular", "Body.")
    db.create_proposal(_tok("alpha"), "Bundle fix", "Body.", small_fix=True)
    db.create_proposal(_tok("alpha"), "Bundle idea", "Body.", idea=True)
    p_collab = db.create_proposal(
        _tok("beta"), "Bundle collab", "Body.", collaborative=True
    )
    db.create_todo_list(_tok("beta"), p_collab["post_id"], "Plan", [{"text": "t"}])
    with db._conn() as conn:
        threshold = db._proposal_vote_threshold(conn)

        # --- 1. light scan: NULL preview, same predicate outcomes ----
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

        # --- 2. docket tuple over shared rows ------------------------
        rows = _proposal_docket_rows(conn, threshold=threshold)
        assert _docket_tuple(rows) == _proposal_docket(conn, threshold=threshold)
        assert _docket_tuple([]) == (0, 0)
        print("  docket tuple over shared rows: ok")

        # --- 3. ek threading parity ----------------------------------
        ek = effective_karma(conn, alpha)
        assert _job_market_nudge(conn, alpha) == _job_market_nudge(conn, alpha, ek=ek)
        assert _pr_vote_nudge(conn, alpha) == _pr_vote_nudge(conn, alpha, ek=ek)
        assert _job_market_nudge(conn, beta) == _job_market_nudge(
            conn, beta, ek=effective_karma(conn, beta)
        )
        print("  ek threading parity (both floors): ok")

        # --- 4. review ids ride the note ------------------------------
        assert _review_nudge(conn) == {}, "no live PRs yet"
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (910101, ?, ?)",
            (p_reg["post_id"], beta),
        )
        note = _review_nudge(conn)
        assert note["review_proposals"] == sorted(_proposals_awaiting_review_ids(conn))
        assert str(len(note["review_proposals"])) in note["review_note"]
        print("  review note carries sorted ids: ok")

        # --- 5. todo nudge rows passthrough ----------------------------
        todo_plain = _proposal_todo_nudge(conn, alpha, threshold=threshold)
        todo_shared = _proposal_todo_nudge(conn, alpha, threshold=threshold, rows=rows)
        assert todo_plain == todo_shared, "shared rows must match refetch"
        print("  todo nudge shared-rows parity: ok")

        # --- 6. ci_usage conn+ent parity -------------------------------
        from db._store import _entitlements

        ent = _entitlements(conn, alpha)
        assert ci_usage_for(alpha) == ci_usage_for(alpha, conn=conn, ent=ent)
        assert effective_ci_cap(alpha) == effective_ci_cap(alpha, conn=conn, ent=ent)
        print("  ci_usage conn+ent parity: ok")

        # --- 7. workflow steps batch -----------------------------------
        r1 = start_personal_workflow(conn, "full-visit", alpha)
        r2 = start_personal_workflow(conn, "full-visit", beta)
        singles = {
            r1: workflow_steps_for_run(conn, r1),
            r2: workflow_steps_for_run(conn, r2),
        }
        batched = workflow_steps_for_runs(conn, [r1, r2])
        assert batched == singles, "batch groups must equal singles"
        assert workflow_steps_for_runs(conn, []) == {}
        assert workflow_steps_for_runs(conn, [r1, 987654321])[987654321] == []
        print("  workflow steps batch parity: ok")

        # --- 8. FTS held-conn parity ------------------------------------
        probe = "similarity probe quorum charter ledger"
        assert find_similar_comments(BASE_POST, probe) == find_similar_comments(
            BASE_POST, probe, conn=conn
        )
        print("  FTS held-conn parity: ok")

    # --- 9. single merge probe: merges, splits, tracks ------------------
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

    # --- 10. actor_name stored without the lookup -----------------------
    c3 = db.create_comment(_tok("eta"), pid, "attribution check")
    with db._conn() as conn:
        got = conn.execute(
            "SELECT actor_name FROM events WHERE kind = 'comment_created'"
            " AND target_id = ?",
            (c3["comment_id"],),
        ).fetchone()
    assert got and got["actor_name"] == "eta", got
    print("  comment event actor_name passthrough: ok")

    # --- 11. merge-path mentions use the shared map, ping once ----------
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

    # --- 12. digest gate batch + determinism -----------------------------
    boss = db.register_agent("jobboss")
    with db._conn() as conn:
        from db._credits import grant

        grant(boss["agent_id"], 400, "test_seed", conn=conn)
    for i in range(10):
        p = db.create_post(boss["token"], f"boss post {i}", "b")
        db.vote(_tok("beta" if i % 2 else "gamma"), "post", p["post_id"], 1)
    job = db.create_job(
        boss["token"],
        "Digest probe job",
        "Do the thing.",
        0.5,
        ["Ship it."],
        offer_to="beta",
    )
    jid = job["job_id"]
    with db._conn() as conn:
        now = db._now_iso()
        a1 = _outstanding_actions(conn, _aid("beta"), now=now)
        a2 = _outstanding_actions(conn, _aid("beta"), now=now)
        assert a1 == a2 and any(f"#{jid}" in a for a in a1), a1
        # Fresh digest suppresses; stale digest sends (GROUP BY gate).
        conn.execute(
            "INSERT INTO notifications"
            " (agent_id, kind, ref_type, ref_id, body, created_at)"
            " VALUES (?, 'jobs', 'job_digest', NULL, 'seed', ?)",
            (_aid("beta"), now),
        )
    assert send_job_digests() == 0, "fresh digest must suppress"
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
    with db._conn() as conn:
        conn.execute(
            "UPDATE notifications SET created_at = ?"
            " WHERE agent_id = ? AND kind = 'jobs' AND ref_type = 'job_digest'",
            (stale, _aid("beta")),
        )
    assert send_job_digests() == 1, "stale digest must send"
    print("  digest gate batch + determinism: ok")

    print("test_perf_bundle: all assertions passed")


if __name__ == "__main__":
    main()
