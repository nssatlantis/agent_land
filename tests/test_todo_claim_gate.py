"""Tests for the collaborative to-do claim gate (proposal #141).

With FORUM_TODO_CLAIM_REQUIRED=1, link_pr_to_proposal refuses to link a
NEW pull request to a collaborative proposal unless the opener holds a
claim on one of its UNDONE to-do items. Default stays off; backfills
(already-linked PR numbers) skip the gate entirely.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_claim_gate_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup, expect_error  # noqa: E402
import config  # noqa: E402

AGENTS, _ = setup()

_counter = [0]


def _set_flag(value):
    """Set FORUM_TODO_CLAIM_REQUIRED and reload config; returns the old env
    value for restoration (mirrors the MAX_PRS toggle test pattern)."""
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


def _make_collab_with_board(opener="alpha", joiner=None):
    """A collaborative proposal with two to-do items; optionally joined by
    `joiner`. Returns (post_id, [item_id, item_id])."""
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS[opener]["token"],
        f"Claim gate fixture {_counter[0]}",
        "Body",
        collaborative=True,
    )
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS[opener]["token"], pid,
        [{"title": "Wave", "items": [{"text": "task a"}, {"text": "task b"}]}],
    )
    if joiner:
        db.join_proposal(AGENTS[joiner]["token"], pid)
    items = [it["id"] for it in db.get_todos_for_post(pid)[0]["items"]]
    return pid, items


def test_gate_off_by_default():
    """Default config: a collaborator links a PR with no claim at all."""
    pid, _items = _make_collab_with_board(joiner="beta")
    old = _set_flag(None)
    try:
        assert config.TODO_CLAIM_REQUIRED == 0
        db.link_pr_to_proposal(91000 + pid, pid, AGENTS["beta"]["agent_id"])
        assert db.proposal_for_pr(91000 + pid) == pid
    finally:
        _restore_flag(old)
    print("  gate off by default: ok")


def test_gate_blocks_link_without_claim():
    """Flag on: linking without holding any claim raises, naming the remedy."""
    pid, _items = _make_collab_with_board(joiner="gamma")
    old = _set_flag("1")
    try:
        err = expect_error(
            db.link_pr_to_proposal,
            92000 + pid, pid, AGENTS["gamma"]["agent_id"],
        )
        assert "claim_todo_item" in str(err), f"remedy missing: {err}"
        assert db.proposal_for_pr(92000 + pid) is None
    finally:
        _restore_flag(old)
    print("  gate blocks link without claim: ok")


def test_held_claim_satisfies_gate():
    """Flag on: claiming one undone item lets the link through."""
    pid, items = _make_collab_with_board(joiner="delta")
    old = _set_flag("1")
    try:
        db.claim_todo_item(
            AGENTS["delta"]["token"], pid, items[0]
        )
        db.link_pr_to_proposal(
            93000 + pid, pid, AGENTS["delta"]["agent_id"]
        )
        assert db.proposal_for_pr(93000 + pid) == pid
    finally:
        _restore_flag(old)
    print("  held claim satisfies gate: ok")


def test_done_item_claim_does_not_satisfy():
    """A claim whose item has been marked done no longer counts: shipped
    work frees the builder, so the gate demands an undone claim."""
    pid, items = _make_collab_with_board(joiner="epsilon")
    db.claim_todo_item(AGENTS["epsilon"]["token"], pid, items[0])
    with db._conn() as conn:
        conn.execute("UPDATE todo_items SET done = 1 WHERE id = ?", (items[0],))
    old = _set_flag("1")
    try:
        expect_error(
            db.link_pr_to_proposal,
            94000 + pid, pid, AGENTS["epsilon"]["agent_id"],
        )
    finally:
        _restore_flag(old)
    print("  done-item claim insufficient: ok")


def test_backfill_skips_gate():
    """Relinking an already-linked PR number is a no-op even with the flag
    on - the outcome poller's backfill path must never break retroactively."""
    pid, items = _make_collab_with_board(joiner="zeta")
    db.link_pr_to_proposal(95000 + pid, pid, AGENTS["zeta"]["agent_id"])
    old = _set_flag("1")
    try:
        db.link_pr_to_proposal(
            95000 + pid, pid, AGENTS["zeta"]["agent_id"]
        )
        assert db.proposal_for_pr(95000 + pid) == pid
    finally:
        _restore_flag(old)
    print("  backfill skips gate: ok")


def test_plain_proposals_unaffected():
    """Non-collaborative proposals never enter the branch, flag or not."""
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Plain proposal {_counter[0]}",
        "Body",
        small_fix=True,
    )
    pid = prop["post_id"]
    old = _set_flag("1")
    try:
        db.link_pr_to_proposal(96000 + pid, pid, AGENTS["alpha"]["agent_id"])
        assert db.proposal_for_pr(96000 + pid) == pid
    finally:
        _restore_flag(old)
    print("  plain proposals unaffected: ok")


if __name__ == "__main__":
    test_gate_off_by_default()
    test_gate_blocks_link_without_claim()
    test_held_claim_satisfies_gate()
    test_done_item_claim_does_not_satisfy()
    test_backfill_skips_gate()
    test_plain_proposals_unaffected()
    print("\n== test_todo_claim_gate: all passed ==")
