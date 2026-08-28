"""Tests for repo_get_pr enhancements: ci_note field, include_diff
parameter, and the proposal-hold message wording."""

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_prview_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, _ = setup()

# Load server package under a private name so tests can monkeypatch its github.*
_ROOT = Path(__file__).resolve().parent.parent / "server" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "agentland_root_server_prview",
    _ROOT,
)
root_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_server)


def _payload(number, checks_state="unknown", checks_source="stub"):
    """Minimal aget_pr-shaped dict with configurable checks."""
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "head": f"branch-{number}",
        "base": "main",
        "author": "nssatlantis",
        "state": "open",
        "outcome": "open",
        "mergeable": None,
        "mergeable_state": None,
        "commits": 1,
        "created_at": "2026-08-25T00:00:00Z",
        "html_url": f"https://github.com/nssatlantis/agent_land/pull/{number}",
        "checks": {
            "source": checks_source,
            "state": checks_state,
            "runs": [
                {
                    "name": "test",
                    "status": "completed",
                    "conclusion": checks_state,
                    "html_url": "https://example.com/1",
                },
            ],
            "failures": [],
        },
        "comments": [],
        "files": [],
    }


def _payload_multi_run(number, n_runs=3):
    """Payload with multiple check runs (for the run-count suffix)."""
    p = _payload(number, checks_state="success")
    p["checks"]["runs"] = [
        {
            "name": f"job-{i}",
            "status": "completed",
            "conclusion": "success",
            "html_url": f"https://example.com/{i}",
        }
        for i in range(n_runs)
    ]
    return p


def _install_mock(payload_by_number):
    real = root_server.github.aget_pr

    async def fake(number):
        p = payload_by_number.get(number)
        if p is None:
            raise root_server.github.RepoError(
                f"pull request #{number} not found.",
            )
        return dict(p)

    root_server.github.aget_pr = fake
    return real


def _install_diff_mock(diff_by_number):
    real = root_server.github.apr_diff

    async def fake(number):
        d = diff_by_number.get(number)
        if d is None:
            raise root_server.github.RepoError(
                f"pull request #{number} not found.",
            )
        return dict(d)

    root_server.github.apr_diff = fake
    return real


# -- ci_note tests -------------------------------------------------------


def test_ci_note_success():
    real = _install_mock({1: _payload(1, checks_state="success")})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=1))
        assert got["ci_note"] == "CI: passing", got["ci_note"]
    finally:
        root_server.github.aget_pr = real
    print("  ci_note success: ok")


def test_ci_note_failure():
    real = _install_mock({2: _payload(2, checks_state="failure")})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=2))
        assert got["ci_note"] == "CI: failing", got["ci_note"]
    finally:
        root_server.github.aget_pr = real
    print("  ci_note failure: ok")


def test_ci_note_pending():
    real = _install_mock({3: _payload(3, checks_state="pending")})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=3))
        assert got["ci_note"] == "CI: pending", got["ci_note"]
    finally:
        root_server.github.aget_pr = real
    print("  ci_note pending: ok")


def test_ci_note_unknown_source():
    real = _install_mock({4: _payload(4, checks_state="unknown", checks_source=None)})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=4))
        assert got["ci_note"] == "CI: unknown", got["ci_note"]
    finally:
        root_server.github.aget_pr = real
    print("  ci_note unknown: ok")


def test_ci_note_run_count_suffix():
    real = _install_mock({5: _payload_multi_run(5, n_runs=3)})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=5))
        assert got["ci_note"] == "CI: passing (3 runs)", got["ci_note"]
    finally:
        root_server.github.aget_pr = real
    print("  ci_note run count: ok")


def test_ci_note_single_run_no_suffix():
    real = _install_mock({6: _payload(6, checks_state="success")})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=6))
        assert got["ci_note"] == "CI: passing", got["ci_note"]
    finally:
        root_server.github.aget_pr = real
    print("  ci_note single run no suffix: ok")


def test_ci_note_batch_mode():
    real = _install_mock(
        {
            7: _payload(7, checks_state="success"),
            8: _payload(8, checks_state="failure"),
        }
    )
    try:
        got = asyncio.run(root_server.repo_get_pr(numbers=[7, 8]))
        assert got[7]["ci_note"] == "CI: passing", got[7]["ci_note"]
        assert got[8]["ci_note"] == "CI: failing", got[8]["ci_note"]
    finally:
        root_server.github.aget_pr = real
    print("  ci_note batch mode: ok")


# -- include_diff tests --------------------------------------------------


def test_include_diff_false_by_default():
    real = _install_mock({10: _payload(10)})
    try:
        got = asyncio.run(root_server.repo_get_pr(number=10))
        assert "diff" not in got, "diff must not appear when include_diff=False"
    finally:
        root_server.github.aget_pr = real
    print("  include_diff absent by default: ok")


def test_include_diff_true_adds_diff_field():
    real_aper = _install_mock({11: _payload(11)})
    real_diff = _install_diff_mock(
        {
            11: {
                "number": 11,
                "title": "PR 11",
                "head": "branch-11",
                "base": "main",
                "html_url": "https://example.com/11",
                "files": [
                    {
                        "path": "foo.py",
                        "status": "modified",
                        "additions": 5,
                        "deletions": 2,
                        "changes": 7,
                        "patch": "@@ -1,3 +1,4 @@\n+a",
                    },
                ],
            },
        }
    )
    try:
        got = asyncio.run(root_server.repo_get_pr(number=11, include_diff=True))
        assert "diff" in got, "diff must appear when include_diff=True"
        assert got["diff"]["number"] == 11
        assert len(got["diff"]["files"]) == 1
        f = got["diff"]["files"][0]
        assert f["filename"] == "foo.py", f"expected filename key, got: {f}"
        assert "path" not in f, f"'path' must be renamed to 'filename': {f}"
        assert f["status"] == "modified"
        assert f["additions"] == 5 and f["deletions"] == 2
        assert f["patch"] == "@@ -1,3 +1,4 @@\n+a"
    finally:
        root_server.github.aget_pr = real_aper
        root_server.github.apr_diff = real_diff
    print("  include_diff=true adds diff field: ok")


def test_include_diff_filename_normalization():
    real_aper = _install_mock({12: _payload(12)})
    real_diff = _install_diff_mock(
        {
            12: {
                "number": 12,
                "title": "PR 12",
                "head": "b",
                "base": "main",
                "html_url": "https://example.com/12",
                "files": [
                    {
                        "path": "a/b/c.py",
                        "status": "added",
                        "additions": 10,
                        "deletions": 0,
                        "changes": 10,
                        "patch": "+new",
                    },
                    {
                        "path": "README.md",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "changes": 2,
                        "patch": "-old\n+new",
                    },
                ],
            },
        }
    )
    try:
        got = asyncio.run(root_server.repo_get_pr(number=12, include_diff=True))
        files = got["diff"]["files"]
        assert len(files) == 2
        assert files[0]["filename"] == "a/b/c.py"
        assert "path" not in files[0]
        assert files[1]["filename"] == "README.md"
        assert "path" not in files[1]
    finally:
        root_server.github.aget_pr = real_aper
        root_server.github.apr_diff = real_diff
    print("  include_diff path->filename normalization: ok")


def test_include_diff_batch_mode():
    real_aper = _install_mock({13: _payload(13), 14: _payload(14)})
    real_diff = _install_diff_mock(
        {
            13: {
                "number": 13,
                "title": "PR 13",
                "head": "b",
                "base": "main",
                "html_url": "https://example.com/13",
                "files": [
                    {
                        "path": "x.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "changes": 1,
                        "patch": "+x",
                    }
                ],
            },
            14: {
                "number": 14,
                "title": "PR 14",
                "head": "b",
                "base": "main",
                "html_url": "https://example.com/14",
                "files": [
                    {
                        "path": "y.py",
                        "status": "added",
                        "additions": 3,
                        "deletions": 0,
                        "changes": 3,
                        "patch": "+y",
                    }
                ],
            },
        }
    )
    try:
        got = asyncio.run(
            root_server.repo_get_pr(
                numbers=[13, 14],
                include_diff=True,
            )
        )
        assert "diff" in got[13] and "diff" in got[14]
        assert got[13]["diff"]["files"][0]["filename"] == "x.py"
        assert got[14]["diff"]["files"][0]["filename"] == "y.py"
    finally:
        root_server.github.aget_pr = real_aper
        root_server.github.apr_diff = real_diff
    print("  include_diff batch mode: ok")


# -- proposal_hold message test ------------------------------------------


def test_proposal_hold_message_wording():
    """_pr_view must include 'Vote on the proposal now' in the hold
    message."""
    pid = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Hold message test",
        "Body",
    )["post_id"]
    db.link_pr_to_proposal(9900 + pid, pid, AGENTS["alpha"]["agent_id"])

    real_aper = _install_mock(
        {
            9900 + pid: _payload(9900 + pid, checks_state="success"),
        }
    )
    real_pid = root_server.db.proposal_for_pr
    real_vote_state = root_server.db.proposal_vote_state
    root_server.db.proposal_for_pr = lambda n, conn=None: pid
    root_server.db.proposal_vote_state = lambda p, conn=None: {
        "post_id": p,
        "small_fix": False,
        "net": 0,
        "threshold": 3,
        "approved": False,
        "locked": False,
    }
    try:
        got = asyncio.run(root_server.repo_get_pr(number=9900 + pid))
        assert "proposal_hold" in got, "must include proposal_hold"
        msg = got["proposal_hold"]["message"]
        assert "Vote on the proposal now" in msg, (
            f"hold message must say 'Vote on the proposal now': {msg}"
        )
        assert "or wait for it to clear" in msg, (
            f"hold message must say 'or wait for it to clear': {msg}"
        )
    finally:
        root_server.github.aget_pr = real_aper
        root_server.db.proposal_for_pr = real_pid
        root_server.db.proposal_vote_state = real_vote_state
    print("  proposal_hold message wording: ok")


# -- repo_comment_on_pr hold error message -------------------------------


def test_comment_on_pr_hold_error_wording():
    """The proposal-hold error in repo_comment_on_pr must say
    'Vote on the proposal now or wait for it to clear'."""
    pid = db.create_proposal(
        AGENTS["alpha"]["token"],
        "Comment hold error test",
        "Body",
    )["post_id"]
    db.link_pr_to_proposal(9950 + pid, pid, AGENTS["alpha"]["agent_id"])

    real_aper = _install_mock(
        {
            9950 + pid: _payload(9950 + pid, checks_state="success"),
        }
    )
    real_pid = root_server.db.proposal_for_pr
    real_vote_state = root_server.db.proposal_vote_state
    root_server.db.proposal_for_pr = lambda n, conn=None: pid
    root_server.db.proposal_vote_state = lambda p, conn=None: {
        "post_id": p,
        "small_fix": False,
        "net": 0,
        "threshold": 3,
        "approved": False,
        "locked": False,
    }
    try:
        err = expect_error(
            asyncio.run,
            root_server.repo_comment_on_pr(
                AGENTS["gamma"]["token"],
                9950 + pid,
                "test comment",
            ),
        )
        assert "Vote on the proposal now" in err, (
            f"error must say 'Vote on the proposal now': {err}"
        )
        assert "or wait for it to clear" in err, (
            f"error must say 'or wait for it to clear': {err}"
        )
    finally:
        root_server.github.aget_pr = real_aper
        root_server.db.proposal_for_pr = real_pid
        root_server.db.proposal_vote_state = real_vote_state
    print("  comment_on_pr hold error wording: ok")


if __name__ == "__main__":
    test_ci_note_success()
    test_ci_note_failure()
    test_ci_note_pending()
    test_ci_note_unknown_source()
    test_ci_note_run_count_suffix()
    test_ci_note_single_run_no_suffix()
    test_ci_note_batch_mode()
    test_include_diff_false_by_default()
    test_include_diff_true_adds_diff_field()
    test_include_diff_filename_normalization()
    test_include_diff_batch_mode()
    test_proposal_hold_message_wording()
    test_comment_on_pr_hold_error_wording()
    print("\n== test_pr_view: all passed ==")
