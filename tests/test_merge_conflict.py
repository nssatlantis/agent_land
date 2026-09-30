"""Test merge-conflict helpers: _parse_conflict_markers, _has_conflict_markers,
_safe_path, _repo_url, _push_ref (PR #184)."""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

os.environ.setdefault("GITHUB_REPO", "nssatlantis/agent_land")
os.environ.setdefault("GITHUB_TOKEN", "")
# FORUM_DB_PATH / AGENTLAND_DATA_DIR must be in place before tests._setup
# imports db - required by the server import in test_merge_base_requires_owner.
if not os.environ.get("FORUM_DB_PATH"):
    _TMP = Path(tempfile.mkdtemp(prefix="agentland_test_merge_conflict_"))
    os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
    os.environ.setdefault("AGENTLAND_DATA_DIR", str(_TMP))

from github import (  # noqa: E402
    _has_conflict_markers,
    _parse_conflict_markers,
    _push_ref,
    _repo_url,
    _safe_path,
)
from tests._setup import github  # noqa: E402

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
        "<<<<<<< HEAD\nline A\nline B\nline C\n=======\nline X\nline Y\n>>>>>>> main\n"
    )
    regions = _parse_conflict_markers(text)
    assert len(regions) == 1
    assert regions[0]["ours"] == "line A\nline B\nline C"
    assert regions[0]["theirs"] == "line X\nline Y"


def test_parse_empty_conflict():
    text = "<<<<<<< HEAD\n=======\n>>>>>>> main\n"
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
    assert (
        _has_conflict_markers(
            "normal\n<<<<<<< HEAD\n ours\n=======\n theirs\n>>>>>>> main\n"
        )
        is True
    )


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
    old = github._core.GITHUB_TOKEN
    try:
        github._core.GITHUB_TOKEN = "ghp_test123"
        url = _repo_url(with_token=True)
        assert "x-access-token" in url
        assert "nssatlantis/agent_land.git" in url
    finally:
        github._core.GITHUB_TOKEN = old


def test_repo_url_with_special_chars_in_token():
    old = github._core.GITHUB_TOKEN
    try:
        github._core.GITHUB_TOKEN = "ghp_abc/def+ghi"
        url = _repo_url(with_token=True)
        assert "ghp_abc%2Fdef%2Bghi" in url
    finally:
        github._core.GITHUB_TOKEN = old


# ---- _push_ref ------------------------------------------------------------


def test_push_ref():
    assert _push_ref("feature-branch") == "HEAD:feature-branch"
    assert _push_ref("main") == "HEAD:main"


# ---- Integration tests (mocked _git / _clone_repo / _request) ----------


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _force_temp_workspace():
    """Pin the temp workspace path for tests that mock the temp-path seam.

    `detect_merge_conflicts`/`apply_merge_resolutions` acquire their dir
    via `_workspace()`, which only touches `_clone_repo`/`_cleanup` on the
    temp path — under FORUM_GIT_WORKSPACE_MODE=persistent the pool-slot
    path never calls them, so `_cleanup` mocks record 0 calls (#B26).
    Tests below mock that seam, so they pin temp mode and stay green in
    either env."""
    return patch("github._gitops._ws_mode_persistent", return_value=False)


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
        patch("github._core._request", return_value=pr_data),
        _force_temp_workspace(),
        patch("github._gitops._clone_repo", return_value=fake_repo),
        patch("github._gitops._git", side_effect=fake_git),
        patch("github._gitops._cleanup") as mc,
    ):
        result = github.detect_merge_conflicts(42)
    assert result["status"] == "clean", result
    assert result["pr_number"] == 42
    assert result["head"] == "pr-head"
    assert result["base"] == "main"
    mc.assert_called_once_with(fake_repo)


def test_workspace_unwritable_home_falls_back_to_temp():
    """Persistent mode with an unwritable pool home degrades to the legacy
    temp path instead of raising (read-only DATA_DIR, e.g. the CI sandbox
    after the persistent default landed)."""
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
        patch("config.GIT_WORKSPACE_MODE", "persistent"),
        patch("github._gitops._ws_root", side_effect=OSError(30, "Read-only")),
        patch("github._core._request", return_value=pr_data),
        patch("github._gitops._clone_repo", return_value=fake_repo),
        patch("github._gitops._git", side_effect=fake_git),
        patch("github._gitops._cleanup") as mc,
    ):
        result = github.detect_merge_conflicts(42)
    assert result["status"] == "clean", result
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
        patch("github._core._request", return_value=pr_data),
        _force_temp_workspace(),
        patch("github._gitops._clone_repo", return_value=fake_repo),
        patch("github._gitops._git", side_effect=fake_git),
        patch("github._gitops._safe_path", side_effect=fake_sp),
        patch("github._gitops._cleanup"),
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
        patch("github._core._request", return_value=pr_data),
        _force_temp_workspace(),
        patch("github._gitops._clone_repo", return_value=fake_repo),
        patch("github._gitops._git", side_effect=fake_git),
        patch("github._gitops._safe_path", side_effect=fake_sp),
        patch("github._gitops._cleanup"),
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
        patch("github._core._ensure_token"),
        patch("github._core._request", return_value=pr_data),
        _force_temp_workspace(),
        patch("github._gitops._clone_repo", return_value=fake_repo),
        patch("github._gitops._git", side_effect=fake_git),
        patch("github._gitops._cleanup"),
    ):
        try:
            github.apply_merge_resolutions(
                42,
                resolutions,
                "test-citizen",
                _pr=pr_data,
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
        patch("github._core._ensure_token"),
        patch("github._core._request", return_value=pr_data),
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

    seen: list[tuple] = []

    def fake_git(repo_dir, *args, check=True):
        seen.append(args)
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
        patch("github._core._ensure_token"),
        patch("github._core._request", return_value=pr_data),
        _force_temp_workspace(),
        patch("github._gitops._clone_repo", return_value=fake_repo),
        patch("github._gitops._git", side_effect=fake_git),
        patch("github._gitops._cleanup"),
        patch("github._core._invalidate_pr"),
    ):
        result = github.apply_merge_resolutions(
            42,
            resolutions,
            "test-citizen",
            _pr=pr_data,
        )
    assert result["status"] == "resolved", result
    assert result["commit_sha"] == "abc123"
    assert result["files_resolved"] == ["a.py"]
    # The merge commit is authored under the resolving citizen's identity.
    commit_calls = [a for a in seen if "commit" in a]
    assert commit_calls, "no commit invocation captured"
    assert all(
        "user.name=test-citizen" in a and "user.email=test-citizen@agentland.dev" in a
        for a in commit_calls
    ), commit_calls


def test_git_timeout_scrubs_token():
    """_git scrubbing works in the timeout path too."""
    import subprocess as _sp

    old = github._core.GITHUB_TOKEN
    try:
        github._core.GITHUB_TOKEN = "ghp_secret123"
        with tempfile.TemporaryDirectory() as tmp:
            # Mock subprocess.run to immediately raise TimeoutExpired
            # so the test doesn't actually wait 120s.
            def fake_run(cmd, **kwargs):
                raise _sp.TimeoutExpired(cmd=cmd, timeout=120)

            with patch("subprocess.run", side_effect=fake_run):
                try:
                    github._gitops._git(
                        tmp,
                        "remote",
                        "set-url",
                        "origin",
                        "https://x-access-token:ghp_secret123@github.com/x/y.git",
                    )
                    assert False, "should have raised"
                except github.RepoError as e:
                    msg = str(e)
                    assert "ghp_secret123" not in msg, f"token leaked: {msg}"
                    assert "secret123" not in msg, f"token leaked (partial): {msg}"
                    assert "timed out" in msg
    finally:
        github._core.GITHUB_TOKEN = old


# ---- rebase already-current fast path (real git, local bare remote) -------


def _rgit(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _rcommit(repo_dir, filename, content, message):
    with open(os.path.join(repo_dir, filename), "w") as f:
        f.write(content)
    _rgit("add", "-A", cwd=repo_dir)
    _rgit(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        message,
        cwd=repo_dir,
    )


def _mk_rebase_fixture():
    """Bare remote with main + a feature branch that already contains main."""
    tmp = tempfile.mkdtemp()
    bare = os.path.join(tmp, "remote.git")
    seed = os.path.join(tmp, "seed")
    os.makedirs(seed)
    _rgit("init", "--bare", "-q", "-b", "main", bare)
    _rgit("init", "-q", "-b", "main", cwd=seed)
    _rcommit(seed, "README.md", "seed\n", "seed")
    _rgit("push", "-q", bare, "main", cwd=seed)
    _rgit("checkout", "-q", "-b", "feature", cwd=seed)
    _rcommit(seed, "feature.txt", "feature\n", "feature work")
    _rgit("push", "-q", bare, "feature", cwd=seed)
    return tmp, bare


def test_rebase_skips_already_current_branch():
    """rebase_pr_onto_main fast-paths when the head already contains main
    (no rebase, no push, no invalidation, same sha) - and rebases normally
    once behind."""
    import config as _config
    import github._gitops as gitops

    tmp, bare = _mk_rebase_fixture()
    pr_data = {"state": "open", "head": {"ref": "feature"}, "base": {"ref": "main"}}
    verbs: list[str] = []
    real_git = gitops._git

    def spy_git(repo_dir, *args, **kwargs):
        verbs.append(args[0])
        return real_git(repo_dir, *args, **kwargs)

    try:
        with (
            patch("github._core._ensure_token"),
            patch("github._core._request", return_value=pr_data),
            patch("github._gitops._repo_url", return_value=bare),
            patch("github._gitops._git", side_effect=spy_git),
            patch("github._core._invalidate_pr") as mock_inv,
            # Hermetic pool root: persistent slots live under DATA_DIR,
            # read-only in some CI sandboxes (#B26).
            patch.object(_config, "DATA_DIR", tmp),
        ):
            res = github.rebase_pr_onto_main(42)
            assert res["status"] == "ok", res
            assert "rebase" not in verbs, verbs
            assert "push" not in verbs, verbs
            mock_inv.assert_not_called()
            # Advance main on the remote so the feature falls behind.
            work2 = os.path.join(tmp, "work2")
            _rgit("clone", "-q", bare, work2)
            _rcommit(work2, "main2.txt", "more\n", "second")
            _rgit("push", "-q", "origin", "main", cwd=work2)
            verbs.clear()
            res2 = github.rebase_pr_onto_main(42)
            assert res2["status"] == "ok", res2
            assert "rebase" in verbs, verbs
            assert "push" in verbs, verbs
            assert res2["new_sha"] != res["new_sha"], (res, res2)
            mock_inv.assert_called_once()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  rebase skips already-current branch, proceeds when behind: ok")


# ---- merge_base_clean (proposal #820) -------------------------------------


def _advance_main(tmp, bare, filename, content, message):
    """Push a fresh commit to main on the bare remote so a branch that
    already contained main falls one commit behind it."""
    work = os.path.join(tmp, "advance_main")
    _rgit("clone", "-q", bare, work)
    _rcommit(work, filename, content, message)
    _rgit("push", "-q", "origin", "main", cwd=work)


def test_merge_base_clean_lands_merge():
    """merge_base_clean commits a clean merge of base into the head branch
    and pushes it: the remote tip becomes a real merge commit (two parents)
    and the PR cache is invalidated."""
    import config as _config

    tmp, bare = _mk_rebase_fixture()
    pr_data = {
        "state": "open",
        "head": {"ref": "feature"},
        "base": {"ref": "main"},
    }
    _advance_main(tmp, bare, "behind.txt", "behind\n", "move main ahead")
    try:
        with (
            _force_temp_workspace(),
            patch("github._core._ensure_token"),
            patch("github._core._request", return_value=pr_data),
            patch("github._gitops._repo_url", return_value=bare),
            patch("github._core._invalidate_pr") as mock_inv,
            # Hermetic pool root: persistent slots live under DATA_DIR,
            # read-only in some CI sandboxes (#B26).
            patch.object(_config, "DATA_DIR", tmp),
        ):
            res = github.merge_base_clean(42, "alice (agent_id=1)", _pr=pr_data)
        assert res["status"] == "merged", res
        assert res["head"] == "feature" and res["base"] == "main", res
        assert len(res["commit_sha"]) == 40, res
        mock_inv.assert_called_once_with(42)
        # The pushed tip is a merge commit: <tip> <parent1> <parent2>.
        out = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", "feature"],
            cwd=bare,
            check=True,
            capture_output=True,
            text=True,
        )
        assert len(out.stdout.split()) == 3, out.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  merge_base_clean lands a clean merge on the head branch: ok")


def test_merge_base_clean_up_to_date():
    """merge_base_clean no-ops when the head already contains base: no
    merge commit, no push, no cache invalidation."""
    import config as _config

    tmp, bare = _mk_rebase_fixture()
    pr_data = {
        "state": "open",
        "head": {"ref": "feature"},
        "base": {"ref": "main"},
    }
    calls: list[tuple] = []
    real_git = github._gitops._git

    def spy_git(repo_dir, *args, **kwargs):
        calls.append(args)
        return real_git(repo_dir, *args, **kwargs)

    def feature_sha():
        return subprocess.run(
            ["git", "rev-parse", "feature"],
            cwd=bare,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    before = feature_sha()
    try:
        with (
            _force_temp_workspace(),
            patch("github._core._ensure_token"),
            patch("github._core._request", return_value=pr_data),
            patch("github._gitops._repo_url", return_value=bare),
            patch("github._gitops._git", side_effect=spy_git),
            patch("github._core._invalidate_pr") as mock_inv,
            patch.object(_config, "DATA_DIR", tmp),
        ):
            res = github.merge_base_clean(42, "alice (agent_id=1)", _pr=pr_data)
        assert res["status"] == "up_to_date", res
        assert "commit_sha" not in res, res
        assert not any("commit" in a for a in calls), calls
        assert not any(a and a[0] == "push" for a in calls), calls
        mock_inv.assert_not_called()
        assert feature_sha() == before, "the head branch was pushed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  merge_base_clean no-ops when base is already contained: ok")


def test_merge_base_clean_conflicts_points_at_resolve():
    """merge_base_clean refuses a conflicting merge by name - the pointer
    to repo_resolve_conflicts carries the structured conflict data - and
    aborts the merge without ever committing or pushing."""
    fake_dir = tempfile.mkdtemp()
    fake_repo = os.path.join(fake_dir, "repo")
    os.makedirs(fake_repo)
    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }
    seen: list[tuple] = []

    def fake_git(repo_dir, *args, check=True):
        seen.append(args)
        if args and args[0] == "merge-base":
            return _fake_completed(returncode=1)
        if args and args[0] == "merge" and "--no-commit" in args:
            return _fake_completed(returncode=1, stderr="CONFLICT (content)")
        if args and args[0] == "merge" and "--abort" in args:
            return _fake_completed()
        if args and args[0] == "diff" and "--diff-filter=U" in args:
            return _fake_completed(stdout="conflicted.py\n")
        return _fake_completed()

    try:
        with (
            _force_temp_workspace(),
            patch("github._core._ensure_token"),
            patch("github._core._request", return_value=pr_data),
            patch("github._gitops._clone_repo", return_value=fake_repo),
            patch("github._gitops._git", side_effect=fake_git),
            patch("github._gitops._cleanup"),
        ):
            try:
                github.merge_base_clean(42, "alice (agent_id=1)", _pr=pr_data)
                assert False, "should have raised RepoError"
            except github.RepoError as e:
                msg = str(e)
                assert "repo_resolve_conflicts" in msg, msg
                assert "conflicted.py" in msg, msg
        assert any(a and a[0] == "merge" and "--abort" in a for a in seen), seen
        assert not any(a and a[0] == "push" for a in seen), seen
        assert not any("commit" in a for a in seen), seen
    finally:
        shutil.rmtree(fake_dir, ignore_errors=True)
    print("  merge_base_clean names repo_resolve_conflicts on conflict: ok")


def test_merge_base_requires_owner():
    """repo_merge_base is owner-only: a non-owner is refused before any git
    work is attempted (same gate as repo_update_pr / repo_close_pr)."""
    import db
    import server.tools.repo._pr_ops as pr_ops

    pr_data = {
        "state": "open",
        "head": {"ref": "pr-head"},
        "base": {"ref": "main"},
    }
    with (
        patch.object(pr_ops.db, "require_active_agent"),
        patch.object(pr_ops.db, "require_active"),
        patch.object(pr_ops.db, "_conn", return_value=MagicMock()),
        patch.object(
            pr_ops.db, "whoami", return_value={"name": "alice", "agent_id": 1}
        ),
        patch.object(
            pr_ops.db, "pr_opener", return_value={"name": "bob", "agent_id": 2}
        ),
        patch.object(pr_ops.db, "agent_id_for_token", return_value=None),
        patch.object(pr_ops.db, "record_tool_call"),
        patch.object(pr_ops.github, "aget_pr", new=AsyncMock(return_value=pr_data)),
        patch.object(pr_ops.github, "amerge_base_clean", new=AsyncMock()) as m_merge,
    ):
        try:
            asyncio.run(pr_ops.repo_merge_base("tok", 42))
            assert False, "should have refused a non-owner"
        except db.ForumError as e:
            assert "is not yours" in str(e), str(e)
    m_merge.assert_not_called()
    print("  repo_merge_base refuses a non-owner before any git work: ok")


def test_branch_refs_reads_nested_and_flat_payloads():
    """_branch_refs reads head/base off a caller-supplied PR payload. Two
    shapes reach it: the raw API's nested form and get_pr's flattened one
    (#B184). A payload with no usable ref must refuse rather than fall back
    to a default branch that may not be this PR's base."""
    from github._gitops import _branch_refs

    nested = {"state": "open", "head": {"ref": "feature"}, "base": {"ref": "main"}}
    flat = {"state": "open", "head": "feature", "base": "main"}
    assert _branch_refs(nested) == ("feature", "main")
    assert _branch_refs(flat) == ("feature", "main")

    for bad, label in (
        ({"state": "open", "head": None, "base": "main"}, "head=None"),
        ({"state": "open", "head": {"ref": None}, "base": "main"}, "ref=None"),
        ({"state": "open", "head": "", "base": "main"}, "empty head"),
        ({"state": "open", "head": "feature", "base": ""}, "empty base"),
        ({"state": "open", "base": "main"}, "no head key"),
        ({"state": "open", "head": "feature"}, "no base key"),
    ):
        try:
            _branch_refs(bad)
            assert False, f"should have raised RepoError on {label}"
        except github.RepoError:
            pass
    print("  _branch_refs reads both shapes and refuses an absent ref: ok")


def test_repo_merge_base_accepts_the_get_pr_shape():
    """The seam #B184 lives on: repo_merge_base passes get_pr's flattened
    payload into merge_base_clean, which read pr["head"]["ref"] and raised
    TypeError before the fix. Drives the real tool and the real merge."""
    import config as _config
    import server.tools.repo._findings as findings
    import server.tools.repo._pr_ops as pr_ops

    tmp, bare = _mk_rebase_fixture()
    # get_pr's shape - head/base flattened to bare strings (github/_reads.py).
    pr_data = {
        "number": 42,
        "title": "a pr",
        "body": "",
        "head": "feature",
        "base": "main",
        "author": "alice",
        "state": "open",
        "outcome": "open",
        "mergeable": None,
        "mergeable_state": "behind",
        "commits": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "html_url": "https://example.invalid/pr/42",
        "checks": {},
        "comments": [],
        "files": [],
    }
    _advance_main(tmp, bare, "behind.txt", "behind\n", "move main ahead")
    try:
        with (
            _force_temp_workspace(),
            patch("github._core._ensure_token"),
            patch("github._gitops._repo_url", return_value=bare),
            patch("github._core._invalidate_pr"),
            patch.object(_config, "DATA_DIR", tmp),
            patch.object(pr_ops.db, "require_active_agent"),
            patch.object(pr_ops.db, "require_active"),
            patch.object(pr_ops.db, "_conn", return_value=MagicMock()),
            patch.object(
                pr_ops.db, "whoami", return_value={"name": "alice", "agent_id": 1}
            ),
            patch.object(
                pr_ops.db, "pr_opener", return_value={"name": "alice", "agent_id": 1}
            ),
            patch.object(pr_ops.db, "agent_id_for_token", return_value=None),
            patch.object(pr_ops.db, "record_tool_call"),
            patch.object(pr_ops.github, "aget_pr", new=AsyncMock(return_value=pr_data)),
            patch.object(findings, "_stale_and_refresh", new=AsyncMock()),
        ):
            res = asyncio.run(pr_ops.repo_merge_base("tok", 42))
        assert res["status"] == "merged", res
        assert res["head"] == "feature" and res["base"] == "main", res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  repo_merge_base survives get_pr's flattened payload: ok")


def test_get_pr_flattens_head_and_base():
    """Ties the flat fixture above to the reader that produces it, by DRIVING
    that reader. A text-over-file pin cannot do this job: ``_reads.py``
    carries the same ``"head": pr["head"]["ref"],`` line in ``pr_diff`` and
    ``pr_commits`` too, so a substring check stays green even when ``get_pr``
    alone reverts to nested - which is precisely when the fixture the seam pin
    feeds in becomes fiction."""
    import github._checks as checks
    import github._reads as reads

    raw = {
        "number": 42,
        "title": "a pr",
        "body": "",
        "head": {"ref": "feature", "sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
        "user": {"login": "alice"},
        "state": "open",
        "mergeable": None,
        "mergeable_state": "behind",
        "commits": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "html_url": "https://example.invalid/pr/42",
    }

    class _NoCache:
        """get_pr caches on a TTLCache whose methods are read-only
        attributes, so the cache object itself is replaced, not its .get."""

        def get(self, key, default=None):
            return None

        def set(self, key, value):
            pass

    with (
        patch.object(reads._core, "_pr_cache", _NoCache()),
        patch.object(checks, "_checks_for_head", return_value={}),
        patch.object(reads, "pr_comments", return_value=[]),
        patch.object(reads, "pr_files", return_value=[]),
    ):
        res = reads.get_pr(42, _pr=raw)
    assert res["head"] == "feature" and isinstance(res["head"], str), res
    assert res["base"] == "main" and isinstance(res["base"], str), res
    print("  get_pr returns flattened head/base strings: ok")


# ---- runner ---------------------------------------------------------------


def main():
    # These integration tests assert the legacy clone-per-call contract
    # (mocked _clone_repo + _cleanup called once), which only the temp
    # path provides - pin it for the run. Persistent-mode mechanics live
    # in tests/test_git_workspace.py; the unwritable-home fallback has
    # its own test below.
    import config

    config.GIT_WORKSPACE_MODE = "temp"
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
    test_workspace_unwritable_home_falls_back_to_temp()
    test_detect_conflicts_with_regions()
    test_detect_unreadable_file_graceful()
    test_resolve_partial_coverage_rejected()
    test_resolve_markers_in_content_rejected()
    test_resolve_success()
    test_git_timeout_scrubs_token()
    test_rebase_skips_already_current_branch()
    test_merge_base_clean_lands_merge()
    test_merge_base_clean_up_to_date()
    test_merge_base_clean_conflicts_points_at_resolve()
    test_merge_base_requires_owner()
    test_branch_refs_reads_nested_and_flat_payloads()
    test_repo_merge_base_accepts_the_get_pr_shape()
    test_get_pr_flattens_head_and_base()
    print("test_merge_conflict: all assertions passed")


if __name__ == "__main__":
    main()
