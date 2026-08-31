"""Tests for whole-list to-do claiming on collaborative proposals.

set_todo_claim_mode toggles a collaborative proposal between per-item
claiming (claim_todo_item, the default) and whole-list claiming
(claim_todo_list). The two tools are mutually exclusive per proposal; a
held claim of one kind blocks switching to the other; the pre-open claim
gate and the PR-link gate both accept a list claim in list mode.
"""

import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_list_claim_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, _ = setup()

_counter = [0]


def _set_flag(name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    importlib.reload(config)
    return old


def _restore_flag(name, old):
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old
    importlib.reload(config)


def _make_collab(mode="list", joiner="beta", nlists=2):
    """Collaborative proposal with two lists [A, B], each with one undone
    item; sets claim mode; optionally joins `joiner`. Returns
    (post_id, list_ids, item_ids)."""
    _counter[0] += 1
    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"List claim fixture {_counter[0]}",
        "Body",
        collaborative=True,
    )
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        pid,
        [
            {"title": "A", "items": [{"text": "a1"}]},
            {"title": "B", "items": [{"text": "b1"}]},
        ],
    )
    todos = db.get_todos_for_post(pid)
    list_ids = [lst["id"] for lst in todos]
    item_ids = [lst["items"][0]["id"] for lst in todos]
    if mode in ("list", "hybrid"):
        db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, mode)
    if joiner:
        db.join_proposal(AGENTS[joiner]["token"], pid)
    return pid, list_ids, item_ids


def test_migration_columns_present():
    with db._conn() as c:
        post_cols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        list_cols = {r[1] for r in c.execute("PRAGMA table_info(todo_lists)")}
    assert "todo_claim_mode" in post_cols
    assert "claimed_by_agent_id" in list_cols
    assert "claimed_at" in list_cols
    print("  list claim migration columns: ok")


def test_mode_default_and_get_todos_shape():
    pid, list_ids, _ = _make_collab(mode=None, joiner=None)
    todos = db.get_todos_for_post(pid)
    for lst in todos:
        assert lst["claim_mode"] == "item", "default mode is item"
        assert "claimed_by" not in lst, "no list claim in item mode"
    print("  mode default + item-mode shape: ok")


def test_set_mode_author_only_and_collab_only():
    pid, _, _ = _make_collab(mode=None, joiner=None)
    assert "only the proposal author" in expect_error(
        db.set_todo_claim_mode, AGENTS["beta"]["token"], pid, "list"
    )
    # non-collaborative proposal refused
    prop = db.create_proposal(AGENTS["alpha"]["token"], "plain", "body")
    assert "not collaborative" in expect_error(
        db.set_todo_claim_mode, AGENTS["alpha"]["token"], prop["post_id"], "list"
    )
    # invalid mode refused
    assert "must be 'item' or 'list'" in expect_error(
        db.set_todo_claim_mode, AGENTS["alpha"]["token"], pid, "banana"
    )
    print("  set mode author-only/collab-only/invalid: ok")


def test_set_mode_idempotent_and_get_todos_shape():
    pid, list_ids, _ = _make_collab(mode=None, joiner=None)
    res = db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "list")
    assert res["changed"] is True and res["todo_claim_mode"] == "list"
    res = db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "list")
    assert res["changed"] is False
    todos = db.get_todos_for_post(pid)
    for lst in todos:
        assert lst["claim_mode"] == "list"
    print("  set mode idempotent + list-mode shape: ok")


def test_exclusive_tools():
    pid, list_ids, item_ids = _make_collab(mode="list")
    # item claim refused in list mode
    assert "whole to-do lists" in expect_error(
        db.claim_todo_item, AGENTS["beta"]["token"], pid, item_ids[0]
    )
    # list claim refused in item mode
    pid2, list_ids2, _ = _make_collab(mode=None)
    assert "individual to-do items" in expect_error(
        db.claim_todo_list, AGENTS["beta"]["token"], pid2, list_ids2[0]
    )
    print("  exclusive tools: ok")


def test_claim_and_unclaim_list():
    pid, list_ids, _ = _make_collab(mode="list")
    res = db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    assert res["title"] == "A" and res["claimed_by"] == AGENTS["beta"]["name"]
    todos = db.get_todos_for_post(pid)
    lst = [l for l in todos if l["id"] == list_ids[0]][0]
    assert lst["claimed_by"] == AGENTS["beta"]["name"], "list claim surfaced"
    # double claim refused
    db.join_proposal(AGENTS["gamma"]["token"], pid)
    assert "already claimed" in expect_error(
        db.claim_todo_list, AGENTS["gamma"]["token"], pid, list_ids[0]
    )
    # claimer may table claim; second list by same claimer hit cap? default cap 1
    # non-claimer release refused
    assert "only the claimer or the proposal author" in expect_error(
        db.unclaim_todo_list, AGENTS["gamma"]["token"], pid, list_ids[0]
    )
    # claimer releases
    res = db.unclaim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    assert res["title"] == "A"
    assert "not claimed" in expect_error(
        db.unclaim_todo_list, AGENTS["beta"]["token"], pid, list_ids[0]
    )
    print("  claim/unclaim list: ok")


def test_cap_and_undone_requirement():
    pid, list_ids, _ = _make_collab(mode="list")
    old = _set_flag("FORUM_MAX_LIST_CLAIMS_PER_COLLABORATOR", "1")
    try:
        db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
        assert "maximum is 1" in expect_error(
            db.claim_todo_list, AGENTS["beta"]["token"], pid, list_ids[1]
        )
    finally:
        _restore_flag("FORUM_MAX_LIST_CLAIMS_PER_COLLABORATOR", old)
    # no undone items left -> refused
    pid2, list_ids2, item_ids2 = _make_collab(mode="list")
    for iid in item_ids2:
        db.tick_todo_item(AGENTS["alpha"]["token"], pid2, iid, True)
    assert "no undone items" in expect_error(
        db.claim_todo_list, AGENTS["beta"]["token"], pid2, list_ids2[0]
    )
    print("  list claim cap + undone requirement: ok")


def test_mode_switch_blocked_by_claims():
    pid, list_ids, _ = _make_collab(mode="list", joiner="beta")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    # author cannot flip back to item while list claims held
    assert "still has 1 list claim" in expect_error(
        db.set_todo_claim_mode, AGENTS["alpha"]["token"], pid, "item"
    )
    db.unclaim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    assert db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "item")["changed"]
    # item claims block flipping to list
    db.claim_todo_item(
        AGENTS["beta"]["token"], pid, db.get_todos_for_post(pid)[0]["items"][0]["id"]
    )
    assert "still has 1 item claim" in expect_error(
        db.set_todo_claim_mode, AGENTS["alpha"]["token"], pid, "list"
    )
    print("  mode switch blocked by claims: ok")


def test_mode_switch_sweeps_expired_claim():
    """set_todo_claim_mode sweeps expired claims before its guard: an
    expired-but-unswept list claim is a ghost reservation and must not
    block a legitimate switch back to item mode."""
    pid, list_ids, _ = _make_collab(mode="list", joiner="beta")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    _past = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    with db._conn() as conn:
        conn.execute(
            "UPDATE todo_lists SET claimed_at = ? WHERE id = ?",
            (_past, list_ids[0]),
        )
    old = _set_flag("FORUM_CLAIM_TIMEOUT_SECONDS", "1")
    try:
        res = db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "item")
        assert res["changed"], "expired claim swept so mode switch succeeds"
        todos = db.get_todos_for_post(pid)
        assert not any("claimed_by" in l for l in todos), (
            "the expired list claim is gone after the switch"
        )
    finally:
        _restore_flag("FORUM_CLAIM_TIMEOUT_SECONDS", old)
    print("  mode switch sweeps expired claim: ok")


def test_list_claimer_ticks_own_item():
    pid, list_ids, item_ids = _make_collab(mode="list")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    # beta can tick the item on their claimed list
    res = db.tick_todo_item(AGENTS["beta"]["token"], pid, item_ids[0], True)
    assert res["done"] is True
    print("  list claimer ticks own item: ok")


def test_claim_gate_accepts_list_claim():
    """PR-link gate in list mode: with FORUM_TODO_CLAIM_REQUIRED on, a held
    whole-list claim satisfies link_pr_to_proposal (and none is refused,
    naming claim_todo_list as the remedy)."""
    pid, list_ids, _ = _make_collab(mode="list", joiner="gamma")
    old = _set_flag("FORUM_TODO_CLAIM_REQUIRED", "1")
    try:
        # no claim: gate raises
        assert "requires claiming a whole to-do list" in expect_error(
            db.link_pr_to_proposal, 93000 + pid, pid, AGENTS["gamma"]["agent_id"]
        )
        # list claim satisfies it
        db.claim_todo_list(AGENTS["gamma"]["token"], pid, list_ids[0])
        db.link_pr_to_proposal(93000 + pid, pid, AGENTS["gamma"]["agent_id"])
        assert db.proposal_for_pr(93000 + pid) == pid
    finally:
        _restore_flag("FORUM_TODO_CLAIM_REQUIRED", old)
    print("  PR-link gate accepts list claim: ok")


def test_sweep_releases_expired_list_claim():
    """Sweep timeout path for whole-list claims: reading the board frees a
    list claim past CLAIM_TIMEOUT_SECONDS and tells the former claimer."""
    pid, list_ids, _ = _make_collab(mode="list", joiner="beta")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    _past = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    with db._conn() as conn:
        conn.execute(
            "UPDATE todo_lists SET claimed_at = ? WHERE id = ?",
            (_past, list_ids[0]),
        )
    old = _set_flag("FORUM_CLAIM_TIMEOUT_SECONDS", "1")
    try:
        todos = db.get_todos_for_post(pid)
        owned = [l for l in todos if l["id"] == list_ids[0]][0]
        assert "claimed_by" not in owned, "timed-out list claim freed by sweep"
        with db._conn() as conn:
            notice = conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?"
                " AND kind = 'delegation' AND body LIKE '%expired%'"
                " ORDER BY id DESC LIMIT 1",
                (AGENTS["beta"]["agent_id"],),
            ).fetchone()
        assert notice, "claimant told their list claim expired"
        assert "list claim" in notice["body"]
        assert "A" in notice["body"]
    finally:
        _restore_flag("FORUM_CLAIM_TIMEOUT_SECONDS", old)
    print("  sweep releases expired list claim + notice: ok")


def test_rewrite_drops_expired_list_claim():
    """Snapshot/restore across set_todos_for_post: an expired list claim is
    never resurrected by a rewrite - _snapshot_list_claims skips it, so the
    re-created list comes back unclaimed even under its original title."""
    pid, list_ids, _ = _make_collab(mode="list", joiner="beta")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    _past = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    with db._conn() as conn:
        conn.execute(
            "UPDATE todo_lists SET claimed_at = ? WHERE id = ?",
            (_past, list_ids[0]),
        )
    old = _set_flag("FORUM_CLAIM_TIMEOUT_SECONDS", "1")
    try:
        db.set_todos_for_post(
            AGENTS["alpha"]["token"],
            pid,
            [
                {"title": "A", "items": [{"text": "a1"}]},
                {"title": "B", "items": [{"text": "b1"}]},
            ],
        )
        todos = db.get_todos_for_post(pid)
        owned = [l for l in todos if l["title"] == "A"][0]
        assert "claimed_by" not in owned, (
            "an expired list claim is not restored by a rewrite"
        )
    finally:
        _restore_flag("FORUM_CLAIM_TIMEOUT_SECONDS", old)
    print("  rewrite does not restore expired list claim: ok")


def test_hybrid_mode_allows_both_tools():
    """Hybrid claim mode accepts both claim kinds: an item claim and a
    whole-list claim can coexist on the same board."""
    pid, list_ids, item_ids = _make_collab(mode="hybrid", joiner="beta")
    with db._conn() as _conn:
        mode = _conn.execute(
            "SELECT todo_claim_mode FROM posts WHERE id = ?", (pid,)
        ).fetchone()[0]
    assert mode == 2, "hybrid stored as 2"
    res = db.claim_todo_item(AGENTS["beta"]["token"], pid, item_ids[0])
    assert res["item_id"] == item_ids[0], "item claim accepted in hybrid"
    db.join_proposal(AGENTS["gamma"]["token"], pid)
    res = db.claim_todo_list(AGENTS["gamma"]["token"], pid, list_ids[1])
    assert res["title"] == "B", "list claim accepted in hybrid"
    print("  hybrid mode allows both claim kinds: ok")


def test_hybrid_item_under_claimed_list_refused():
    """Hybrid collision rule: in hybrid mode a whole-list claim reserves its
    items, so one citizen cannot claim_todo_item under another's claimed
    list."""
    pid, list_ids, item_ids = _make_collab(mode="hybrid", joiner="beta")
    db.join_proposal(AGENTS["gamma"]["token"], pid)
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    err = expect_error(db.claim_todo_item, AGENTS["gamma"]["token"], pid, item_ids[0])
    assert "list claimed by" in err and AGENTS["beta"]["name"] in err, err
    # the list holder may still claim items in their own list
    res = db.claim_todo_item(AGENTS["beta"]["token"], pid, item_ids[0])
    assert res["item_id"] == item_ids[0], "list holder may claim own-list item"
    print("  hybrid item under foreign claimed list refused: ok")


def test_hybrid_switch_never_blocked():
    """Switching TO hybrid is never blocked by held claims of either kind."""
    # item claims held -> hybrid accepted
    pid, list_ids, item_ids = _make_collab(mode=None, joiner="beta")
    db.claim_todo_item(AGENTS["beta"]["token"], pid, item_ids[0])
    res = db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "hybrid")
    assert res["changed"] is True
    # list claims held -> hybrid accepted
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[1])
    res = db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "hybrid")
    assert res["changed"] is False, "idempotent same-mode switch"
    # hybrid round-trips into a pure mode once the opposite claims are gone
    db.unclaim_todo_item(AGENTS["beta"]["token"], pid, item_ids[0])
    db.unclaim_todo_list(AGENTS["beta"]["token"], pid, list_ids[1])
    assert db.set_todo_claim_mode(AGENTS["alpha"]["token"], pid, "item")["changed"]
    print("  hybrid switch never blocked + round-trip: ok")


def test_hybrid_get_todos_shape():
    """In hybrid mode both claim kinds ride the board: list-level claim keys
    AND per-item claim keys, with claim_mode == 'hybrid'."""
    pid, list_ids, item_ids = _make_collab(mode="hybrid", joiner="beta")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    db.claim_todo_item(AGENTS["beta"]["token"], pid, item_ids[1])
    todos = db.get_todos_for_post(pid)
    for lst in todos:
        assert lst["claim_mode"] == "hybrid"
        if lst["id"] == list_ids[0]:
            assert lst["claimed_by"] == AGENTS["beta"]["name"]
            assert (
                lst["items"][0].get("claimed_by") is None
                or lst["items"][0].get("claimed_by") == AGENTS["beta"]["name"]
            )
        elif lst["id"] == list_ids[1]:
            assert "claimed_by" not in lst
            assert lst["items"][0]["claimed_by"] == AGENTS["beta"]["name"], (
                "item claim rides in hybrid mode"
            )
    print("  hybrid get_todos shape: ok")


def test_hybrid_gate_accepts_either_reservation():
    """PR-link gate in hybrid mode: a held item claim OR a held whole-list
    claim satisfies link_pr_to_proposal (none is refused, naming both
    remedies)."""
    pid, list_ids, item_ids = _make_collab(mode="hybrid", joiner="gamma")
    old = _set_flag("FORUM_TODO_CLAIM_REQUIRED", "1")
    try:
        # no claim: gate raises naming both tools
        assert "requires claiming a to-do item or a whole to-do list" in expect_error(
            db.link_pr_to_proposal, 93050 + pid, pid, AGENTS["gamma"]["agent_id"]
        )
        # item claim satisfies it
        db.claim_todo_item(AGENTS["gamma"]["token"], pid, item_ids[0])
        db.link_pr_to_proposal(93051 + pid, pid, AGENTS["gamma"]["agent_id"])
        assert db.proposal_for_pr(93051 + pid) == pid
        # a list claim satisfies it too (fresh proposal)
        pid2, list_ids2, _ = _make_collab(mode="hybrid", joiner="delta")
        db.claim_todo_list(AGENTS["delta"]["token"], pid2, list_ids2[0])
        db.link_pr_to_proposal(93052 + pid2, pid2, AGENTS["delta"]["agent_id"])
        assert db.proposal_for_pr(93052 + pid2) == pid2
    finally:
        _restore_flag("FORUM_TODO_CLAIM_REQUIRED", old)
    print("  hybrid gate accepts item or list claim: ok")


def test_close_proposal_releases_list_claims():
    """Auto-release on proposal close: close_proposal clears any remaining
    whole-list claims alongside per-item ones (release_claims_for_proposal)."""
    pid, list_ids, _ = _make_collab(mode="list", joiner="beta")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    # Give the proposal a decided PR so it can close (all decided PRs).
    _pr = 98000 + pid
    db.link_pr_to_proposal(_pr, pid, AGENTS["alpha"]["agent_id"])
    db.record_proposal_outcome(_pr, pid, "merged", "2026-08-27T12:00:00.000Z")
    res = db.close_proposal(AGENTS["alpha"]["token"], pid)
    assert res["status"] in ("merged", "closed")
    todos = db.get_todos_for_post(pid)
    assert not any("claimed_by" in l for l in todos), (
        "closing the proposal releases its list claims"
    )
    print("  close_proposal releases list claims: ok")


def test_release_functions_clear_list_claims():
    pid, list_ids, item_ids = _make_collab(mode="list")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    with db._conn() as c:
        freed = db.release_claims_for_proposal(pid, conn=c)
    assert freed == 1
    todos = db.get_todos_for_post(pid)
    assert not any("claimed_by" in l for l in todos)
    # agent-scoped release
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    with db._conn() as c:
        freed = db.release_claims_for_agent(pid, AGENTS["beta"]["agent_id"], conn=c)
    assert freed == 1
    todos = db.get_todos_for_post(pid)
    assert not any("claimed_by" in l for l in todos)
    print("  release functions clear list claims: ok")


def test_rewrite_preserves_list_claim():
    pid, list_ids, _ = _make_collab(mode="list")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    # A bulk rewrite that keeps the same list title keeps the claim (it is
    # matched by title, mirroring how item claims re-attach by text).
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        pid,
        [
            {"title": "A", "items": [{"text": "a1"}, {"text": "a1b"}]},
            {"title": "B", "items": [{"text": "b1"}]},
        ],
    )
    todos = db.get_todos_for_post(pid)
    lst = [l for l in todos if l["title"] == "A"][0]
    assert lst.get("claimed_by") == AGENTS["beta"]["name"], (
        "list claim survives a same-title rewrite"
    )
    # Renaming the category drops the claim (by-title match finds nothing).
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        pid,
        [
            {"title": "A-prime", "items": [{"text": "a1"}]},
            {"title": "B", "items": [{"text": "b1"}]},
        ],
    )
    todos = db.get_todos_for_post(pid)
    lst = [l for l in todos if l["title"] == "A-prime"][0]
    assert "claimed_by" not in lst, "renaming a category drops its list claim"


def test_supersede_preserves_list_claim_and_goal():
    """Superseding a collaborative proposal preserves the claiming state:
    todo_claim_mode, held whole-list claims and the PR goal all survive
    the rewrite that creates the locked successor."""
    pid, list_ids, _ = _make_collab(mode="list", joiner="beta")
    db.claim_todo_list(AGENTS["beta"]["token"], pid, list_ids[0])
    db.set_proposal_goal(AGENTS["alpha"]["token"], pid, pr_goal=3)
    res = db.supersede_proposal(
        AGENTS["alpha"]["token"],
        pid,
        title="List claim v2",
        body="revised body",
    )
    new_pid = res["post_id"]
    # claim mode carried over
    todos = db.get_todos_for_post(new_pid)
    assert all(lst["claim_mode"] == "list" for lst in todos), (
        "list claim mode survives supersede"
    )
    # whole-list claim restored by title
    lst = [l for l in todos if l["title"] == "A"][0]
    assert lst.get("claimed_by") == AGENTS["beta"]["name"], (
        "a held list claim survives supersede"
    )
    new_l = db.list_proposal_collaborators(new_pid)
    assert AGENTS["beta"]["agent_id"] in [c["agent_id"] for c in new_l]
    # PR goal carried over
    assert db.get_post(new_pid)["proposal"]["pr_goal"] == 3, (
        "PR goal survives supersede"
    )


def main():
    test_migration_columns_present()
    test_mode_default_and_get_todos_shape()
    test_set_mode_author_only_and_collab_only()
    test_set_mode_idempotent_and_get_todos_shape()
    test_exclusive_tools()
    test_claim_and_unclaim_list()
    test_cap_and_undone_requirement()
    test_mode_switch_blocked_by_claims()
    test_mode_switch_sweeps_expired_claim()
    test_list_claimer_ticks_own_item()
    test_claim_gate_accepts_list_claim()
    test_sweep_releases_expired_list_claim()
    test_rewrite_drops_expired_list_claim()
    test_release_functions_clear_list_claims()
    test_hybrid_mode_allows_both_tools()
    test_hybrid_item_under_claimed_list_refused()
    test_hybrid_switch_never_blocked()
    test_hybrid_get_todos_shape()
    test_hybrid_gate_accepts_either_reservation()
    test_rewrite_preserves_list_claim()
    test_close_proposal_releases_list_claims()
    test_supersede_preserves_list_claim_and_goal()
    print("test_todo_list_claim: all assertions passed")


if __name__ == "__main__":
    main()
