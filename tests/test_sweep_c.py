"""Tests for the PR vote sweep — shard C (6/22).

Covers: decline_after_grace, batches_multiple_prs,
db_reads_are_batched, drains_past_rebase_conflict, relinks_unlinked_open_prs,
collaborative_digest_sweep.
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_sweep_c_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
import github  # noqa: E402, I001
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
    def fake(number, label, **kw):
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
    assert ("decline", pr_number) in log.calls, (
        f"decline should fire after grace: {actions}"
    )
    print("  sweep decline after grace: ok")


def test_sweep_batches_multiple_prs():
    """Three candidates in one sweep with mixed verdicts."""
    pid_m, pr_merge = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_merge, 1)
    pid_d, pr_decline = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_decline, -1)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_decline_grace (pr_number, since) VALUES (?, ?)",
            (pr_decline, int(time.time()) - 99999),
        )
    pid_n, pr_neutral = _make_small_fix()
    db.vote_on_pr(AGENTS["beta"]["token"], pr_neutral, 1)

    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(
            *[
                _open_pr_dict(n, citizen=opener, created_at=old)
                for n in (pr_merge, pr_decline, pr_neutral)
            ]
        ),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
    ):
        _pr_vote_sweep()

    assert ("merge", pr_merge) in log.calls, f"eligible PR must merge: {log.calls}"
    assert ("decline", pr_decline) in log.calls, f"opposed PR must decline: {log.calls}"
    assert not any(a[1] == pr_neutral for a in log.calls), (
        f"neutral PR must be untouched: {log.calls}"
    )
    print("  sweep batches multiple prs: ok")


def test_sweep_db_reads_are_batched():
    """Regression guard: DB reads stay O(1) per sweep."""
    import contextlib

    import server.poller as poller_mod

    numbers = []
    for _ in range(3):
        pid, pr_number = _make_small_fix()
        for name in ("beta", "gamma", "delta"):
            db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)
        numbers.append(pr_number)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}
    with _patch(
        open_prs=_stub_open_prs(
            *[_open_pr_dict(n, citizen=opener, created_at=old) for n in numbers]
        ),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
        rebase_pr_onto_main=log.rebase,
        wait_for_ci=log.wait_ci,
    ):
        sql_log: list[str] = []

        class _SpyConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                sql_log.append(" ".join(sql.split()))
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, *exc):
                return self._conn.__exit__(*exc)

        real_conn = poller_mod.db._conn

        @contextlib.contextmanager
        def spy_conn(*a, **kw):
            with real_conn(*a, **kw) as c:
                yield _SpyConn(c)

        poller_mod.db._conn = spy_conn
        try:
            _pr_vote_sweep()
        finally:
            poller_mod.db._conn = real_conn

    agent_counts = sum(1 for s in sql_log if "FROM agents" in s)
    assert agent_counts == 1, (
        f"threshold must be derived once per sweep, got {agent_counts}"
        " active-citizen reads"
    )
    tally_batches = [s for s in sql_log if "FROM pr_votes WHERE pr_number IN" in s]
    assert len(tally_batches) == 1, (
        f"exactly one grouped tally expected, got {len(tally_batches)}"
    )
    per_pr_tallies = [s for s in sql_log if "FROM pr_votes WHERE pr_number = ?" in s]
    assert not per_pr_tallies, f"per-PR tallies must be gone: {per_pr_tallies}"
    kind_fetches = [s for s in sql_log if "proposal_kind = 'small_fix'" in s]
    assert len(kind_fetches) == 1, (
        f"one batched small-fix kind fetch expected, got {len(kind_fetches)}"
    )
    merges = [c[1] for c in log.calls if c[0] == "merge"]
    assert merges == numbers, (
        f"every eligible PR must drain in candidate order per sweep: {log.calls}"
    )
    print("  sweep db reads are batched: ok")


def test_sweep_drains_past_rebase_conflict():
    """Conflicted candidate must not starve queue."""
    pid_bad, pr_bad = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_bad, 1)
    pid_good, pr_good = _make_small_fix()
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_good, 1)

    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    log = _CallLog()
    opener = {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]}

    def _rebase_conflicts_first(number, **kw):
        log.calls.append(("rebase", number))
        if number == pr_bad:
            return {"status": "conflict", "files": ["x.py"]}
        return {"status": "ok", "new_sha": f"sha-{number}"}

    with _patch(
        open_prs=_stub_open_prs(
            *[
                _open_pr_dict(n, citizen=opener, created_at=old)
                for n in (pr_bad, pr_good)
            ]
        ),
        pr_has_label=_stub_pr_has_label(hold=False),
        pr_checks=_stub_pr_checks("success"),
        merge_pr=log.merge,
        decline_pr=log.decline,
        rebase_pr_onto_main=_rebase_conflicts_first,
        wait_for_ci=log.wait_ci,
    ):
        actions = _pr_vote_sweep()

    merged = [c[1] for c in log.calls if c[0] == "merge"]
    assert merged == [pr_good], (
        f"conflicted candidate skipped, later one merged: {log.calls}"
    )
    rebased = [c[1] for c in log.calls if c[0] == "rebase"]
    assert rebased == [pr_bad, pr_good], (
        f"both candidates must be attempted in order: {log.calls}"
    )
    assert {"action": "auto_merge", "pr_number": pr_good} in actions
    print("  sweep drains past rebase conflict: ok")


def test_sweep_relinks_unlinked_open_prs():
    """Unlinked open PR retried; links once opener claims."""
    import importlib

    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Relink fixture {_counter[0]}",
        "Body",
        collaborative=True,
    )
    _counter[0] += 1
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS["alpha"]["token"], pid, [{"title": "W", "items": [{"text": "task"}]}]
    )
    items = [it["id"] for it in db.get_todos_for_post(pid)[0]["items"]]
    late = db.register_agent(f"relink-late-{_counter[0]}")
    _counter[0] += 1
    db.join_proposal(late["token"], pid)

    pr_number = 8500 + pid
    row = _open_pr_dict(
        pr_number,
        citizen={"name": late["name"], "agent_id": late["agent_id"]},
        created_at=(datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )
    row["body"] = (
        "Implements the relink item.\n\n"
        f"Proposal: #{pid}\n"
        f"Citizen: {late['name']} (agent_id={late['agent_id']})"
    )

    saved_flag = os.environ.get("FORUM_TODO_CLAIM_REQUIRED")
    os.environ["FORUM_TODO_CLAIM_REQUIRED"] = "1"
    importlib.reload(config)
    try:
        with _patch(
            open_prs=_stub_open_prs(row),
            pr_has_label=_stub_pr_has_label(hold=False),
            pr_checks=_stub_pr_checks("success"),
            merge_pr=lambda *a, **k: None,
            decline_pr=lambda *a, **k: None,
            rebase_pr_onto_main=lambda *a, **k: {"status": "ok", "new_sha": "x"},
            wait_for_ci=lambda *a, **k: "success",
        ):
            _pr_vote_sweep()
            assert db.proposal_for_pr(pr_number) is None, (
                "gate must hold while the opener claims nothing"
            )
            db.claim_todo_item(late["token"], pid, items[0])

            _pr_vote_sweep()
            assert db.proposal_for_pr(pr_number) == pid, (
                "the sweep must relink once the opener holds a claim"
            )
    finally:
        if saved_flag is None:
            os.environ.pop("FORUM_TODO_CLAIM_REQUIRED", None)
        else:
            os.environ["FORUM_TODO_CLAIM_REQUIRED"] = saved_flag
        importlib.reload(config)
    print("  sweep relinks unlinked open prs: ok")


def test_collaborative_digest_sweep():
    """_collaborative_digest_sweep sends digests to collaborators."""
    from server.poller import _collaborative_digest_sweep

    prop = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Collab digest sweep test",
        "body",
        collaborative=True,
    )
    pid = prop["post_id"]
    db.set_todos_for_post(
        AGENTS["alpha"]["token"],
        pid,
        [
            {"title": "Tasks", "items": [{"text": "task1"}, {"text": "task2"}]},
        ],
    )
    db.join_proposal(AGENTS["beta"]["token"], pid)
    from db._nudges import _collab_work_list

    with db._conn() as conn:
        items = _collab_work_list(conn, AGENTS["beta"]["agent_id"])
    assert items, f"beta should have collab work items, got {items}"
    _collaborative_digest_sweep()
    with db._conn() as conn:
        notifs = conn.execute(
            "SELECT kind FROM notifications WHERE agent_id = ?"
            " AND kind = 'collab_digest'",
            (AGENTS["beta"]["agent_id"],),
        ).fetchall()
    assert notifs, "beta should receive collab_digest notification"
    print("  collaborative_digest_sweep sends digest: ok")
    _collaborative_digest_sweep()
    with db._conn() as conn:
        notifs2 = conn.execute(
            "SELECT kind FROM notifications WHERE agent_id = ?"
            " AND kind = 'collab_digest'",
            (AGENTS["beta"]["agent_id"],),
        ).fetchall()
    assert len(notifs2) == 1, "time-gate should suppress second digest"
    print("  collaborative_digest_sweep time-gate: ok")


# -- run all --
if __name__ == "__main__":
    test_sweep_decline_after_grace()
    test_sweep_batches_multiple_prs()
    test_sweep_db_reads_are_batched()
    test_sweep_drains_past_rebase_conflict()
    test_sweep_relinks_unlinked_open_prs()
    test_collaborative_digest_sweep()
    print("\n== test_sweep_c: all passed ==")
