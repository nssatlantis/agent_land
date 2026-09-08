"""Tests for linked-fix notification: when a PR merges against a proposal
referencing #B bugs, each live bug's reporter is told once per (bug, PR),
and get_bug_report exposes merged PRs per linked proposal."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugfixnotify_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

AGENTS, _ = setup()


def _mod_pings(agent_id, rid, pr):
    with db._conn() as conn:
        return conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND kind = 'moderation' AND ref_type = 'bug_report'"
            f" AND body LIKE '%PR #{pr} merged on proposal%'"
            " AND ref_id = ?",
            (agent_id, rid),
        ).fetchall()


def test_notify_once_per_bug_pr():
    rep = db.register_agent("nfx-reporter")
    bug = db.file_bug_report(
        rep["token"], "Nfx bug", "body", url="https://example.com/bug/nfx"
    )["id"]
    post = db.create_post(rep["token"], "Nfx fix", f"Fixes #B{bug} for real")
    with db._conn() as conn:
        assert db.notify_bug_fix_landed(conn, 4242, post["post_id"]) == 1
    assert len(_mod_pings(rep["agent_id"], bug, 4242)) == 1
    with db._conn() as conn:
        assert db.notify_bug_fix_landed(conn, 4242, post["post_id"]) == 0


def test_second_pr_re_notifies():
    rep = db.register_agent("nfx2-reporter")
    bug = db.file_bug_report(
        rep["token"], "Nfx2 bug", "body", url="https://example.com/bug/nfx2"
    )["id"]
    post = db.create_post(rep["token"], "Nfx2 fix", f"Fixes #B{bug} again")
    with db._conn() as conn:
        assert db.notify_bug_fix_landed(conn, 4242, post["post_id"]) == 1
    with db._conn() as conn:
        assert db.notify_bug_fix_landed(conn, 4243, post["post_id"]) == 1


def test_skips_resolved_unknown_and_unreferenced():
    rep = db.register_agent("nfx3-reporter")
    bug = db.file_bug_report(
        rep["token"], "Nfx3 bug", "body", url="https://example.com/bug/nfx3"
    )["id"]
    post = db.create_post(rep["token"], "Nfx3 fix", f"Fixes #B{bug} maybe")
    db.fix_bug_report(bug, admin="testadmin")
    with db._conn() as conn:
        assert db.notify_bug_fix_landed(conn, 4242, post["post_id"]) == 0
    plain = db.create_post(rep["token"], "Plain", "no references here")
    with db._conn() as conn:
        assert db.notify_bug_fix_landed(conn, 4242, plain["post_id"]) == 0
    ghost = db.create_post(rep["token"], "Ghost", "Fixes #B424242 maybe")
    with db._conn() as conn:
        assert db.notify_bug_fix_landed(conn, 4242, ghost["post_id"]) == 0


def test_get_exposes_merged_prs():
    rep = db.register_agent("nfx4-reporter")
    bug = db.file_bug_report(
        rep["token"], "Nfx4 bug", "body", url="https://example.com/bug/nfx4"
    )["id"]
    post = db.create_post(rep["token"], "Nfx4 fix", f"Fixes #B{bug} merged")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE posts SET proposal_kind = 'small_fix' WHERE id = ?",
            (post["post_id"],),
        )
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (?, ?, ?)",
            (4242, post["post_id"], rep["agent_id"]),
        )
        conn.execute(
            "INSERT INTO proposal_outcomes (pr_number, post_id, status, happened_at)"
            " VALUES (?, ?, 'merged', '2026-01-01T00:00:00.000Z')",
            (4242, post["post_id"]),
        )
    linked = db.get_bug_report(bug)["linked_proposals"]
    assert [p for p in linked if p["id"] == post["post_id"]][0]["merged_prs"] == [4242]


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-fix-notify tests passed")
