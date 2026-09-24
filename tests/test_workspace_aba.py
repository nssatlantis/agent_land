"""Focused workspace ABA regressions for the mutator-lock PR."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_aba_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
import server._transfer as transfer  # noqa: E402
import server.tools.repo._transfer as ticket_tools  # noqa: E402
import server.tools.repo._workspace as workspace_tools  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _mk_bare(tmp):
    bare = os.path.join(tmp, "remote.git")
    seed = os.path.join(tmp, "seed")
    os.makedirs(seed)
    _git("init", "--bare", "-b", "main", bare)
    _git("init", "-b", "main", cwd=seed)
    Path(seed, "README.md").write_text("seed\n", encoding="utf-8")
    _git("-C", seed, "add", "-A")
    _git(
        "-C",
        seed,
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "seed",
    )
    _git("-C", seed, "push", bare, "main")
    return bare


_SHARED_BARE = _mk_bare(tempfile.mkdtemp(prefix="agentland_workspace_aba_remote_"))


class _Sandbox:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_workspace_aba_test_")
        self.orig_claims = ws._claims_root
        self.orig_repo = ws._repo_url
        self.orig_git_repo = gh._repo_url
        ws._claims_root = lambda: os.path.join(self.tmp, "claims")
        ws._repo_url = lambda with_token=False: _SHARED_BARE
        gh._repo_url = lambda with_token=False: _SHARED_BARE

    def close(self):
        ws._claims_root = self.orig_claims
        ws._repo_url = self.orig_repo
        gh._repo_url = self.orig_git_repo
        shutil.rmtree(self.tmp, ignore_errors=True)


def _request(ticket, path):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/transfer/{ticket}/{path}",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "path_params": {"ticket": ticket, "fpath": path},
        "state": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    from starlette.requests import Request

    return Request(scope, receive)


def test_stale_release_cas(agents):
    tok = agents["alpha"]["token"]
    pid = db.create_proposal(tok, "ABA Release", "body")["post_id"]
    old = db.claim_workspace(tok, pid, "dev")
    assert "id" in old, old
    db.release_workspace(tok, pid, "dev", claim_id=old["id"])
    fresh = db.claim_workspace(tok, pid, "dev")
    assert fresh["id"] != old["id"], (old, fresh)
    assert "changed" in expect_error(db.release_workspace, tok, pid, "dev", old["id"])
    assert db.get_workspace(tok, pid, "dev")["id"] == fresh["id"]
    db.release_workspace(tok, pid, "dev", claim_id=fresh["id"])


def test_read_ticket_reclaim_aba(agents):
    sb = _Sandbox()
    try:
        tok = agents["beta"]["token"]
        pid = db.create_proposal(tok, "ABA Download", "body")["post_id"]
        workspace_tools.claim_workspace(tok, pid, "dl")
        workspace_tools.workspace_write_file(tok, pid, "dl", "note.txt", "old\n")
        old = db.get_workspace(tok, pid, "dl")
        ticket = ticket_tools.workspace_fetch_ticket(tok, pid, "dl", ["note.txt"])
        original_read = ws.read_transfer_bytes

        def reclaim_before_read(*args, **kwargs):
            workspace_tools.release_workspace(tok, pid, "dl")
            workspace_tools.claim_workspace(tok, pid, "dl")
            fresh = db.get_workspace(tok, pid, "dl")
            assert fresh["id"] != old["id"], (old, fresh)
            return original_read(*args, **kwargs)

        with patch.object(ws, "read_transfer_bytes", reclaim_before_read):
            response = asyncio.run(
                transfer.transfer_download(_request(ticket["ticket"], "note.txt"))
            )
        assert response.status_code == 404, (response.status_code, response.body)
        assert "gone" in response.body.decode(), response.body
        workspace_tools.release_workspace(tok, pid, "dl")
    finally:
        sb.close()


def test_sweeper_rechecks_live_and_idle_state():
    sb = _Sandbox()
    try:
        root = os.path.join(sb.tmp, "claims", "7", "4242")
        reclaimed = os.path.join(root, "reclaimed")
        os.makedirs(reclaimed)
        Path(reclaimed, ".workspace.json").write_text(
            json.dumps({"agent_id": 7, "proposal_id": 4242, "name": "reclaimed"}),
            encoding="utf-8",
        )
        assert ws.sweep_released_claim_trees(lambda: {(7, 4242, "reclaimed")}) == 0
        assert os.path.isdir(reclaimed), reclaimed

        idle = os.path.join(root, "touched")
        os.makedirs(idle)
        Path(idle, ".workspace.json").write_text(
            json.dumps(
                {
                    "agent_id": 7,
                    "proposal_id": 4242,
                    "name": "touched",
                    "updated_at": 0,
                }
            ),
            encoding="utf-8",
        )
        original_read = ws._read_manifest
        reads = 0

        def reread(path):
            nonlocal reads
            manifest = original_read(path)
            if path == idle:
                reads += 1
                if reads == 2:
                    manifest["updated_at"] = time.time()
            return manifest

        with patch.object(ws, "_read_manifest", reread):
            assert ws.sweep_idle_claim_trees() == 0
        assert reads == 2, reads
        assert os.path.isdir(idle), idle
    finally:
        sb.close()


def main():
    agents, _post_id = setup()
    test_stale_release_cas(agents)
    test_read_ticket_reclaim_aba(agents)
    test_sweeper_rechecks_live_and_idle_state()
    print("test_workspace_aba: all scenarios passed")


if __name__ == "__main__":
    main()
