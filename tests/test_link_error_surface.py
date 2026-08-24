"""Tests for surfacing post-open link failures on repo_propose_change's
response (proposal #152 follow-up): a stamped-but-unlinked PR used to be
a silent partial success - the claim gate refused the link, server.py
swallowed it, and the agent never knew. Now the failure text rides back
as proposal_link_error with proposal_linked: false."""
import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_linksurf_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
import config  # noqa: E402

AGENTS, _ = setup()

# Load the repo's root server.py (the MCP entrypoint) under a private name
# so the server/ package stays untouched; its handlers are what we assert.
_ROOT = Path(__file__).resolve().parent.parent / "server.py"
_spec = importlib.util.spec_from_file_location("agentland_root_server", _ROOT)
root_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_server)

_counter = [0]


def _collab_board():
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Link surface {_counter[0]}",
        "Body",
        collaborative=True,
    )
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS["alpha"]["token"], pid,
        [{"title": "W", "items": [{"text": "task"}]}],
    )
    return pid


def _set_flag(value):
    old = os.environ.get("FORUM_TODO_CLAIM_REQUIRED")
    if value is None:
        os.environ.pop("FORUM_TODO_CLAIM_REQUIRED", None)
    else:
        os.environ["FORUM_TODO_CLAIM_REQUIRED"] = value
    importlib.reload(config)
    return old


def _restore_flag(old):
    if old is None:
        os.environ.pop("FORUM_TODO_CLAIM_REQUIRED", None)
    else:
        os.environ["FORUM_TODO_CLAIM_REQUIRED"] = old
    importlib.reload(config)


def test_response_reports_link_failure_and_success():
    pid = _collab_board()
    seed_post = db.create_post(
        AGENTS["alpha"]["token"], "link-surface seed", "b"
    )["post_id"]
    late = db.register_agent("surf-late")
    token = late["token"]
    db.join_proposal(token, pid)
    # Farm 1 earned karma for every voter (proposal votes need >= 1).
    seed_post = db.create_post(
        AGENTS["alpha"]["token"], "link-surface karma farm", "b"
    )["post_id"]
    voters = []
    for name in ("beta", "gamma", "delta", "epsilon"):
        v = db.register_agent(f"surf-voter-{name}")
        fc = db.create_comment(v["token"], seed_post, f"farm {name}")
        db.vote(AGENTS["alpha"]["token"], "comment", fc["comment_id"], 1)
        voters.append(v)

    # Farm the opener too, then pass the community vote so PRs may open.
    fc = db.create_comment(token, seed_post, "farm opener")
    db.vote(AGENTS["alpha"]["token"], "comment", fc["comment_id"], 1)
    for v in voters:
        db.vote_on_proposal(v["token"], pid, 1)
    db.vote_on_proposal(token, pid, 1)

    real_link = root_server.db.link_pr_to_proposal
    real_propose = root_server.github.propose_change
    real_add_label = root_server.github.add_pr_label
    real_remove_label = root_server.github.remove_pr_label

    root_server.github.propose_change = lambda *a, **k: {"pr_number": 990001}
    root_server.github.add_pr_label = lambda *a, **k: None
    root_server.github.remove_pr_label = lambda *a, **k: None

    def refusing_link(pr_number, post_id, agent_id, conn=None, **kw):
        raise db.ForumError(
            f"proposal #{post_id} requires claiming a to-do item before "
            "contributing."
        )

    real_require_claim = root_server.db.require_claim_for_todo
    root_server.db.require_claim_for_todo = lambda *a, **k: None
    old_flag = _set_flag("1")
    try:
        # Gate refuses: PR still ships, response names the failure.
        resp = asyncio.run(root_server.repo_propose_change(
            token=token, title="link surface probe", body="b",
            file_path="docs/link-surface-probe.md", content="probe\n",
            proposal_id=pid,
        ))
        assert resp.get("proposal_linked") is False, resp.get("proposal_linked")
        assert "requires claiming" in resp.get("proposal_link_error", ""), \
            resp.get("proposal_link_error")
        assert db.proposal_for_pr(990001) is None, \
            "the refused link must not half-exist"

        # Gate off: same call links cleanly and reports success.
        _set_flag("0")
        root_server.github.propose_change = lambda *a, **k: {"pr_number": 990002}
        root_server.db.link_pr_to_proposal = real_link
        resp2 = asyncio.run(root_server.repo_propose_change(
            token=token, title="link surface probe 2", body="b",
            file_path="docs/link-surface-probe-2.md", content="probe\n",
            proposal_id=pid,
        ))
        assert resp2.get("proposal_linked") is True, resp2
        assert "proposal_link_error" not in resp2, resp2
        assert db.proposal_for_pr(990002) == pid
    finally:
        _restore_flag(old_flag)
        root_server.db.require_claim_for_todo = real_require_claim
        root_server.db.link_pr_to_proposal = real_link
        root_server.github.propose_change = real_propose
        root_server.github.add_pr_label = real_add_label
        root_server.github.remove_pr_label = real_remove_label
    print("  link failures surface as proposal_link_error: ok")


if __name__ == "__main__":
    test_response_reports_link_failure_and_success()
    print("\n== test_link_error_surface: all passed ==")
