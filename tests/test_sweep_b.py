"""Tests for the PR vote sweep — shard B (7/22).

Covers: handles_decline_error, rebase_conflict_skips_merge, ci_fails_after_rebase,
normal_when_toggle_off, declines_when_toggle_off, merge_delayed_when_young,
merge_proceeds_when_old.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_sweep_b_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
import github  # noqa: E402, I001
import events  # noqa: E402, I001
import config  # noqa: E402, I001
from server.poller import _pr_vote_sweep  # noqa: E402, I001

# -- shared setup --
AGENTS, _ = setup()

_orig = {
    "open_prs": github.open_prs,
    "pr_has_label": github.pr_has_label,
    "pr_checks": github.pr_checks,
    "merge_pr": github.merge_pr,
    "decline_pr": github.decline_pr,
    "rebase_pr_onto_main": github.rebase_pr_onto_main,
    "wait_for_ci": github.wait_for_ci,
}


# -- helpers --


def _make_small_fix(opener_name="alpha"):
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
    def fake():
        return list(prs)

    return fake


def _stub_pr_has_label(hold=False):
    def fake(number, label):
        if label == "hold":
            return hold
        return False

    return fake


def _stub_pr_checks(state="success"):
    def fake(number, *, _head_sha=None):
        return {"state": state}

    return fake


def _stub_rebase(status="ok", files=None):
    def fake(number, **kw):
        if status == "ok":
            return {"status": "ok", "new_sha": "rebased_sha"}
        return {"status": status, "files": files or []}

    return fake


def _stub_wait_ci(result="success"):
    def fake(number, **kw):
        return result

    return fake


class _CallLog:
    def __init__(self):
        self.calls = []

    def merge(self, number, **kw):
        self.calls.append(("merge", number))
        return {"pr_number": number, "merged": True, "sha": ""}

    def decline(self, number, **kw):
        self.calls.append(("decline", number))
        return {"pr_number": number}

    def rebase(self, number, **kw):
        self.calls.append(("rebase", number))
        return {"status": "ok", "new_sha": "rebased_sha"}

    def wait_ci(self, number, **kw):
        self.calls.append(("wait_ci", number))
        return "success"


def _open_pr_dict(number, citizen=None, created_at=""):
    d = {
        "number": number,
        "title": "test",
        "head": "branch",
        "base": "main",
        "author": "nobody",
        "created_at": created_at,
        "html_url": "",
        "mergeable_state": "clean",
        "body": "",
        "head_sha": "sha",
        "citizen": citizen,
    }
    return d


def _patch(**attrs):
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
        rebase_pr_onto_main=_stub_rebase("ok"),
        wait_for_ci=_stub_wait_ci("success"),
    ):
        _pr_vote_sweep()

    evts = events.query_events(kind="pr_auto_declined", target_id=pr_number)
    assert len(evts) == 0, "no auto_declined event should be logged on failure"
    print("  sweep handles decline error: ok")


def test_sweep_rebase_conflict_skips_merge():
    """rebase_pr_onto_main returns conflict -> merge NOT attempted."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}

    def conflict_rebase(number, **kw):
        log.calls.append(("rebase", number))
        return {"status": "conflict", "files": ["server.py", "db/_core.py"]}

    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
        rebase_pr_onto_main=conflict_rebase,
        wait_for_ci=log.wait_ci,
    ):
        actions = _pr_vote_sweep()

    assert ("rebase", pr_number) in log.calls, f"rebase should be called: {actions}"
    assert ("wait_ci", pr_number) not in log.calls, (
        "wait_for_ci should not be called on conflict"
    )
    assert ("merge", pr_number) not in log.calls, (
        "merge should not be called on conflict"
    )
    evts = events.query_events(kind="pr_auto_merged", target_id=pr_number)
    assert len(evts) == 0, "no auto_merge event on rebase conflict"
    print("  sweep rebase conflict skips merge: ok")


def test_sweep_ci_fails_after_rebase():
    """Rebase succeeds but CI fails after rebase -> merge NOT attempted."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}

    def failing_wait_ci(number, **kw):
        log.calls.append(("wait_ci", number))
        return "failure"

    with _patch(
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=failing_wait_ci,
    ):
        actions = _pr_vote_sweep()

    assert ("rebase", pr_number) in log.calls, f"rebase should be called: {actions}"
    assert ("wait_ci", pr_number) in log.calls, "wait_for_ci should be called"
    assert ("merge", pr_number) not in log.calls, (
        "merge should not be called when CI fails"
    )
    evts = events.query_events(kind="pr_auto_merged", target_id=pr_number)
    assert len(evts) == 0, "no auto_merge event when CI fails after rebase"
    print("  sweep CI fails after rebase: ok")


def test_sweep_normal_proposal_when_toggle_off():
    """Normal-proposal PR merges when PR_AUTO_MERGE_SMALL_FIX_ONLY=0."""
    proposal = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Normal proposal toggle {_counter[0]}",
        "Body",
        small_fix=False,
    )
    _counter[0] += 1
    pid = proposal["post_id"]
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        if db.proposal_vote_state(pid)["approved"]:
            break
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    assert db.proposal_vote_state(pid)["approved"] is True
    pr_number = 8000 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    old = os.environ.get("FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY")
    try:
        os.environ["FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY"] = "0"
        import importlib

        import config as _cfg

        importlib.reload(_cfg)

        log = _CallLog()
        opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
        with _patch(
            open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
            pr_has_label=_stub_pr_has_label(hold=False),
            pr_checks=_stub_pr_checks("success"),
            merge_pr=log.merge,
            decline_pr=log.decline,
            rebase_pr_onto_main=log.rebase,
            wait_for_ci=log.wait_ci,
        ):
            _pr_vote_sweep()

        assert ("merge", pr_number) in log.calls, (
            f"merge_pr should be called for normal proposal when toggle=0: {log.calls}"
        )
    finally:
        if old is None:
            os.environ.pop("FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY", None)
        else:
            os.environ["FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY"] = old
        importlib.reload(_cfg)
    print("  sweep normal proposal toggle off: ok")


def test_sweep_declines_normal_proposal_when_toggle_off():
    """Normal-proposal PR declines when enough oppose and toggle=0."""
    proposal = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Normal decline toggle {_counter[0]}",
        "Body",
        small_fix=False,
    )
    _counter[0] += 1
    pid = proposal["post_id"]
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        if db.proposal_vote_state(pid)["approved"]:
            break
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    assert db.proposal_vote_state(pid)["approved"] is True
    pr_number = 8000 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, -1)

    old = os.environ.get("FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY")
    old_grace = config.PR_DECLINE_GRACE_SECONDS
    try:
        os.environ["FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY"] = "0"
        import importlib

        import config as _cfg

        importlib.reload(_cfg)
        config.PR_DECLINE_GRACE_SECONDS = 0

        log = _CallLog()
        opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
        with _patch(
            open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
            pr_has_label=_stub_pr_has_label(hold=False),
            pr_checks=_stub_pr_checks("success"),
            merge_pr=log.merge,
            decline_pr=log.decline,
            rebase_pr_onto_main=log.rebase,
            wait_for_ci=log.wait_ci,
        ):
            _pr_vote_sweep()

        assert ("decline", pr_number) in log.calls, (
            f"decline_pr should be called for normal proposal when toggle=0: {log.calls}"
        )
    finally:
        if old is None:
            os.environ.pop("FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY", None)
        else:
            os.environ["FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY"] = old
        importlib.reload(_cfg)
        config.PR_DECLINE_GRACE_SECONDS = old_grace
    print("  sweep declines normal proposal toggle off: ok")


def test_sweep_merge_delayed_when_young():
    """Merge-eligible PR created < PR_MERGE_MIN_AGE_SECONDS ago is NOT merged."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(
            _open_pr_dict(pr_number, citizen=opener, created_at=recent)
        ),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
    ):
        _pr_vote_sweep()
    assert not log.calls, f"young PR must not auto-merge yet: {log.calls}"
    print("  sweep merge delayed when young: ok")


def test_sweep_merge_proceeds_when_old():
    """Merge-eligible PR older than PR_MERGE_MIN_AGE_SECONDS IS merged."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(
            _open_pr_dict(pr_number, citizen=opener, created_at=old)
        ),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
    ):
        actions = _pr_vote_sweep()
    assert ("merge", pr_number) in log.calls, f"old PR should auto-merge: {actions}"
    print("  sweep merge proceeds when old: ok")


# -- run all --
if __name__ == "__main__":
    test_sweep_handles_decline_error()
    test_sweep_rebase_conflict_skips_merge()
    test_sweep_ci_fails_after_rebase()
    test_sweep_normal_proposal_when_toggle_off()
    test_sweep_declines_normal_proposal_when_toggle_off()
    test_sweep_merge_delayed_when_young()
    test_sweep_merge_proceeds_when_old()
    print("\n== test_sweep_b: all passed ==")
