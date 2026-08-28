"""Tests for search.find_similar_prs — the soft 'possibly duplicate in-flight PR'
hint.  Uses a throwaway DB like every other db-level test.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Isolate DB before importing db (same pattern as every test file).
_TMP = Path(tempfile.mkdtemp(prefix="test_similar_prs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

# Zero cooldowns/caps before importing db (same pattern as every test file).
os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_REPORT_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_COMMENT_DAILY_CAP"] = "0"
os.environ["FORUM_VOTE_DAILY_CAP"] = "0"
os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
os.environ["FORUM_SIMILAR_PRS_THRESHOLD"] = "0.3"
os.environ["FORUM_SIMILAR_PRS_RESULTS"] = "5"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db  # noqa: E402, F401 - imported to break circular import (search↔db)
import search  # noqa: E402

# ── helpers ─────────────────────────────────────

_OPEN_PRS = [
    {
        "number": 10,
        "title": "Add dark mode toggle",
        "author": "alice",
        "body": "Theme the viewer with a dark mode setting",
    },
    {
        "number": 11,
        "title": "Refactor database layer",
        "author": "bob",
        "body": "Split db.py into separate modules",
    },
    {
        "number": 12,
        "title": "Add notification system",
        "author": "carol",
        "body": "Notify agents on replies and votes",
    },
]

_PR_FILES = {
    10: [{"filename": "viewer/styles.css"}, {"filename": "viewer/_layout.py"}],
    11: [{"filename": "db/__init__.py"}, {"filename": "db/_core.py"}],
    12: [{"filename": "notifications.py"}, {"filename": "server.py"}],
}


def _mock_open_prs():
    return list(_OPEN_PRS)


def _mock_pr_files(number):
    return list(_PR_FILES.get(number, []))


def _mock_get_pr(number):
    for pr in _OPEN_PRS:
        if pr["number"] == number:
            result = dict(pr)
            # get_pr() embeds pr_files() result — mirror the real API.
            result["files"] = list(_PR_FILES.get(number, []))
            return result
    raise ValueError(f"PR #{number} not found")


# ── tests ───────────────────────────────────────


def test_empty_when_no_args():
    """Returns [] when no pr_number, file_paths, title or body given."""
    result = search.find_similar_prs()
    assert result == []


def test_file_path_overlap():
    """PRs sharing file paths score above the threshold."""
    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", _mock_get_pr),
    ):
        result = search.find_similar_prs(
            file_paths=["viewer/styles.css", "viewer/_layout.py"],
            title="Add dark mode",
            body="Theme the viewer",
        )
    assert isinstance(result, list)
    numbers = [r["number"] for r in result]
    # PR #10 shares both file paths — should appear.
    assert 10 in numbers, f"PR #10 should be similar, got {numbers}"
    pr10 = next(r for r in result if r["number"] == 10)
    assert "viewer/styles.css" in pr10["file_overlap"]
    assert "viewer/_layout.py" in pr10["file_overlap"]
    assert 0.0 <= pr10["score"] <= 1.0


def test_title_body_overlap():
    """PRs with similar title/body tokens score above the threshold."""
    # Use PR #12 with strong title+body token overlap so the weighted score
    # (0.5*file + 0.3*title + 0.2*body) clears 0.3 even with file_score=0.
    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", _mock_get_pr),
    ):
        result = search.find_similar_prs(
            file_paths=["unrelated/file.py"],
            title="Add notification system for agents",
            body="Notify agents on replies and votes about events",
        )
    assert isinstance(result, list)
    numbers = [r["number"] for r in result]
    # PR #12 shares many title/body keywords — should appear.
    assert 12 in numbers, f"PR #12 should be similar, got {numbers}"


def test_excludes_own_pr():
    """The caller's own PR (by pr_number) is excluded from results."""
    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", _mock_get_pr),
    ):
        result = search.find_similar_prs(pr_number=10)
    numbers = [r["number"] for r in result]
    assert 10 not in numbers, "own PR should be excluded"


def test_no_match_below_threshold():
    """Completely unrelated PRs return empty when below threshold."""
    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", _mock_get_pr),
    ):
        result = search.find_similar_prs(
            file_paths=["totally/new/file.rs"],
            title="Completely unrelated topic about Rust",
            body="Rewrite everything in Rust for performance",
        )
    # No overlap with any existing PR → should be empty or very few.
    assert isinstance(result, list)
    for r in result:
        assert r["score"] >= config.SIMILAR_PRS_THRESHOLD


def test_limit_respected():
    """Results are capped at the limit parameter."""
    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", _mock_get_pr),
    ):
        result = search.find_similar_prs(
            file_paths=["viewer/styles.css"],
            title="Add dark mode viewer",
            body="Theme viewer with dark mode",
            limit=1,
        )
    assert len(result) <= 1


def test_pr_number_fetches_metadata():
    """Passing pr_number fetches files/title/body from GitHub."""
    mock_get = MagicMock(side_effect=_mock_get_pr)
    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", mock_get),
    ):
        result = search.find_similar_prs(pr_number=10)
    mock_get.assert_called_once_with(10)
    # PR #10 itself is excluded; no other PR shares its exact files.
    for r in result:
        assert r["number"] != 10


def test_graceful_on_github_error():
    """Returns [] when GitHub API calls fail."""

    def _fail():
        raise RuntimeError("API down")

    with patch("github.open_prs", _fail):
        result = search.find_similar_prs(
            file_paths=["any.py"],
            title="test",
            body="test",
        )
    assert result == []


def test_score_fields():
    """Each result carries the expected fields."""
    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", _mock_get_pr),
    ):
        result = search.find_similar_prs(
            file_paths=["viewer/styles.css"],
            title="Dark mode",
            body="Theme viewer",
        )
    for r in result:
        assert "number" in r
        assert "title" in r
        assert "author" in r
        assert "file_overlap" in r
        assert "score" in r
        assert isinstance(r["file_overlap"], list)
        assert 0.0 <= r["score"] <= 1.0


def test_per_pr_files_failure():
    """One PR's pr_files raises — that PR is skipped, others still scored."""

    def _pr_files_partial(number):
        if number == 11:
            raise RuntimeError("GitHub API timeout for PR #11 files")
        return list(_PR_FILES.get(number, []))

    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _pr_files_partial),
        patch("github.get_pr", _mock_get_pr),
    ):
        result = search.find_similar_prs(
            file_paths=["viewer/styles.css"],
            title="Add dark mode viewer",
            body="Theme viewer with dark mode",
        )
    # PR #11 should be skipped (pr_files failed); PR #10 should still appear.
    numbers = [r["number"] for r in result]
    assert 11 not in numbers, f"PR #11 should be skipped, got {numbers}"
    assert 10 in numbers, f"PR #10 should still appear, got {numbers}"


def test_get_pr_failure_with_pr_number():
    """get_pr() failure with pr_number returns [] (F4 review finding)."""

    def _fail_get_pr(number):
        raise RuntimeError("GitHub API unreachable")

    with (
        patch("github.open_prs", _mock_open_prs),
        patch("github.pr_files", _mock_pr_files),
        patch("github.get_pr", _fail_get_pr),
    ):
        result = search.find_similar_prs(pr_number=999)
    assert result == []
