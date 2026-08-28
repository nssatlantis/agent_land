"""Test proposal claiming system."""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_claiming_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (
    db,
    expect_error,
    setup,
)


def main():
    agents, post_id = setup()

    # --- schema migration ------------------------------------------------
    with db._conn() as _conn:
        info = {row[1] for row in _conn.execute("PRAGMA table_info(posts)").fetchall()}
    assert "claimable" in info, "posts table must have a claimable column"
    with db._conn() as _conn:
        tables = {
            row[0]
            for row in _conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "proposal_claims" in tables, "proposal_claims table must exist"
    print("  claiming schema: ok")

    # --- set_claimable ---------------------------------------------------
    # only author may toggle
    prop = db.create_proposal(agents["alpha"]["token"], "Claim Me", "body")
    pid = prop["post_id"]

    # non-author refused
    assert "only the author" in expect_error(
        db.set_claimable, agents["beta"]["token"], pid, True
    ), "non-author should be refused"
    print("  set_claimable non-author: ok")

    # author toggles on
    res = db.set_claimable(agents["alpha"]["token"], pid, True)
    assert res["claimable"] is True
    post = db.get_post(pid)
    assert post["proposal"]["claimable"] is True
    print("  set_claimable on: ok")

    # toggle off clears claims (none yet, so no-op)
    res = db.set_claimable(agents["alpha"]["token"], pid, False)
    assert res["claimable"] is False
    post = db.get_post(pid)
    assert post["proposal"]["claimable"] is False
    print("  set_claimable off (no claim): ok")

    # toggle on again for claim tests
    db.set_claimable(agents["alpha"]["token"], pid, True)

    # cannot toggle on superseded proposal
    sup = db.create_proposal(agents["alpha"]["token"], "Sup Test", "body")
    sup_id = sup["post_id"]
    db.supersede_proposal(agents["alpha"]["token"], sup_id, "Sup Test v2", "body v2")
    assert "locked" in expect_error(
        db.set_claimable, agents["alpha"]["token"], sup_id, True
    ), "superseded proposal should be refused"
    print("  set_claimable on superseded: ok")

    # --- claim_proposal --------------------------------------------------
    # author cannot claim own proposal
    assert "cannot claim your own" in expect_error(
        db.claim_proposal, agents["alpha"]["token"], pid
    ), "author self-claim should be refused"
    print("  claim own proposal refused: ok")

    # non-claimable proposal refused
    p2 = db.create_proposal(agents["alpha"]["token"], "No Claim", "body")
    assert "does not accept claims" in expect_error(
        db.claim_proposal, agents["beta"]["token"], p2["post_id"]
    ), "non-claimable should be refused"
    print("  claim non-claimable refused: ok")

    # beta claims successfully
    res = db.claim_proposal(agents["beta"]["token"], pid)
    assert res["claimer_id"] == agents["beta"]["agent_id"]
    assert res["claimer_name"] == "beta"
    post = db.get_post(pid)
    assert post["proposal"]["delegate_id"] == agents["beta"]["agent_id"]
    assert post["proposal"]["claim_name"] == "beta"
    print("  claim proposal: ok")

    # second claimer refused (delegate already set by first claim)
    assert "already assigned" in expect_error(
        db.claim_proposal, agents["gamma"]["token"], pid
    ), "second claim should be refused (delegate already set)"
    print("  exclusive claim: ok")

    # --- unclaim_proposal ------------------------------------------------
    # gamma cannot unclaim (not the claimer)
    assert "only the claimer" in expect_error(
        db.unclaim_proposal, agents["gamma"]["token"], pid
    ), "non-claimer unclaim should be refused"
    print("  unclaim by non-claimer refused: ok")

    # beta unclaims
    res = db.unclaim_proposal(agents["beta"]["token"], pid)
    assert res["proposal_id"] == pid
    post = db.get_post(pid)
    assert post["proposal"]["delegate_id"] is None
    assert post["proposal"]["claim_name"] is None
    print("  unclaim proposal: ok")

    # double unclaim refused
    assert "no active claim" in expect_error(
        db.unclaim_proposal, agents["beta"]["token"], pid
    ), "double unclaim should be refused"
    print("  double unclaim refused: ok")

    # --- claiming gate in require_proposal_approval ----------------------
    # author cannot open PR while someone else has claimed
    db.claim_proposal(agents["gamma"]["token"], pid)
    assert "claimed by" in expect_error(
        db.require_proposal_approval,
        agents["alpha"]["token"],
        pid,
        "open a PR",
    ), "author blocked by claim should be refused"
    print("  author blocked by claim: ok")

    # claimer CAN open PR (passes the gate)
    # gamma is the claimer/delegate, so they pass the delegate check
    # We don't actually open a PR here since that needs GitHub, just verify
    # the claimer is not blocked by the claiming gate specifically.

    # unclaim and verify author can proceed
    db.unclaim_proposal(agents["gamma"]["token"], pid)
    # author is no longer blocked by claiming gate (may still fail on vote threshold,
    # which is fine - we're testing the claim gate specifically)
    err = expect_error(
        db.require_proposal_approval,
        agents["alpha"]["token"],
        pid,
        "open a PR",
    )
    # should NOT contain "claimed by" error
    assert "claimed by" not in (err or ""), "author should not be blocked after unclaim"
    print("  author unblocked after unclaim: ok")

    # --- turn off claimable clears existing claim ------------------------
    db.claim_proposal(agents["delta"]["token"], pid)
    db.set_claimable(agents["alpha"]["token"], pid, False)
    post = db.get_post(pid)
    assert post["proposal"]["delegate_id"] is None, (
        "turning off claimable should clear delegate_id"
    )
    assert post["proposal"]["claim_name"] is None, (
        "turning off claimable should clear claim"
    )
    print("  turn off claimable clears claim: ok")

    # --- list_proposals includes claim fields ----------------------------
    db.set_claimable(agents["alpha"]["token"], pid, True)
    db.claim_proposal(agents["epsilon"]["token"], pid)
    proposals = db.list_proposals(view="all")
    claimed = [p for p in proposals if p["id"] == pid]
    assert len(claimed) == 1
    assert claimed[0]["claimable"] is True
    assert claimed[0]["claim_agent_id"] == agents["epsilon"]["agent_id"]
    assert claimed[0]["claim_name"] == "epsilon"
    print("  list_proposals claim fields: ok")

    # --- my_proposals includes claim fields --------------------------------
    my = db.my_proposals(agents["alpha"]["token"])
    mine = [p for p in my["proposals"] if p["id"] == pid]
    assert len(mine) == 1
    assert mine[0]["claimable"] is True
    assert mine[0]["claim_agent_id"] == agents["epsilon"]["agent_id"]
    print("  my_proposals claim fields: ok")

    # --- claimed proposal in list_proposals shows claimable=true ----------
    proposals = db.list_proposals(view="all")
    unclaimed = [p for p in proposals if p["id"] == p2["post_id"]]
    assert len(unclaimed) == 1
    assert unclaimed[0]["claimable"] is False
    assert unclaimed[0]["claim_agent_id"] is None
    print("  list_proposals unclaimed: ok")

    # --- require_claim_for_todo gate (direct unit tests) ------------------
    import config

    _saved_flag = os.environ.get("FORUM_TODO_CLAIM_REQUIRED")

    def _set_flag(value):
        if value is None:
            os.environ.pop("FORUM_TODO_CLAIM_REQUIRED", None)
        else:
            os.environ["FORUM_TODO_CLAIM_REQUIRED"] = value
        importlib.reload(config)

    def _restore_flag(old):
        if old is None:
            os.environ.pop("FORUM_TODO_CLAIM_REQUIRED", None)
        else:
            os.environ["FORUM_TODO_CLAIM_REQUIRED"] = old
        importlib.reload(config)

    cp = db.create_proposal(
        agents["alpha"]["token"],
        "Claim Gate Test",
        "body",
        collaborative=True,
    )
    cpid = cp["post_id"]
    db.set_todos_for_post(
        agents["alpha"]["token"],
        cpid,
        [{"title": "W", "items": [{"text": "task1"}, {"text": "task2"}]}],
    )
    with db._conn() as _conn:
        list_row = _conn.execute(
            "SELECT id FROM todo_lists WHERE post_id = ?", (cpid,)
        ).fetchone()
        list_id = list_row["id"]
        items = _conn.execute(
            "SELECT id FROM todo_items WHERE list_id = ? ORDER BY id",
            (list_id,),
        ).fetchall()
    item1_id = items[0]["id"]
    item2_id = items[1]["id"]

    gate_citizen = db.register_agent("gate-tester")
    gate_token = gate_citizen["token"]
    gate_agent_id = gate_citizen["agent_id"]
    db.join_proposal(gate_token, cpid)

    try:
        # flag off → no-op (no claim needed)
        _set_flag("0")
        with db._conn() as conn:
            db.require_claim_for_todo(conn, cpid, gate_agent_id)
        print("  require_claim_for_todo flag-off no-op: ok")

        _set_flag("1")

        # non-collaborative proposal → no-op
        with db._conn() as conn:
            db.require_claim_for_todo(conn, post_id, gate_agent_id)
        print("  require_claim_for_todo non-collab no-op: ok")

        # collab, no claim → raises
        try:
            with db._conn() as conn:
                db.require_claim_for_todo(conn, cpid, gate_agent_id)
            assert False, "should have raised"
        except db.ForumError as e:
            assert "requires claiming" in str(e)
        print("  require_claim_for_todo no claim raises: ok")

        # active claim on undone item → passes
        db.claim_todo_item(gate_token, cpid, item1_id)
        with db._conn() as conn:
            db.require_claim_for_todo(conn, cpid, gate_agent_id)
        print("  require_claim_for_todo active claim passes: ok")

        # claim on done item only → raises
        db.unclaim_todo_item(gate_token, cpid, item1_id)
        db.claim_todo_item(gate_token, cpid, item2_id)
        with db._conn() as _conn:
            _conn.execute("UPDATE todo_items SET done = 1 WHERE id = ?", (item2_id,))
        try:
            with db._conn() as conn:
                db.require_claim_for_todo(conn, cpid, gate_agent_id)
            assert False, "should have raised when only done items claimed"
        except db.ForumError as e:
            assert "requires claiming" in str(e)
        print("  require_claim_for_todo done-only claim raises: ok")

        # expired claim swept → raises
        db.unclaim_todo_item(gate_token, cpid, item2_id)
        db.claim_todo_item(gate_token, cpid, item1_id)
        with db._conn() as _conn:
            _conn.execute(
                "UPDATE todo_items SET claimed_at = '2000-01-01T00:00:00.000Z'"
                " WHERE id = ?",
                (item1_id,),
            )
        try:
            with db._conn() as conn:
                db.require_claim_for_todo(conn, cpid, gate_agent_id)
            assert False, "should have raised after expired claim sweep"
        except db.ForumError as e:
            assert "requires claiming" in str(e)
        print("  require_claim_for_todo expired claim raises: ok")

    finally:
        _restore_flag(_saved_flag)

    print("  require_claim_for_todo all branches: ok")

    # --- teardown --------------------------------------------------------
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_claiming: all assertions passed")


if __name__ == "__main__":
    main()
