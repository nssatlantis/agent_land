"""Tests for branch-mode CI runs (repo_ci_run with pr_number): sandbox
argv shape, image hash-tagging, the live-git merge preview (clean and
conflict paths), gate-bucket separation, validation refusals, and - when
docker exists on the host - a hostile-payload containment proof."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_branch_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config  # noqa: E402
import db  # noqa: E402
import events  # noqa: E402
import server.ci_runner as ci_runner  # noqa: E402

db.init_db()

_uid_counter = iter(range(8000, 8999))
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
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


class _GitFixture:
    """A local bare remote carrying main plus a synthetic pull/<n>/head
    ref, mirroring just enough of GitHub for the merge-preview path."""

    def __init__(self, conflicting: bool, pr_edits_requirements: bool = False):
        self.work = Path(tempfile.mkdtemp(prefix="agentland_ci_fx_work_"))
        self.bare = Path(tempfile.mkdtemp(prefix="agentland_ci_fx_bare_")) / "origin.git"
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
        subprocess.run(["git", "-C", str(self.work), "commit", "-m", msg],
                       check=True, capture_output=True, env=env)
        return subprocess.run(["git", "-C", str(self.work), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

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
            # Rewrites line 1 of shared.txt, which main will ALSO rewrite
            # after the fork -> guaranteed content conflict.
            (self.work / self.conflict_file).write_text("pr side\n")
        else:
            (self.work / "extra.txt").write_text("harmless addition\n")
        if self.pr_edits_requirements:
            # The attack-shaped case: the PR changes the dependency set.
            (self.work / "requirements.txt").write_text(
                "# fixture requirements\nattacker-pkg==6.6.6\n"
            )
        self.pr_sha = self._commit("pull head")

    def _advance_main_onto_same_lines(self):
        _git(self.work, "checkout", "main")
        (self.work / self.conflict_file).write_text("main side\n")
        self.main_sha = self._commit("main advances the same file")

    def _make_bare(self):
        subprocess.run(["git", "clone", "--bare", str(self.work), str(self.bare)],
                       check=True, capture_output=True)
        # Re-push main explicitly and expose the feature branch the way
        # GitHub does: as refs/pull/7/head.
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
    assert float(config.CI_RUN_SANDBOX_CPUS) == 1.5
    assert config.CI_RUN_SANDBOX_MEMORY_MB == 768
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


def test_gate_bucket_is_branch_kind():
    actor = _uid()
    events.log_event(events.EVT_CI_BRANCH_RUN, actor_agent_id=actor,
                     detail={"checks": "tests"})
    _shadow("CI_RUN_COOLDOWN_SECONDS", 300)
    fx = _GitFixture(conflicting=False)
    fx.patch_runner()
    saved_avail = ci_runner._docker_available
    ci_runner._docker_available = lambda: True
    saved_img = ci_runner._ensure_image
    ci_runner._ensure_image = lambda tree, rev: "fake:tag"
    saved_argv = ci_runner._sandbox_argv
    ci_runner._sandbox_argv = (
        lambda tree, image_tag, script_rel:
            ([sys.executable, "-c", "print('hi')"], "c1")
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
        # memory-swap pinned to memory => no swap on any host.
        assert "--memory-swap" in argv
        assert argv[argv.index("--memory-swap") + 1] == "777m"
        assert "--pids-limit" in argv and "64" in argv
        assert "--tmpfs" in argv
        assert argv[argv.index("--tmpfs") + 1] == f"/tmp:rw,size={32 * 1024 * 1024}"
        assert "PYTHONDONTWRITEBYTECODE=1" in argv
        assert "/tree:/repo:ro" in argv
        assert argv[-2:] == ["python3", "tests/run_all.py"]
        assert argv[argv.index("img:abc") + 1:] == ["python3", "tests/run_all.py"]
        assert name.startswith("agentland-ci-")
        assert "GITHUB_TOKEN" not in text
    finally:
        _restore_cfg()


def test_image_tag_tracks_requirements_hash():
    # The digest source is now "requirements.txt AS OF a revision", read
    # via git show - so an uncommitted worktree edit must be invisible,
    # which is exactly the property that keeps PR deps out of the build.
    repo = Path(tempfile.mkdtemp(prefix="agentland_ci_revfx_"))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "fixture")
    (repo / "requirements.txt").write_text("httpx==0.28.1\n")
    _git(repo, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "fixture"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "f@e.com"
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "reqs"],
                   check=True, capture_output=True, env=env)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    # Worktree now differs from the commit - only the committed bytes count.
    (repo / "requirements.txt").write_text("httpx==0.28.1\nstarlette==1.6.0\n")
    at_rev = ci_runner._requirements_at(str(repo), sha)
    assert at_rev == b"httpx==0.28.1\n"
    tag_a = ci_runner._image_tag(ci_runner._digest(at_rev))
    tag_b = ci_runner._image_tag(ci_runner._digest(b"httpx==0.28.1\nstarlette==1.6.0\n"))
    assert tag_a.startswith("agentland-ci:") and tag_b.startswith("agentland-ci:")
    assert tag_a != tag_b


def _patched_execution(stub_script: str):
    """Patch image+argv seams so branch mode executes a local stub without
    docker; returns (restore_fn, holder) where holder records the rev the
    image was pinned to and the call count."""
    holder = {"image_calls": 0}
    rev_holder = {"rev": None}
    saved = (ci_runner._ensure_image, ci_runner._sandbox_argv,
             ci_runner._docker_available)

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
        (ci_runner._ensure_image, ci_runner._sandbox_argv,
         ci_runner._docker_available) = saved

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
        # The image must be pinned to MAIN's requirements revision, never
        # the merge result - this is the wiring half of the supply-chain
        # invariant (the bytes half is test_image_tag_tracks_requirements_hash).
        assert rev_holder["rev"] == result["base_sha"]
        rows = events.query_events(agent_id=actor, kind="ci_branch_run")
        assert len(rows) == 1 and rows[0]["detail"]["pr_number"] == 7
    finally:
        restore()
        fx.unpatch()


def test_merge_conflict_reports_files_without_running():
    actor = _uid()
    fx = _GitFixture(conflicting=True)
    fx.patch_runner()
    restore, holder, rev_holder = _patched_execution("raise SystemExit('must not run')")
    before = len(events.query_events(kind="ci_branch_run"))
    try:
        result = ci_runner.run_checks(actor, "t", "tests", pr_number=7)
        assert result["merge_conflict"] is True
        assert result["ok"] is False
        assert result["exit_code"] is None
        assert result["output_tail"] == ""
        assert any("shared.txt" in f for f in result["conflict_files"])
        assert holder["image_calls"] == 0, "no image build on conflicts"
        after = events.query_events(kind="ci_branch_run")
        assert len(after) == before + 1, "conflicts still land in the ledger"
        assert after[0]["detail"]["merge_conflict"] is True
    finally:
        restore()
        fx.unpatch()


def test_pr_requirements_never_reach_the_build():
    """The supply-chain invariant, end to end: a PR that edits
    requirements.txt gets its dependency set into the MERGE TREE (that is
    what the sandbox tests against), but the image pin source stays main's
    committed bytes - the reviewer-found host-build taint cannot recur."""
    actor = _uid()
    fx = _GitFixture(conflicting=False, pr_edits_requirements=True)
    fx.patch_runner()
    restore, holder, rev_holder = _patched_execution(
        "import sys\nprint('all 1 test files passed')\nsys.exit(0)\n"
    )
    try:
        result = ci_runner.run_checks(actor, "t", "tests", pr_number=7)
        assert result["ok"] is True and holder["image_calls"] == 1
        tree = Path(ci_runner._runner_dir())
        merged_reqs = (tree / "requirements.txt").read_text()
        assert "attacker-pkg==6.6.6" in merged_reqs, \
            "fixture sanity: merge tree carries the PR's deps"
        pinned = ci_runner._requirements_at(str(tree), result["base_sha"])
        assert b"attacker-pkg" not in pinned
        assert pinned == b"# fixture requirements\n"
    finally:
        restore()
        fx.unpatch()


def test_native_mode_still_reports_native():
    actor = _uid()
    saved_prepare = ci_runner._prepare_tree
    scratch = Path(tempfile.mkdtemp(prefix="agentland_ci_nat_"))
    (scratch / "requirements.txt").write_text("# x\n")
    ci_runner._prepare_tree = lambda: (str(scratch), "f" * 40)
    restore_exec, _, _ = _patched_execution("")
    try:
        result = ci_runner.run_checks(actor, "t", "benchmarks",
                                      pr_number=None)
        assert result["mode"] == "native"
        assert "pr_number" not in result
    finally:
        ci_runner._prepare_tree = saved_prepare
        restore_exec()


def _docker_present() -> bool:
    return ci_runner._docker_available()


def test_hostile_payload_contained():
    """THE containment proof, executed wherever docker exists (GitHub CI
    runners do). The stub suite probes for secrets and tries the network;
    the container must return neither."""
    if not _docker_present():
        print("  skipping hostile-payload proof (no docker on host)")
        return
    daemon = subprocess.run(["docker", "info", "--format", "."],
                            capture_output=True, timeout=30)
    if daemon.returncode != 0:
        print("  skipping hostile-payload proof (docker daemon unreachable)")
        return
    actor = _uid()
    fx = _GitFixture(conflicting=False)
    fx.patch_runner()
    # Empty requirements -> the image build pulls only python:3.14-slim.
    (fx.work / "requirements.txt").write_text("\n")
    _git(fx.work, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "fixture"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "fixture@example.com"
    subprocess.run(["git", "-C", str(fx.work), "commit", "-m", "empty reqs"],
                   check=True, capture_output=True, env=env)
    # Push (not update-ref): the bare fixture has never seen this commit,
    # so the object must travel with the ref change.
    _git(fx.work, "push", str(fx.bare), "feature:refs/pull/7/head")

    payload = textwrap.dedent("""
        import json, socket, sys
        leaked = [k for k in __import__('os').environ
                  if 'TOKEN' in k.upper() or 'SECRET' in k.upper()
                  or 'GITHUB' in k.upper()]
        net = False
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=3).close()
            net = True
        except OSError:
            net = False
        print(json.dumps({"leaked": sorted(leaked), "net": net}))
        sys.exit(0)
    """)
    saved_prepare = ci_runner._prepare_pr_tree

    def seeded_prepare(pr_number):
        tree, sha, info = saved_prepare(pr_number)
        script = Path(tree) / "tests" / "run_all.py"
        script.write_text(payload)
        return tree, sha, info

    ci_runner._prepare_pr_tree = seeded_prepare
    try:
        result = ci_runner.run_checks(actor, "t", "tests", pr_number=7)
        assert result["ok"] is True, result["output_tail"]
        report = json.loads(result["output_tail"].strip().splitlines()[-1])
        assert report["leaked"] == [], f"secrets reached the sandbox: {report}"
        assert report["net"] is False, "network egress was possible!"
    finally:
        ci_runner._prepare_pr_tree = saved_prepare
        fx.unpatch()


def main():
    test_knob_defaults()
    test_invalid_pr_rejected()
    test_branch_disabled_refuses()
    test_sandbox_missing_refuses()
    test_gate_bucket_is_branch_kind()
    test_sandbox_argv_shape()
    test_image_tag_tracks_requirements_hash()
    test_clean_merge_runs_and_reports_shape()
    test_merge_conflict_reports_files_without_running()
    test_pr_requirements_never_reach_the_build()
    test_native_mode_still_reports_native()
    test_hostile_payload_contained()
    print("test_ci_branch_runner: all ok")


if __name__ == "__main__":
    main()
