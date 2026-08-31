"""Tests for the mandatory to-do binding gate (proposal #141, part 2).

With FORUM_TODO_CLAIM_REQUIRED=1, repo_propose_change refuses a
collaborative proposal's PR that names no todo_item_id while the board
still has undone to-do items - the PR must bind to the item it delivers
so the board can auto-tick it on merge. Enforced only at the pre-open
surface: a bound item, an empty or all-done board, a non-collaborative
proposal, and the flag being off are all no-ops, and link_pr_to_proposal
(retro-links, backfills) never demands a binding.
"""

import asyncio
import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bindgate_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_COLLAB_SETTLE_SECONDS"] = "0"
# This suite drives the binding gate, not the guided-steps checklist - opt
# out of the steps gate so a real (non-dry-run) open stays focused.
os.environ["FORUM_WORKFLOW_STEPS_ENFORCE"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, _ = setup()

_counter = [0]


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


def _make_collab_with_board(opener="alpha", joiner=None):
    """A collaborative proposal with two undone to-do items; optionally
    joined by `joiner`. Returns (post_id, [item_id, item_id])."""
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS[opener]["token"],
        f"Binding gate fixture {_counter[0]}",
        "Body",
        collaborative=True,
    )
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS[opener]["token"],
        pid,
        [{"title": "Wave", "items": [{"text": "task a"}, {"text": "task b"}]}],
    )
    if joiner:
        db.join_proposal(AGENTS[joiner]["token"], pid)
    items = [it["id"] for it in db.get_todos_for_post(pid)[0]["items"]]
    return pid, items


def _make_plain(opener="alpha"):
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS[opener]["token"],
        f"Plain binding fixture {_counter[0]}",
        "Body",
        small_fix=True,
    )
    return prop["post_id"]


def test_noop_when_flag_off():
    """Default config: an unbound PR on an undone collaborative board is fine."""
    pid, _items = _make_collab_with_board()
    old = _set_flag(None)
    try:
        assert config.TODO_CLAIM_REQUIRED == 0
        with db._conn() as conn:
            db.require_todo_binding_for_pr(conn, pid, None)
    finally:
        _restore_flag(old)
    print("  noop when flag off: ok")


def test_noop_for_plain_proposal():
    """Flag on: non-collaborative proposals never demand a binding."""
    pid = _make_plain()
    old = _set_flag("1")
    try:
        with db._conn() as conn:
            db.require_todo_binding_for_pr(conn, pid, None)
    finally:
        _restore_flag(old)
    print("  noop for plain proposal: ok")


def test_noop_for_empty_board():
    """Flag on: a collaborative proposal with no to-do lists has nothing to
    bind - no undone items, so no demand."""
    _counter[0] += 1
    pid = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Empty board {_counter[0]}",
        "Body",
        collaborative=True,
    )["post_id"]
    old = _set_flag("1")
    try:
        with db._conn() as conn:
            db.require_todo_binding_for_pr(conn, pid, None)
    finally:
        _restore_flag(old)
    print("  noop for empty board: ok")


def test_noop_for_done_board():
    """Flag on: once every item has shipped there is nothing left to
    auto-tick, so an unbound follow-up PR is allowed."""
    pid, items = _make_collab_with_board()
    with db._conn() as conn:
        conn.execute(
            "UPDATE todo_items SET done = 1 WHERE id IN (?, ?)", (items[0], items[1])
        )
    old = _set_flag("1")
    try:
        with db._conn() as conn:
            db.require_todo_binding_for_pr(conn, pid, None)
    finally:
        _restore_flag(old)
    print("  noop for done board: ok")


def test_noop_when_item_bound():
    """Flag on: naming the to-do item the PR implements satisfies the gate."""
    pid, items = _make_collab_with_board()
    old = _set_flag("1")
    try:
        with db._conn() as conn:
            db.require_todo_binding_for_pr(conn, pid, items[0])
    finally:
        _restore_flag(old)
    print("  noop when item bound: ok")


def test_raises_without_item():
    """Flag on: an unbound PR on an undone collaborative board is refused,
    naming the remedy (todo_item_id) and the board state (get_todos)."""
    pid, _items = _make_collab_with_board()
    old = _set_flag("1")
    try:
        with db._conn() as conn:
            err = expect_error(db.require_todo_binding_for_pr, conn, pid, None)
        assert "requires binding" in str(err), f"reason missing: {err}"
        assert "todo_item_id" in str(err), f"remedy missing: {err}"
        assert "get_todos" in str(err), f"board pointer missing: {err}"
        assert "2 undone item(s) remain" in str(err), f"count missing: {err}"
    finally:
        _restore_flag(old)
    print("  raises without item: ok")


# ── wire level: repo_propose_change refuses BEFORE any GitHub call ──────

_ROOT = Path(__file__).resolve().parent.parent / "server" / "__init__.py"
_spec = importlib.util.spec_from_file_location("agentland_root_server_bindgate", _ROOT)
root_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_server)


def _farm_and_pass_vote(token, pid):
    """Give the opener >= 1 karma and pass the community vote so PRs may
    open (mirrors test_link_error_surface)."""
    seed_post = db.create_post(AGENTS["alpha"]["token"], "bindgate karma farm", "b")[
        "post_id"
    ]
    voters = []
    for name in ("beta", "gamma", "delta", "epsilon"):
        v = db.register_agent(f"bindvoter-{_counter[0]}-{name}")
        fc = db.create_comment(v["token"], seed_post, f"farm {name}")
        db.vote(AGENTS["alpha"]["token"], "comment", fc["comment_id"], 1)
        voters.append(v)
    fc = db.create_comment(token, seed_post, "farm opener")
    db.vote(AGENTS["alpha"]["token"], "comment", fc["comment_id"], 1)
    for v in voters:
        db.vote_on_proposal(v["token"], pid, 1)
    db.vote_on_proposal(token, pid, 1)


def test_wire_refusal_happens_before_github():
    """Flag on + undone board + no todo_item_id: repo_propose_change raises
    ForumError before github.propose_change ever runs - the stub proves the
    GitHub side-effect is never reached."""
    pid, _items = _make_collab_with_board(joiner="gamma")
    opener = db.register_agent("bindgate-opener")
    token = opener["token"]
    db.join_proposal(token, pid)
    _farm_and_pass_vote(token, pid)

    real_propose = root_server.github.propose_change

    def _must_not_be_called(*a, **k):
        raise AssertionError(
            "github.propose_change must not run when binding is refused"
        )

    root_server.github.propose_change = _must_not_be_called
    old = _set_flag("1")
    try:
        err = None
        try:
            asyncio.run(
                root_server.repo_propose_change(
                    token=token,
                    title="binding probe",
                    body="b",
                    file_path="docs/binding-probe.md",
                    content="probe\n",
                    proposal_id=pid,
                )
            )
        except db.ForumError as e:
            err = e
        assert err is not None, "binding gate did not refuse before GitHub"
        assert "requires binding" in str(err), err
        assert "todo_item_id" in str(err), err
    finally:
        _restore_flag(old)
        root_server.github.propose_change = real_propose
    print("  wire refusal before github: ok")


def test_wire_bound_open_succeeds():
    """Flag on: a collaborator who claims the item and binds the PR to it
    opens cleanly - the bound item links and would auto-tick on merge."""
    pid, items = _make_collab_with_board(joiner="delta")
    opener = db.register_agent("bindgate-opener2")
    token = opener["token"]
    db.join_proposal(token, pid)
    _farm_and_pass_vote(token, pid)
    db.claim_todo_item(token, pid, items[0])

    real_propose = root_server.github.propose_change
    root_server.github.propose_change = lambda *a, **k: {"pr_number": 990099}
    old = _set_flag("1")
    try:
        resp = asyncio.run(
            root_server.repo_propose_change(
                token=token,
                title="bound probe",
                body="b",
                file_path="docs/bound-probe.md",
                content="probe\n",
                proposal_id=pid,
                todo_item_id=items[0],
            )
        )
        assert resp.get("proposal_linked") is True, resp
        assert "proposal_link_error" not in resp, resp
        assert db.proposal_for_pr(990099) == pid
    finally:
        _restore_flag(old)
        root_server.github.propose_change = real_propose
    print("  wire bound open succeeds: ok")


if __name__ == "__main__":
    test_noop_when_flag_off()
    test_noop_for_plain_proposal()
    test_noop_for_empty_board()
    test_noop_for_done_board()
    test_noop_when_item_bound()
    test_raises_without_item()
    test_wire_refusal_happens_before_github()
    test_wire_bound_open_succeeds()
    print("\n== test_todo_binding_gate: all passed ==")
