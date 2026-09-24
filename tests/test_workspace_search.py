"""Live substring search over one claim tree (proposal #671).

Pins workspace_search against a claimed tree on a local bare remote
(no network): live dirty + untracked coverage across every UTF-8 text
file (.txt included, .github included), skips (.git, manifest, symlink,
binary, empty, over-cap), caps (max_results, per-file, trim),
validation, owner isolation and both-clocks touch, plus ref search
(committed tree at branch/tag/sha with dirt invisible, unknown/invalid
refs) over the same file universe (a .txt hit proves no allowlist).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_search_"))
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


_SHARED_BARE = _mk_remote(tempfile.mkdtemp(prefix="agentland_ws_search_remote_"))


def _expect_tool_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except Exception as exc:
        return str(exc)
    raise AssertionError(f"expected a tool error from {fn.__name__}()")


class _SearchSandbox:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agentland_ws_search_test_")
        self._orig = {
            "repo_url": ws._repo_url,
            "claims_root": ws._claims_root,
            "gitops_url": gh._repo_url,
        }
        ws._repo_url = lambda with_token=False: _SHARED_BARE
        ws._claims_root = lambda: os.path.join(self.tmp, "claims")
        gh._repo_url = lambda with_token=False: _SHARED_BARE

    def close(self):
        ws._repo_url = self._orig["repo_url"]
        ws._claims_root = self._orig["claims_root"]
        gh._repo_url = self._orig["gitops_url"]
        shutil.rmtree(self.tmp, ignore_errors=True)


def _claim(agents, wstools, key, title):
    tok = agents[key]["token"]
    prop = db.create_proposal(tok, title, "body")
    pid = prop["post_id"]
    claimed = wstools.claim_workspace(tok, pid, "dev")
    assert claimed["claim"]["status"] == "active", claimed
    return pid, tok


def test_search_live_and_all_text(agents, wstools):
    sb = _SearchSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Search Shop")
        s = wstools.workspace_search
        w = wstools.workspace_write_file
        w(tok, pid, "dev", "notes/todo.txt", "findme one\n")
        aid = agents["alpha"]["agent_id"]
        dest = ws._claim_dir(aid, pid, "dev")
        Path(dest, ".github", "workflows").mkdir(parents=True, exist_ok=True)
        Path(dest, ".github", "workflows", "ci.yml").write_text(
            "findme gh\n", encoding="utf-8"
        )
        got = s(tok, pid, "dev", "findme")
        paths = {m["path"] for m in got["matches"]}
        assert "notes/todo.txt" in paths, got
        assert ".github/workflows/ci.yml" in paths, got
        assert got["query"] == "findme", got
        assert got["proposal_id"] == pid, got
        assert got["name"] == "dev", got
        mod = next(m for m in got["matches"] if m["path"] == "notes/todo.txt")
        assert mod["matches"][0]["line_number"] == 1, mod
        assert "findme" in mod["matches"][0]["text"], mod
        upper = s(tok, pid, "dev", "FINDME")
        assert {m["path"] for m in upper["matches"]} == paths, upper
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  live dirty + all-text + .github: ok")


def test_search_skips(agents, wstools):
    sb = _SearchSandbox()
    try:
        pid, tok = _claim(agents, wstools, "beta", "Skip Shop")
        s = wstools.workspace_search
        w = wstools.workspace_write_file
        marker = "skipneedle"
        w(tok, pid, "dev", "live.txt", f"holds {marker}\n")
        aid = agents["beta"]["agent_id"]
        dest = ws._claim_dir(aid, pid, "dev")
        Path(dest, ".git", "marker.txt").write_text(f"{marker}\n", encoding="utf-8")
        Path(dest, ".workspace.json").write_text(f"{marker}\n", encoding="utf-8")
        Path(dest, "blob.bin").write_bytes(b"\xff\xfe\x00" + marker.encode())
        Path(dest, "empty.txt").write_text("", encoding="utf-8")
        try:
            os.symlink(
                os.path.join(dest, "live.txt"),
                os.path.join(dest, "link.txt"),
            )
        except (OSError, NotImplementedError):
            pass
        try:
            os.mkfifo(os.path.join(dest, "fifo"))
        except (OSError, NotImplementedError, AttributeError):
            pass
        got = s(tok, pid, "dev", marker)
        paths = [m["path"] for m in got["matches"]]
        assert paths == ["live.txt"], got
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  git/manifest/symlink/binary/empty skips: ok")


def test_search_caps_and_validation(agents, wstools):
    sb = _SearchSandbox()
    try:
        pid, tok = _claim(agents, wstools, "gamma", "Cap Shop")
        s = wstools.workspace_search
        w = wstools.workspace_write_file
        assert "non-empty" in _expect_tool_error(s, tok, pid, "dev", "")
        assert "too short" in _expect_tool_error(s, tok, pid, "dev", "x")
        assert "too long" in _expect_tool_error(s, tok, pid, "dev", "x" * 201)
        assert "integer" in _expect_tool_error(
            s, tok, pid, "dev", "ab", max_results="nope"
        )
        w(tok, pid, "dev", "a.txt", "capmark one\n")
        w(tok, pid, "dev", "b.txt", "capmark two\n")
        w(tok, pid, "dev", "c.txt", "capmark three\n")
        bounded = s(tok, pid, "dev", "capmark", max_results=2)
        assert len(bounded["matches"]) <= 2, bounded
        w(tok, pid, "dev", "many.txt", "perfile\n" * 60)
        many = s(tok, pid, "dev", "perfile")
        entry = next(m for m in many["matches"] if m["path"] == "many.txt")
        assert len(entry["matches"]) == config.REPO_SEARCH_MAX_PER_FILE, entry
        w(tok, pid, "dev", "long.txt", "y" * 300 + "\n")
        long = s(tok, pid, "dev", "y" * 10)
        text = next(m for m in long["matches"] if m["path"] == "long.txt")["matches"][
            0
        ]["text"]

        assert len(text) <= 160 and text.endswith("..."), text
        w(tok, pid, "dev", "big.txt", "z" * ((1 << 20) + 1))
        big = s(tok, pid, "dev", "zzzzzzzzzz")
        assert all(m["path"] != "big.txt" for m in big["matches"]), big
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  caps/trim/validation/over-cap: ok")


def test_search_owner_and_clocks(agents, wstools):
    sb = _SearchSandbox()
    try:
        pid, tok = _claim(agents, wstools, "delta", "Clock Shop")
        s = wstools.workspace_search
        beta = agents["alpha"]["token"]
        assert "no active workspace" in _expect_tool_error(s, beta, pid, "dev", "seed")
        aid = agents["delta"]["agent_id"]
        before_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        before_manifest = dict(ws.claim_tree_info(aid, pid, "dev")["manifest"])
        got = s(tok, pid, "dev", "seed")
        assert any(m["path"] == "README.md" for m in got["matches"]), got
        after_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        after_manifest = ws.claim_tree_info(aid, pid, "dev")["manifest"]
        assert after_record >= before_record, (before_record, after_record)
        assert after_manifest["updated_at"] > before_manifest["updated_at"]
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  owner isolation + both clocks: ok")


def test_search_at_ref(agents, wstools):
    sb = _SearchSandbox()
    try:
        pid, tok = _claim(agents, wstools, "alpha", "Ref Search Shop")
        s = wstools.workspace_search
        w = wstools.workspace_write_file
        aid = agents["alpha"]["agent_id"]
        dest = ws._claim_dir(aid, pid, "dev")
        w(tok, pid, "dev", "notes/todo.txt", "frozenmarker one\n")
        w(tok, pid, "dev", "notes/typed.txt", "frozenmarker: typed details\n")
        big_line = "biggrepmarker:" + "x" * ((1 << 20) + 1) + "\n"
        w(tok, pid, "dev", "notes/biggrep.txt", big_line)
        _git(
            "-C", dest, "add", "notes/todo.txt", "notes/typed.txt", "notes/biggrep.txt"
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
            "freeze",
        )
        old = subprocess.run(
            ["git", "-C", dest, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert old, "need the frozen commit sha"
        w(tok, pid, "dev", "notes/todo.txt", "livemarker two\n")
        at_ref = s(tok, pid, "dev", "frozenmarker", ref=old)
        # .txt is outside repo_search's allowlist: these hits prove the
        # ref path keeps the workspace file universe, not the allowlist.
        assert {m["path"] for m in at_ref["matches"]} == {
            "notes/todo.txt",
            "notes/typed.txt",
        }, at_ref
        assert at_ref["ref"] == old, at_ref
        # Colon-bearing match lines must survive the grep parse (rsplit
        # silently dropped them: lineno parsed as text).
        typed = s(tok, pid, "dev", "typed details", ref=old)
        assert [m["path"] for m in typed["matches"]] == ["notes/typed.txt"], typed
        assert typed["matches"][0]["matches"][0]["line_number"] == 1, typed
        # Over-budget committed output refuses instead of buffering whole:
        # this single committed line already exceeds the transfer cap.
        assert "over the" in _expect_tool_error(
            s, tok, pid, "dev", "biggrepmarker", ref=old
        )
        # Search-side origin/ fallback: publish, drop local, resolve remote.
        _git("-C", dest, "branch", "search-pin", old)
        _git("-C", dest, "push", "origin", "search-pin")
        _git("-C", dest, "branch", "-D", "search-pin")
        via_origin = s(tok, pid, "dev", "frozenmarker", ref="search-pin")
        assert {m["path"] for m in via_origin["matches"]} == {
            "notes/todo.txt",
            "notes/typed.txt",
        }, via_origin
        assert via_origin["ref"] == "origin/search-pin", via_origin
        # A poisoned per-file knob must degrade (live-path parity), not 500.
        os.environ["FORUM_REPO_SEARCH_MAX_PER_FILE"] = "garbage"
        try:
            poisoned = s(tok, pid, "dev", "frozenmarker", ref=old)
            assert {m["path"] for m in poisoned["matches"]} == {
                "notes/todo.txt",
                "notes/typed.txt",
            }, poisoned
        finally:
            del os.environ["FORUM_REPO_SEARCH_MAX_PER_FILE"]
        assert s(tok, pid, "dev", "livemarker", ref=old)["matches"] == [], at_ref
        live = s(tok, pid, "dev", "livemarker")
        assert [m["path"] for m in live["matches"]] == ["notes/todo.txt"], live
        assert "ref" not in live, live
        assert "unknown ref" in _expect_tool_error(
            s, tok, pid, "dev", "frozenmarker", ref="no-such-branch-xyz"
        )
        assert "invalid ref" in _expect_tool_error(
            s, tok, pid, "dev", "frozenmarker", ref="bad..ref"
        )
        assert "invalid ref" in _expect_tool_error(
            s, tok, pid, "dev", "frozenmarker", ref="a~1"
        )
        beta = agents["beta"]["token"]
        assert "no active workspace" in _expect_tool_error(
            s, beta, pid, "dev", "frozenmarker", ref=old
        )
        before_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        before_manifest = dict(ws.claim_tree_info(aid, pid, "dev")["manifest"])
        s(tok, pid, "dev", "frozenmarker", ref=old)
        after_record = db.get_workspace(tok, pid, "dev")["updated_at"]
        after_manifest = ws.claim_tree_info(aid, pid, "dev")["manifest"]
        assert after_record >= before_record, (before_record, after_record)
        assert after_manifest["updated_at"] > before_manifest["updated_at"]
        wstools.release_workspace(tok, pid, "dev")
    finally:
        sb.close()
    print("  ref search (committed vs dirty): ok")


def main():
    from server.tools.repo import _workspace as wstools  # noqa: E402

    agents, _post_id = setup()
    test_search_live_and_all_text(agents, wstools)
    test_search_skips(agents, wstools)
    test_search_caps_and_validation(agents, wstools)
    test_search_at_ref(agents, wstools)
    test_search_owner_and_clocks(agents, wstools)
    print("test_workspace_search: all scenarios passed")


if __name__ == "__main__":
    main()
