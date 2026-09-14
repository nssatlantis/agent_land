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


def _claim(agents, wstools, key, title):
    tok = agents[key]["token"]
    prop = db.create_proposal(tok, title, "body")
    pid = prop["post_id"]
    claimed = wstools.claim_workspace(tok, pid, "dev")
    assert claimed["claim"]["status"] == "active", claimed
    return pid, tok


def _advance_remote():
    work = tempfile.mkdtemp(prefix="agentland_claim_adv_")
    _git("clone", _SHARED_BARE, "work", cwd=work)
    w = os.path.join(work, "work")
    Path(w, "NEW.txt").write_text("new\n", encoding="utf-8")
    _git("-C", w, "add", "-A")
    _git("-C", w, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-m", "more")
    _git("-C", w, "push", "origin", "main")
    shutil.rmtree(work, ignore_errors=True)


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
        assert snap["head_sha"], snap
        assert snap["total_bytes"] > 0, snap
    finally:
        sb.close()
    print("  snapshot roundtrip (text/binary/empty/git/manifest): ok")


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
        prop = db.create_proposal(agents["alpha"]["token"], "Rehearse Shop", "body")
        pid = prop["post_id"]
        tok = agents["alpha"]["token"]
        claimed = wstools.claim_workspace(tok, pid, "dev")
        assert claimed["claim"]["status"] == "active", claimed
        wstools.workspace_write_file(tok, pid, "dev", "feat.txt", "feat\n")
        direct = wstools.workspace_rehearse(tok, pid, "dev")
        assert direct["ok"] is True, direct
        assert direct["workspace"]["files"] >= 2, direct
        assert direct["workspace"]["head_sha"], direct
        sent = {f["path"]: f["content"] for f in seen["kwargs"]["files"]}
        assert sent["feat.txt"] == "feat\n", sorted(sent)
        assert "README.md" in sent, sorted(sent)
        assert ".workspace.json" not in sent, sorted(sent)
        assert seen["args"][1] == agents["alpha"]["agent_id"], seen["args"]
        seen["hand_off"] = True
        handed = wstools.workspace_rehearse(tok, pid, "dev")
        assert handed["status"] == "running" and handed["run_id"] == "run-9", handed
        assert handed["workspace"]["files"] >= 2, handed
        assert "watch_url" in handed and "note" in handed, handed
        wstools.release_workspace(tok, pid, "dev")
    finally:
        ci_runner.run_checks_with_deadline = orig
        sb.close()
    print("  tool wiring (direct + handed-off): ok")


def test_tool_guards(agents, wstools):
    sb = _RehearseSandbox()
    try:
        prop = db.create_proposal(agents["beta"]["token"], "Rehearse Guard", "body")
        pid = prop["post_id"]
        beta = agents["beta"]["token"]
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_rehearse, agents["alpha"]["token"], pid, "dev"
        )
        wstools.claim_workspace(beta, pid, "dev")
        wstools.release_workspace(beta, pid, "dev")
    finally:
        sb.close()
    print("  tool guards (foreign claim refused): ok")


def main():
    from server.tools.repo import _workspace as wstools  # noqa: E402

    agents, _post_id = setup()
    test_snapshot_roundtrip()
    test_snapshot_guards()
    test_tool_wiring(agents, wstools)
    test_tool_guards(agents, wstools)
    print("test_workspace_rehearse: all scenarios passed")


if __name__ == "__main__":
    main()
