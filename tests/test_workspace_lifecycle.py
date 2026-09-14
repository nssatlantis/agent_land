"""Claimable workspace lifecycle (proposal #472, part 7): terminal release
on close plus the active-claims listing.

One setup() seeds the file; every test reuses its agents on dedicated
citizens so caps never leak across scenarios.
"""

import os
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


def main():
    agents, _post_id = setup()
    test_close_proposal_releases_workspaces(agents)
    test_active_workspace_claims(agents)
    print("test_workspace_lifecycle: all scenarios passed")


if __name__ == "__main__":
    main()
