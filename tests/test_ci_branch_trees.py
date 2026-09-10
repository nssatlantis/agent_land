"""Tests for warm branch trees (repo_ci_run(pr_number=...) registry).

Per-PR registry trees skip the re-clone + re-merge when neither the PR
head nor origin/main moved. Real local git throughout (bare fixture
carrying refs/pull/7/head, like test_ci_branch_runner_a); no network.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_brtrees_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.ci_runner as ci_runner  # noqa: E402
import server.ci_runner._trees as trees  # noqa: E402
from tests._setup import config, db, setup  # noqa: E402


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _git_out(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _Fixture:
    def __init__(self):
        self.work = Path(tempfile.mkdtemp(prefix="agentland_brt_work_"))
        self.bare = Path(tempfile.mkdtemp(prefix="agentland_brt_bare_")) / "o.git"
        _git(self.work, "init", "-b", "main")
        _git(self.work, "config", "user.email", "f@x.co")
        _git(self.work, "config", "user.name", "f")
        (self.work / "base.txt").write_text("base\n")
        (self.work / "shared.txt").write_text("base line\n")
        self.main_sha = self._commit("base")
        _git(self.work, "checkout", "-b", "feature")
        (self.work / "extra.txt").write_text("pr addition\n")
        self.pr_sha = self._commit("pr head")
        subprocess.run(
            ["git", "clone", "--bare", str(self.work), str(self.bare)],
            check=True,
            capture_output=True,
        )
        _git(self.bare, "update-ref", "refs/heads/main", self.main_sha)
        _git(self.bare, "update-ref", "refs/pull/7/head", self.pr_sha)
        _git(self.bare, "update-ref", "refs/pull/8/head", self.pr_sha)

    def _commit(self, msg):
        _git(self.work, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "f"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "f@x.co"
        subprocess.run(
            ["git", "-C", str(self.work), "commit", "-m", msg],
            check=True,
            capture_output=True,
            env=env,
        )
        return _git_out(self.work, "rev-parse", "HEAD")

    def _push(self, branch):
        subprocess.run(
            ["git", "-C", str(self.work), "push", str(self.bare), branch],
            check=True,
            capture_output=True,
        )

    def advance_pr(self):
        _git(self.work, "checkout", "feature")
        (self.work / "extra2.txt").write_text("more\n")
        self.pr_sha = self._commit("pr advances")
        self._push("feature")
        _git(self.bare, "update-ref", "refs/pull/7/head", self.pr_sha)

    def advance_main_clean(self):
        _git(self.work, "checkout", "main")
        (self.work / "main_only.txt").write_text("main side\n")
        self.main_sha = self._commit("main advances elsewhere")
        self._push("main")
        _git(self.bare, "update-ref", "refs/heads/main", self.main_sha)

    def advance_main_conflicting(self):
        _git(self.work, "checkout", "main")
        (self.work / "extra.txt").write_text("main side\n")
        self.main_sha = self._commit("main conflicts")
        self._push("main")
        _git(self.bare, "update-ref", "refs/heads/main", self.main_sha)


def _patch(fx):
    saved = (
        ci_runner.github._gitops._repo_url,
        ci_runner.github.base_branch,
    )
    ci_runner.github._gitops._repo_url = lambda with_token=False: str(fx.bare)
    ci_runner.github.base_branch = lambda: "main"
    return saved


def _unpatch(saved):
    ci_runner.github._gitops._repo_url, ci_runner.github.base_branch = saved


def main():
    agents, _ = setup()
    fx = _Fixture()
    saved = _patch(fx)
    old_max = config.CI_BRANCH_TREE_MAX
    old_ttl = config.CI_BRANCH_TREE_TTL_HOURS
    try:
        # 1. cold build merges and records.
        tree, sha, info = trees._prepare_br_tree(7)
        assert info["conflict"] is False and info["tree_warm"] is False
        assert info["base"] == fx.main_sha, "base recorded"
        assert (Path(tree) / "extra.txt").exists(), "PR content merged"
        assert trees.list_br_trees()[0]["pr_number"] == 7
        print("  cold build: ok")

        # 2. identical repeat is warm (no reset/merge verbs).
        seen = []
        real_git = trees._git

        def spy(t, *a):
            seen.append(a[0] if a else "")
            return real_git(t, *a)

        trees._git = spy
        try:
            tree2, sha2, info2 = trees._prepare_br_tree(7)
        finally:
            trees._git = real_git
        assert tree2 == tree and sha2 == sha and info2["tree_warm"] is True
        assert "reset" not in seen and "merge" not in seen, f"warm ran verbs: {seen}"
        assert "fetch" in seen, "warm still revalidates via fetch"
        print("  warm hit: ok")

        # 3. PR advance rebuilds.
        fx.advance_pr()
        _, sha3, info3 = trees._prepare_br_tree(7)
        assert info3["tree_warm"] is False and sha3 != sha
        assert (Path(tree) / "extra2.txt").exists()
        print("  head advance rebuild: ok")

        # 4. main advance rebuilds (clean side).
        fx.advance_main_clean()
        _, _, info4 = trees._prepare_br_tree(7)
        assert info4["conflict"] is False and info4["tree_warm"] is False
        assert (Path(tree) / "main_only.txt").exists()
        print("  base advance rebuild: ok")

        # 5. LRU eviction at cap 1.
        config.CI_BRANCH_TREE_MAX = 1
        try:
            trees._prepare_br_tree(8)
            remaining = [r["pr_number"] for r in trees.list_br_trees()]
            assert 8 in remaining and 7 not in remaining, f"lru: {remaining}"
        finally:
            config.CI_BRANCH_TREE_MAX = old_max
        print("  lru eviction: ok")

        # 6. TTL sweep + explicit evict.
        trees._prepare_br_tree(7)
        import json as _json

        man = os.path.join(trees._br_dir(7), ".ci-br.json")
        with open(man, encoding="utf-8") as fh:
            m = _json.load(fh)
        m["updated_at"] = time.time() - 10 * 365 * 24 * 3600
        with open(man, "w", encoding="utf-8") as fh:
            _json.dump(m, fh)
        assert trees._sweep_idle_br_trees() >= 1
        assert 7 not in [r["pr_number"] for r in trees.list_br_trees()]
        trees._prepare_br_tree(8)
        assert trees.evict_br_tree(8) is True
        assert trees.evict_br_tree(8) is False
        assert trees.evict_br_tree("nope") is False
        print("  sweep + evict: ok")

        # 7. end-to-end result carries tree_warm (stub execution).
        _saved_sb = (
            ci_runner._sandbox._ensure_image,
            ci_runner._sandbox._sandbox_argv,
            ci_runner._sandbox._docker_available,
        )
        _old_cd = config.CI_RUN_COOLDOWN_SECONDS
        config.CI_RUN_COOLDOWN_SECONDS = 0
        ci_runner._sandbox._docker_available = lambda: True
        ci_runner._sandbox._ensure_image = lambda t, rev: "fake:tag"
        ci_runner._sandbox._sandbox_argv = lambda t, tag, rel, extra_env=None: (
            [sys.executable, "-c", "pass"],
            "test",
        )
        try:
            agent = db.register_agent("br-warm-reader")
            r1 = ci_runner.run_checks(agent["agent_id"], "t", "tests", pr_number=8)
            assert r1["mode"] == "branch" and r1["tree_warm"] is False
            r2 = ci_runner.run_checks(agent["agent_id"], "t", "tests", pr_number=8)
            assert r2["tree_warm"] is True, "repeat run is warm"
            assert r2["head_sha"] == r1["head_sha"], "same merge reused"
        finally:
            (
                ci_runner._sandbox._ensure_image,
                ci_runner._sandbox._sandbox_argv,
                ci_runner._sandbox._docker_available,
            ) = _saved_sb
            config.CI_RUN_COOLDOWN_SECONDS = _old_cd
        print("  end-to-end warm flag: ok")

        # 8. conflict path reports files without executing (last: the
        # fixture stays conflicted from here, so nothing follows).
        fx.advance_main_conflicting()
        _, _, info8 = trees._prepare_br_tree(7)
        assert info8["conflict"] is True and info8["files"], "conflict reported"
        assert info8.get("tree_warm") is False
        print("  conflict path: ok")
    finally:
        _unpatch(saved)
        config.CI_BRANCH_TREE_MAX = old_max
        config.CI_BRANCH_TREE_TTL_HOURS = old_ttl

    print("test_ci_branch_trees: all ok")


if __name__ == "__main__":
    main()
