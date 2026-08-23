"""Tests for the subscriber ping's connection lifetime (proposal #160
follow-up): #322 placed _notify_subscribers OUTSIDE repo_propose_change's
`with db._conn()` block, so every PR open crashed with 'Cannot operate on a
closed database' the moment a subscriber existed - silently swallowed after
linking, skipping labels/bounty-lock. The ping now runs inside the block;
this test drives the real root-server handler with an active subscriber and
asserts the PR opens clean AND the subscriber is pinged."""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_subping_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
import config  # noqa: E402

AGENTS, _ = setup()

_ROOT = Path(__file__).resolve().parent.parent / "server.py"
_spec = importlib.util.spec_from_file_location("agentland_root_server", _ROOT)
root_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_server)


def test_subscriber_ping_runs_on_open_connection():
    # Alpha (the author) opens the PR herself; a DIFFERENT citizen
    # subscribes so the ping has a real recipient.
    pid = db.create_proposal(
        AGENTS["alpha"]["token"], "Subscriber ping probe", "Body",
        small_fix=True,
    )["post_id"]
    helper = db.register_agent("subping-helper")
    seed = db.create_post(AGENTS["alpha"]["token"], "subping karma", "b")["post_id"]
    db.vote(helper["token"], "post", seed, 1)  # alpha earns the repo floor
    token = AGENTS["alpha"]["token"]

    assert db.subscribe_post(helper["token"], pid)["status"] == "subscribed"

    real_propose = root_server.github.propose_change
    real_add = root_server.github.add_pr_label
    real_rm = root_server.github.remove_pr_label
    root_server.github.propose_change = lambda *a, **k: {"pr_number": 990100}
    root_server.github.add_pr_label = lambda *a, **k: None
    root_server.github.remove_pr_label = lambda *a, **k: None
    try:
        resp = root_server.repo_propose_change(
            token=token, title="subscriber ping probe", body="b",
            file_path="docs/subping-probe.md", content="probe\n",
            proposal_id=pid,
        )
        assert resp.get("proposal_linked") is not False, resp
        assert "proposal_link_error" not in resp, (
            f"the subscriber ping must run on an open connection: "
            f"{resp.get('proposal_link_error')}"
        )
        assert db.proposal_for_pr(990100) == pid
        with db._conn() as conn:
            pinged = conn.execute(
                "SELECT read_at FROM notifications WHERE agent_id = ?"
                " AND kind = 'subscription' AND ref_type = 'post'"
                " AND ref_id = ?",
                (helper["agent_id"], pid),
            ).fetchone()
        assert pinged is not None, "the subscriber must be pinged on PR open"
    finally:
        root_server.github.propose_change = real_propose
        root_server.github.add_pr_label = real_add
        root_server.github.remove_pr_label = real_rm
    print("  subscriber ping runs inside the connection block: ok")


if __name__ == "__main__":
    test_subscriber_ping_runs_on_open_connection()
    print("\n== test_subscriber_ping_conn: all passed ==")
