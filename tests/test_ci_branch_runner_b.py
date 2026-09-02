"""Tests for branch-mode CI runs — shard B (4/12).

Covers: gate bucket, sandbox argv, image tag, clean merge.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_branch_b_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import events  # noqa: E402
import server.ci_runner as ci_runner  # noqa: E402
from tests._setup import config  # noqa: E402

db.init_db()

_uid_counter = iter(range(8100, 8199))
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


def test_gate_bucket_is_branch_kind():
    actor = _uid()
    events.log_event(
        events.EVT_CI_BRANCH_RUN, actor_agent_id=actor, detail={"checks": "tests"}
    )
    _shadow("CI_RUN_COOLDOWN_SECONDS", 300)
    fx = _GitFixture(conflicting=False)
    fx.patch_runner()
    saved_avail = ci_runner._docker_available
    ci_runner._docker_available = lambda: True
    saved_img = ci_runner._ensure_image
    ci_runner._ensure_image = lambda tree, rev: "fake:tag"
    saved_argv = ci_runner._sandbox_argv
    ci_runner._sandbox_argv = lambda tree, image_tag, script_rel: (
        [sys.executable, "-c", "print('hi')"],
        "c1",
    )
    try:
        ci_runner.run_checks(actor, "t", "tests", pr_number=7)
        raise AssertionError("expected ForumError")
    except db.ForumError as exc:
        assert "cooldown" in str(exc)
    finally:
        _restore_cfg()
        ci_runner._docker_available = saved_avail
        ci_runner._ensure_image = saved_img
        ci_runner._sandbox_argv = saved_argv
        fx.unpatch()


def test_sandbox_argv_shape():
    _shadow("CI_RUN_SANDBOX_CPUS", 2.0)
    _shadow("CI_RUN_SANDBOX_MEMORY_MB", 777)
    _shadow("CI_RUN_SANDBOX_SWAP_MB", 123)
    _shadow("CI_RUN_SANDBOX_PIDS", 64)
    _shadow("CI_RUN_SANDBOX_TMP_SIZE_MB", 32)
    try:
        argv, name = ci_runner._sandbox_argv("/tree", "img:abc", "tests/run_all.py")
        text = json.dumps(argv)
        assert "--network" in argv and "none" in argv
        assert "--read-only" in argv
        assert "--cap-drop" in argv and "ALL" in argv
        assert "--security-opt" in argv and "no-new-privileges" in argv
        assert "--user" in argv and "1000:1000" in argv
        assert "--cpus" in argv and "2.0" in argv
        assert "--memory" in argv and "777m" in argv
        assert "--memory-swap" in argv
        assert argv[argv.index("--memory-swap") + 1] == "900m"  # 777+123
        assert "--pids-limit" in argv and "64" in argv
        assert "--tmpfs" in argv
        assert argv[argv.index("--tmpfs") + 1] == f"/tmp:rw,size={32 * 1024 * 1024}"
        assert "PYTHONDONTWRITEBYTECODE=1" in argv
        assert "GIT_CONFIG_COUNT=1" in argv
        assert "GIT_CONFIG_KEY_0=safe.directory" in argv
        assert "GIT_CONFIG_VALUE_0=/repo" in argv
        assert "/tree:/repo:ro" in argv
        assert argv[-2:] == ["python3", "tests/run_all.py"]
        assert argv[argv.index("img:abc") + 1 :] == ["python3", "tests/run_all.py"]
        assert name.startswith("agentland-ci-")
        assert "GITHUB_TOKEN" not in text
    finally:
        _restore_cfg()


def test_image_tag_tracks_requirements_hash():
    repo = Path(tempfile.mkdtemp(prefix="agentland_ci_revfx_"))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "fixture")
    (repo / "requirements.txt").write_text("httpx==0.28.1\n")
    _git(repo, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "fixture"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "f@e.com"
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "reqs"],
        check=True,
        capture_output=True,
        env=env,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    (repo / "requirements.txt").write_text("httpx==0.28.1\nstarlette==1.6.0\n")
    at_rev = ci_runner._requirements_at(str(repo), sha)
    assert at_rev == b"httpx==0.28.1\n"
    tag_a = ci_runner._image_tag(ci_runner._digest(at_rev))
    tag_b = ci_runner._image_tag(
        ci_runner._digest(b"httpx==0.28.1\nstarlette==1.6.0\n")
    )
    assert tag_a.startswith("agentland-ci:") and tag_b.startswith("agentland-ci:")
    assert tag_a != tag_b


def test_dev_requirements_fold_into_image_tag():
    """requirements-dev.txt (static tooling: mypy/ruff) is read at the same
    trusted rev and folds into the image digest - so a dev-dependency bump
    invalidates the sandbox image just like a runtime one."""
    repo = Path(tempfile.mkdtemp(prefix="agentland_ci_revdev_"))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "fixture")
    (repo / "requirements.txt").write_text("httpx==0.28.1\n")
    (repo / "requirements-dev.txt").write_text("ruff==0.9.0\n")
    _git(repo, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "fixture"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "f@e.com"
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "reqs"],
        check=True,
        capture_output=True,
        env=env,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    reqs = ci_runner._requirements_at(str(repo), sha)
    dev = ci_runner._requirements_dev_at(str(repo), sha)
    assert reqs == b"httpx==0.28.1\n"
    assert dev == b"ruff==0.9.0\n"
    # _ensure_image folds dev into the digest: _digest(data + b"\x00" + dev).
    tag_a = ci_runner._image_tag(ci_runner._digest(reqs + b"\x00" + dev))
    tag_b = ci_runner._image_tag(ci_runner._digest(reqs + b"\x00" + b"ruff==0.9.1\n"))
    assert tag_a != tag_b, "dev-dep change must invalidate the image tag"


def test_requirements_dev_absent_returns_empty():
    """A commit too old to carry requirements-dev.txt yields empty bytes, so
    the image still builds with just the runtime deps (no static tooling)."""
    repo = Path(tempfile.mkdtemp(prefix="agentland_ci_nodev_"))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "fixture")
    (repo / "requirements.txt").write_text("httpx==0.28.1\n")
    _git(repo, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "fixture"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "f@e.com"
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "reqs"],
        check=True,
        capture_output=True,
        env=env,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert ci_runner._requirements_dev_at(str(repo), sha) == b""


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


def test_clean_merge_runs_and_reports_shape():
    actor = _uid()
    fx = _GitFixture(conflicting=False)
    fx.patch_runner()
    restore, holder, rev_holder = _patched_execution(
        "import sys\nprint('all 2 test files passed')\nsys.exit(0)\n"
    )
    try:
        result = ci_runner.run_checks(actor, "t", "tests", pr_number=7)
        assert result["mode"] == "branch"
        assert result["pr_number"] == 7
        assert result["merge_conflict"] is False
        assert result["ok"] is True
        assert result["summary"] == {"passed_files": 2, "failed_files": 0}
        assert result["head_sha"] != result["base_sha"], "merge commit expected"
        assert len(result["head_sha"]) == 40 and len(result["base_sha"]) == 40
        assert holder["image_calls"] == 1
        assert rev_holder["rev"] == result["base_sha"]
        rows = events.query_events(agent_id=actor, kind="ci_branch_run")
        assert len(rows) == 1 and rows[0]["detail"]["pr_number"] == 7
    finally:
        restore()
        fx.unpatch()


def main():
    test_gate_bucket_is_branch_kind()
    test_sandbox_argv_shape()
    test_image_tag_tracks_requirements_hash()
    test_dev_requirements_fold_into_image_tag()
    test_requirements_dev_absent_returns_empty()
    test_clean_merge_runs_and_reports_shape()
    print("test_ci_branch_runner_b: all ok")


if __name__ == "__main__":
    main()
