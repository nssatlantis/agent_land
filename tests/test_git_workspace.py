"""Behavioral guards for the persistent git workspace pool (proposal #184,
Phase A). Three merge-family flows used to pay a full network clone per
call; the pool keeps FORUM_GIT_WORKSPACE_POOL warm clones alive between
calls. These tests pin the contract with local bare remotes (no network):

- warm slots reuse the same directory and make zero refetches inside the
  fetch TTL (the merge flows fetch their own specific refs at body start,
  so a dirty-but-fresh slot only needs the local scrub);
- a failed operation marks its slot dirty and the next acquirer scrubs it -
  one citizen's crashed merge can never poison the next;
- an exhausted pool fails fast with a clean busy-error (bounded wait),
  never a hang;
- a corrupted slot directory self-heals via fresh clone;
- the default temp mode keeps the legacy clone-per-call contract.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent / "github.py"
_spec = importlib.util.spec_from_file_location("agentland_root_github", _ROOT)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

gh.GITHUB_TOKEN = "test-token"  # push auth is never exercised here


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _mk_remote(tmp):
    """A local bare remote holding one commit on main. Returns its path."""
    bare = os.path.join(tmp, "remote.git")
    seed = os.path.join(tmp, "seed")
    os.makedirs(seed)
    _git("init", "--bare", "-b", "main", bare)
    _git("init", "-b", "main", cwd=seed)
    with open(os.path.join(seed, "README.md"), "w") as f:
        f.write("seed\n")
    _git("-C", seed, "add", "-A")
    _git("-C", seed, "-c", "user.email=a@b", "-c", "user.name=t",
         "commit", "-m", "seed")
    _git("-C", seed, "push", bare, "main")
    return bare


def _rm_ro(func, path, _exc):
    os.chmod(path, 0o777)
    func(path)


class _PoolSandbox:
    """Isolates one scenario: unique workspace root, fresh pool state,
    patched remote URL, spied git verbs, saved/restored knobs."""

    def __init__(self, pool=1, ttl=3600, lock_timeout=5):
        self.tmp = tempfile.mkdtemp(prefix="agentland_ws_test_")
        self.bare = _mk_remote(self.tmp)
        self._orig = {
            "mode": gh.config.GIT_WORKSPACE_MODE,
            "pool": gh.config.GIT_WORKSPACE_POOL,
            "ttl": gh.config.GIT_WORKSPACE_FETCH_TTL,
            "lock": gh.config.GIT_WORKSPACE_LOCK_TIMEOUT,
            "repo_url": gh._repo_url,
            "ws_root": gh._ws_root,
            "git": gh._git,
        }
        gh.config.GIT_WORKSPACE_MODE = "persistent"
        gh.config.GIT_WORKSPACE_POOL = pool
        gh.config.GIT_WORKSPACE_FETCH_TTL = ttl
        gh.config.GIT_WORKSPACE_LOCK_TIMEOUT = lock_timeout
        gh._repo_url = lambda with_token=False: self.bare
        gh._ws_root = lambda: os.path.join(self.tmp, "slots")
        self.verbs: list[str] = []
        real_git = gh._git

        def spy_git(dir_, *args, **kwargs):
            self.verbs.append(args[0])
            return real_git(dir_, *args, **kwargs)

        gh._git = spy_git
        self.reset()

    def reset(self):
        gh._workspace_queue = None
        gh._ws_slots = []

    def close(self):
        gh.config.GIT_WORKSPACE_MODE = self._orig["mode"]
        gh.config.GIT_WORKSPACE_POOL = self._orig["pool"]
        gh.config.GIT_WORKSPACE_FETCH_TTL = self._orig["ttl"]
        gh.config.GIT_WORKSPACE_LOCK_TIMEOUT = self._orig["lock"]
        gh._repo_url = self._orig["repo_url"]
        gh._ws_root = self._orig["ws_root"]
        gh._git = self._orig["git"]
        gh._workspace_queue = None
        gh._ws_slots = []
        shutil.rmtree(self.tmp, onerror=_rm_ro)


def test_temp_mode_keeps_legacy_contract():
    sb = _PoolSandbox()
    sb.reset()
    gh.config.GIT_WORKSPACE_MODE = "temp"
    cleaned: list[str] = []
    orig_cleanup = gh._cleanup

    def spying_cleanup(d):
        cleaned.append(d)
        orig_cleanup(d)

    gh._cleanup = spying_cleanup
    try:
        with gh._workspace() as d1:
            assert os.path.isdir(os.path.join(d1, ".git"))
        with gh._workspace() as d2:
            pass
        assert d1 != d2, "temp mode must clone fresh per call"
        # Cleanup runs for every temp workspace. (On Windows the leftover
        # .git objects can survive rmtree's best effort, so we pin the
        # contract - cleanup called exactly once per clone - not the bytes.)
        assert cleaned == [d1, d2], cleaned
        assert sb.verbs.count("clone") == 2
    finally:
        gh._cleanup = orig_cleanup
        sb.close()
    print("  temp mode keeps legacy clone-per-call contract: ok")


def test_warm_reuse_scrub_and_no_refetch_within_ttl():
    sb = _PoolSandbox(pool=1)
    try:
        try:
            with gh._workspace() as d1:
                # A failed operation leaves junk behind...
                with open(os.path.join(d1, "JUNK.txt"), "w") as f:
                    f.write("x")
                raise RuntimeError("simulated op failure")
        except RuntimeError:
            pass
        # ...but the next acquirer gets the SAME slot, scrubbed clean, and
        # pays zero network cost while the fetch TTL holds.
        with gh._workspace() as d2:
            assert d1 == d2, "warm slot must be reused"
            assert not os.path.exists(os.path.join(d2, "JUNK.txt")), \
                "dirty-slot scrub must remove leftovers"
        assert sb.verbs.count("clone") == 1, sb.verbs
        assert "fetch" not in sb.verbs, sb.verbs
    finally:
        sb.close()
    print("  warm reuse + dirty scrub + no refetch within TTL: ok")


def test_ttl_expiry_triggers_refetch():
    sb = _PoolSandbox(pool=1, ttl=3600)
    try:
        with gh._workspace():
            pass
        # Force staleness deterministically - relying on TTL=0 races the
        # monotonic clock (two calls inside one tick compare equal).
        gh._ws_slots[0]["last_fetch"] -= (
            gh.config.GIT_WORKSPACE_FETCH_TTL + 1
        )
        with gh._workspace():
            pass
        assert "fetch" in sb.verbs, \
            f"TTL expiry must refetch: {sb.verbs}"
    finally:
        sb.close()
    print("  TTL expiry triggers refetch: ok")


def test_exhausted_pool_fails_fast_with_busy_error():
    sb = _PoolSandbox(pool=2, lock_timeout=0)
    try:
        cm_a, cm_b = gh._workspace(), gh._workspace()
        a = cm_a.__enter__()
        b = cm_b.__enter__()
        try:
            assert a != b, "concurrent acquires need distinct slots"
            raised = None
            try:
                with gh._workspace():
                    raise AssertionError("exhausted pool must not yield")
            except gh.RepoError as exc:
                raised = str(exc)
            assert raised is not None and "busy" in raised, raised
        finally:
            cm_b.__exit__(None, None, None)
            cm_a.__exit__(None, None, None)
        # Slots released - a later acquire succeeds again.
        with gh._workspace():
            pass
    finally:
        sb.close()
    print("  exhausted pool fails fast with clean busy-error: ok")


def test_corrupted_slot_self_heals():
    sb = _PoolSandbox(pool=1)
    try:
        with gh._workspace() as d:
            pass
        # Simulate a killed process mid-write: wreck the repository data.
        shutil.rmtree(os.path.join(d, ".git"), onerror=_rm_ro)
        with gh._workspace() as d2:
            assert os.path.isdir(os.path.join(d2, ".git")), \
                "corruption must trigger a fresh clone"
            assert os.path.isfile(os.path.join(d2, "README.md"))
    finally:
        sb.close()
    print("  corrupted slot self-heals via fresh clone: ok")


def main():
    test_temp_mode_keeps_legacy_contract()
    test_warm_reuse_scrub_and_no_refetch_within_ttl()
    test_ttl_expiry_triggers_refetch()
    test_exhausted_pool_fails_fast_with_busy_error()
    test_corrupted_slot_self_heals()
    print("test_git_workspace: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
