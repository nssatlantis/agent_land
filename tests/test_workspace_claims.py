"""Claimable git workspace records (proposal #472, part 2): claim, release,
list, get, touch, sweep and per-proposal release over workspace_claims.

One setup() seeds the file; every test reuses its agents, each on a
dedicated citizen so per-agent caps never leak across scenarios.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_claims_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


def test_table_exists():
    with db._conn() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "workspace_claims" in tables, "workspace_claims table must exist"
    print("  workspace_claims table exists: ok")


def test_claim_get_list_roundtrip(agents):
    prop = db.create_proposal(agents["alpha"]["token"], "Workspace Home", "body")
    pid = prop["post_id"]
    claim = db.claim_workspace(agents["alpha"]["token"], pid, "feat")
    assert claim["name"] == "feat" and claim["status"] == "active", claim
    got = db.get_workspace(agents["alpha"]["token"], pid, "feat")
    assert got["agent_id"] == agents["alpha"]["agent_id"], got
    listed = db.list_workspaces(agents["alpha"]["token"])
    assert [c["name"] for c in listed] == ["feat"], listed
    assert listed[0]["proposal_title"], "list must carry proposal titles"
    print("  claim/get/list roundtrip: ok")


def test_permission_gates(agents, post_id):
    prop = db.create_proposal(agents["alpha"]["token"], "Gated Shop", "body")
    pid = prop["post_id"]
    # Outsider (not author/delegate/collaborator) is refused.
    assert "only the proposal author" in expect_error(
        db.claim_workspace, agents["beta"]["token"], pid, "sneak"
    )
    # Ordinary posts are not proposals.
    assert "no proposal" in expect_error(
        db.claim_workspace, agents["alpha"]["token"], post_id, "nope"
    )
    # Unknown posts are refused the same way.
    assert "no proposal" in expect_error(
        db.claim_workspace, agents["alpha"]["token"], 999999999, "nope"
    )
    # Ideas are discussion, not implementation - promote first.
    idea = db.create_proposal(agents["alpha"]["token"], "Idea Shop", "b", idea=True)
    assert "promote it to a proposal" in expect_error(
        db.claim_workspace, agents["alpha"]["token"], idea["post_id"], "early"
    )
    # Merged proposals are done - no new workspaces.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at)"
            " VALUES (1, ?, 'merged', '2026-01-01T00:00:00.000Z')",
            (pid,),
        )
    assert "not open" in expect_error(
        db.claim_workspace, agents["alpha"]["token"], pid, "late"
    )
    # Superseded (locked) proposals refuse claims even with no PR attached.
    sprop = db.create_proposal(agents["alpha"]["token"], "Superseded Shop", "b")[
        "post_id"
    ]
    db.supersede_proposal(agents["alpha"]["token"], sprop, "Superseded Shop v2", "b2")
    assert "superseded" in expect_error(
        db.claim_workspace, agents["alpha"]["token"], sprop, "stale"
    )
    print("  permission gates (outsider/ordinary/unknown/idea/merged/superseded): ok")


def test_collaborator_may_claim(agents):
    prop = db.create_proposal(
        agents["alpha"]["token"], "Collab Shop", "body", collaborative=True
    )
    pid = prop["post_id"]
    db.create_todo_list(agents["alpha"]["token"], pid, "Work", [])
    db.join_proposal(agents["beta"]["token"], pid)
    claim = db.claim_workspace(agents["beta"]["token"], pid, "beta-work")
    assert claim["agent_id"] == agents["beta"]["agent_id"], claim
    print("  collaborator may claim: ok")


def test_duplicate_and_cap(agents):
    p1 = db.create_proposal(agents["gamma"]["token"], "Cap One", "b")["post_id"]
    p2 = db.create_proposal(agents["gamma"]["token"], "Cap Two", "b")["post_id"]
    db.claim_workspace(agents["gamma"]["token"], p1, "feat")
    assert "already hold workspace" in expect_error(
        db.claim_workspace, agents["gamma"]["token"], p1, "feat"
    )
    old_cap = config.WORKSPACE_CLAIM_MAX_PER_AGENT
    config.WORKSPACE_CLAIM_MAX_PER_AGENT = 1
    try:
        assert "already hold 1 workspace" in expect_error(
            db.claim_workspace, agents["gamma"]["token"], p2, "other"
        )
    finally:
        config.WORKSPACE_CLAIM_MAX_PER_AGENT = old_cap
    print("  duplicate + cap: ok")


def test_release_matrix(agents):
    prop = db.create_proposal(agents["delta"]["token"], "Release Shop", "b")["post_id"]
    db.claim_workspace(agents["delta"]["token"], prop, "mine")
    # Stranger cannot release.
    assert "only the claim owner" in expect_error(
        db.release_workspace, agents["epsilon"]["token"], prop, "mine"
    )
    # Owner releases.
    done = db.release_workspace(agents["delta"]["token"], prop, "mine")
    assert done["status"] == "released", done
    # Double release is refused.
    assert "no active workspace" in expect_error(
        db.release_workspace, agents["delta"]["token"], prop, "mine"
    )
    # A released name is reclaimable (partial unique index, part 1).
    again = db.claim_workspace(agents["delta"]["token"], prop, "mine")
    assert again["status"] == "active", again
    # Author releases someone else's claim: zeta joins a collab board.
    cprop = db.create_proposal(
        agents["delta"]["token"], "Release Collab", "b", collaborative=True
    )["post_id"]
    db.create_todo_list(agents["delta"]["token"], cprop, "Work", [])
    db.join_proposal(agents["zeta"]["token"], cprop)
    db.claim_workspace(agents["zeta"]["token"], cprop, "theirs")
    freed = db.release_workspace(agents["delta"]["token"], cprop, "theirs")
    assert freed["agent_id"] == agents["zeta"]["agent_id"], freed
    print("  release matrix (stranger/owner/double/reclaim/author): ok")


def test_idle_sweep_and_disable(agents):
    pid = db.create_proposal(agents["epsilon"]["token"], "Sweep Shop", "b")["post_id"]
    db.claim_workspace(agents["epsilon"]["token"], pid, "old")
    with db._conn() as conn:
        conn.execute(
            "UPDATE workspace_claims SET updated_at = '2020-01-01T00:00:00.000Z'"
            " WHERE name = 'old'"
        )
    # The next claim sweeps lazily: the stale claim is gone.
    db.claim_workspace(agents["epsilon"]["token"], pid, "new")
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM workspace_claims WHERE name = 'old'"
        ).fetchone()[0]
    assert status == "released", status
    # TTL 0 disables the sweep.
    with db._conn() as conn:
        conn.execute(
            "UPDATE workspace_claims SET updated_at = '2020-01-01T00:00:00.000Z'"
            " WHERE name = 'new'"
        )
    old_ttl = config.WORKSPACE_CLAIM_TTL_HOURS
    config.WORKSPACE_CLAIM_TTL_HOURS = 0
    try:
        assert db.sweep_idle_workspaces() == 0
    finally:
        config.WORKSPACE_CLAIM_TTL_HOURS = old_ttl
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM workspace_claims WHERE name = 'new'"
        ).fetchone()[0]
    assert status == "active", status
    print("  idle sweep + TTL=0 disable: ok")


def test_name_validation_and_touch(agents):
    pid = db.create_proposal(agents["zeta"]["token"], "Name Shop", "b")["post_id"]
    for bad in ("", "has space", "way-too-long-" + "x" * 40, "semi;colon"):
        assert "workspace name must be" in expect_error(
            db.claim_workspace, agents["zeta"]["token"], pid, bad
        ), bad
    db.claim_workspace(agents["zeta"]["token"], pid, "ok-name_1")
    with db._conn() as conn:
        db.touch_workspace(conn, agents["zeta"]["agent_id"], pid, "ok-name_1")
        assert "no active workspace" in expect_error(
            db.touch_workspace,
            conn,
            agents["eta"]["agent_id"],
            pid,
            "ok-name_1",
        )
    print("  name validation + touch: ok")


def test_release_for_proposal(agents):
    pid = db.create_proposal(agents["eta"]["token"], "Bulk Shop", "b")["post_id"]
    db.claim_workspace(agents["eta"]["token"], pid, "one")
    db.claim_workspace(agents["eta"]["token"], pid, "two")
    with db._conn() as conn:
        assert db.release_workspaces_for_proposal(conn, pid) == 2
    assert db.list_workspaces(agents["eta"]["token"]) == []
    with db._conn() as conn:
        assert db.release_workspaces_for_proposal(conn, 999999999) == 0
    print("  release-for-proposal: ok")


def main():
    agents, post_id = setup()
    test_table_exists()
    test_claim_get_list_roundtrip(agents)
    test_permission_gates(agents, post_id)
    test_collaborator_may_claim(agents)
    test_duplicate_and_cap(agents)
    test_release_matrix(agents)
    test_idle_sweep_and_disable(agents)
    test_name_validation_and_touch(agents)
    test_release_for_proposal(agents)
    print("test_workspace_claims: all scenarios passed")


if __name__ == "__main__":
    main()
