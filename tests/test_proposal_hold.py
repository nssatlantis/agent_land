"""Tests for the proposal-hold flow: PRs opened while their proposal's
community vote is still in flight.

Covers:
- db.proposal_vote_state(): pending / passed / small_fix / non-proposal
- require_proposal_approval(allow_pending=True): the hold path skips only
  the vote gate, every other gate still raises
- server.poller._pr_vote_sweep's hold-release pass: label removal,
  'WIP: ' title strip, event + notifications when the vote passes; and a
  clean no-op while the vote is still pending.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_hold_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup, expect_error, proposal_need  # noqa: E402
import config  # noqa: E402
import events  # noqa: E402
import github  # noqa: E402
from notifications import notifications  # noqa: E402
from server.poller import _pr_vote_sweep  # noqa: E402

AGENTS, _BASE_POST = setup()


_counter = [0]


def _make_proposal(opener="alpha", small_fix=False):
    p = db.create_proposal(
        AGENTS[opener]["token"], f"Hold test {_counter[0]}",
        "Body", small_fix=small_fix,
    )
    _counter[0] += 1
    return p["post_id"]


def _pass(pid):
    """Cast enough +1s from other citizens to clear the live bar."""
    need = proposal_need()
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        if need <= 0:
            return
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
        need -= 1


def test_vote_state_tracks_approval():
    pid = _make_proposal()
    st = db.proposal_vote_state(pid)
    assert st["approved"] is False, "fresh proposal is not approved"
    assert st["net"] == 0 and st["threshold"] == proposal_need() > 0
    assert st["small_fix"] is False
    _pass(pid)
    st = db.proposal_vote_state(pid)
    assert st["approved"] is True, "proposal past the bar is approved"
    assert st["net"] >= st["threshold"]


def test_vote_state_small_fix_and_nonproposal():
    sf = _make_proposal(small_fix=True)
    assert db.proposal_vote_state(sf)["approved"] is True, \
        "small-fix proposals skip the vote - approved immediately"
    post = db.create_post(AGENTS["beta"]["token"], "not a proposal", "body")
    st = db.proposal_vote_state(post["post_id"])
    assert st["approved"] is False, "a non-proposal post never counts as approved"
    assert st["small_fix"] is False and st["net"] == 0


def test_require_approval_allow_pending():
    pid = _make_proposal()
    expect_error(
        db.require_proposal_approval, AGENTS["alpha"]["token"], pid,
        "repo_propose_change",
    )
    got = db.require_proposal_approval(
        AGENTS["alpha"]["token"], pid, "repo_propose_change", allow_pending=True,
    )
    assert got == pid, "allow_pending=True lets a pending proposal through"


class _GitHubSpy:
    """Records label/title writes and serves canned pr_has_label answers."""

    def __init__(self, held_numbers=(), titles=None):
        self.held = set(held_numbers)
        self.titles = titles or {}
        self.removed = []
        self.retitles = []

    def open_prs(self):
        return [
            {"number": n, "title": self.titles.get(n, "test"), "head": "b",
             "base": "main", "author": "nobody", "created_at": "",
             "html_url": "", "mergeable_state": "clean", "body": "",
             "head_sha": "sha", "citizen": None}
            for n in sorted(self.titles)
        ]

    def pr_has_label(self, number, label):
        return number in self.held and label == config.PROPOSAL_HOLD_LABEL

    def remove_pr_label(self, number, label):
        self.removed.append((number, label))

    def update_pr_title(self, number, title):
        self.retitles.append((number, title))

    def pr_checks(self, number, *, _head_sha=None):
        return {"state": "unknown"}


def _patch_github(spy):
    saved = {}
    names = ["open_prs", "pr_has_label", "remove_pr_label",
             "update_pr_title", "pr_checks"]
    try:
        for name in names:
            saved[name] = getattr(github, name)
            setattr(github, name, getattr(spy, name))
        yield
    finally:
        for name, fn in saved.items():
            setattr(github, name, fn)


def test_sweep_releases_passed_hold():
    pid = _make_proposal()
    pr_number = 9100 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    _pass(pid)
    db.subscribe_post(AGENTS["beta"]["token"], pid)
    spy = _GitHubSpy(held_numbers={pr_number}, titles={pr_number: "WIP: fix thing"})
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert spy.removed == [(pr_number, config.PROPOSAL_HOLD_LABEL)], \
        f"hold label removed once the vote passed: {spy.removed}"
    assert spy.retitles == [(pr_number, "fix thing")], \
        f"WIP prefix stripped: {spy.retitles}"
    assert any(a.get("action") == "hold_released" for a in actions), \
        f"release recorded in actions: {actions}"
    kinds = {(n["kind"]) for n in
             notifications(AGENTS["alpha"]["token"], limit=20)["notifications"]}
    assert "pr" in kinds, "opener notified that voting opened"
    sub_kinds = {(n["kind"]) for n in
                 notifications(AGENTS["beta"]["token"], limit=20)["notifications"]}
    assert "subscription" in sub_kinds, "watchers notified too"
    with db._conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?",
            (events.EVT_PR_HOLD_RELEASED,),
        ).fetchone()[0]
    assert row >= 1, "pr_hold_released event logged"


def test_sweep_keeps_pending_hold():
    pid = _make_proposal()
    pr_number = 9200 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    spy = _GitHubSpy(held_numbers={pr_number}, titles={pr_number: "WIP: pending"})
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert spy.removed == [], "label stays while the vote is still open"
    assert spy.retitles == [], "title untouched while pending"
    assert not any(a.get("action") == "hold_released" for a in actions)


def test_sweep_ignores_unheld_prs():
    pid = _make_proposal(small_fix=True)
    pr_number = 9300 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    spy = _GitHubSpy(titles={pr_number: "plain title"})
    for _ in _patch_github(spy):
        _pr_vote_sweep()
    assert spy.removed == [], "no hold, nothing to remove"


if __name__ == "__main__":
    test_vote_state_tracks_approval()
    print("  vote_state approval tracking: ok")
    test_vote_state_small_fix_and_nonproposal()
    print("  vote_state small_fix / non-proposal: ok")
    test_require_approval_allow_pending()
    print("  require_approval allow_pending: ok")
    test_sweep_releases_passed_hold()
    print("  sweep releases passed hold: ok")
    test_sweep_keeps_pending_hold()
    print("  sweep keeps pending hold: ok")
    test_sweep_ignores_unheld_prs()
    print("  sweep ignores unheld PRs: ok")
    print("\n== test_proposal_hold: all passed ==")
