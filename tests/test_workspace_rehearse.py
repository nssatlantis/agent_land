"""Snapshot a claim tree into a CI files-overlay (proposal #482, part 5)."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_rehearse_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
from github._core import RepoError  # noqa: E402
from tests._setup import db, setup  # noqa: E402


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _mk_remote(tmp):
    bare = os.path.join(tmp, "remote.git")
    seed = os.path.join(tmp, "seed")
    os.makedirs(seed)
    _git("init", "--bare", "-b", "main", bare)
    _git("init", "-b", "main", cwd=seed)
    with open(os.path.join(seed, "README.md"), "w") as f:
        f.write("seed\n")
    _git("-C", seed, "add", "-A")
    _git(
        "-C", seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-m", "seed"
    )
    _git("-C", seed, "push", bare, "main")
    return bare


_SHARED_BARE = _mk_remote(tempfile.mkdtemp(prefix="agentland_ws_rehearse_remote_"))


class _RehearseSandbox:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_ws_rehearse_test_")
        self._orig = {
            "repo_url": ws._repo_url,
            "claims_root": ws._claims_root,
            "gitops_url": gh._repo_url,
            "max_mb": config.WORKSPACE_CLAIM_MAX_MB,
        }
        ws._repo_url = lambda with_token=False: _SHARED_BARE
        ws._claims_root = lambda: os.path.join(self.tmp, "claims")
        gh._repo_url = lambda with_token=False: _SHARED_BARE

    def close(self):
        ws._repo_url = self._orig["repo_url"]
        ws._claims_root = self._orig["claims_root"]
        gh._repo_url = self._orig["gitops_url"]
        config.WORKSPACE_CLAIM_MAX_MB = self._orig["max_mb"]
        shutil.rmtree(self.tmp, ignore_errors=True)


def _expect_repo_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except RepoError as exc:
        return str(exc)
    raise AssertionError(f"expected RepoError from {fn.__name__}()")


def _expect_tool_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except Exception as exc:
        return str(exc)
    raise AssertionError(f"expected a tool error from {fn.__name__}()")