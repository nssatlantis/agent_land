"""Claim-tree bytes plus claim/release/list tools (proposal #476, part 3).

Covers ``github._workspaces`` against local bare remotes (no network):
clone with manifest, resume that never wipes dirty work, owner-mismatch
rebuild, retire/info, budget and name gates, TTL sweep - plus one
end-to-end run through the MCP tools (record + tree + list + release).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspaces_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
from github._core import RepoError  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


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
    _git(
        "-C", seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-m", "seed"
    )
    _git("-C", seed, "push", bare, "main")
    return bare


_SHARED_BARE = _mk_remote(tempfile.mkdtemp(prefix="agentland_ws_claim_remote_"))


def _expect_repo_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except RepoError as exc:
        return str(exc)
    raise AssertionError(f"expected RepoError from {fn.__name__}()")


class _ClaimsSandbox:
    """Isolates one scenario: unique claims root, bare remote, saved knobs."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_claims_test_")
        self._orig = {
            "repo_url": ws._repo_url,
            "claims_root": ws._claims_root,
            "gitops_url": gh._repo_url,
            "max_mb": config.WORKSPACE_CLAIM_MAX_MB,
            "ttl": config.WORKSPACE_CLAIM_TTL_HOURS,
        }
        ws._repo_url = lambda with_token=False: _SHARED_BARE
        ws._claims_root = lambda: os.path.join(self.tmp, "claims")
        gh._repo_url = lambda with_token=False: _SHARED_BARE

    def close(self):
        ws._repo_url = self._orig["repo_url"]
        ws._claims_root = self._orig["claims_root"]
        gh._repo_url = self._orig["gitops_url"]
        config.WORKSPACE_CLAIM_MAX_MB = self._orig["max_mb"]
        config.WORKSPACE_CLAIM_TTL_HOURS = self._orig["ttl"]
        shutil.rmtree(self.tmp, ignore_errors=True)


def _manifest_of(path):
    return json.loads(Path(path, ".workspace.json").read_text(encoding="utf-8"))


def test_ensure_clones_with_manifest():
    sb = _ClaimsSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 22, "feat")
        assert os.path.isdir(os.path.join(tree["path"], ".git"))
        assert tree["resumed"] is False
        assert tree["dirty"] is False
        manifest = _manifest_of(tree["path"])
        assert manifest["agent_id"] == 11, manifest
        assert manifest["proposal_id"] == 22, manifest
        assert manifest["name"] == "feat", manifest
    finally:
        sb.close()
    print("  ensure clones with manifest: ok")


def test_resume_never_wipes_dirty():
    sb = _ClaimsSandbox()
    try:
        first = ws.ensure_claim_tree(11, 23, "work")
        junk = os.path.join(first["path"], "JUNK.txt")
        Path(junk).write_text("x", encoding="utf-8")
        second = ws.ensure_claim_tree(11, 23, "work")
        assert second["path"] == first["path"]
        assert second["resumed"] is True
        assert second["dirty"] is True
        assert os.path.isfile(junk), "resume must never wipe dirty work"
    finally:
        sb.close()
    print("  resume never wipes dirty work: ok")


def test_owner_mismatch_rebuilds():
    sb = _ClaimsSandbox()
    try:
        first = ws.ensure_claim_tree(11, 24, "mine")
        Path(first["path"], "JUNK.txt").write_text("x", encoding="utf-8")
        manifest_path = Path(first["path"], ".workspace.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["agent_id"] = 999
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        second = ws.ensure_claim_tree(11, 24, "mine")
        assert second["resumed"] is False
        assert not os.path.exists(os.path.join(second["path"], "JUNK.txt"))
        assert _manifest_of(second["path"])["agent_id"] == 11
        # A corrupt (non-numeric) manifest never raises: mismatch rebuilds.
        manifest_path = Path(second["path"], ".workspace.json")
        manifest_path.write_text(json.dumps({"agent_id": "x"}), encoding="utf-8")
        third = ws.ensure_claim_tree(11, 24, "mine")
        assert third["resumed"] is False
        assert _manifest_of(third["path"])["agent_id"] == 11
    finally:
        sb.close()
    print("  owner mismatch rebuilds: ok")


def test_retire_and_info():
    sb = _ClaimsSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 25, "tmp")
        info = ws.claim_tree_info(11, 25, "tmp")
        assert info["exists"] is True
        assert info["size_mb"] >= 0.0
        assert ws.retire_claim_tree(11, 25, "tmp") is True
        assert ws.retire_claim_tree(11, 25, "tmp") is False
        assert ws.claim_tree_info(11, 25, "tmp")["exists"] is False
        assert tree["path"], "ensure must report the tree path"
    finally:
        sb.close()
    print("  retire + info: ok")


def test_budget_and_name_gates():
    sb = _ClaimsSandbox()
    try:
        for bad in ("", "has space", "semi;colon"):
            assert "workspace name must be" in _expect_repo_error(
                ws.ensure_claim_tree, 11, 26, bad
            ), bad
        old = config.WORKSPACE_CLAIM_MAX_MB
        config.WORKSPACE_CLAIM_MAX_MB = 0
        try:
            err = _expect_repo_error(ws.ensure_claim_tree, 11, 26, "over")
            assert "MAX_MB" in err, err
        finally:
            config.WORKSPACE_CLAIM_MAX_MB = old
    finally:
        sb.close()
    print("  budget + name gates: ok")


def test_idle_sweep_and_disable():
    sb = _ClaimsSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 27, "old")
        manifest_path = Path(tree["path"], ".workspace.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["updated_at"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        assert ws.sweep_idle_claim_trees() == 1
        assert not os.path.isdir(tree["path"])
        old_ttl = config.WORKSPACE_CLAIM_TTL_HOURS
        config.WORKSPACE_CLAIM_TTL_HOURS = 0
        try:
            assert ws.sweep_idle_claim_trees() == 0
        finally:
            config.WORKSPACE_CLAIM_TTL_HOURS = old_ttl
    finally:
        sb.close()
    print("  idle sweep + TTL=0 disable: ok")


def test_end_to_end_tool(agents):
    from server.tools.repo import _workspace as wstools  # noqa: E402

    sb = _ClaimsSandbox()
    try:
        prop = db.create_proposal(agents["alpha"]["token"], "Claim Shop", "body")
        pid = prop["post_id"]
        claimed = wstools.claim_workspace(agents["alpha"]["token"], pid, "e2e")
        assert claimed["claim"]["status"] == "active", claimed
        assert os.path.isdir(claimed["tree"]["path"]), claimed
        listed = wstools.list_workspaces(agents["alpha"]["token"])
        assert [c["name"] for c in listed] == ["e2e"], listed
        assert listed[0]["tree"]["exists"] is True, listed
        done = wstools.release_workspace(agents["alpha"]["token"], pid, "e2e")
        assert done["status"] == "released", done
        assert not os.path.isdir(claimed["tree"]["path"])
        assert "no active workspace" in expect_error(
            db.get_workspace, agents["alpha"]["token"], pid, "e2e"
        )
    finally:
        sb.close()
    print("  end-to-end tool run (claim/list/release): ok")


def test_dir_size_skips_git():
    sb = _ClaimsSandbox()
    try:
        tree = ws.ensure_claim_tree(11, 28, "sized")
        before = ws._dir_size_mb(tree["path"])
        Path(tree["path"], ".git", "junk.bin").write_bytes(b"z" * (1 << 20))
        after = ws._dir_size_mb(tree["path"])
        assert after == before, (before, after)
        assert ws.claim_tree_info(11, 28, "sized")["size_mb"] == round(before, 2)
    finally:
        sb.close()
    print("  dir size skips .git: ok")


def main():
    agents, _post_id = setup()
    test_ensure_clones_with_manifest()
    test_resume_never_wipes_dirty()
    test_owner_mismatch_rebuilds()
    test_retire_and_info()
    test_budget_and_name_gates()
    test_idle_sweep_and_disable()
    test_end_to_end_tool(agents)
    test_dir_size_skips_git()
    print("test_workspaces: all scenarios passed")


if __name__ == "__main__":
    main()
