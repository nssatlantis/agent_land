"""Tests for branch-mode CI runs — shard A (5/12).

Covers: knob defaults, invalid PR rejected, branch disabled, sandbox missing,
native mode.
Split from test_ci_branch_runner.py for harness parallelism.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_branch_a_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import server.ci_runner as ci_runner  # noqa: E402
from tests._setup import config  # noqa: E402

db.init_db()

_uid_counter = iter(range(8000, 8099))
_SAVED_CFG: dict[str, object] = {}


def _uid() -> int:
    return next(_uid_counter)


def _shadow(name: str, value):
    _SAVED_CFG[name] = getattr(config, name)
    setattr(config, name, value)


def _restore_cfg():
    for name, value in _SAVED_CFG.items():
        setattr(config, name, value)
    _SAVED_CFG.clear()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


class _GitFixture:
    """A local bare remote carrying main plus synthetic pull/<n>/head."""

    def __init__(self, conflicting: bool, pr_edits_requirements: bool = False):
        self.work = Path(tempfile.mkdtemp(prefix="agentland_ci_fx_work_"))
        self.bare = (
            Path(tempfile.mkdtemp(prefix="agentland_ci_fx_bare_")) / "origin.git"
        )
        self.tree_dir = Path(tempfile.mkdtemp(prefix="agentland_ci_fx_run_"))
        self.conflict_file = "shared.txt"
        self.pr_edits_requirements = pr_edits_requirements
        self._seed_main()
        self._seed_pull_head(conflicting)
        if conflicting:
            self._advance_main_onto_same_lines()
        self._make_bare()

    def _commit(self, msg: str):
        _git(self.work, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "fixture"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "fixture@example.com"
        subprocess.run(
            ["git", "-C", str(self.work), "commit", "-m", msg],
            check=True,
            capture_output=True,
            env=env,
        )
        return subprocess.run(
            ["git", "-C", str(self.work), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _seed_main(self):
        _git(self.work, "init", "-b", "main")
        _git(self.work, "config", "user.email", "fixture@example.com")
        _git(self.work, "config", "user.name", "fixture")
        (self.work / "requirements.txt").write_text("# fixture requirements\n")
        tests = self.work / "tests"
        tests.mkdir()
        (tests / "run_all.py").write_text(
            "import sys\nprint('all 1 test files passed')\nsys.exit(0)\n"
        )
        (self.work / self.conflict_file).write_text("base line\n")
        self.main_sha = self._commit("base")

    def _seed_pull_head(self, conflicting: bool):
        _git(self.work, "checkout", "-b", "feature")
        if conflicting:
            (self.work / self.conflict_file).write_text("pr side\n")
        else:
            (self.work / "extra.txt").write_text("harmless addition\n")
        if self.pr_edits_requirements:
            (self.work / "requirements.txt").write_text(
                "# fixture requirements\nattacker-pkg==6.6.6\n"
            )
        self.pr_sha = self._commit("pull head")

    def _advance_main_onto_same_lines(self):
        _git(self.work, "checkout", "main")
        (self.work / self.conflict_file).write_text("main side\n")
        self.main_sha = self._commit("main advances the same file")

    def _make_bare(self):
        subprocess.run(
            ["git", "clone", "--bare", str(self.work), str(self.bare)],
            check=True,
            capture_output=True,
        )
        _git(self.bare, "update-ref", "refs/heads/main", self.main_sha)
        _git(self.bare, "update-ref", "refs/pull/7/head", self.pr_sha)

    def patch_runner(self):
        self._saved = (
            (ci_runner, "_runner_dir", ci_runner._runner_dir),
            (ci_runner.github._gitops, "_repo_url", ci_runner.github._gitops._repo_url),
            (ci_runner.github, "base_branch", ci_runner.github.base_branch),
        )
        ci_runner._runner_dir = lambda: str(self.tree_dir)
        ci_runner.github._gitops._repo_url = lambda with_token=False: str(self.bare)
        ci_runner.github.base_branch = lambda: "main"

    def unpatch(self):
        for obj, name, fn in self._saved:
            setattr(obj, name, fn)


def test_knob_defaults():
    assert config.CI_RUN_BRANCH_ENABLED == 1
    assert config.CI_RUN_IMAGE_BASE == "agentland-ci"
    assert float(config.CI_RUN_SANDBOX_CPUS) == 2.5
    assert config.CI_RUN_SANDBOX_MEMORY_MB == 1024
    assert config.CI_RUN_SANDBOX_SWAP_MB == 256
    assert config.CI_RUN_SANDBOX_PIDS == 128
    assert config.CI_RUN_SANDBOX_TMP_SIZE_MB == 256


def test_invalid_pr_rejected():
    for bad in (0, -5, "seven", True):
        try:
            ci_runner.run_checks(_uid(), "t", "tests", pr_number=bad)  # type: ignore[arg-type]
            raise AssertionError(f"expected ForumError for {bad!r}")
        except db.ForumError as exc:
            assert "positive integer" in str(exc)


def test_branch_disabled_refuses():
    _shadow("CI_RUN_BRANCH_ENABLED", 0)
    try:
        ci_runner.run_checks(_uid(), "t", "tests", pr_number=7)
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "disabled" in str(exc)
    finally:
        _restore_cfg()


def test_sandbox_missing_refuses():
    saved = ci_runner._docker_available
    ci_runner._docker_available = lambda: False
    try:
        ci_runner.run_checks(_uid(), "t", "tests", pr_number=7)
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "docker" in str(exc)
    finally:
        ci_runner._docker_available = saved


def _patched_execution(stub_script: str):
    holder = {"image_calls": 0}
    rev_holder = {"rev": None}
    saved = (
        ci_runner._ensure_image,
        ci_runner._sandbox_argv,
        ci_runner._docker_available,
    )

    def fake_image(tree, rev):
        holder["image_calls"] += 1
        rev_holder["rev"] = rev
        return "fake:tag"

    def fake_argv(tree, image_tag, script_rel):
        return [sys.executable, "-c", stub_script], "agentland-ci-test"

    ci_runner._ensure_image = fake_image
    ci_runner._sandbox_argv = fake_argv
    ci_runner._docker_available = lambda: True

    def restore():
        (
            ci_runner._ensure_image,
            ci_runner._sandbox_argv,
            ci_runner._docker_available,
        ) = saved

    return restore, holder, rev_holder


def test_native_mode_still_reports_native():
    actor = _uid()
    saved_prepare = ci_runner._prepare_tree
    scratch = Path(tempfile.mkdtemp(prefix="agentland_ci_nat_"))
    (scratch / "requirements.txt").write_text("# x\n")
    ci_runner._prepare_tree = lambda: (str(scratch), "f" * 40)
    restore_exec, _, _ = _patched_execution("")
    try:
        result = ci_runner.run_checks(actor, "t", "benchmarks", pr_number=None)
        assert result["mode"] == "native"
        assert "pr_number" not in result
    finally:
        ci_runner._prepare_tree = saved_prepare
        restore_exec()


def main():
    test_knob_defaults()
    test_invalid_pr_rejected()
    test_branch_disabled_refuses()
    test_sandbox_missing_refuses()
    test_native_mode_still_reports_native()
    print("test_ci_branch_runner_a: all ok")


if __name__ == "__main__":
    main()
