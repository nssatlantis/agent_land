"""Tests for the PR vote sweep (server.poller._pr_vote_sweep).

Covers every gate in the auto-merge/decline orchestrator: proposal kind,
hold label, CI status, threshold eligibility, rebase flow, and error handling.
No real GitHub calls â€” all github module functions are replaced with stubs.
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_sweep_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
import github  # noqa: E402
import events  # noqa: E402
import config  # noqa: E402
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
    "rebase_pr_onto_main": github.rebase_pr_onto_main,
    "wait_for_ci": github.wait_for_ci,
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


def _stub_rebase(status="ok", files=None):
    """Return a function that replaces github.rebase_pr_onto_main."""
    def fake(number, **kw):
        if status == "ok":
            return {"status": "ok", "new_sha": "rebased_sha"}
        return {"status": status, "files": files or []}
    return fake


def _stub_wait_ci(result="success"):
    """Return a function that replaces github.wait_for_ci."""
    def fake(number, **kw):
        return result
    return fake


class _CallLog:
    """Records calls to merge_pr / decline_pr / rebase."""
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
    """Minimal github.open_prs() row shape."""
    d = {"number": number, "title": "test", "head": "branch",
         "base": "main", "author": "nobody", "created_at": created_at,
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
    """Small-fix PR with net >= threshold, CI green, no hold -> rebase + CI + merge."""
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
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
    ):
        actions = _pr_vote_sweep()

    assert ("rebase", pr_number) in log.calls, f"rebase not called; actions={actions}"
    assert ("wait_ci", pr_number) in log.calls, f"wait_for_ci not called; actions={actions}"
    assert ("merge", pr_number) in log.calls, f"merge_pr not called; actions={actions}"
    assert not any(a[0] == "decline" for a in log.calls), "decline_pr should not be called"
    evts = events.query_events(kind="pr_auto_merged", target_id=pr_number)
    assert len(evts) == 1, "EVT_PR_AUTO_MERGED not logged"
    print("  sweep merges eligible: ok")


def test_sweep_skips_normal_proposal():
    """PR linked to a 'proposal' kind (not small_fix) -> merge NOT attempted."""
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
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
    ):
        _pr_vote_sweep()

    assert not log.calls, f"neither merge nor decline for normal proposal: {log.calls}"
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
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
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
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
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
    old_grace = config.PR_DECLINE_GRACE_SECONDS
    config.PR_DECLINE_GRACE_SECONDS = 0
    try:
        with _patch(
            open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener)),
            pr_has_label=_stub_pr_has_label(hold=False),
            pr_checks=_stub_pr_checks("success"),
            merge_pr=log.merge,
            decline_pr=log.decline,
            rebase_pr_onto_main=log.rebase,
            wait_for_ci=log.wait_ci,
        ):
            actions = _pr_vote_sweep()
    finally:
        config.PR_DECLINE_GRACE_SECONDS = old_grace

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
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
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
        rebase_pr_onto_main=_stub_rebase("ok"),
        wait_for_ci=_stub_wait_ci("success"),
    ):
        _pr_vote_sweep()

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
    assert ("wait_ci", pr_number) not in log.calls, "wait_for_ci should not be called on conflict"
    assert ("merge", pr_number) not in log.calls, "merge should not be called on conflict"
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
    assert ("merge", pr_number) not in log.calls, "merge should not be called when CI fails"
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
    pr_number = 8000 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)

    old = os.environ.get("FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY")
    try:
        os.environ["FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY"] = "0"
        import importlib, config as _cfg
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

        assert ("merge", pr_number) in log.calls, \
            f"merge_pr should be called for normal proposal when toggle=0: {log.calls}"
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
    pr_number = 8000 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, -1)

    old = os.environ.get("FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY")
    old_grace = config.PR_DECLINE_GRACE_SECONDS
    try:
        os.environ["FORUM_PR_AUTO_MERGE_SMALL_FIX_ONLY"] = "0"
        import importlib, config as _cfg
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

        assert ("decline", pr_number) in log.calls, \
            f"decline_pr should be called for normal proposal when toggle=0: {log.calls}"
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
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener, created_at=recent)),
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
        open_prs=_stub_open_prs(_open_pr_dict(pr_number, citizen=opener, created_at=old)),
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


def test_sweep_decline_grace_delays():
    """Decline-eligible PR is NOT declined until the grace window elapses."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, -1)
    old_grace = config.PR_DECLINE_GRACE_SECONDS
    config.PR_DECLINE_GRACE_SECONDS = 43200
    try:
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
        assert not log.calls, f"decline must wait out the grace window: {log.calls}"
        with db._conn() as conn:
            row = conn.execute(
                "SELECT since FROM pr_decline_grace WHERE pr_number = ?", (pr_number,)
            ).fetchone()
        assert row is not None, "grace marker should be recorded"
    finally:
        config.PR_DECLINE_GRACE_SECONDS = old_grace
    print("  sweep decline grace delays: ok")


def test_sweep_decline_after_grace():
    """Decline-eligible PR with an expired grace marker IS declined."""
    pid, pr_number = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, -1)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_decline_grace (pr_number, since) VALUES (?, ?)",
            (pr_number, int(time.time()) - 99999),
        )
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
        actions = _pr_vote_sweep()
    assert ("decline", pr_number) in log.calls, f"decline should fire after grace: {actions}"
    print("  sweep decline after grace: ok")


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
    test_sweep_rebase_conflict_skips_merge()
    test_sweep_ci_fails_after_rebase()
    test_sweep_normal_proposal_when_toggle_off()
    test_sweep_declines_normal_proposal_when_toggle_off()
    test_sweep_merge_delayed_when_young()
    test_sweep_merge_proceeds_when_old()
    test_sweep_decline_grace_delays()
    test_sweep_decline_after_grace()
    print("\n== test_sweep: all passed ==")
