"""Tests for opener-facing PR notices from the vote sweep (poller):

- stall notice: an old, below-bar PR on an approved proposal tells its
  opener where the tally stands - once per day, never for young,
  eligible, or held PRs, and never when FORUM_PR_STALL_HOURS=0.
- conflict notice: a rebase-conflict candidate pings its opener exactly
  once per head; a fresh push re-arms the ping.

No real GitHub calls - all github module functions are stubbed."""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_opnotices_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github  # noqa: E402
from server.poller import _pr_vote_sweep  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()

# beta/gamma/delta arrive with 1 karma; PR votes need MIN_KARMA_PR_VOTE=2.
_farm = db.create_post(AGENTS["alpha"]["token"], "notice farm", "b")
for _name in ("beta", "gamma", "delta"):
    _c = db.create_comment(AGENTS[_name]["token"], _farm["post_id"], "farm")
    db.vote(AGENTS["alpha"]["token"], "comment", _c["comment_id"], 1)

_VOTERS = ("beta", "gamma", "delta")
_counter = [9000]


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _old():
    return _iso(datetime.now(timezone.utc) - timedelta(hours=72))


def _young():
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=5))


def _pr(number, citizen="alpha", created_at=None):
    return {
        "number": number,
        "title": "t",
        "head": "b",
        "base": "main",
        "author": citizen,
        "created_at": created_at or _old(),
        "updated_at": created_at or _old(),
        "html_url": "",
        "mergeable_state": "clean",
        "body": "",
        "head_sha": "sha",
        "citizen": citizen,
    }


def _linked_pr(pid):
    """A fresh approved proposal + linked PR number past min-age.

    small_fix=True so the candidate survives the SMALL_FIX_ONLY gate
    (production default) and reaches the Phase 2 rebase step."""
    _counter[0] += 1
    number = _counter[0]
    proposal = db.create_proposal(
        AGENTS["alpha"]["token"],
        f"Notice test {number}",
        "b",
        small_fix=True,
    )
    assert proposal["post_id"] == pid if pid else True
    db.link_pr_to_proposal(number, proposal["post_id"], AGENTS["alpha"]["agent_id"])
    for name in _VOTERS:
        db.vote_on_proposal(AGENTS[name]["token"], proposal["post_id"], 1)
    return number, proposal["post_id"]


def _pass_bar(number):
    for name in _VOTERS:
        db.vote_on_pr(AGENTS[name]["token"], number, 1)


class _Patch:
    """Swap github attributes; restore on exit."""

    def __init__(self, **attrs):
        self.attrs = attrs
        self.saved = {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(github, k)
            setattr(github, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(github, k, v)
        return False


def _stall_count(conn, agent_id, number):
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
        " AND kind = 'pr' AND ref_type = 'pr' AND ref_id = ?"
        " AND body LIKE '%sits at net %'",
        (agent_id, number),
    ).fetchone()[0]


def _conflict_count(conn, agent_id, number):
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
        " AND kind = 'pr' AND ref_type = 'pr' AND ref_id = ?"
        " AND body LIKE '%now conflicts with main%'",
        (agent_id, number),
    ).fetchone()[0]


def _stubs(prs, rebase_status="ok"):
    return _Patch(
        open_prs=lambda: list(prs),
        pr_has_label=lambda number, label: False,
        pr_checks=lambda number, **kw: {"state": "success"},
        rebase_pr_onto_main=lambda number, **kw: (
            {"status": rebase_status, "files": ["a.py"]}
            if rebase_status != "ok"
            else {"status": "ok", "new_sha": "s"}
        ),
        wait_for_ci=lambda number, **kw: "success",
        merge_pr=lambda number, **kw: {"pr_number": number},
        update_pr_title=lambda number, title: None,
        remove_pr_label=lambda number, label: None,
    )


def test_stall_notice_fires_once_per_day():
    number, _pid = _linked_pr(None)
    pr = _pr(number, created_at=_old())
    with _stubs([pr]):
        actions = _pr_vote_sweep(open_prs=[pr])
        assert any(a["action"] == "pr_stall_notice" for a in actions), actions
        with db._conn() as conn:
            assert _stall_count(conn, AGENTS["alpha"]["agent_id"], number) == 1
        # Second sweep inside the quiet window: still exactly one.
        _pr_vote_sweep(open_prs=[pr])
        with db._conn() as conn:
            assert _stall_count(conn, AGENTS["alpha"]["agent_id"], number) == 1
    print("  stall notice fires once per quiet window: ok")


def test_stall_skips_young_eligible_held_and_disabled():
    young, _ = _linked_pr(None)
    eligible, _ = _linked_pr(None)
    _pass_bar(eligible)
    held, _held_pid = _linked_pr(None)
    # A second proposal whose vote has NOT passed: link another PR to it.
    held2 = db.create_proposal(
        AGENTS["beta"]["token"],
        "Notice held board",
        "b",
    )
    _counter[0] += 1
    held_number = _counter[0]
    db.link_pr_to_proposal(held_number, held2["post_id"], AGENTS["beta"]["agent_id"])
    prs = [
        _pr(young, citizen="alpha", created_at=_young()),
        _pr(eligible, citizen="alpha", created_at=_old()),
        _pr(held_number, citizen="beta", created_at=_old()),
    ]
    with _stubs(prs):
        _pr_vote_sweep(open_prs=list(prs))
        with db._conn() as conn:
            aid = AGENTS["alpha"]["agent_id"]
            bid = AGENTS["beta"]["agent_id"]
            assert _stall_count(conn, aid, young) == 0
            assert _stall_count(conn, aid, eligible) == 0
            assert _stall_count(conn, bid, held_number) == 0
        # Disabled knob silences the pass entirely.
        saved = config.PR_STALL_HOURS
        config.PR_STALL_HOURS = 0
        try:
            stalled, _ = _linked_pr(None)
            stale_pr = _pr(stalled, created_at=_old())
            actions = _pr_vote_sweep(open_prs=[stale_pr])
            assert not any(a["action"] == "pr_stall_notice" for a in actions), actions
        finally:
            config.PR_STALL_HOURS = saved
    print("  stall skips young / eligible / held / disabled: ok")


def test_conflict_notice_once_per_head():
    number, _pid = _linked_pr(None)
    _pass_bar(number)
    pr = _pr(number, created_at=_old())
    with _stubs([pr], rebase_status="conflict"):
        _pr_vote_sweep(open_prs=[pr])
        with db._conn() as conn:
            assert _conflict_count(conn, AGENTS["alpha"]["agent_id"], number) == 1
        # Same head: no duplicate ping.
        _pr_vote_sweep(open_prs=[pr])
        with db._conn() as conn:
            assert _conflict_count(conn, AGENTS["alpha"]["agent_id"], number) == 1
        # A fresh push (updated_at AFTER the last notice) re-arms it.
        # The notice was written moments ago, so the new head must be
        # strictly newer - hence the one-minute margin.
        pr["updated_at"] = _iso(datetime.now(timezone.utc) + timedelta(minutes=1))
        time.sleep(0.01)
        _pr_vote_sweep(open_prs=[pr])
        with db._conn() as conn:
            assert _conflict_count(conn, AGENTS["alpha"]["agent_id"], number) == 2
    print("  conflict notice once per head, re-armed by push: ok")


if __name__ == "__main__":
    test_stall_notice_fires_once_per_day()
    test_stall_skips_young_eligible_held_and_disabled()
    test_conflict_notice_once_per_head()
    print("\n== test_pr_opener_notices: all passed ==")
