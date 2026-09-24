"""File ops on claim trees plus both-clocks touch (proposal #478, part 4).

Covers the seven workspace_* MCP tools against a claimed tree on a
local bare remote (no network): write/read roundtrip with ranges,
list without .git, status/diff pins, delete semantics, sync
fast-forward plus dirty-refusal, both-clocks touch, per-write budget,
path guards (.git/manifest/traversal/protected), owner isolation, and
ref reads (committed bytes at branch/tag/sha with dirt invisible,
unknown/invalid-ref and dir/missing pins).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_files_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
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


_SHARED_BARE = _mk_remote(tempfile.mkdtemp(prefix="agentland_ws_files_remote_"))


def _expect_tool_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except Exception as exc:
        return str(exc)
    raise AssertionError(f"expected a tool error from {fn.__name__}()")


class _FilesSandbox:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_ws_files_test_")
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


def test_write_read_roundtrip(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "File Shop")
        w = wstools.workspace_write_file
        got = w(tok, pid, "dev", "notes/todo.txt", "hello\n")
        assert got["path"] == "notes/todo.txt", got
        assert got["bytes"] == 6, got
        full = wstools.workspace_read_file(tok, pid, "dev", "notes/todo.txt")
        assert full["content"] == "hello", full
        assert full["total_lines"] == 1, full
        unscoped = wstools.workspace_diff(tok, pid, "dev")
        assert "hello" in unscoped["diff"], unscoped
        scoped = wstools.workspace_diff(tok, pid, "dev", path="notes/todo.txt")
        assert "hello" in scoped["diff"], scoped
        body = "l1\nl2\nl3\nl4\nl5\n"
        w(tok, pid, "dev", "lines.txt", body)
        part = wstools.workspace_read_file(tok, pid, "dev", "lines.txt", 2, 4)
        assert part["content"] == "l2\nl3\nl4", part
        assert (part["line_start"], part["line_end"]) == (2, 4), part
        clamped = wstools.workspace_read_file(tok, pid, "dev", "lines.txt", 4, 99)
        assert clamped["content"] == "l4\nl5", clamped
        assert clamped["line_end"] == 5, clamped
        assert "together" in _expect_tool_error(
            wstools.workspace_read_file, tok, pid, "dev", "lines.txt", 1, None
        )
        assert "below line_start" in _expect_tool_error(
            wstools.workspace_read_file, tok, pid, "dev", "lines.txt", 4, 2
        )
        assert "below 1" in _expect_tool_error(
            wstools.workspace_read_file, tok, pid, "dev", "lines.txt", 0, 2
        )
        assert "1000" in _expect_tool_error(
            wstools.workspace_read_file, tok, pid, "dev", "lines.txt", 1, 1001
        )
        assert "non-empty" in _expect_tool_error(w, tok, pid, "dev", "e.txt", "")
        w(tok, pid, "dev", "big.txt", "z" * ((1 << 20) + 1))
        assert "read cap" in _expect_tool_error(
            wstools.workspace_read_file, tok, pid, "dev", "big.txt"
        )
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  write/read roundtrip + ranges: ok")


def test_list_status_diff(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "gamma", "List Shop")
        st = wstools.workspace_status(tok, pid, "dev")
        assert st["dirty"] is False and st["changes"] == [], st
        assert st["head_sha"], st
        d0 = wstools.workspace_diff(tok, pid, "dev")
        assert d0["diff"] == "" and d0["truncated"] is False, d0
        wstools.workspace_write_file(tok, pid, "dev", "work.txt", "hello\n")
        st = wstools.workspace_status(tok, pid, "dev")
        assert st["dirty"] is True, st
        assert [c["path"] for c in st["changes"]] == ["work.txt"], st
        d1 = wstools.workspace_diff(tok, pid, "dev")
        assert "hello" in d1["diff"] and d1["truncated"] is False, d1
        d2 = wstools.workspace_diff(tok, pid, "dev", path="work.txt", max_bytes=10)
        assert d2["truncated"] is False, d2  # below the 1KB floor clamps up
        wstools.workspace_write_file(tok, pid, "dev", "big.txt", "x\n" * 600)
        d3 = wstools.workspace_diff(tok, pid, "dev", path="big.txt", max_bytes=1024)
        assert d3["truncated"] is True and len(d3["diff"]) == 1024, d3
        listed = wstools.workspace_list_tree(tok, pid, "dev")
        paths = [r["path"] for r in listed]
        assert "work.txt" in paths and "README.md" in paths, paths
        assert not [p for p in paths if p == ".git" or p.startswith(".git/")]
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  list/status/diff pins: ok")


def test_path_guards(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "delta", "Guard Shop")
        w = wstools.workspace_write_file
        r = wstools.workspace_read_file
        assert "invalid path" in _expect_tool_error(
            w, tok, pid, "dev", "../evil.txt", "x"
        )
        assert "invalid path" in _expect_tool_error(r, tok, pid, "dev", "a/../../evil")
        assert "relative" in _expect_tool_error(r, tok, pid, "dev", "/abs.txt")
        assert "managed" in _expect_tool_error(w, tok, pid, "dev", ".git/config", "x")
        assert "managed" in _expect_tool_error(r, tok, pid, "dev", ".git/HEAD")
        assert "managed" in _expect_tool_error(
            w, tok, pid, "dev", ".workspace.json", "x"
        )
        assert "managed" in _expect_tool_error(r, tok, pid, "dev", ".workspace.json")
        assert "protected" in _expect_tool_error(
            w, tok, pid, "dev", ".github/workflows/x.yml", "x"
        )
        assert "could not read" in _expect_tool_error(r, tok, pid, "dev", "missing.txt")
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  path guards: ok")


def test_delete_semantics(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "epsilon", "Delete Shop")
        wstools.workspace_write_file(tok, pid, "dev", "gone.txt", "bye\n")
        done = wstools.workspace_delete_file(tok, pid, "dev", "gone.txt")
        assert done == {"path": "gone.txt", "deleted": True}, done
        assert "could not read" in _expect_tool_error(
            wstools.workspace_read_file, tok, pid, "dev", "gone.txt"
        )
        assert "no file" in _expect_tool_error(
            wstools.workspace_delete_file, tok, pid, "dev", "gone.txt"
        )
        wstools.workspace_write_file(tok, pid, "dev", "sub/f.txt", "x\n")
        assert "directory" in _expect_tool_error(
            wstools.workspace_delete_file, tok, pid, "dev", "sub"
        )
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  delete semantics: ok")


def test_sync_and_clocks_and_budget(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "zeta", "Sync Shop")
        aid = agents["zeta"]["agent_id"]
        before_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        before_manifest = dict(ws.claim_tree_info(aid, pid, "dev")["manifest"])
        wstools.workspace_write_file(tok, pid, "dev", "a.txt", "a\n")
        after_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        after_manifest = ws.claim_tree_info(aid, pid, "dev")["manifest"]
        assert after_record >= before_record, (before_record, after_record)
        assert after_manifest["updated_at"] > before_manifest["updated_at"]
        assert "uncommitted work" in _expect_tool_error(
            wstools.workspace_sync, tok, pid, "dev"
        )
        wstools.workspace_delete_file(tok, pid, "dev", "a.txt")
        old = wstools.workspace_status(tok, pid, "dev")["head_sha"]
        _advance_remote()
        synced = wstools.workspace_sync(tok, pid, "dev")
        assert synced["old_sha"] == old, synced
        assert synced["new_sha"] and synced["new_sha"] != old, synced
        assert synced["base"] == "main", synced
        old_cap = config.WORKSPACE_CLAIM_MAX_MB
        config.WORKSPACE_CLAIM_MAX_MB = 0
        try:
            err = _expect_tool_error(
                wstools.workspace_write_file, tok, pid, "dev", "big.txt", "x\n"
            )
            assert "MAX_MB" in err, err
        finally:
            config.WORKSPACE_CLAIM_MAX_MB = old_cap
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  sync + both clocks + budget: ok")


def test_read_at_ref(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Ref Shop")
        w = wstools.workspace_write_file
        r = wstools.workspace_read_file
        aid = agents["alpha"]["agent_id"]
        dest = ws._claim_dir(aid, pid, "dev")
        w(tok, pid, "dev", "frozen.txt", "committed one\ncommitted two\n")
        w(tok, pid, "dev", "sub/f.txt", "inner\n")
        Path(dest, "blob.bin").write_bytes(b"\xff\xfe\x00binary\n")
        Path(dest, "bigref.txt").write_text("z" * ((1 << 20) + 1), encoding="utf-8")
        extras = ["blob.bin", "bigref.txt"]
        try:
            os.symlink("frozen.txt", os.path.join(dest, "linkref.txt"))
            extras.append("linkref.txt")
        except (OSError, NotImplementedError):
            pass
        _git("-C", dest, "add", "frozen.txt", "sub/f.txt", *extras)
        _git(
            "-C",
            dest,
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "freeze",
        )
        old = subprocess.run(
            ["git", "-C", dest, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert old, "need the frozen commit sha"
        _git("-C", dest, "branch", "frozen-ref-pin", old)
        w(tok, pid, "dev", "frozen.txt", "dirty one\ndirty two\n")
        at_sha = r(tok, pid, "dev", "frozen.txt", ref=old)
        assert at_sha["content"] == "committed one\ncommitted two", at_sha
        assert at_sha["ref"] == old, at_sha
        live = r(tok, pid, "dev", "frozen.txt")
        assert live["content"] == "dirty one\ndirty two", live
        assert "ref" not in live, live
        at_branch = r(tok, pid, "dev", "frozen.txt", ref="frozen-ref-pin")
        assert at_branch["content"] == at_sha["content"], at_branch
        assert at_branch["ref"] == "frozen-ref-pin", at_branch
        page = r(
            tok,
            pid,
            "dev",
            "frozen.txt",
            line_start=2,
            line_end=2,
            ref=old,
        )
        assert page["content"] == "committed two", page
        assert page["content_sha256"] == at_sha["content_sha256"], page
        # origin/ fallback: publish the branch, drop the local one, so
        # only origin/frozen-ref-pin resolves.
        _git("-C", dest, "push", "origin", "frozen-ref-pin")
        _git("-C", dest, "branch", "-D", "frozen-ref-pin")
        via_origin = r(tok, pid, "dev", "frozen.txt", ref="frozen-ref-pin")
        assert via_origin["content"] == at_sha["content"], via_origin
        assert via_origin["ref"] == "origin/frozen-ref-pin", via_origin
        assert "unknown ref" in _expect_tool_error(
            r, tok, pid, "dev", "frozen.txt", ref="no-such-branch-xyz"
        )
        assert "invalid ref" in _expect_tool_error(
            r, tok, pid, "dev", "frozen.txt", ref="bad..ref"
        )
        assert "no file at" in _expect_tool_error(
            r, tok, pid, "dev", "ghost.txt", ref=old
        )
        assert "is a directory" in _expect_tool_error(
            r, tok, pid, "dev", "sub", ref=old
        )
        # Binary at ref decodes with replacement (proves the bytes path -
        # text-mode git would raise before returning).
        at_bin = r(tok, pid, "dev", "blob.bin", ref=old)
        assert "binary" in at_bin["content"], at_bin
        assert "over the" in _expect_tool_error(
            r, tok, pid, "dev", "bigref.txt", ref=old
        )
        if "linkref.txt" in extras:
            # Unreachable via the MCP tool (live symlink components
            # refuse first), so pinned at the engine: the link blob reads
            # as its target text.
            link_raw, link_ref = ws.read_file_at_ref(dest, "linkref.txt", old)
            assert link_raw == b"frozen.txt", link_raw
            assert link_ref == old, link_ref
        assert "must be a" in _expect_tool_error(
            r, tok, pid, "dev", "frozen.txt", ref=123
        )
        assert "invalid ref" in _expect_tool_error(
            r, tok, pid, "dev", "frozen.txt", ref="@{head}"
        )
        assert "invalid ref" in _expect_tool_error(
            r, tok, pid, "dev", "frozen.txt", ref="-lead"
        )
        beta = agents["beta"]["token"]
        assert "no active workspace" in _expect_tool_error(
            r, beta, pid, "dev", "frozen.txt", ref=old
        )
        before_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        before_manifest = dict(ws.claim_tree_info(aid, pid, "dev")["manifest"])
        r(tok, pid, "dev", "frozen.txt", ref=old)
        after_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        after_manifest = ws.claim_tree_info(aid, pid, "dev")["manifest"]
        assert after_record >= before_record, (before_record, after_record)
        assert after_manifest["updated_at"] > before_manifest["updated_at"]
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  ref reads (committed vs dirty): ok")


def test_owner_isolation(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Isolation Shop")
        beta = agents["beta"]["token"]
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_read_file, beta, pid, "dev", "README.md"
        )
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_write_file, beta, pid, "dev", "evil.txt", "x\n"
        )
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_sync, beta, pid, "dev"
        )
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_list_tree, beta, pid, "dev"
        )
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_status, beta, pid, "dev"
        )
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_diff, beta, pid, "dev"
        )
        assert "no active workspace" in _expect_tool_error(
            wstools.workspace_delete_file, beta, pid, "dev", "README.md"
        )
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  owner isolation: ok")


def test_workspace_edits(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Edits Shop")
        w = wstools.workspace_write_file
        w(tok, pid, "dev", "doc.txt", "alpha beta\ngamma delta\n")
        got = w(
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [{"find": "beta", "replace": "BETA"}],
        )
        assert got["path"] == "doc.txt", got
        assert got["bytes"] > 0 and len(got["patch_log"]) == 1, got
        assert got["patch_log"][0]["matched"] == 1, got
        read_back = wstools.workspace_read_file(tok, pid, "dev", "doc.txt")
        assert "BETA" in read_back["content"], read_back
        got2 = w(
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [
                {"find": "alpha", "replace": "A"},
                {"find": "gamma", "replace": "G"},
            ],
        )
        assert len(got2["patch_log"]) == 2, got2
        read_back2 = wstools.workspace_read_file(tok, pid, "dev", "doc.txt")
        assert read_back2["content"] == "A BETA\nG delta", read_back2
        w(tok, pid, "dev", "rep.txt", "x\nx\nx\n")
        assert "matched 3 times" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "rep.txt",
            None,
            [{"find": "x", "replace": "y"}],
        )
        got3 = w(
            tok,
            pid,
            "dev",
            "rep.txt",
            None,
            [{"find": "x", "replace": "y", "occurrence": 2}],
        )
        assert got3["patch_log"] == [
            {"find": "x", "replace": "y", "occurrence": 2, "matched": 3}
        ], got3
        rep = wstools.workspace_read_file(tok, pid, "dev", "rep.txt")
        assert rep["content"] == "x\ny\nx", rep
        assert "did not match" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [{"find": "zzz-nope", "replace": "y"}],
        )
        assert "no file" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "ghost.txt",
            None,
            [{"find": "a", "replace": "b"}],
        )
        assert "not both" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            "full",
            [{"find": "a", "replace": "b"}],
        )
        assert "non-empty list" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [],
        )
        assert "leave the file empty" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [{"find": "A BETA\nG delta\n", "replace": ""}],
        )
        assert "managed" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            ".workspace.json",
            None,
            [{"find": "a", "replace": "b"}],
        )
        assert "protected" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            ".github/workflows/x.yml",
            None,
            [{"find": "a", "replace": "b"}],
        )
        assert "must be a dict" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            ["not-a-dict"],
        )
        assert "needs a non-empty 'find'" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [{"find": "", "replace": "y"}],
        )
        assert "positive integer" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [{"find": "A", "replace": "y", "occurrence": 0}],
        )
        assert "out of range" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [{"find": "A", "replace": "y", "occurrence": 9}],
        )
        assert "too many edits" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "doc.txt",
            None,
            [{"find": "A", "replace": "y"}] * 201,
        )
        w(tok, pid, "dev", "sub/f.txt", "x\n")
        assert "directory" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "sub",
            None,
            [{"find": "a", "replace": "b"}],
        )
        old_cap = config.WORKSPACE_CLAIM_MAX_MB
        config.WORKSPACE_CLAIM_MAX_MB = 0
        try:
            err = _expect_tool_error(
                w,
                tok,
                pid,
                "dev",
                "doc.txt",
                None,
                [{"find": "G delta", "replace": "g"}],
            )
            assert "MAX_MB" in err, err
        finally:
            config.WORKSPACE_CLAIM_MAX_MB = old_cap
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  workspace edits mode: ok")


def main():
    from server.tools.repo import _workspace as wstools  # noqa: E402

    agents, _post_id = setup()
    test_write_read_roundtrip(agents, wstools)
    test_list_status_diff(agents, wstools)
    test_path_guards(agents, wstools)
    test_delete_semantics(agents, wstools)
    test_sync_and_clocks_and_budget(agents, wstools)
    test_workspace_edits(agents, wstools)
    test_read_at_ref(agents, wstools)
    test_owner_isolation(agents, wstools)
    print("test_workspace_files: all scenarios passed")


if __name__ == "__main__":
    main()
