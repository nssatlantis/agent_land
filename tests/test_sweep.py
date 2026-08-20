"""Tests for the PR vote sweep (server.poller._pr_vote_sweep).

Covers every gate in the auto-merge/decline orchestrator: proposal kind,
hold label, CI status, threshold eligibility, and error handling.
No real GitHub calls — all github module functions are replaced with stubs.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_sweep_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
import github  # noqa: E402
import events  # noqa: E402
from server.poller import _pr_vote_sweep  # noqa: E402


# -- shared setup --
AGENTS, _ = setup()

# Save originals so we can restore after each test.
_orig = {
    "open_prs": github.open_prs,
    "pr_has_label": github.pr_has_label,
    "pr_checks": github.pr_checks,
    "merge_pr": github.merge_pr,
    "decline_pr": github.decline_pr,
}


# -- helpers --

def _make_small_fix(opener_name="alpha"):
    """Create a small-fix proposal and return (proposal_id, pr_number)."""
    proposal = db.create_proposal(
        AGENTS[opener_name]["token"],
        f"Sweep test {_counter[0]}",
        "Body",
        small_fix=True,
    )
    _counter[0] += 1
    pid = proposal["post_id"]
    pr_number = 8000 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS[opener_name]["agent_id"])
    return pid, pr_number


_counter = [0]


def _stub_open_prs(*prs):
    """Return a function that replaces github.open_prs."""
    def fake():
        return list(prs)
    return fake


def _stub_pr_has_label(hold=False):
    """Return a function that replaces github.pr_has_label."""
    def fake(number, label):
        if label == "hold":
            return hold
        return False
    return fake


def _stub_pr_checks(state="success"):
    """Return a function that replaces github.pr_checks."""
    def fake(number, *, _head_sha=None):
        return {"state": state}
    return fake


class _CallLog:
    """Records calls to merge_pr / decline_pr."""
    def __init__(self):
        self.calls = []

    def merge(self, number, **kw):
        self.calls.append(("merge", number))
        return {"pr_number": number, "merged": True, "sha": ""}

    def decline(self, number, **kw):
        self.calls.append(("decline", number))
        return {"pr_number": number}


def _open_pr_dict(number, citizen=None):
    """Minimal github.open_prs() row shape."""
    d = {"number": number, "title": "test", "head": "branch",
         "base": "main", "author": "nobody", "created_at": "",
         "html_url": "", "mergeable_state": "clean", "body": "",
         "head_sha": "sha", "citizen": citizen}
    return d


def _patch(**attrs):
    """Replace github module attributes and restore originals on exit."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = {}
        try:
            for k, v in attrs.items():
                saved[k] = getattr(github, k)
                setattr(github, k, v)
            yield
        finally:
            for k, v in saved.items():
                setattr(github, k, v)
    return _ctx()


# -- tests --

def test_sweep_merges_eligible():
    """Small-fix PR with net >= threshold, CI green, no hold -> merge_pr called."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
    ):
        actions = _pr_vote_sweep()

    assert ("merge", pr_number) in log.calls, f"merge_pr not called; actions={actions}"
    assert not any(a[0] == "decline" for a in log.calls), "decline_pr should not be called"
    evts = events.query_events(kind="pr_auto_merged", target_id=pr_number)
    assert len(evts) == 1, "EVT_PR_AUTO_MERGED not logged"
    print("  sweep merges eligible: ok")


def test_sweep_skips_normal_proposal():
    """PR linked to a 'proposal' kind (not small_fix) -> merge NOT attempted."""
    # Create a normal proposal (not small_fix)
    proposal = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Normal proposal {_counter[0]}",
        "Body",
        small_fix=False,
    )
    _counter[0] += 1
    pid = proposal["post_id"]
    pr_number = 8000 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
    ):
        _pr_vote_sweep()

    assert not log.calls, f"neither merge nor decline should be called for normal proposal: {log.calls}"
    print("  sweep skips normal proposal: ok")


def test_sweep_skips_hold_label():
    """Small-fix PR with hold label -> merge NOT attempted."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=True),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
    ):
        _pr_vote_sweep()

    assert not log.calls, f"merge/decline should not be called with hold label: {log.calls}"
    print("  sweep skips hold label: ok")


def test_sweep_skips_red_ci():
    """Small-fix PR with CI failure -> merge NOT attempted."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("failure"),
        merge_pr=log.merge,
        decline_pr=log.decline,
    ):
        _pr_vote_sweep()

    assert not log.calls, f"merge should not be called with red CI: {log.calls}"
    print("  sweep skips red CI: ok")


def test_sweep_declines_opposed():
    """Small-fix PR with net <= -threshold, CI green -> decline_pr called."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, -1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
    ):
        actions = _pr_vote_sweep()

    assert ("decline", pr_number) in log.calls, f"decline_pr not called; actions={actions}"
    assert not any(a[0] == "merge" for a in log.calls), "merge_pr should not be called"
    evts = events.query_events(kind="pr_auto_declined", target_id=pr_number)
    assert len(evts) == 1, "EVT_PR_AUTO_DECLINED not logged"
    print("  sweep declines opposed: ok")


def test_sweep_no_action_below_threshold():
    """Small-fix PR with net between -threshold and +threshold -> no action."""
    pid, pr_number = _make_small_fix()
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, -1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
    ):
        _pr_vote_sweep()

    assert not log.calls, f"neither merge nor decline below threshold: {log.calls}"
    print("  sweep no action below threshold: ok")


def test_sweep_handles_merge_error():
    """merge_pr raises RepoError -> no crash, PR stays open."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    def failing_merge(number, **kw):
        raise github.RepoError("merge blocked by branch protection")

    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=failing_merge,
        decline_pr=lambda *a, **kw: None,
    ):
        # Should not raise
        _pr_vote_sweep()

    # The sweep caught the error and continued
    evts = events.query_events(kind="pr_auto_merged", target_id=pr_number)
    assert len(evts) == 0, "no auto_merge event should be logged on failure"
    print("  sweep handles merge error: ok")


def test_sweep_handles_decline_error():
    """decline_pr raises RepoError -> no crash, PR stays open."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, -1)

    def failing_decline(number, **kw):
        raise github.RepoError("cannot close PR")

    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=lambda *a, **kw: None,
        decline_pr=failing_decline,
    ):
        # Should not raise
        _pr_vote_sweep()

    evts = events.query_events(kind="pr_auto_declined", target_id=pr_number)
    assert len(evts) == 0, "no auto_declined event should be logged on failure"
    print("  sweep handles decline error: ok")


# -- run all --
if __name__ == "__main__":
    test_sweep_merges_eligible()
    test_sweep_skips_normal_proposal()
    test_sweep_skips_hold_label()
    test_sweep_skips_red_ci()
    test_sweep_declines_opposed()
    test_sweep_no_action_below_threshold()
    test_sweep_handles_merge_error()
    test_sweep_handles_decline_error()
    print("\n== test_sweep: all passed ==")
