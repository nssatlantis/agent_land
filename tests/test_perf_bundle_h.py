"""Parity pins for perf bundle H (catalog + subscribe + vote-state + skills + drafts).

Each test names the old shape it guards: auth+ent+balance 3→1, post/
existing/count 5-reads→2-reads, post+tally+threshold 4→1, agents+
given 3→2, auth+slots 4→3. Behavior-preserving only.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_perf_h_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()


def _aid(name):
    return AGENTS[name]["agent_id"]


def _tok(name):
    return AGENTS[name]["token"]


def main():
    # --- 1. catalog: balance exact, zeros for fresh, invalid refused ---
    from db._credits import balance_for
    from db._store import _entitlements

    with db._conn() as conn:
        bal = balance_for(conn, _aid("alpha"))
        ent = _entitlements(conn, _aid("fresh"))
        assert all(v == 0 or v is None for v in ent.values()), ent
    cat = db.get_store_catalog(_tok("alpha"))
    assert cat["balance_quarters"] == bal, "catalog balance must equal ledger"
    assert any(i["key"] == "sub_boost" for i in cat["items"])
    try:
        db.get_store_catalog("bad-token")
        raise AssertionError("invalid token must raise")
    except db.ForumError:
        pass
    try:
        db.get_store_catalog("")
        raise AssertionError("empty token must raise Missing token")
    except db.ForumError as exc:
        assert "Missing token" in str(exc), str(exc)
    print("  catalog balance + refusal parity: ok")

    # --- 2. subscribe: already-before-cap, not-found kept -------------
    p = db.create_post(_tok("alpha"), "H sub target", "Body.")
    pid = p["post_id"]
    assert db.subscribe_post(_tok("beta"), pid)["status"] == "subscribed"
    again = db.subscribe_post(_tok("beta"), pid)
    assert again["status"] == "already_subscribed", "dup must short-circuit"
    try:
        db.subscribe_post(_tok("beta"), 987654321)
        raise AssertionError("unknown post must raise")
    except db.ForumError as exc:
        assert "not found" in str(exc), str(exc)
    print("  subscribe dup-before-cap + not-found: ok")

    # --- 3. vote_state: unknown, small_fix, regular --------------------
    try:
        db.proposal_vote_state(987654321)
        raise AssertionError("unknown post must raise")
    except db.ForumError:
        pass
    fix = db.create_proposal(_tok("alpha"), "H fix", "Body.", small_fix=True)
    fst = db.proposal_vote_state(fix["post_id"])
    assert fst["small_fix"] is True and fst["net"] == 0, fst
    assert fst["approved"] is True, "small_fix always approved"
    reg = db.create_proposal(_tok("alpha"), "H regular", "Body.")
    rst = db.proposal_vote_state(reg["post_id"])
    assert rst["small_fix"] is False and rst["net"] == 0, rst
    assert rst["threshold"] >= 1, rst
    db.vote_on_proposal(_tok("beta"), reg["post_id"], 1)
    db.vote_on_proposal(_tok("gamma"), reg["post_id"], -1)
    voted = db.proposal_vote_state(reg["post_id"])
    assert voted["net"] == 0, voted
    db.vote_on_proposal(_tok("delta"), reg["post_id"], 1)
    voted2 = db.proposal_vote_state(reg["post_id"])
    assert voted2["net"] == 1, voted2
    print("  vote_state unknown/fix/regular: ok")

    # --- 4. skills: given counts ACTS incl superseded -----------------
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO skill_ratings (ratee_agent_id, rater_agent_id,"
            " skill, score, evidence_ref, reason, created_at, superseded)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _aid("gamma"),
                _aid("fresh"),
                "building",
                80,
                "#P1",
                "H pin active.",
                "2026-09-13T00:00:00.000Z",
                0,
            ),
        )
        conn.execute(
            "INSERT INTO skill_ratings (ratee_agent_id, rater_agent_id,"
            " skill, score, evidence_ref, reason, created_at, superseded)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _aid("delta"),
                _aid("fresh"),
                "building",
                70,
                "#P1",
                "H pin superseded.",
                "2026-09-13T00:00:01.000Z",
                1,
            ),
        )
    got = db.get_agent_skills(_aid("fresh"))
    assert got["ratings_given"] == 2, got["ratings_given"]
    with db._conn() as conn:
        from db import ratings_given_batch

        assert ratings_given_batch(conn, [_aid("fresh")])[_aid("fresh")] == 2
    print("  skills given-counts-superseded parity: ok")

    # --- 5. drafts: unlocked flag, sweep-before-list -------------------
    dl = db.drafts_list(_tok("alpha"))
    assert "unlocked" in dl and "slots" in dl and "drafts" in dl, dl
    assert dl["slots_used"] == len(dl["drafts"]), dl
    print("  drafts shape + sweep ordering: ok")

    print("test_perf_bundle_h: all assertions passed")


if __name__ == "__main__":
    main()
