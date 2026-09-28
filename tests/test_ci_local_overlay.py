"""Regression test for the local-rehearsal overlay traversal (PR #445).

_validates that _apply_local_changes and the repo_ci_run wiring correctly
gate host-side writes via github._core._validate_path, consistent with the
#431/#437/#440 file-gutted-on-push ratchet discipline. Cheap insurance:
if the gate is ever removed, this test fails before any host write.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_local_overlay_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.ci_runner as ci_runner  # noqa: E402
from github._core import RepoError  # noqa: E402
from tests._setup import db, init, setup  # noqa: E402


def _expect_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except (RepoError, db.ForumError) as exc:
        return str(exc)
    except Exception as exc:  # pragma: no cover
        return f"wrong exception {type(exc).__name__}: {exc}"
    raise AssertionError("expected RepoError/ForumError but call succeeded")


def main():
    # Use the real init to get a clean DB for any run_checks wiring test.
    init()

    # 1) _apply_local_changes directly — host write must be gated before open().
    tree = tempfile.mkdtemp(prefix="agentland_overlay_gate_")
    try:
        for bad in ["../../evil", "/etc/evil", "a/../../b", "../evil", "a//b", ""]:
            msg = _expect_error(
                ci_runner._apply_local_changes, tree, [{"path": bad, "content": "x"}]
            )
            assert (
                "invalid path" in msg.lower()
                or "cannot be empty" in msg.lower()
                or "must be relative" in msg.lower()
            ), f"bad {bad!r} not rejected: {msg}"
            # Ensure no file was created outside tree (or inside with traversal name)
            if bad:
                # Empty path maps to the tree dir itself, which exists — skip that check
                assert not (Path(tree) / bad).exists() or Path(tree) / bad == Path(
                    tree
                ), f"traversal file unexpectedly exists for {bad!r}"
            # Also ensure no file escaped to parent
            assert not Path("/tmp/evil").exists(), "escaped to /tmp"
        # Leading slash
        msg = _expect_error(
            ci_runner._apply_local_changes,
            tree,
            [{"path": "/absolute/path.py", "content": "x"}],
        )
        assert "relative" in msg.lower() or "invalid" in msg.lower(), (
            f"absolute not rejected: {msg}"
        )

        # Valid path should succeed (no exception, file appears)
        ci_runner._apply_local_changes(
            tree, [{"path": "good.py", "content": "print('hi')\n"}]
        )
        assert (Path(tree) / "good.py").exists(), "valid good.py not written"
        # Valid nested
        ci_runner._apply_local_changes(tree, [{"path": "a/b/c.py", "content": "x"}])
        assert (Path(tree) / "a/b/c.py").exists(), "valid nested not written"

        print("  _apply_local_changes gate: ok")

        # 2) Patch mode — same gate must apply before isfile check
        # Create a file to patch
        Path(tree, "patchme.py").write_text("hello world\n", encoding="utf-8")
        msg = _expect_error(
            ci_runner._apply_local_changes,
            tree,
            [{"path": "../../evil2", "edits": [{"find": "hello", "replace": "hi"}]}],
        )
        assert "invalid path" in msg.lower() or "relative" in msg.lower(), (
            f"patch bad not rejected: {msg}"
        )

        # Valid patch should succeed
        ci_runner._apply_local_changes(
            tree,
            [{"path": "patchme.py", "edits": [{"find": "hello", "replace": "hi"}]}],
        )
        assert Path(tree, "patchme.py").read_text(encoding="utf-8") == "hi world\n", (
            "valid patch not applied"
        )

        # Byte-faithfulness: patch mode must preserve a CRLF file's line
        # endings and write replacements verbatim - the rehearsal tests
        # exactly the bytes the open/PR path would upload (universal-newline
        # reads + newline="\n" writes left MIXED endings that
        # ruff format --check flagged).
        crlf = Path(tree, "crlf.py")
        crlf.write_bytes(b"alpha = 1\r\nbeta = 2\r\n")
        ci_runner._apply_local_changes(
            tree,
            [
                {
                    "path": "crlf.py",
                    "edits": [{"find": "beta = 2", "replace": "gamma = 3"}],
                }
            ],
        )
        assert crlf.read_bytes() == b"alpha = 1\r\ngamma = 3\r\n", (
            "patch mode mangled CRLF line endings"
        )
        # A replacement that itself carries \r\n lands verbatim (double
        # newline here - the find covered only the token, so its own CRLF
        # survives too) - the same bytes the open/PR path would upload.
        repl = Path(tree, "repl.py")
        repl.write_bytes(b"beta = 2\r\n")
        ci_runner._apply_local_changes(
            tree,
            [
                {
                    "path": "repl.py",
                    "edits": [{"find": "beta = 2\r\n", "replace": "gamma = 3\r\n"}],
                }
            ],
        )
        assert repl.read_bytes() == b"gamma = 3\r\n", (
            "patch mode rewrote the replacement's CRLF"
        )
        # A \n newline in the replacement is normalized to the file's EOL
        # (CRLF base → CRLF replacement) so mixed endings never land.
        lf_repl = Path(tree, "lf_repl.py")
        lf_repl.write_bytes(b"a = 1\r\nb = 2\r\n")
        ci_runner._apply_local_changes(
            tree,
            [
                {
                    "path": "lf_repl.py",
                    "edits": [{"find": "b = 2\r\n", "replace": "c = 3\n"}],
                }
            ],
        )
        assert lf_repl.read_bytes() == b"a = 1\r\nc = 3\r\n", (
            "patch mode should normalize replacement EOL to base EOL"
        )
        # Ledger detail carries the run's output for post-hoc diagnosis -
        # but only for a RED run: a green run's transcript is never read
        # again, so it drops output_tail while verdict facts still fold.
        got = ci_runner._ci_detail_with_output(
            {"checks": "tests"},
            {
                "ok": True,
                "timed_out": False,
                "exit_code": 0,
                "output_tail": "tail...",
                "output_truncated": True,
                "summary": {"static": {"result": "pass"}},
            },
        )
        assert "output_tail" not in got, "green runs drop their transcript"
        assert "output_truncated" not in got
        assert got["summary"]["static"]["result"] == "pass"
        assert got.get("failed_files") is None
        got_red = ci_runner._ci_detail_with_output(
            {"checks": "tests"},
            {
                "ok": False,
                "timed_out": False,
                "exit_code": 1,
                "output_tail": "tail...",
                "output_truncated": True,
                "summary": {"static": {"result": "fail"}},
                "failed_files": ["viewer/_utils.py"],
            },
        )
        assert got_red["output_tail"] == "tail..."
        assert got_red["output_truncated"] is True
        assert got_red["failed_files"] == ["viewer/_utils.py"]
        print("  _apply_local_changes byte-faithfulness: ok")
    finally:
        import shutil

        shutil.rmtree(tree, ignore_errors=True)

    # 3) Wiring via repo_ci_run(files=...) — should fail closed before any
    # host write or runner slot is taken (belt-and-suspenders in
    # server/tools/repo/_govern.py after _changes_for_repo_propose).
    import server.tools.repo as repo_tool  # noqa: E402

    agents, _ = setup()
    # Pick an alpha-like agent (setup creates alpha/beta)
    token = None
    for ag in agents.values():
        token = ag["token"]
        break
    assert token, "no agent token from setup"
    for bad in ["../../evil", "/etc/evil", "a/../../b"]:
        msg = _expect_error(
            repo_tool.repo_ci_run, token, "tests", None, [{"path": bad, "content": "x"}]
        )
        assert "invalid path" in msg.lower() or "relative" in msg.lower(), (
            f"repo_ci_run bad {bad!r} not rejected: {msg}"
        )
        # Ensure no file escaped to host
        assert not Path("/tmp/evil").exists(), "escaped to /tmp via repo_ci_run"
    print("  repo_ci_run(files=) gate: ok")

    # Leading slash via repo_ci_run
    msg = _expect_error(
        repo_tool.repo_ci_run,
        token,
        "tests",
        None,
        [{"path": "/absolute/path.py", "content": "x"}],
    )
    assert "relative" in msg.lower() or "invalid" in msg.lower(), (
        f"repo_ci_run absolute not rejected: {msg}"
    )
    print("  repo_ci_run(absolute) gate: ok")

    # 4) #B128 base_sha guard on content-mode overlay entries. The whole-file
    # guard already existed on the write tools and was already carried in the
    # change dict here - it was simply never read, so a stale copy silently
    # reverted base content while the receipt named the refreshed base.
    import subprocess

    guard_tree = tempfile.mkdtemp(prefix="agentland_overlay_b128_")
    try:
        # A real repo, exactly as _prepare_local_tree's _ensure_clone gives,
        # so `git hash-object` is exercised on the path production takes.
        subprocess.run(
            ["git", "init", "-q", guard_tree], check=True, capture_output=True
        )
        target = os.path.join(guard_tree, "cfg.py")
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write("BASE = 1\n")

        def _git(*args):
            return subprocess.run(
                ["git", "-C", guard_tree, *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        # Commit it. Production reads the BLOB OBJECT (rev-parse HEAD:<path>),
        # so the fixture's "current base" must be a real commit rather than a
        # bare worktree file. A caller's base_sha comes from repo_read_file,
        # which echoes a blob sha - deriving the expected value any other way
        # couples the pin to the very instrument it exists to police.
        _git("add", "cfg.py")
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "b")

        def _blob(rel):
            # rev-parse, NOT hash-object: the caller's base_sha names the blob
            # object, so the expected value must come from the object store.
            # `rel` is REPO-RELATIVE: a rev-parse spec is not a filesystem
            # path, so an absolute one dies with exit 128.
            return _git("rev-parse", "HEAD:" + rel)

        good = _blob("cfg.py")

        # (a) A matching guard proceeds and the write lands.
        ci_runner._apply_local_changes(
            guard_tree,
            [{"path": "cfg.py", "content": "BASE = 2\n", "base_sha": good}],
        )
        assert Path(target).read_text() == "BASE = 2\n", "guarded write did not land"

        # (b) A STALE guard refuses - and the load-bearing half is that
        # NOTHING was written. A guard that raised after the overwrite would
        # still pass (a), while causing the exact revert this fixes.
        before = Path(target).read_bytes()
        msg = _expect_error(
            ci_runner._apply_local_changes,
            guard_tree,
            [{"path": "cfg.py", "content": "REVERTED = 1\n", "base_sha": "0" * 40}],
        )
        assert "stale base" in msg.lower(), f"stale-guard message: {msg}"
        assert Path(target).read_bytes() == before, (
            "stale guard refused AFTER overwriting - the revert still happened"
        )

        # (c) null asserts ABSENCE, so a present file trips it loudly rather
        # than silently overwriting.
        msg = _expect_error(
            ci_runner._apply_local_changes,
            guard_tree,
            [{"path": "cfg.py", "content": "x = 1\n", "base_sha": None}],
        )
        assert "stale base" in msg.lower(), f"absent-assert message: {msg}"

        # (d) Unguarded stays LEGAL - refusing it would break every existing
        # caller, including shipped payloads - but is REPORTED, so a green
        # run can no longer imply a base it did not test.
        assert ci_runner._apply_local_changes(
            guard_tree, [{"path": "cfg.py", "content": "UNGUARDED = 1\n"}]
        ) == ["cfg.py"], "unguarded content entry not reported"

        # (e) edits mode reports nothing: it resolves against the on-disk file
        # and already fails loud on drift, so a guard there is redundant.
        edited = ci_runner._apply_local_changes(
            guard_tree,
            [
                {
                    "path": "cfg.py",
                    "edits": [{"find": "UNGUARDED", "replace": "EDITED"}],
                }
            ],
        )
        assert edited == [], "edits mode must not be reported as unguarded"

        # (f) CRLF worktree vs LF blob - the pin that catches the INSTRUMENT
        # bug rather than the guard's presence. Production must read the blob
        # object (rev-parse HEAD:<path>), never the worktree bytes
        # (hash-object <path>). Under core.autocrlf those disagree
        # permanently, so a worktree read makes the guard false-positive on
        # EVERY entry while looking exactly like real staleness: it disables
        # the feature and still reports a plausible reason. Three asserts -
        # the fixture genuinely diverges, the guard ACCEPTS the correct blob
        # sha despite a CRLF worktree, and it still REJECTS a wrong sha in the
        # same tree (so the arm is not satisfied by a guard that never fires).
        crlf_rel = "crlf.py"
        crlf_abs = os.path.join(guard_tree, crlf_rel)
        with open(crlf_abs, "w", encoding="utf-8", newline="") as fh:
            fh.write("A = 1\nB = 2\n")
        _git("add", crlf_rel)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c")
        lf_blob = _blob(crlf_rel)
        with open(crlf_abs, "w", encoding="utf-8", newline="") as fh:
            fh.write("A = 1\r\nB = 2\r\n")
        worktree_hash = _git("hash-object", crlf_rel)
        assert worktree_hash != lf_blob, (
            "fixture is not discriminating: the CRLF worktree must hash "
            "differently from the stored LF blob for this pin to mean anything"
        )
        ci_runner._apply_local_changes(
            guard_tree,
            [{"path": crlf_rel, "content": "A = 99\nB = 2\n", "base_sha": lf_blob}],
        )
        assert "A = 99" in Path(crlf_abs).read_text(), (
            "guarded write over a CRLF worktree did not land"
        )
        stale_crlf = _expect_error(
            ci_runner._apply_local_changes,
            guard_tree,
            [{"path": crlf_rel, "content": "A = 1\n", "base_sha": "0" * 40}],
        )
        assert "stale base" in stale_crlf.lower(), stale_crlf
    finally:
        import shutil

        shutil.rmtree(guard_tree, ignore_errors=True)
    print("  #B128 base_sha guard: ok")

    print("\ntest_ci_local_overlay: all assertions passed")


if __name__ == "__main__":
    main()
