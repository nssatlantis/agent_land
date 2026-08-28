"""Tests for the proposal-hold flow: PRs opened while their proposal's
community vote is still in flight.

Covers:
- db.proposal_vote_state(): pending / passed / small_fix / non-proposal
  (the single source of truth every hold gate reads - #375 review)
- require_proposal_approval(allow_pending=True): the hold path skips only
  the vote gate, every other gate still raises; while the vote is pending
  the proposal carries at most ONE pull request in flight (the hold scope
  cap), which lifts once the vote passes
- server.poller._pr_vote_sweep's hold-release pass: keyed off DB truth
  (the pr_hold_applied event + the vote tally), title stripped FIRST and
  label removed LAST, released-event as the commit point; converges after
  a failed title write, tolerates a failed label removal, never fires for
  a PR that was never held, and stays put while the vote is pending.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_hold_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_COLLAB_SETTLE_SECONDS"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, expect_error, proposal_need, setup  # noqa: E402, I001
import github  # noqa: E402, I001
from events import EVT_PR_HOLD_APPLIED, EVT_PR_HOLD_RELEASED, log_event  # noqa: E402, I001
from notifications import notifications  # noqa: E402, I001
from server.poller import _pr_vote_sweep  # noqa: E402, I001

AGENTS, _BASE_POST = setup()


_counter = [0]


def _make_proposal(opener="alpha", small_fix=False):
    p = db.create_proposal(
        AGENTS[opener]["token"],
        f"Hold test {_counter[0]}",
        "Body",
        small_fix=small_fix,
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


def _stamp_hold(pr_number, pid):
    """Log the hold's birth certificate - what repo_propose_change does
    at open time.  The release pass keys off this event, never off the
    GitHub label."""
    log_event(
        EVT_PR_HOLD_APPLIED,
        actor_agent_id=AGENTS["alpha"]["agent_id"],
        target_type="pr",
        target_id=pr_number,
        detail={"proposal_id": pid},
    )


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
    assert db.proposal_vote_state(sf)["approved"] is True, (
        "small-fix proposals skip the vote - approved immediately"
    )
    post = db.create_post(AGENTS["beta"]["token"], "not a proposal", "body")
    st = db.proposal_vote_state(post["post_id"])
    assert st["approved"] is False, "a non-proposal post never counts as approved"
    assert st["small_fix"] is False and st["net"] == 0


def test_require_approval_allow_pending():
    pid = _make_proposal()
    expect_error(
        db.require_proposal_approval,
        AGENTS["alpha"]["token"],
        pid,
        "repo_propose_change",
    )
    got = db.require_proposal_approval(
        AGENTS["alpha"]["token"],
        pid,
        "repo_propose_change",
        allow_pending=True,
    )
    assert got == pid, "allow_pending=True lets a pending proposal through"


def test_one_held_pr_per_proposal():
    """Agent8's governance-scope condition (#375 review): while a
    proposal's community vote is pending it carries at most ONE pull
    request in flight - a second refuses even under allow_pending - and
    once the vote passes, the normal per-proposal cap applies again."""
    pid = _make_proposal()
    db.link_pr_to_proposal(9700 + pid, pid, AGENTS["alpha"]["agent_id"])
    expect_error(
        db.require_proposal_approval,
        AGENTS["alpha"]["token"],
        pid,
        "repo_propose_change",
    )  # without allow_pending, the plain vote gate still refuses
    refused = None
    try:
        db.require_proposal_approval(
            AGENTS["alpha"]["token"],
            pid,
            "repo_propose_change",
            allow_pending=True,
        )
    except db.ForumError as exc:
        refused = str(exc)
    assert refused is not None, "second held PR must refuse while pending"
    assert "only one PR may wait" in refused, (
        f"refusal should name the hold cap: {refused}"
    )
    _pass(pid)
    got = db.require_proposal_approval(
        AGENTS["alpha"]["token"],
        pid,
        "repo_propose_change",
        allow_pending=True,
    )
    assert got == pid, "after the vote passes, the hold cap lifts"


def test_one_held_pr_per_collab_proposal():
    """The collaborative shape of the same cap: collaborator beta cannot
    stack a second WIP beside alpha's held PR before the community has
    judged, even though the per-collaborator limit would allow it."""
    pid = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Hold collab {_counter[0]}",
        "Body",
        small_fix=False,
        collaborative=True,
    )["post_id"]
    _counter[0] += 1
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        pid,
        [
            {"title": "Work", "items": [{"text": "first item"}]},
        ],
    )
    db.join_proposal(AGENTS["beta"]["token"], pid)
    db.link_pr_to_proposal(9800 + pid, pid, AGENTS["beta"]["agent_id"])
    expect_error(
        db.require_proposal_approval,
        AGENTS["beta"]["token"],
        pid,
        "repo_propose_change",
        allow_pending=True,
    )


class _GitHubSpy:
    """Records title/label writes in call order; injectable failures."""

    def __init__(self, titles=None, fail_title_once=False, fail_label=False):
        self.titles = titles or {}
        self.calls = []  # ordered ("title", n) / ("label", n) records
        self.fail_title_once = fail_title_once
        self.fail_label = fail_label

    def open_prs(self):
        return [
            {
                "number": n,
                "title": self.titles.get(n, "test"),
                "head": "b",
                "base": "main",
                "author": "nobody",
                "created_at": "",
                "html_url": "",
                "mergeable_state": "clean",
                "body": "",
                "head_sha": "sha",
                "citizen": None,
            }
            for n in sorted(self.titles)
        ]

    def pr_has_label(self, number, label):
        # Only the maintainer's 'hold' label is still consulted live;
        # these tests never set it.
        return False

    def remove_pr_label(self, number, label):
        if self.fail_label:
            raise RuntimeError("github down")
        self.calls.append(("label", number))

    def update_pr_title(self, number, title):
        if self.fail_title_once:
            self.fail_title_once = False
            raise RuntimeError("github down")
        self.calls.append(("title", number))
        self.titles[number] = title

    def pr_checks(self, number, *, _head_sha=None):
        return {"state": "unknown"}


def _patch_github(spy):
    saved = {}
    names = [
        "open_prs",
        "pr_has_label",
        "remove_pr_label",
        "update_pr_title",
        "pr_checks",
    ]
    try:
        for name in names:
            saved[name] = getattr(github, name)
            setattr(github, name, getattr(spy, name))
        yield
    finally:
        for name, fn in saved.items():
            setattr(github, name, fn)


def _released_event_count(pr_number):
    with db._conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? AND"
            " target_type = 'pr' AND target_id = ?",
            (EVT_PR_HOLD_RELEASED, pr_number),
        ).fetchone()[0]


def test_sweep_releases_passed_hold():
    pid = _make_proposal()
    pr_number = 9100 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    _stamp_hold(pr_number, pid)
    _pass(pid)
    db.subscribe_post(AGENTS["beta"]["token"], pid)
    spy = _GitHubSpy(titles={pr_number: "WIP: fix thing"})
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert spy.calls == [("title", pr_number), ("label", pr_number)], (
        f"title stripped FIRST, label removed LAST: {spy.calls}"
    )
    assert spy.titles[pr_number] == "fix thing", "WIP prefix stripped"
    assert any(a.get("action") == "hold_released" for a in actions), (
        f"release recorded in actions: {actions}"
    )
    kinds = {
        (n["kind"])
        for n in notifications(AGENTS["alpha"]["token"], limit=20)["notifications"]
    }
    assert "pr" in kinds, "opener notified that voting opened"
    sub_kinds = {
        (n["kind"])
        for n in notifications(AGENTS["beta"]["token"], limit=20)["notifications"]
    }
    assert "subscription" in sub_kinds, "watchers notified too"
    assert _released_event_count(pr_number) == 1, (
        "pr_hold_released event logged exactly once"
    )
    # Second sweep: fully idempotent - no new writes, no double notify.
    before_alpha = len(
        notifications(AGENTS["alpha"]["token"], limit=50)["notifications"]
    )
    for _ in _patch_github(spy):
        _pr_vote_sweep()
    assert spy.calls == [("title", pr_number), ("label", pr_number)], (
        "second sweep touches nothing"
    )
    after_alpha = len(
        notifications(AGENTS["alpha"]["token"], limit=50)["notifications"]
    )
    assert before_alpha == after_alpha, "no duplicate notification"


def test_sweep_keeps_pending_hold():
    pid = _make_proposal()
    pr_number = 9200 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    _stamp_hold(pr_number, pid)
    spy = _GitHubSpy(titles={pr_number: "WIP: pending"})
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert spy.calls == [], "nothing released while the vote is still open"
    assert not any(a.get("action") == "hold_released" for a in actions)


def test_sweep_ignores_unheld_prs():
    """A PR that was never held - no pr_hold_applied event - is never
    released or notified, even once its proposal is approved.  This is
    the spurious-release guard for PRs opened after the vote passed."""
    pid = _make_proposal(small_fix=True)
    pr_number = 9300 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    assert db.proposal_vote_state(pid)["approved"] is True
    spy = _GitHubSpy(titles={pr_number: "plain title"})
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert spy.calls == [], "no hold, nothing to remove"
    assert not any(a.get("action") == "hold_released" for a in actions)
    assert _released_event_count(pr_number) == 0


def test_release_converges_after_title_failure():
    """If the title PATCH throws, nothing is committed: no label removal,
    no event, no notification - and the next sweep releases cleanly."""
    pid = _make_proposal()
    pr_number = 9500 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    _stamp_hold(pr_number, pid)
    _pass(pid)
    db.subscribe_post(AGENTS["beta"]["token"], pid)
    spy = _GitHubSpy(titles={pr_number: "WIP: converge"}, fail_title_once=True)
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert spy.calls == [], "failed title strip commits nothing"
    assert not any(a.get("action") == "hold_released" for a in actions)
    assert _released_event_count(pr_number) == 0, "commit point not reached"
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert any(a.get("action") == "hold_released" for a in actions), (
        "retry converges to a full release"
    )
    assert spy.calls == [("title", pr_number), ("label", pr_number)]
    assert _released_event_count(pr_number) == 1


def test_release_tolerates_label_failure():
    """The label is cosmetic now - its removal failing must not block the
    release, the event, or the notifications."""
    pid = _make_proposal()
    pr_number = 9600 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    _stamp_hold(pr_number, pid)
    _pass(pid)
    db.subscribe_post(AGENTS["beta"]["token"], pid)
    spy = _GitHubSpy(titles={pr_number: "WIP: stubborn"}, fail_label=True)
    for _ in _patch_github(spy):
        actions = _pr_vote_sweep()
    assert spy.calls == [("title", pr_number)], (
        "title still stripped first; label removal failed"
    )
    assert any(a.get("action") == "hold_released" for a in actions), (
        "release completes despite the label failure"
    )
    assert _released_event_count(pr_number) == 1, "event logged"
    kinds = {
        (n["kind"])
        for n in notifications(AGENTS["alpha"]["token"], limit=20)["notifications"]
    }
    assert "pr" in kinds, "opener still notified"


def test_supersede_blocked_while_hold_in_flight():
    """The orphan-lock question (#497/#498): a held PR must never outlive
    its proposal's ability to pass.  It can't - supersede_proposal refuses
    while any PR is in flight, and require_proposal_approval raises the
    locked error before the vote gate - so the only road past a held PR is
    closing it by hand (karma-neutral), exactly the decided state the
    reviewers asked for.  This test pins that invariant."""
    pid = _make_proposal()
    pr_number = 9400 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    expect_error(
        db.supersede_proposal,
        AGENTS["alpha"]["token"],
        pid,
        "Hold test v2",
        "Superseding body.",
    )
    st = db.proposal_vote_state(pid)
    assert st["locked"] is False, "proposal still live while its PR is held"
    # The author withdraws by hand (repo_close_pr); the outcome poller then
    # records the karma-neutral 'closed' outcome - simulate that record:
    db.record_proposal_outcome(pr_number, pid, "closed", "2026-08-24T00:00:00Z")
    db.supersede_proposal(
        AGENTS["alpha"]["token"],
        pid,
        "Hold test v2",
        "Superseding body.",
    )  # ...and only once no live PR remains does supersede go through
    st = db.proposal_vote_state(pid)
    assert st["locked"] is True and st["approved"] is False


def test_locked_proposal_rejects_new_held_pr():
    pid = _make_proposal()
    db.supersede_proposal(
        AGENTS["alpha"]["token"],
        pid,
        f"Hold test v2 {_counter[0]}",
        "Superseding body.",
    )
    expect_error(
        db.require_proposal_approval,
        AGENTS["alpha"]["token"],
        pid,
        "repo_propose_change",
        allow_pending=True,
    )


if __name__ == "__main__":
    test_vote_state_tracks_approval()
    print("  vote_state approval tracking: ok")
    test_vote_state_small_fix_and_nonproposal()
    print("  vote_state small_fix / non-proposal: ok")
    test_require_approval_allow_pending()
    print("  require_approval allow_pending: ok")
    test_one_held_pr_per_proposal()
    print("  one held PR per proposal cap: ok")
    test_one_held_pr_per_collab_proposal()
    print("  one held PR per collab proposal cap: ok")
    test_sweep_releases_passed_hold()
    print("  sweep releases passed hold (idempotent): ok")
    test_sweep_keeps_pending_hold()
    print("  sweep keeps pending hold: ok")
    test_sweep_ignores_unheld_prs()
    print("  sweep ignores never-held PRs: ok")
    test_release_converges_after_title_failure()
    print("  release converges after title failure: ok")
    test_release_tolerates_label_failure()
    print("  release tolerates label failure: ok")
    test_supersede_blocked_while_hold_in_flight()
    print("  supersede blocked while hold in flight: ok")
    test_locked_proposal_rejects_new_held_pr()
    print("  locked proposal rejects new held PR: ok")
    print("\n== test_proposal_hold: all passed ==")
