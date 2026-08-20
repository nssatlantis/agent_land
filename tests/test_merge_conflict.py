"""Test merge-conflict helpers: _parse_conflict_markers, _has_conflict_markers,
_safe_path, _repo_url, _push_ref (PR #184)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

os.environ.setdefault("GITHUB_REPO", "nssatlantis/agent_land")
os.environ.setdefault("GITHUB_TOKEN", "")

from tests._setup import github  # noqa: E402

from github import (  # noqa: E402
    _has_conflict_markers,
    _parse_conflict_markers,
    _push_ref,
    _repo_url,
    _safe_path,
)


# ---- _parse_conflict_markers ---------------------------------------------

def test_parse_no_markers():
    text = "just a normal file\nno conflicts here\n"
    assert _parse_conflict_markers(text) == []


def test_parse_single_conflict():
    text = (
        "line 1\n"
        "line 2\n"
        "line 3\n"
        "<<<<<<< HEAD\n"
        "ours content\n"
        "=======\n"
        "theirs content\n"
        ">>>>>>> main\n"
        "line after\n"
    )
    regions = _parse_conflict_markers(text)
    assert len(regions) == 1
    r = regions[0]
    assert r["line"] == 4
    assert r["ours"] == "ours content"
    assert r["theirs"] == "theirs content"
    assert r["context_before"] == "line 1\nline 2\nline 3"
    assert r["context_after"] == "line after"


def test_parse_multiple_conflicts():
    text = (
        "aaa\n"
        "<<<<<<< HEAD\n"
        "ours1\n"
        "=======\n"
        "theirs1\n"
        ">>>>>>> main\n"
        "middle\n"
        "<<<<<<< HEAD\n"
        "ours2\n"
        "=======\n"
        "theirs2\n"
        ">>>>>>> main\n"
        "end\n"
    )
    regions = _parse_conflict_markers(text)
    assert len(regions) == 2
    assert regions[0]["line"] == 2
    assert regions[0]["ours"] == "ours1"
    assert regions[0]["theirs"] == "theirs1"
    assert regions[1]["line"] == 8
    assert regions[1]["ours"] == "ours2"
    assert regions[1]["theirs"] == "theirs2"


def test_parse_multiline_ours_theirs():
    text = (
        "<<<<<<< HEAD\n"
        "line A\n"
        "line B\n"
        "line C\n"
        "=======\n"
        "line X\n"
        "line Y\n"
        ">>>>>>> main\n"
    )
    regions = _parse_conflict_markers(text)
    assert len(regions) == 1
    assert regions[0]["ours"] == "line A\nline B\nline C"
    assert regions[0]["theirs"] == "line X\nline Y"


def test_parse_empty_conflict():
    text = (
        "<<<<<<< HEAD\n"
        "=======\n"
        ">>>>>>> main\n"
    )
    regions = _parse_conflict_markers(text)
    assert len(regions) == 1
    assert regions[0]["ours"] == ""
    assert regions[0]["theirs"] == ""


def test_parse_context_at_boundaries():
    # Conflict at start of file -- no context before
    text = (
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> main\n"
        "after1\n"
        "after2\n"
        "after3\n"
        "after4\n"
    )
    regions = _parse_conflict_markers(text)
    assert regions[0]["context_before"] == ""
    assert "after1" in regions[0]["context_after"]

    # Conflict at end of file -- no context after
    text2 = (
        "before4\n"
        "before3\n"
        "before2\n"
        "before1\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> main\n"
    )
    regions2 = _parse_conflict_markers(text2)
    assert "before1" in regions2[0]["context_before"]
    assert regions2[0]["context_after"] == ""


def test_parse_unmatched_markers():
    # Only >>>>>>> without <<<<<<< -- should not be parsed as a conflict
    text = "some text\n>>>>>>> main\nmore text\n"
    assert _parse_conflict_markers(text) == []


# ---- _has_conflict_markers -----------------------------------------------

def test_has_markers_true():
    assert _has_conflict_markers("<<<<<<< HEAD") is True
    assert _has_conflict_markers("=======\n") is True
    assert _has_conflict_markers(">>>>>>> main") is True
    assert _has_conflict_markers(
        "normal\n<<<<<<< HEAD\n ours\n=======\n theirs\n>>>>>>> main\n"
    ) is True


def test_has_markers_false():
    assert _has_conflict_markers("no markers here") is False
    assert _has_conflict_markers("") is False
    assert _has_conflict_markers("just regular code\n") is False


# ---- _safe_path -----------------------------------------------------------

def test_safe_path_normal():
    with tempfile.TemporaryDirectory() as tmp:
        result = _safe_path(tmp, "src/main.py")
        expected = os.path.realpath(os.path.join(tmp, "src/main.py"))
        assert result == expected


def test_safe_path_rejects_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _safe_path(tmp, "../../etc/passwd")
            assert False, "should have raised RepoError"
        except Exception as e:
            assert "escapes the repository root" in str(e)


def test_safe_path_rejects_absolute():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _safe_path(tmp, "/etc/passwd")
            assert False, "should have raised RepoError"
        except Exception as e:
            assert "escapes the repository root" in str(e)


def test_safe_path_rejects_dotdot_in_middle():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _safe_path(tmp, "src/../../etc/passwd")
            assert False, "should have raised RepoError"
        except Exception as e:
            assert "escapes the repository root" in str(e)


def test_safe_path_accepts_root_dir():
    with tempfile.TemporaryDirectory() as tmp:
        result = _safe_path(tmp, ".")
        assert result == os.path.realpath(tmp)


# ---- _repo_url ------------------------------------------------------------

def test_repo_url_without_token():
    url = _repo_url(with_token=False)
    assert url == "https://github.com/nssatlantis/agent_land.git"
    assert "x-access-token" not in url


def test_repo_url_with_token():
    old = github.GITHUB_TOKEN
    try:
        github.GITHUB_TOKEN = "ghp_test123"
        url = _repo_url(with_token=True)
        assert "x-access-token" in url
        assert "nssatlantis/agent_land.git" in url
    finally:
        github.GITHUB_TOKEN = old


def test_repo_url_with_special_chars_in_token():
    old = github.GITHUB_TOKEN
    try:
        github.GITHUB_TOKEN = "ghp_abc/def+ghi"
        url = _repo_url(with_token=True)
        assert "ghp_abc%2Fdef%2Bghi" in url
    finally:
        github.GITHUB_TOKEN = old


# ---- _push_ref ------------------------------------------------------------

def test_push_ref():
    assert _push_ref("feature-branch") == "HEAD:feature-branch"
    assert _push_ref("main") == "HEAD:main"


# ---- Integration tests (mocked _git / _clone_repo / _request) ----------


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def test_detect_clean_merge():
    """detect_merge_conflicts returns clean when merge succeeds."""
    fake_dir = tempfile.mkdtemp()
    fake_repo = os.path.join(fake_dir, "repo")
    os.makedirs(fake_repo)
    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }

    def fake_git(repo_dir, *args, check=True):
        cmd = " ".join(args)
        if "merge" in cmd and "--no-commit" in cmd:
            return _fake_completed(returncode=0)
        if "diff" in cmd and "--diff-filter=U" in cmd:
            return _fake_completed(stdout="")
        if "merge" in cmd and "--abort" in cmd:
            return _fake_completed()
        return _fake_completed()

    with (
        patch("github._request", return_value=pr_data),
        patch("github._clone_repo", return_value=fake_repo),
        patch("github._git", side_effect=fake_git),
        patch("github._cleanup") as mc,
    ):
        result = github.detect_merge_conflicts(42)
    assert result["status"] == "clean", result
    assert result["pr_number"] == 42
    assert result["head"] == "pr-head"
    assert result["base"] == "main"
    mc.assert_called_once_with(fake_repo)


def test_detect_conflicts_with_regions():
    """detect_merge_conflicts returns structured conflict data."""
    fake_dir = tempfile.mkdtemp()
    fake_repo = os.path.join(fake_dir, "repo")
    os.makedirs(fake_repo)
    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }
    conflict_content = "<<<<<<<\nours line\n=======\ntheirs line\n>>>>>>>\n"

    def fake_git(repo_dir, *args, check=True):
        cmd = " ".join(args)
        if "merge" in cmd and "--no-commit" in cmd:
            return _fake_completed(returncode=1, stderr="CONFLICT")
        if "diff" in cmd and "--diff-filter=U" in cmd:
            return _fake_completed(stdout="conflicted.py\n")
        if "merge" in cmd and "--abort" in cmd:
            return _fake_completed()
        return _fake_completed()

    def fake_sp(repo_dir, file_path):
        return os.path.join(repo_dir, file_path)

    with (
        patch("github._request", return_value=pr_data),
        patch("github._clone_repo", return_value=fake_repo),
        patch("github._git", side_effect=fake_git),
        patch("github._safe_path", side_effect=fake_sp),
        patch("github._cleanup"),
    ):
        with patch.object(Path, "read_text", return_value=conflict_content):
            result = github.detect_merge_conflicts(42)
    assert result["status"] == "conflicts", result
    assert len(result["conflicts"]) == 1
    c = result["conflicts"][0]
    assert c["file"] == "conflicted.py"
    assert len(c["regions"]) == 1
    assert c["regions"][0]["ours"] == "ours line"
    assert c["regions"][0]["theirs"] == "theirs line"


def test_detect_unreadable_file_graceful():
    """detect_merge_conflicts handles unreadable conflicted files gracefully."""
    fake_dir = tempfile.mkdtemp()
    fake_repo = os.path.join(fake_dir, "repo")
    os.makedirs(fake_repo)
    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }

    def fake_git(repo_dir, *args, check=True):
        cmd = " ".join(args)
        if "merge" in cmd and "--no-commit" in cmd:
            return _fake_completed(returncode=1, stderr="CONFLICT")
        if "diff" in cmd and "--diff-filter=U" in cmd:
            return _fake_completed(stdout="binary.bin\n")
        if "merge" in cmd and "--abort" in cmd:
            return _fake_completed()
        return _fake_completed()

    def fake_sp(repo_dir, file_path):
        raise github.RepoError("path escapes the repository root")

    with (
        patch("github._request", return_value=pr_data),
        patch("github._clone_repo", return_value=fake_repo),
        patch("github._git", side_effect=fake_git),
        patch("github._safe_path", side_effect=fake_sp),
        patch("github._cleanup"),
    ):
        result = github.detect_merge_conflicts(42)
    assert result["status"] == "conflicts", result
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["error"] == "could not read conflicted file"
    assert result["conflicts"][0]["regions"] == []


def test_resolve_partial_coverage_rejected():
    """apply_merge_resolutions rejects incomplete coverage."""
    fake_dir = tempfile.mkdtemp()
    fake_repo = os.path.join(fake_dir, "repo")
    os.makedirs(fake_repo)
    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }

    def fake_git(repo_dir, *args, check=True):
        cmd = " ".join(args)
        if "merge" in cmd and "--no-commit" in cmd:
            return _fake_completed(returncode=1, stderr="CONFLICT")
        if "diff" in cmd and "--diff-filter=U" in cmd:
            return _fake_completed(stdout="a.py\nb.py\n")
        return _fake_completed()

    resolutions = [{"file": "a.py", "content": "resolved a"}]
    with (
        patch("github._ensure_token"),
        patch("github._request", return_value=pr_data),
        patch("github._clone_repo", return_value=fake_repo),
        patch("github._git", side_effect=fake_git),
        patch("github._cleanup"),
    ):
        try:
            github.apply_merge_resolutions(
                42, resolutions, "test-citizen", _pr=pr_data,
            )
            assert False, "should have raised RepoError"
        except github.RepoError as e:
            assert "missing" in str(e) and "b.py" in str(e), str(e)


def test_resolve_markers_in_content_rejected():
    """apply_merge_resolutions rejects content with conflict markers."""
    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }
    bad_content = "<<<<<<< still in conflict ======= nope >>>>>>>"
    with (
        patch("github._ensure_token"),
        patch("github._request", return_value=pr_data),
    ):
        try:
            github.apply_merge_resolutions(
                42,
                [{"file": "x.py", "content": bad_content}],
                "test-citizen",
                _pr=pr_data,
            )
            assert False, "should have raised RepoError"
        except github.RepoError as e:
            assert "conflict markers" in str(e), str(e)


def test_resolve_success():
    """apply_merge_resolutions succeeds with valid resolutions."""
    fake_dir = tempfile.mkdtemp()
    fake_repo = os.path.join(fake_dir, "repo")
    os.makedirs(fake_repo)
    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }

    def fake_git(repo_dir, *args, check=True):
        cmd = " ".join(args)
        if "merge" in cmd and "--no-commit" in cmd:
            return _fake_completed(returncode=1, stderr="CONFLICT")
        if "diff" in cmd and "--diff-filter=U" in cmd:
            return _fake_completed(stdout="a.py\n")
        if any(k in cmd for k in ("abort", "add", "commit", "push", "remote")):
            return _fake_completed()
        if "rev-parse" in cmd:
            return _fake_completed(stdout="abc123\n")
        return _fake_completed()

    resolutions = [{"file": "a.py", "content": "resolved content"}]
    with (
        patch("github._ensure_token"),
        patch("github._request", return_value=pr_data),
        patch("github._clone_repo", return_value=fake_repo),
        patch("github._git", side_effect=fake_git),
        patch("github._cleanup"),
        patch("github._setup_push_auth"),
        patch("github._invalidate_pr"),
    ):
        result = github.apply_merge_resolutions(
            42, resolutions, "test-citizen", _pr=pr_data,
        )
    assert result["status"] == "resolved", result
    assert result["commit_sha"] == "abc123"
    assert result["files_resolved"] == ["a.py"]


def test_git_timeout_scrubs_token():
    """_git scrubbing works in the timeout path too."""
    old = github.GITHUB_TOKEN
    try:
        github.GITHUB_TOKEN = "ghp_secret123"
        with tempfile.TemporaryDirectory() as tmp:
            try:
                github._git(tmp, "sleep", "300", check=True)
                assert False, "should have raised"
            except github.RepoError as e:
                msg = str(e)
                assert "ghp_secret123" not in msg, f"token leaked: {msg}"
                assert "secret123" not in msg, f"token leaked (partial): {msg}"
                assert "timed out" in msg
    finally:
        github.GITHUB_TOKEN = old


# ---- runner ---------------------------------------------------------------

def main():
    test_parse_no_markers()
    test_parse_single_conflict()
    test_parse_multiple_conflicts()
    test_parse_multiline_ours_theirs()
    test_parse_empty_conflict()
    test_parse_context_at_boundaries()
    test_parse_unmatched_markers()
    test_has_markers_true()
    test_has_markers_false()
    test_safe_path_normal()
    test_safe_path_rejects_traversal()
    test_safe_path_rejects_absolute()
    test_safe_path_rejects_dotdot_in_middle()
    test_safe_path_accepts_root_dir()
    test_repo_url_without_token()
    test_repo_url_with_token()
    test_repo_url_with_special_chars_in_token()
    test_push_ref()
    test_detect_clean_merge()
    test_detect_conflicts_with_regions()
    test_detect_unreadable_file_graceful()
    test_resolve_partial_coverage_rejected()
    test_resolve_markers_in_content_rejected()
    test_resolve_success()
    test_git_timeout_scrubs_token()
    print("test_merge_conflict: all assertions passed")


if __name__ == "__main__":
    main()
