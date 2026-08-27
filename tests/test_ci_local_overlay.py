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

from tests._setup import db, setup, init  # noqa: E402
import server.ci_runner as ci_runner  # noqa: E402
from github._core import RepoError  # noqa: E402


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
            msg = _expect_error(ci_runner._apply_local_changes, tree, [{"path": bad, "content": "x"}])
            assert "invalid path" in msg.lower() or "cannot be empty" in msg.lower() or "must be relative" in msg.lower(), f"bad {bad!r} not rejected: {msg}"
            # Ensure no file was created outside tree (or inside with traversal name)
            if bad:
                # Empty path maps to the tree dir itself, which exists — skip that check
                assert not (Path(tree) / bad).exists() or Path(tree) / bad == Path(tree), f"traversal file unexpectedly exists for {bad!r}"
            # Also ensure no file escaped to parent
            assert not Path("/tmp/evil").exists(), "escaped to /tmp"
        # Leading slash
        msg = _expect_error(ci_runner._apply_local_changes, tree, [{"path": "/absolute/path.py", "content": "x"}])
        assert "relative" in msg.lower() or "invalid" in msg.lower(), f"absolute not rejected: {msg}"

        # Valid path should succeed (no exception, file appears)
        ci_runner._apply_local_changes(tree, [{"path": "good.py", "content": "print('hi')\n"}])
        assert (Path(tree) / "good.py").exists(), "valid good.py not written"
        # Valid nested
        ci_runner._apply_local_changes(tree, [{"path": "a/b/c.py", "content": "x"}])
        assert (Path(tree) / "a/b/c.py").exists(), "valid nested not written"

        print("  _apply_local_changes gate: ok")

        # 2) Patch mode — same gate must apply before isfile check
        # Create a file to patch
        Path(tree, "patchme.py").write_text("hello world\n", encoding="utf-8")
        msg = _expect_error(ci_runner._apply_local_changes, tree, [{"path": "../../evil2", "edits": [{"find": "hello", "replace": "hi"}]}])
        assert "invalid path" in msg.lower() or "relative" in msg.lower(), f"patch bad not rejected: {msg}"

        # Valid patch should succeed
        ci_runner._apply_local_changes(tree, [{"path": "patchme.py", "edits": [{"find": "hello", "replace": "hi"}]}])
        assert Path(tree, "patchme.py").read_text(encoding="utf-8") == "hi world\n", "valid patch not applied"
        print("  _apply_local_changes patch gate: ok")
    finally:
        import shutil
        shutil.rmtree(tree, ignore_errors=True)

    # 3) Wiring via repo_ci_run(files=...) — should fail closed before any
    # host write or runner slot is taken (belt-and-suspenders in
    # server/tools/repo.py after _changes_for_repo_propose).
    import server.tools.repo as repo_tool  # noqa: E402
    agents, _ = setup()
    # Pick an alpha-like agent (setup creates alpha/beta)
    token = None
    for ag in agents.values():
        token = ag["token"]
        break
    assert token, "no agent token from setup"
    for bad in ["../../evil", "/etc/evil", "a/../../b"]:
        msg = _expect_error(repo_tool.repo_ci_run, token, "tests", None, [{"path": bad, "content": "x"}])
        assert "invalid path" in msg.lower() or "relative" in msg.lower(), f"repo_ci_run bad {bad!r} not rejected: {msg}"
        # Ensure no file escaped to host
        assert not Path("/tmp/evil").exists(), "escaped to /tmp via repo_ci_run"
    print("  repo_ci_run(files=) gate: ok")

    # Leading slash via repo_ci_run
    msg = _expect_error(repo_tool.repo_ci_run, token, "tests", None, [{"path": "/absolute/path.py", "content": "x"}])
    assert "relative" in msg.lower() or "invalid" in msg.lower(), f"repo_ci_run absolute not rejected: {msg}"
    print("  repo_ci_run(absolute) gate: ok")

    print("\ntest_ci_local_overlay: all assertions passed")


if __name__ == "__main__":
    main()
