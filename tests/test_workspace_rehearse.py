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
    os.makedirs(os.path.join(seed, ".github", "workflows"), exist_ok=True)
    with open(os.path.join(seed, ".github", "workflows", "ci.yml"), "w") as f:
        f.write("jobs:\n")
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


def _claim(agents, wstools, key, title):
    tok = agents[key]["token"]
    prop = db.create_proposal(tok, title, "body")
    pid = prop["post_id"]
    claimed = wstools.claim_workspace(tok, pid, "dev")
    assert claimed["claim"]["status"] == "active", claimed
    return pid, tok


def test_snapshot_roundtrip():
    sb = _RehearseSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 31, "snap")
        Path(tree["path"], "note.txt").write_text("hi\n", encoding="utf-8")
        Path(tree["path"], "blob.bin").write_bytes(b"\xff\xfe\x00")
        Path(tree["path"], "empty.txt").write_text("", encoding="utf-8")
        snap = ws.snapshot_claim_tree(11, 31, "snap")
        by_path = {f["path"]: f["content"] for f in snap["files"]}
        assert by_path["note.txt"] == "hi\n", by_path
        assert "README.md" in by_path, sorted(by_path)
        assert "blob.bin" not in by_path, sorted(by_path)
        assert "empty.txt" not in by_path, sorted(by_path)
        assert ".workspace.json" not in by_path, sorted(by_path)
        assert not [p for p in by_path if p == ".git" or p.startswith(".git/")]
        assert snap["skipped_binaries"] == 1, snap
        assert snap["skipped_empty"] == 1, snap
        assert snap["skipped_protected"] == 1, snap
        try:
            os.symlink(
                os.path.join(tree["path"], "note.txt"),
                os.path.join(tree["path"], "link.txt"),
            )
        except (
            OSError,
            NotImplementedError,
        ):  # domain: degrade-silently - no-symlink platforms skip pin
            pass
        else:
            snap2 = ws.snapshot_claim_tree(11, 31, "snap")
            assert "link.txt" not in {f["path"] for f in snap2["files"]}, snap2
        assert snap["head_sha"], snap
        assert snap["total_bytes"] > 0, snap
    finally:
        sb.close()
    print("  snapshot roundtrip (text/binary/empty/git/manifest): ok")


def test_snapshot_manifest():
    import hashlib  # noqa: E402

    sb = _RehearseSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 34, "manifest")
        Path(tree["path"], "note.txt").write_text("hi\n", encoding="utf-8")
        snap = ws.snapshot_claim_tree(11, 34, "manifest")
        man = {m["path"]: m for m in snap["content_manifest"]}
        assert (
            man["note.txt"]["content_sha256"] == hashlib.sha256(b"hi\n").hexdigest()
        ), snap
        assert man["note.txt"]["content_bytes"] == 3, snap
        assert {m["path"] for m in snap["content_manifest"]} == {
            f["path"] for f in snap["files"]
        }, snap
    finally:
        sb.close()
    print("  snapshot manifest (sha256 per file, matches files set): ok")


def test_snapshot_guards():
    sb = _RehearseSandbox()
    try:
        assert "no workspace tree" in _expect_repo_error(
            ws.snapshot_claim_tree, 11, 32, "missing"
        )
        ws.ensure_claim_tree(11, 32, "big")
        old = ws._SNAPSHOT_MAX_MB
        ws._SNAPSHOT_MAX_MB = 0
        try:
            err = _expect_repo_error(ws.snapshot_claim_tree, 11, 32, "big")
            assert "snapshot cap" in err, err
        finally:
            ws._SNAPSHOT_MAX_MB = old
    finally:
        sb.close()
    print("  snapshot guards (missing/cap): ok")


def test_snapshot_delta():
    sb = _RehearseSandbox()
    try:
        # a standalone bare so origin/main can advance mid-test without
        # touching _SHARED_BARE (bug #90: a stale claim tree must not
        # re-upload its copies of untouched files over a fresh base)
        bare = _mk_remote(os.path.join(sb.tmp, "remote2"))
        old_ws, old_gh = ws._repo_url, gh._repo_url
        ws._repo_url = lambda with_token=False: bare
        gh._repo_url = lambda with_token=False: bare
        try:
            tree = ws.ensure_claim_tree(11, 37, "delta")
            # the claim tree's only real change: one untracked file
            Path(tree["path"], "note.txt").write_text("hi\n", encoding="utf-8")
            # origin/main advances AFTER the claim; the tree is now stale
            adv = os.path.join(sb.tmp, "advan")
            os.makedirs(adv)
            _git("clone", bare, os.path.join(adv, "work"))
            with open(os.path.join(adv, "work", "README.md"), "w") as f:
                f.write("seed2\n")
            _git("-C", os.path.join(adv, "work"), "add", "-A")
            _git(
                "-C",
                os.path.join(adv, "work"),
                "-c",
                "user.email=a@b",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "advance",
            )
            _git("-C", os.path.join(adv, "work"), "push", bare, "main")
            # delta snapshot carries only the tree's own change - the stale
            # README.md (untouched in the tree) must NOT ride the overlay,
            # or a rehearsal refresh would flatten the fresh base to seed
            delta = ws.snapshot_claim_tree(11, 37, "delta", delta=True)
            by_path = {f["path"]: f["content"] for f in delta["files"]}
            assert by_path == {"note.txt": "hi\n"}, sorted(by_path)
            assert delta["total_bytes"] == 3, delta
            # a locally-changed tracked file still rides the delta
            Path(tree["path"], "README.md").write_text("local-edit\n", encoding="utf-8")
            delta2 = ws.snapshot_claim_tree(11, 37, "delta", delta=True)
            by2 = {f["path"]: f["content"] for f in delta2["files"]}
            assert by2 == {
                "note.txt": "hi\n",
                "README.md": "local-edit\n",
            }, sorted(by2)
            # whole-tree snapshot (the push-manifest contract) keeps
            # untouched files, stale or not
            whole = ws.snapshot_claim_tree(11, 37, "delta")
            whole_by = {f["path"] for f in whole["files"]}
            assert "README.md" in whole_by and "note.txt" in whole_by, sorted(whole_by)
        finally:
            ws._repo_url, gh._repo_url = old_ws, old_gh
    finally:
        sb.close()
    print("  snapshot delta (untouched excluded, whole-tree default): ok")


def test_snapshot_delta_stacked_refuses():
    sb = _RehearseSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 41, "stacked")
        Path(tree["path"], "r1.py").write_text("x = 1\n", encoding="utf-8")
        _git("add", "-A", cwd=tree["path"])
        _git(
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "round-1",
            cwd=tree["path"],
        )
        # a stacked tree carries round-1 as a commit in HEAD, so the
        # delta snapshot would rehearse a phantom tree (bug #97)
        err = _expect_repo_error(ws.snapshot_claim_tree, 11, 41, "stacked", delta=True)
        assert "phantom" in err and "origin/" in err, err
        # the whole-tree snapshot (push-manifest contract) is unaffected
        whole = ws.snapshot_claim_tree(11, 41, "stacked")
        by_path = {f["path"]: f["content"] for f in whole["files"]}
        assert by_path["r1.py"] == "x = 1\n", sorted(by_path)
    finally:
        sb.close()
    print("  snapshot delta stacked refusal (phantom tree guard): ok")


def test_snapshot_delta_stacked_nonmain_base():
    sb = _RehearseSandbox()
    try:
        bare = _mk_remote(os.path.join(sb.tmp, "remote3"))
        old_ws, old_gh = ws._repo_url, gh._repo_url
        ws._repo_url = lambda with_token=False: bare
        gh._repo_url = lambda with_token=False: bare
        try:
            tree = ws.ensure_claim_tree(11, 43, "stacked-base")
            Path(tree["path"], "r1.py").write_text("x = 1\n", encoding="utf-8")
            _git("add", "-A", cwd=tree["path"])
            _git(
                "-c",
                "user.email=a@b",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "round-1",
                cwd=tree["path"],
            )
            # round-1 lands on the stacked base branch itself: the tree's
            # layers ARE the ancestor, so the delta is honest, not phantom
            _git("push", bare, "HEAD:refs/heads/feature-x", cwd=tree["path"])
            _git("fetch", "origin", "feature-x", cwd=tree["path"])
            Path(tree["path"], "r2.py").write_text("y = 2\n", encoding="utf-8")
            delta = ws.snapshot_claim_tree(
                11, 43, "stacked-base", delta=True, base="feature-x"
            )
            by_path = {f["path"]: f["content"] for f in delta["files"]}
            assert by_path == {"r2.py": "y = 2\n"}, sorted(by_path)
            # the same tree compared against origin/main IS stacked: the
            # guard must key on the requested base, never hard-coded main
            for base in (None, "main"):
                err = _expect_repo_error(
                    ws.snapshot_claim_tree,
                    11,
                    43,
                    "stacked-base",
                    delta=True,
                    base=base,
                )
                assert "phantom" in err and "origin/" in err, err
        finally:
            ws._repo_url, gh._repo_url = old_ws, old_gh
    finally:
        sb.close()
    print("  snapshot delta stacked base-aware (non-main base honored): ok")


def test_tool_wiring(agents, wstools):
    import server.ci_runner as ci_runner  # noqa: E402

    sb = _RehearseSandbox()
    orig = ci_runner.run_checks_with_deadline
    seen = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        if seen.pop("hand_off", False):
            return None, True, "2026-09-14T00:00:00.000Z", "run-9"
        result = {"ok": True, "checks": kwargs.get("checks", args[3])}
        return result, False, None, None

    ci_runner.run_checks_with_deadline = fake_run
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Rehearse Shop")
        wstools.workspace_write_file(tok, pid, "dev", "feat.txt", "feat\n")
        direct = wstools.workspace_rehearse(tok, pid, "dev")
        assert direct["ok"] is True, direct
        assert direct["workspace"]["files"] == 1, direct
        assert direct["workspace"]["head_sha"], direct
        sent = {f["path"]: f["content"] for f in seen["kwargs"]["files"]}
        assert sent == {"feat.txt": "feat\n"}, sorted(sent)
        assert "README.md" not in sent, sorted(sent)
        assert ".workspace.json" not in sent, sorted(sent)
        assert seen["args"][1] == agents["alpha"]["agent_id"], seen["args"]
        seen["hand_off"] = True
        handed = wstools.workspace_rehearse(tok, pid, "dev")
        assert handed["status"] == "running" and handed["run_id"] == "run-9", handed
        assert handed["workspace"]["files"] == 1, handed
        assert "watch_url" in handed and "note" in handed, handed
        wstools.release_workspace(tok, pid, "dev")
    finally:
        ci_runner.run_checks_with_deadline = orig
        sb.close()
    print("  tool wiring (direct + handed-off): ok")


def test_tool_guards(agents, wstools):
    sb = _RehearseSandbox()
    try:
        pid, beta = _claim(agents, wstools, "beta", "Rehearse Guard")
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_rehearse, agents["alpha"]["token"], pid, "dev"
        )
        wstools.release_workspace(beta, pid, "dev")
    finally:
        sb.close()
    print("  tool guards (foreign claim refused): ok")


def main():
    from server.tools.repo import _workspace as wstools  # noqa: E402

    agents, _post_id = setup()
    test_snapshot_roundtrip()
    test_snapshot_manifest()
    test_snapshot_guards()
    test_snapshot_delta()
    test_snapshot_delta_stacked_refuses()
    test_snapshot_delta_stacked_nonmain_base()
    test_tool_wiring(agents, wstools)
    test_tool_guards(agents, wstools)
    print("test_workspace_rehearse: all scenarios passed")


if __name__ == "__main__":
    main()
