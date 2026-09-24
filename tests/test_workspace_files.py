"""File ops on claim trees plus both-clocks touch (proposal #478, part 4).

Covers the seven workspace_* MCP tools against a claimed tree on a
local bare remote (no network): write/read roundtrip with ranges,
list without .git, status/diff pins, delete semantics, sync
fast-forward plus dirty-refusal, both-clocks touch, per-write budget,
path guards (.git/manifest/traversal/protected), owner isolation, and
ref reads (committed bytes at branch/tag/sha with dirt invisible,
unknown/invalid-ref and dir/missing pins), and guarded per-file reset
from HEAD or a named ref including dry-run, present/absent guards,
atomic-replace failure, symlink refusal, budget, missing-live-file,
untracked, binary, empty, and mode-validation edges.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

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


def test_read_tools_wait_for_tree_lock(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Read Lock Shop")
        dest = ws._claim_dir(agents["alpha"]["agent_id"], pid, "dev")

        def assert_serialized(call):
            done = threading.Event()
            errors = []

            def invoke():
                try:
                    call()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()

            worker = threading.Thread(target=invoke)
            with ws.workspace_lock(dest):
                worker.start()
                assert not done.wait(0.1), "read tool bypassed the tree lock"
            worker.join(2)
            assert not worker.is_alive(), "read tool did not finish after unlock"
            assert not errors, errors

        assert_serialized(
            lambda: wstools.workspace_list_tree(tok, pid, "dev")
        )
        assert_serialized(
            lambda: wstools.workspace_search(tok, pid, "dev", "seed")
        )
        assert_serialized(
            lambda: wstools.workspace_read_file(tok, pid, "dev", "README.md")
        )
        assert_serialized(lambda: wstools.workspace_status(tok, pid, "dev"))
        assert_serialized(lambda: wstools.workspace_diff(tok, pid, "dev"))

        snapshot_entered = threading.Event()
        original_snapshot = ws.snapshot_claim_tree

        def observed_snapshot(*args, **kwargs):
            snapshot_entered.set()
            return original_snapshot(*args, **kwargs)

        errors = []

        def rehearse():
            try:
                wstools.workspace_rehearse(tok, pid, "dev", checks="format")
            except BaseException as exc:
                errors.append(exc)

        with patch.object(ws, "snapshot_claim_tree", observed_snapshot):
            worker = threading.Thread(target=rehearse)
            with ws.workspace_lock(dest):
                worker.start()
                assert not snapshot_entered.wait(0.1)
            worker.join(2)
        assert not worker.is_alive(), "rehearse did not finish after unlock"
        assert snapshot_entered.is_set()
        assert len(errors) == 1, errors
        assert "snapshot is empty" in str(errors[0]), errors
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  read/status/diff/rehearse wait for the tree lock: ok")


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


def test_workspace_reset(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Reset Shop")
        w = wstools.workspace_write_file
        r = wstools.workspace_read_file
        aid = agents["alpha"]["agent_id"]
        dest = ws._claim_dir(aid, pid, "dev")
        before_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        before_manifest = dict(ws.claim_tree_info(aid, pid, "dev")["manifest"])

        w(tok, pid, "dev", "README.md", "dirty one\n")
        first_sha = r(tok, pid, "dev", "README.md")["content_sha256"]
        w(tok, pid, "dev", "README.md", "dirty two\n")
        assert "stale base" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "README.md",
            reset=True,
            expect_sha256=first_sha,
        )
        assert r(tok, pid, "dev", "README.md")["content"] == "dirty two"

        lock_sha = r(tok, pid, "dev", "README.md")["content_sha256"]
        replace_entered = threading.Event()
        replace_release = threading.Event()
        original_replace = os.replace
        reset_result = {}
        reset_errors = []
        write_errors = []
        write_done = threading.Event()

        def blocked_replace(*args, **kwargs):
            replace_entered.set()
            if not replace_release.wait(5):
                raise AssertionError("reset replacement was not released")
            return original_replace(*args, **kwargs)

        def run_reset():
            try:
                reset_result["value"] = w(
                    tok,
                    pid,
                    "dev",
                    "README.md",
                    reset=True,
                    expect_sha256=lock_sha,
                )
            except BaseException as exc:
                reset_errors.append(exc)

        def run_writer():
            try:
                w(tok, pid, "dev", "README.md", "raced after check\n")
            except BaseException as exc:
                write_errors.append(exc)
            finally:
                write_done.set()

        with patch.object(os, "replace", blocked_replace):
            reset_thread = threading.Thread(target=run_reset)
            reset_thread.start()
            assert replace_entered.wait(5)
            write_thread = threading.Thread(target=run_writer)
            write_thread.start()
            assert not write_done.wait(0.2)
            replace_release.set()
            reset_thread.join(5)
            write_thread.join(5)
        assert not reset_thread.is_alive(), "reset thread did not finish"
        assert not write_thread.is_alive(), "writer thread did not finish"
        assert not reset_errors, reset_errors
        assert not write_errors, write_errors
        assert reset_result["value"]["changed"] is True, reset_result
        assert r(tok, pid, "dev", "README.md")["content"] == "raced after check"
        w(tok, pid, "dev", "README.md", "dirty two\n")

        transfer_sha = r(tok, pid, "dev", "README.md")["content_sha256"]
        transfer_entered = threading.Event()
        transfer_release = threading.Event()
        transfer_done = threading.Event()
        transfer_errors = []
        original_transfer_replace = os.replace
        transfer_reset_result = {}

        def blocked_transfer_replace(*args, **kwargs):
            transfer_entered.set()
            if not transfer_release.wait(5):
                raise AssertionError("transfer reset replacement was not released")
            return original_transfer_replace(*args, **kwargs)

        def run_transfer_reset():
            try:
                transfer_reset_result["value"] = w(
                    tok,
                    pid,
                    "dev",
                    "README.md",
                    reset=True,
                    expect_sha256=transfer_sha,
                )
            except BaseException as exc:
                transfer_errors.append(exc)

        def run_transfer():
            try:
                ws.apply_transfer_bytes(
                    aid,
                    pid,
                    "dev",
                    "README.md",
                    b"transfer after check\n",
                    expect_sha256=transfer_sha,
                )
            except BaseException as exc:
                transfer_errors.append(exc)
            finally:
                transfer_done.set()

        with patch.object(os, "replace", blocked_transfer_replace):
            transfer_reset_thread = threading.Thread(target=run_transfer_reset)
            transfer_reset_thread.start()
            assert transfer_entered.wait(5)
            transfer_thread = threading.Thread(target=run_transfer)
            transfer_thread.start()
            assert not transfer_done.wait(0.2)
            transfer_release.set()
            transfer_reset_thread.join(5)
            transfer_thread.join(5)
        assert not transfer_reset_thread.is_alive(), "transfer reset did not finish"
        assert not transfer_thread.is_alive(), "transfer upload did not finish"
        assert len(transfer_errors) == 1, transfer_errors
        assert "stale base" in str(transfer_errors[0])
        assert transfer_reset_result["value"]["changed"] is True
        assert r(tok, pid, "dev", "README.md")["content"] == "seed"
        w(tok, pid, "dev", "README.md", "dirty two\n")

        current_sha = r(tok, pid, "dev", "README.md")["content_sha256"]
        preview = w(
            tok,
            pid,
            "dev",
            "README.md",
            reset=True,
            expect_sha256=current_sha,
            dry_run=True,
        )
        assert preview["reset"] is True and preview["ref"] == "HEAD", preview
        assert preview["changed"] is True and preview["dry_run"] is True, preview
        assert r(tok, pid, "dev", "README.md")["content"] == "dirty two"

        def fail_replace(*_args, **_kwargs):
            raise OSError("injected replace failure")

        with patch.object(os, "replace", fail_replace):
            assert "atomically reset" in _expect_tool_error(
                w,
                tok,
                pid,
                "dev",
                "README.md",
                reset=True,
                expect_sha256=current_sha,
            )
        assert r(tok, pid, "dev", "README.md")["content"] == "dirty two"
        assert not list(Path(dest, ".git").glob("workspace-reset-*"))

        original_ref_read = wstools.read_regular_file_at_ref

        def racing_ref_read(*args, **kwargs):
            result = original_ref_read(*args, **kwargs)
            Path(dest, "README.md").write_text("raced\n", encoding="utf-8")
            return result

        with patch.object(wstools, "read_regular_file_at_ref", racing_ref_read):
            assert "concurrent change" in _expect_tool_error(
                w,
                tok,
                pid,
                "dev",
                "README.md",
                reset=True,
                expect_sha256=current_sha,
            )
        assert r(tok, pid, "dev", "README.md")["content"] == "raced"
        w(tok, pid, "dev", "README.md", "dirty two\n")

        restored = w(tok, pid, "dev", "README.md", reset=True)
        assert restored["reset"] is True and restored["ref"] == "HEAD", restored
        assert restored["changed"] is True and restored["bytes"] == 5, restored
        assert r(tok, pid, "dev", "README.md")["content"] == "seed"
        before_noop_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        before_noop_manifest = dict(ws.claim_tree_info(aid, pid, "dev")["manifest"])
        noop = w(tok, pid, "dev", "README.md", reset=True)
        after_noop_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        after_noop_manifest = ws.claim_tree_info(aid, pid, "dev")["manifest"]
        assert noop["changed"] is False, noop
        assert after_noop_record > before_noop_record, (
            before_noop_record,
            after_noop_record,
        )
        assert after_noop_manifest["updated_at"] > before_noop_manifest["updated_at"]

        wstools.workspace_delete_file(tok, pid, "dev", "README.md")
        restored_missing = w(
            tok, pid, "dev", "README.md", reset=True, expect_absent=True
        )
        assert restored_missing["changed"] is True, restored_missing
        assert r(tok, pid, "dev", "README.md")["content"] == "seed"
        wstools.workspace_delete_file(tok, pid, "dev", "README.md")
        w(tok, pid, "dev", "README.md", "present\n")
        assert "stale base" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "README.md",
            reset=True,
            expect_absent=True,
        )
        assert r(tok, pid, "dev", "README.md")["content"] == "present"
        assert "not both" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "README.md",
            reset=True,
            expect_absent=True,
            expect_sha256="0" * 64,
        )
        wstools.workspace_delete_file(tok, pid, "dev", "README.md")
        w(tok, pid, "dev", "README.md", "seed\n")

        w(tok, pid, "dev", "fresh.txt", "new\n")
        assert "no file at" in _expect_tool_error(
            w, tok, pid, "dev", "fresh.txt", reset=True
        )
        wstools.workspace_delete_file(tok, pid, "dev", "fresh.txt")

        w(tok, pid, "dev", "named.txt", "base one\n")
        _git("-C", dest, "add", "named.txt")
        _git(
            "-C",
            dest,
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "named base",
        )
        _git("-C", dest, "branch", "reset-base")
        _git("-C", dest, "branch", "mode-base")
        os.chmod(Path(dest, "named.txt"), 0o755)
        _git("-C", dest, "update-index", "--chmod=+x", "named.txt")
        _git(
            "-C",
            dest,
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "executable base",
        )
        _git("-C", dest, "branch", "mode-exec")
        w(tok, pid, "dev", "named.txt", "dirty named\n")
        exec_reset = w(tok, pid, "dev", "named.txt", reset=True)
        assert exec_reset["changed"] is True, exec_reset
        if os.name != "nt":
            assert os.stat(Path(dest, "named.txt")).st_mode & 0o111, exec_reset
        w(tok, pid, "dev", "named.txt", "dirty named again\n")
        mode_reset = w(
            tok,
            pid,
            "dev",
            "named.txt",
            reset=True,
            base_ref="mode-base",
        )
        assert mode_reset["ref"] == "mode-base", mode_reset
        assert mode_reset["changed"] is True, mode_reset
        if os.name != "nt":
            assert not os.stat(Path(dest, "named.txt")).st_mode & 0o111, mode_reset
        named = w(
            tok,
            pid,
            "dev",
            "named.txt",
            reset=True,
            base_ref="reset-base",
        )
        assert named["ref"] == "reset-base" and named["changed"] is False, named
        assert r(tok, pid, "dev", "named.txt")["content"] == "base one"
        assert "unknown ref" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "named.txt",
            reset=True,
            base_ref="missing-reset-ref",
        )
        assert "exactly one" in _expect_tool_error(
            w, tok, pid, "dev", "named.txt", "content\n", reset=True
        )
        assert "exactly one" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "named.txt",
            edits=[{"find": "base", "replace": "dirty"}],
            reset=True,
        )
        assert "base_ref is valid only" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "named.txt",
            "content\n",
            base_ref="HEAD",
        )

        if hasattr(os, "mkfifo"):
            fifo = Path(dest, "live.fifo")
            os.mkfifo(fifo)
            assert "not a regular" in _expect_tool_error(
                w,
                tok,
                pid,
                "dev",
                "live.fifo",
                reset=True,
                expect_absent=True,
            )
            fifo.unlink()

        link_blob = (
            subprocess.run(
                ["git", "-C", dest, "hash-object", "-w", "--stdin"],
                input=b"target.txt",
                check=True,
                capture_output=True,
            )
            .stdout.decode()
            .strip()
        )
        assert link_blob, "need a symlink blob"
        _git(
            "-C",
            dest,
            "update-index",
            "--add",
            "--cacheinfo",
            "120000",
            link_blob,
            "linkbase.txt",
        )
        _git(
            "-C",
            dest,
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "symlink base",
        )
        _git("-C", dest, "branch", "symlink-reset-base")
        Path(dest, "linkbase.txt").write_text("regular live\n", encoding="utf-8")
        assert "not a regular file" in _expect_tool_error(
            w,
            tok,
            pid,
            "dev",
            "linkbase.txt",
            reset=True,
            base_ref="symlink-reset-base",
        )
        assert r(tok, pid, "dev", "linkbase.txt")["content"] == "regular live"
        _git("-C", dest, "reset", "--hard", "HEAD")

        w(tok, pid, "dev", "named.txt", "budget dirty\n")
        budget_sha = r(tok, pid, "dev", "named.txt")["content_sha256"]
        old_cap = config.WORKSPACE_CLAIM_MAX_MB
        config.WORKSPACE_CLAIM_MAX_MB = 0
        try:
            assert "MAX_MB" in _expect_tool_error(
                w,
                tok,
                pid,
                "dev",
                "named.txt",
                reset=True,
                expect_sha256=budget_sha,
            )
        finally:
            config.WORKSPACE_CLAIM_MAX_MB = old_cap
        assert r(tok, pid, "dev", "named.txt")["content"] == "budget dirty"
        w(tok, pid, "dev", "named.txt", "base one\n")

        Path(dest, "blob.bin").write_bytes(b"\xff\xfe\x00binary\n")
        Path(dest, "empty.txt").write_text("", encoding="utf-8")
        _git("-C", dest, "add", "blob.bin", "empty.txt")
        _git(
            "-C",
            dest,
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "reset edge bytes",
        )
        assert "not UTF-8" in _expect_tool_error(
            w, tok, pid, "dev", "blob.bin", reset=True
        )
        assert "empty file" in _expect_tool_error(
            w, tok, pid, "dev", "empty.txt", reset=True
        )

        after_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        after_manifest = ws.claim_tree_info(aid, pid, "dev")["manifest"]
        assert after_record >= before_record, (before_record, after_record)
        assert after_manifest["updated_at"] > before_manifest["updated_at"]
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  workspace reset mode: ok")


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
        assert "exactly one" in _expect_tool_error(
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


def test_workspace_serialized_async(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Async Lock Shop")

        async def run_hold_release():
            first_entered = asyncio.Event()
            first_release = asyncio.Event()
            second_started = asyncio.Event()
            active = 0
            max_active = 0

            async def operation(token, proposal_id, name, value):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                try:
                    if value == 1:
                        first_entered.set()
                        await first_release.wait()
                    elif value == 2:
                        second_started.set()
                    await asyncio.sleep(0)
                    return value
                finally:
                    active -= 1

            serialized = wstools._workspace_serialized(operation)
            first = asyncio.create_task(serialized(tok, pid, "dev", 1))
            await asyncio.wait_for(first_entered.wait(), 1)
            second = asyncio.create_task(serialized(tok, pid, "dev", 2))
            done, _ = await asyncio.wait({second}, timeout=0.05)
            assert not done
            assert not second_started.is_set()
            first_release.set()
            assert await first == 1
            assert await second == 2
            assert max_active == 1

        asyncio.run(run_hold_release())
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  workspace async enter/hold/release serialization: ok")


def test_workspace_serialized_cancellation(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Async Cancellation Shop")

        async def run_cancellation():
            entered = asyncio.Event()
            blocked_entered = asyncio.Event()
            finally_ran = False
            active = 0
            operation_release = asyncio.Event()

            async def operation(token, proposal_id, name, value):
                nonlocal active, finally_ran
                active += 1
                try:
                    if value == 1:
                        entered.set()
                        await operation_release.wait()
                    elif value == 3:
                        blocked_entered.set()
                        await asyncio.sleep(0)
                    else:
                        await asyncio.sleep(0)
                    return value
                finally:
                    active -= 1
                    if value == 1:
                        finally_ran = True

            serialized = wstools._workspace_serialized(operation)
            task = asyncio.create_task(serialized(tok, pid, "dev", 1))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("serialized operation was not cancelled")
            assert finally_ran is False
            assert active == 1
            operation_release.set()
            assert await asyncio.wait_for(serialized(tok, pid, "dev", 4), 2) == 4
            assert finally_ran is True
            assert active == 0

            dest = ws._claim_dir(agents["alpha"]["agent_id"], pid, "dev")
            lock_factory = wstools.workspace_lock
            real_lock = lock_factory(dest)
            holder_entered = threading.Event()
            holder_release = threading.Event()
            acquire_attempted = threading.Event()

            class ObservedLock:
                def __init__(self, lock):
                    self._lock = lock

                def __enter__(self):
                    acquire_attempted.set()
                    return self._lock.__enter__()

                def __exit__(self, *args):
                    return self._lock.__exit__(*args)

            def observed_lock(path):
                assert path == dest
                return ObservedLock(lock_factory(path))

            def hold_lock():
                with real_lock:
                    holder_entered.set()
                    if not holder_release.wait(5):
                        raise AssertionError("workspace lock holder was not released")

            holder = threading.Thread(target=hold_lock)
            with patch.object(wstools, "workspace_lock", observed_lock):
                holder.start()
                try:
                    blocked = asyncio.create_task(serialized(tok, pid, "dev", 3))
                    assert await asyncio.to_thread(holder_entered.wait, 1)
                    assert await asyncio.to_thread(acquire_attempted.wait, 1)
                    blocked.cancel()
                    await asyncio.sleep(0)
                    blocked.cancel()
                    holder_release.set()
                    try:
                        await asyncio.wait_for(blocked, 1)
                    except asyncio.CancelledError:
                        pass
                    else:
                        raise AssertionError("blocked acquisition was not cancelled")
                    assert not blocked_entered.is_set()
                    assert (
                        await asyncio.wait_for(serialized(tok, pid, "dev", 4), 2) == 4
                    )
                finally:
                    holder_release.set()
                    await asyncio.to_thread(holder.join, 5)
            assert not holder.is_alive(), "workspace lock holder did not finish"
            assert await serialized(tok, pid, "dev", 2) == 2

        asyncio.run(run_cancellation())
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  workspace async cancellation: ok")


def test_workspace_serialized_cancelled_worker_keeps_lock(agents, wstools):
    sb = _FilesSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Cancelled Worker Shop")

        async def run_cancelled_worker():
            worker_started = threading.Event()
            worker_release = threading.Event()

            async def operation(token, proposal_id, name, value):
                def worker():
                    worker_started.set()
                    if not worker_release.wait(5):
                        raise AssertionError("cancelled worker was not released")
                    return value

                return await asyncio.to_thread(worker)

            serialized = wstools._workspace_serialized(operation)
            first = asyncio.create_task(serialized(tok, pid, "dev", 1))
            assert await asyncio.to_thread(worker_started.wait, 1)
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("serialized operation was not cancelled")
            second = asyncio.create_task(serialized(tok, pid, "dev", 2))
            done, _ = await asyncio.wait({second}, timeout=0.05)
            assert not done, "cancelled worker released the lock too early"
            worker_release.set()
            assert await asyncio.wait_for(second, 2) == 2

        asyncio.run(run_cancelled_worker())
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  workspace cancellation holds lock through worker: ok")


def test_release_author_and_missing_tree(agents, wstools):
    sb = _FilesSandbox()
    try:
        alpha = agents["alpha"]
        beta = agents["beta"]
        pid = db.create_proposal(
            alpha["token"], "Author Release Shop", "body", collaborative=True
        )["post_id"]
        db.create_todo_list(alpha["token"], pid, "Work", [])
        db.join_proposal(beta["token"], pid)
        claimed = wstools.claim_workspace(beta["token"], pid, "dev")
        dest = str(claimed["tree"]["path"])
        released = wstools.release_workspace(alpha["token"], pid, "dev")
        assert released["status"] == "released", released
        assert not os.path.isdir(dest), dest

        pid2 = db.create_proposal(alpha["token"], "Missing Tree Shop", "body")[
            "post_id"
        ]
        claimed2 = wstools.claim_workspace(alpha["token"], pid2, "dev")
        dest2 = str(claimed2["tree"]["path"])
        shutil.rmtree(dest2)
        released2 = wstools.release_workspace(alpha["token"], pid2, "dev")
        assert released2["status"] == "released", released2
    finally:
        sb.close()
    print("  author release + missing-tree release: ok")


def main():
    from server.tools.repo import _workspace as wstools  # noqa: E402

    agents, _post_id = setup()
    test_write_read_roundtrip(agents, wstools)
    test_list_status_diff(agents, wstools)
    test_read_tools_wait_for_tree_lock(agents, wstools)
    test_path_guards(agents, wstools)
    test_delete_semantics(agents, wstools)
    test_sync_and_clocks_and_budget(agents, wstools)
    test_workspace_edits(agents, wstools)
    test_read_at_ref(agents, wstools)
    test_workspace_reset(agents, wstools)
    test_workspace_serialized_async(agents, wstools)
    test_workspace_serialized_cancellation(agents, wstools)
    test_workspace_serialized_cancelled_worker_keeps_lock(agents, wstools)
    test_release_author_and_missing_tree(agents, wstools)
    test_owner_isolation(agents, wstools)
    print("test_workspace_files: all scenarios passed")


if __name__ == "__main__":
    main()
