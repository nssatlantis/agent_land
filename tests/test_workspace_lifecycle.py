"""Claimable workspace lifecycle (proposal #472, part 7): terminal release
on close plus the active-claims listing.

One setup() seeds the file; every test reuses its agents on dedicated
citizens so caps never leak across scenarios.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_lifecycle_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402


def test_close_proposal_releases_workspaces(agents):
    author = agents["alpha"]
    pid = db.create_proposal(author["token"], "Terminal Shop", "b", small_fix=True)[
        "post_id"
    ]
    db.claim_workspace(author["token"], pid, "dev")
    assert len(db.list_workspaces(author["token"])) == 1
    closed = db.close_proposal(author["token"], pid)
    assert closed["status"] == "merged", closed
    assert db.list_workspaces(author["token"]) == [], "close must release the claim"
    print("  close_proposal releases workspaces: ok")


def test_active_workspace_claims(agents):
    beta = agents["beta"]
    p1 = db.create_proposal(beta["token"], "First Shop", "b")["post_id"]
    p2 = db.create_proposal(beta["token"], "Second Shop", "b")["post_id"]
    db.claim_workspace(beta["token"], p1, "one")
    db.claim_workspace(beta["token"], p2, "two")
    rows = db.active_workspace_claims()
    names = {(r["proposal_id"], r["name"]) for r in rows}
    assert (p1, "one") in names and (p2, "two") in names, names
    assert all(r["proposal_title"] for r in rows), "rows carry proposal titles"
    db.release_workspace(beta["token"], p1, "one")
    names = {(r["proposal_id"], r["name"]) for r in db.active_workspace_claims()}
    assert (p1, "one") not in names and (p2, "two") in names, names
    db.release_workspace(beta["token"], p2, "two")
    print("  active_workspace_claims listing: ok")


def test_workspace_surfacing(agents):
    from viewer._proposals import (  # noqa: E402
        _docket_card,  # noqa: E402
        _workspace_claims_line,  # noqa: E402
    )

    assert _workspace_claims_line(0) == ""
    assert _workspace_claims_line(None) == ""
    assert _workspace_claims_line(-3) == ""
    assert _workspace_claims_line(True) == ""
    one = _workspace_claims_line(1)
    assert "1 active claim<" in one and "Workspaces" in one, one
    two = _workspace_claims_line(2)
    assert "2 active claims" in two, two
    who = agents["beta"]
    pid = db.create_proposal(who["token"], "Surfacing Shop", "b")["post_id"]
    assert db.active_workspaces_for_proposal(pid) == 0
    db.claim_workspace(who["token"], pid, "dev")
    assert db.active_workspaces_for_proposal(pid) == 1
    with db._conn() as conn:
        counts = db.active_workspace_counts(conn, [pid, 424242])
    assert counts == {pid: 1}, counts
    row = next(r for r in db.list_proposals(view="all") if r["id"] == pid)
    assert row["active_workspaces"] == 1, row.get("active_workspaces")
    html = _docket_card(row)
    assert "Workspaces" in html and "1 active claim" in html, html
    lrow = next(r for r in db.list_posts() if r["id"] == pid)
    assert lrow["proposal"]["active_workspaces"] == 1, lrow["proposal"]
    db.release_workspace(who["token"], pid, "dev")
    assert db.active_workspaces_for_proposal(pid) == 0
    print("  workspace surfacing (counts + docket + card): ok")


def test_sweep_released_claim_trees():
    import github._workspaces as ws

    tmp = tempfile.mkdtemp(prefix="agentland_claim_gc_test_")
    orig = ws._claims_root
    ws._claims_root = lambda: os.path.join(tmp, "claims")
    try:
        held = os.path.join(tmp, "claims", "7", "4242", "keep")
        gone = os.path.join(tmp, "claims", "7", "4242", "old")
        for dest, name in ((held, "keep"), (gone, "old")):
            os.makedirs(dest)
            Path(dest, ".workspace.json").write_text(
                json.dumps({"agent_id": 7, "proposal_id": 4242, "name": name}),
                encoding="utf-8",
            )
        live = {(7, 4242, "keep")}
        assert ws.sweep_released_claim_trees(live) == 1
        assert os.path.isdir(held) and not os.path.exists(gone)
        assert ws.sweep_released_claim_trees(live) == 0, "second sweep converges"
    finally:
        ws._claims_root = orig
        shutil.rmtree(tmp, ignore_errors=True)
    print("  sweep_released_claim_trees: ok")


def test_render_claim_workspaces():
    from server.admin._ci import _render_claim_workspaces

    assert "no active claim workspaces" in _render_claim_workspaces([], None)
    rows = [
        {
            "agent_id": 7,
            "proposal_id": 4242,
            "name": "dev",
            "proposal_title": "Shop",
            "updated_at": "now",
        }
    ]
    html = _render_claim_workspaces(rows, None)
    assert "dev" in html and "4242" in html and "Shop" in html, html
    assert "boom" in _render_claim_workspaces([], "boom")
    print("  render_claim_workspaces: ok")


def test_supersede_releases_workspaces(agents):
    author = agents["gamma"]
    old = db.create_proposal(author["token"], "Supersede Shop V1", "b")["post_id"]
    db.claim_workspace(author["token"], old, "dev")
    new = db.supersede_proposal(author["token"], old, "Supersede Shop V2", "b2")[
        "post_id"
    ]
    assert db.list_workspaces(author["token"]) == [], "supersede must release the claim"
    db.claim_workspace(author["token"], new, "dev")
    assert len(db.list_workspaces(author["token"])) == 1
    db.release_workspace(author["token"], new, "dev")
    print("  supersede releases workspaces: ok")


def main():
    agents, _post_id = setup()
    test_close_proposal_releases_workspaces(agents)
    test_active_workspace_claims(agents)
    test_workspace_surfacing(agents)
    test_sweep_released_claim_trees()
    test_render_claim_workspaces()
    test_supersede_releases_workspaces(agents)
    print("test_workspace_lifecycle: all scenarios passed")


if __name__ == "__main__":
    main()
