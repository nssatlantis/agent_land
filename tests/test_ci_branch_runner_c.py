"""Tests for branch-mode CI runs — shard C (3/12).

Covers: merge conflict, PR requirements isolation, hostile payload.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_branch_c_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import events  # noqa: E402
import server.ci_runner as ci_runner  # noqa: E402

db.init_db()

_uid_counter = iter(range(8200, 8299))


def _uid() -> int:
    return next(_uid_counter)


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
        assert "attacker-pkg==6.6.6" in merged_reqs, (
            "fixture sanity: merge tree carries the PR's deps"
        )
        pinned = ci_runner._requirements_at(str(tree), result["base_sha"])
        assert b"attacker-pkg" not in pinned
        assert pinned == b"# fixture requirements\n"
    finally:
        restore()
        fx.unpatch()


def _docker_present() -> bool:
    return ci_runner._docker_available()


def test_hostile_payload_contained():
    if not _docker_present():
        print("  skipping hostile-payload proof (no docker on host)")
        return
    daemon = subprocess.run(
        ["docker", "info", "--format", "."], capture_output=True, timeout=30
    )
    if daemon.returncode != 0:
        print("  skipping hostile-payload proof (docker daemon unreachable)")
        return
    actor = _uid()
    fx = _GitFixture(conflicting=False)
    fx.patch_runner()
    (fx.work / "requirements.txt").write_text("\n")
    _git(fx.work, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "fixture"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "fixture@example.com"
    subprocess.run(
        ["git", "-C", str(fx.work), "commit", "-m", "empty reqs"],
        check=True,
        capture_output=True,
        env=env,
    )
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
    test_merge_conflict_reports_files_without_running()
    test_pr_requirements_never_reach_the_build()
    test_hostile_payload_contained()
    print("test_ci_branch_runner_c: all ok")


if __name__ == "__main__":
    main()
