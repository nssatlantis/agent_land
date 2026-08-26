"""Behavioral guards for the persistent git workspace pool (proposal #184,
Phase A). Three merge-family flows used to pay a full network clone per
call; the pool keeps FORUM_GIT_WORKSPACE_POOL warm clones alive between
calls. These tests pin the contract with local bare remotes (no network):

- warm slots reuse the same directory and make zero refetches inside the
  fetch TTL (the merge flows fetch their own specific refs at body start,
  so a dirty-but-fresh slot only needs the local scrub);
- the LOCAL scrub runs on every acquire and deletes stray local branches -
  flows hardcode `checkout -b pr_head`, so leftovers must never accumulate;
- a failed operation marks its slot dirty; an exhausted pool degrades to
  the legacy temp path instead of surfacing a brand-new error;
- a corrupted slot directory self-heals via fresh clone;
- the default temp mode keeps the legacy clone-per-call contract.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github._core as gh_core  # noqa: E402
import github._gitops as gh  # noqa: E402 - the workspace pool under test

gh_core.GITHUB_TOKEN = "test-token"  # push auth is never exercised here


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
            "mode": config.GIT_WORKSPACE_MODE,
            "pool": config.GIT_WORKSPACE_POOL,
            "ttl": config.GIT_WORKSPACE_FETCH_TTL,
            "lock": config.GIT_WORKSPACE_LOCK_TIMEOUT,
            "repo_url": gh._repo_url,
            "ws_root": gh._ws_root,
            "git": gh._git,
        }
        config.GIT_WORKSPACE_MODE = "persistent"
        config.GIT_WORKSPACE_POOL = pool
        config.GIT_WORKSPACE_FETCH_TTL = ttl
        config.GIT_WORKSPACE_LOCK_TIMEOUT = lock_timeout
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
        config.GIT_WORKSPACE_MODE = self._orig["mode"]
        config.GIT_WORKSPACE_POOL = self._orig["pool"]
        config.GIT_WORKSPACE_FETCH_TTL = self._orig["ttl"]
        config.GIT_WORKSPACE_LOCK_TIMEOUT = self._orig["lock"]
        gh._repo_url = self._orig["repo_url"]
        gh._ws_root = self._orig["ws_root"]
        gh._git = self._orig["git"]
        gh._workspace_queue = None
        gh._ws_slots = []
        shutil.rmtree(self.tmp, onerror=_rm_ro)


def test_temp_mode_keeps_legacy_contract():
    sb = _PoolSandbox()
    sb.reset()
    config.GIT_WORKSPACE_MODE = "temp"
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
            config.GIT_WORKSPACE_FETCH_TTL + 1
        )
        with gh._workspace():
            pass
        assert "fetch" in sb.verbs, \
            f"TTL expiry must refetch: {sb.verbs}"
    finally:
        sb.close()
    print("  TTL expiry triggers refetch: ok")


def test_exhausted_pool_degrades_to_temp_clone():
    sb = _PoolSandbox(pool=2, lock_timeout=0)
    try:
        cm_a, cm_b = gh._workspace(), gh._workspace()
        a = cm_a.__enter__()
        b = cm_b.__enter__()
        cleaned: list[str] = []
        orig_cleanup = gh._cleanup

        def spying_cleanup(d):
            cleaned.append(d)
            orig_cleanup(d)

        gh._cleanup = spying_cleanup
        try:
            # A saturated pool must degrade to the legacy temp path -
            # saturation is not a brand-new failure mode citizens should see.
            with gh._workspace() as c:
                assert c != a and c != b, "fallback must be a fresh temp clone"
                assert os.path.isdir(os.path.join(c, ".git"))
        finally:
            gh._cleanup = orig_cleanup
            cm_b.__exit__(None, None, None)
            cm_a.__exit__(None, None, None)
        assert len(cleaned) == 1, "temp fallback cleans up after itself"
        # Slots came back healthy once pressure released.
        with gh._workspace():
            pass
    finally:
        sb.close()
    print("  exhausted pool degrades to temp clone: ok")


def test_flow_leftover_branches_never_poison_the_slot():
    # The critical regression the first cut missed: merge-family flows
    # hardcode `git checkout -b pr_head origin/<head>`, and legacy code only
    # survived because it DELETED the whole temp clone afterwards. A warm
    # slot keeps local branches, so the scrub must remove every stray one -
    # otherwise op 2 hits `fatal: a branch named 'pr_head' already exists`
    # and the slot fails forever. Drive real flow-shaped bodies twice.
    sb = _PoolSandbox(pool=1)
    try:
        # Op 1: successful flow run - creates pr_head, exits CLEANLY (the
        # success path never sets dirty, which is exactly why the early-
        # return normalize used to skip even the partial scrub).
        with gh._workspace() as d:
            _git("checkout", "-b", "pr_head", "origin/main", cwd=d)

        def local_branches(d):
            return subprocess.run(
                ["git", "branch", "--format=%(refname:short)"],
                cwd=d, check=True, capture_output=True, text=True,
            ).stdout.split()

        # Op 2 + 3 (+1 more for good measure): next citizens' flows must find
        # NO pr_head leftover at acquire entry (normalize scrubbed it), then
        # recreate it freely.
        for _ in range(3):
            with gh._workspace() as dx:
                assert dx == d, "warm slot expected"
                assert "pr_head" not in local_branches(dx), \
                    "acquire must start with stray flow branches scrubbed"
                _git("checkout", "-b", "pr_head", "origin/main", cwd=dx)
    finally:
        sb.close()
    print("  flow leftover branches never poison the slot: ok")


def test_corrupted_slot_self_heals():
    sb = _PoolSandbox(pool=1)
    try:
        with gh._workspace() as d:
            pass
        # Simulate a killed process mid-write: wreck the repository data,
        # and leave a read-only file behind so the re-clone's directory
        # cleanup has to handle Windows' read-only objects too.
        ro = os.path.join(d, "readonly.bin")
        with open(ro, "wb") as f:
            f.write(b"x")
        os.chmod(ro, 0o444)
        shutil.rmtree(os.path.join(d, ".git"), onerror=_rm_ro)
        with gh._workspace() as d2:
            assert os.path.isdir(os.path.join(d2, ".git")), \
                "corruption must trigger a fresh clone"
            assert os.path.isfile(os.path.join(d2, "README.md"))
            assert not os.path.exists(ro), \
                "self-heal must clear leftovers from the dead operation"
    finally:
        sb.close()
    print("  corrupted slot self-heals via fresh clone: ok")



def test_push_auth_restores_anonymous_remote():
    """The anonymous-read invariant: scrub restores the anon URL at
    acquire-entry, _push_auth tokens only for the push and restores
    afterwards - even when the push raises."""
    sb = _PoolSandbox()
    gh._repo_url = lambda with_token=False: sb.bare + ("-auth" if with_token else "")
    saved_token_fn = gh_core._ensure_token
    gh_core._ensure_token = lambda: None
    try:
        def url(d):
            return gh._git(d, "config", "--get", "remote.origin.url").stdout.strip()

        with gh._workspace() as d:
            # A previous operation's push auth must not survive the scrub.
            gh._git(d, "remote", "set-url", "origin", sb.bare + "-auth")
            gh._ws_git_scrub(d)
            assert url(d) == sb.bare, f"scrub left tokened URL: {url(d)}"
            with gh._push_auth(d):
                assert url(d) == sb.bare + "-auth", url(d)
            assert url(d) == sb.bare, f"push auth not restored: {url(d)}"
            try:
                with gh._push_auth(d):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            assert url(d) == sb.bare, "push auth not restored on failure"
        print("  push auth restores the anonymous remote: ok")
    finally:
        gh_core._ensure_token = saved_token_fn
        sb.close()


def test_pool_size_follows_config_changes():
    """FORUM_GIT_WORKSPACE_POOL takes effect without a restart: growth
    adds usable slots, shrink retires surplus slots (and their queued
    tokens), regrow reuses the orphaned slot directories."""
    sb = _PoolSandbox(pool=1, lock_timeout=2)
    try:
        with gh._workspace() as first:
            pass
        assert os.path.dirname(first) == gh._ws_root()

        config.GIT_WORKSPACE_POOL = 2  # grow without restart
        gh._ws_ensure_pool()
        assert len(gh._ws_slots) == 2, gh._ws_slots
        # FIFO re-issues slot0 first, so HOLD slot0's token to force the
        # acquire onto the newly added slot.
        q = gh._ws_ensure_pool()
        held = q.get(timeout=1)
        try:
            with gh._workspace() as second:
                assert second != first, "grown pool did not yield a new slot"
                assert os.path.dirname(second) == gh._ws_root()
        finally:
            if held < len(gh._ws_slots):
                q.put(held)

        config.GIT_WORKSPACE_POOL = 1  # shrink retires the surplus slot
        gh._ws_ensure_pool()
        assert len(gh._ws_slots) == 1, gh._ws_slots
        with gh._workspace() as third:
            assert third == first, f"expected surviving slot0: {third}"

        config.GIT_WORKSPACE_POOL = 2  # regrow reuses the orphaned dir
        gh._ws_ensure_pool()
        with gh._workspace() as fourth:
            assert fourth in (first, second), fourth
        print("  pool size follows config changes: ok")
    finally:
        sb.close()


def test_slots_carry_commit_identity():
    """Every working tree we create is commit-ready even on hosts with no
    global git config: fresh clones seed the fallback identity at creation,
    and normalize re-seeds on every acquire - healing slots made by older
    deploys (regression for the "Committer identity unknown" incident)."""
    sb = _PoolSandbox(pool=1)
    try:
        # Fresh-clone path: identity present right after first acquire.
        with gh._workspace() as d1:
            email = gh._git(d1, "config", "--get", "user.email").stdout.strip()
            assert email == "agentland@local", email
        # Legacy-slot path: a pre-existing slot with NO identity config and
        # a fresh last_fetch (so no refetch) must still be healed by
        # normalize's re-seed.
        gh._ws_ensure_pool()
        slot = gh._ws_slots[0]
        subprocess.run(["git", "init", "-q", slot["dir"]], check=True)
        slot["last_fetch"] = time.monotonic()  # skip the network fetch
        slot["dirty"] = False
        with gh._workspace() as d2:
            assert d2 == slot["dir"]
            email = gh._git(d2, "config", "--get", "user.email").stdout.strip()
            assert email == "agentland@local", email
            name = gh._git(d2, "config", "--get", "user.name").stdout.strip()
            assert name == "AgentLand", name
        print("  slots carry commit identity (fresh + legacy heal): ok")
    finally:
        sb.close()

def main():
    test_temp_mode_keeps_legacy_contract()
    test_warm_reuse_scrub_and_no_refetch_within_ttl()
    test_ttl_expiry_triggers_refetch()
    test_exhausted_pool_degrades_to_temp_clone()
    test_flow_leftover_branches_never_poison_the_slot()
    test_corrupted_slot_self_heals()
    test_push_auth_restores_anonymous_remote()
    test_pool_size_follows_config_changes()
    test_slots_carry_commit_identity()
    print("test_git_workspace: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
