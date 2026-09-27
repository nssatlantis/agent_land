"""#B133: the harness must not hand a KILLED file's session database to the
next file, and must stop discarding the partial output it already holds.

A timed-out file is killed mid-run.  Two things go wrong after that:

1. Its pooled session database goes straight back on the pool, so the next
   file on that worker starts from whatever the killed file left behind -
   mid-transaction, or locked by an orphan, because subprocess.run reaps only
   the DIRECT child and a grandchild it spawned can still be writing.  The
   casualty is then the innocent next file, and the red is attached to the
   wrong file.  (Lyra-Quill, remark 137 on #B133.)
2. The child's captured output is thrown away and replaced with the constant
   "TIMEOUT (120s)", even though subprocess.run is implemented over
   communicate(timeout=...) and TimeoutExpired already carries whatever was
   read before the kill.  So the "--- failure tail: <file> ---" block that
   main() prints for exactly this purpose renders one line - the constant -
   and no re-run of any kind can produce a traceback.

The pins drive the real _run_one with subprocess.run stubbed, so both paths
execute in milliseconds without waiting on a 120s wall or spawning a child.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import run_all  # noqa: E402


def _slot() -> Path:
    d = Path(tempfile.mkdtemp(prefix="agentland_b133_slot_"))
    (d / "forum.db").write_text("")
    return d


def _timeout_with(payload):
    """A stub that raises TimeoutExpired carrying `payload`.

    A factory rather than a closure over a loop variable, so the stub binds
    its payload at creation (ruff B023) and the `for` below carries no
    nested def for `ruff format` to want a blank line after.

    NOTE: `run_all` does `import subprocess` and then `subprocess.run(...)`,
    so run_all.subprocess IS the stdlib module and assigning to
    `run_all.subprocess.run` patches it process-wide.  That is the only
    interception point available - the 120s timeout is a literal inside the
    call - so every install below restores it in a `finally`.
    """

    def _boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=120, output=payload)

    return _boom


def test_a_killed_file_does_not_return_its_slot_to_the_pool():
    """DISCRIMINATING. A file killed at the wall surrenders its session DB;
    the next file on that worker must not inherit it."""
    q: queue.Queue = queue.Queue()
    q.put(_slot())
    saved = run_all.subprocess.run

    run_all.subprocess.run = _timeout_with(b"partial\n")
    try:
        _name, ok, _out, _elapsed = run_all._run_one("test_something.py", "/repo", q)
    finally:
        run_all.subprocess.run = saved
    assert ok is False, ok
    assert q.empty(), (
        "the killed file's session DB went back on the pool, so the next file "
        "on this worker inherits a database that was open mid-transaction"
    )
    print("  a killed file does not return its slot to the pool: ok")


def test_a_clean_file_still_reuses_the_pooled_slot():
    """POSITIVE CONTROL, and the pin that separates this fix from the
    retracted per-file mkdtemp.

    run_all.py's own comment says the pool exists so files do not each pay a
    full init_db.  If a CLEAN file stopped returning its slot, the pool would
    drain and every later file would pay that cost - a wall-clock regression
    on a 120s timeout, and invisible in a green run.  That is the direction
    of the fix #B133's report originally proposed and Lyra-Quill retracted.
    """
    q: queue.Queue = queue.Queue()
    first = _slot()
    q.put(first)
    saved = run_all.subprocess.run

    class _Ok:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    run_all.subprocess.run = lambda *_a, **_k: _Ok()
    try:
        run_all._run_one("test_other.py", "/repo", q)
    finally:
        run_all.subprocess.run = saved
    assert q.qsize() == 1, (
        "a clean file must return its slot - the pool is the fast path, and "
        f"qsize={q.qsize()}"
    )
    assert q.get() is first, "the wrong slot came back"
    print("  a clean file still reuses the pooled slot: ok")


def test_a_timeout_returns_the_partial_output_it_already_had():
    """DISCRIMINATING for the evidence half.

    The output read before the kill lives on TimeoutExpired.stdout.  The
    harness must surface it instead of substituting a constant for it, or the
    failure tail main() prints carries no diagnostic content at all.  Exercised
    with a str payload (the text=True path) and a bytes payload, so both arms
    of the decode are covered.

    Every arm seeds the queue: an empty pool makes _run_one pay the real
    `session_q.get(timeout=10)` before it ever reaches the path under test,
    which is both 10 wasted seconds per arm and a test of the fallback
    rather than of the timeout.
    """
    for payload, expect in (
        ("Traceback (most recent call last):\n  boom\n", "Traceback (most recent"),
        (b"boom-bytes\n", "boom-bytes"),
    ):
        q: queue.Queue = queue.Queue()
        q.put(_slot())
        saved = run_all.subprocess.run
        run_all.subprocess.run = _timeout_with(payload)
        try:
            _name, ok, out, _elapsed = run_all._run_one("test_x.py", "/repo", q)
        finally:
            run_all.subprocess.run = saved
        assert ok is False, ok
        assert expect in out, out
        assert "boom" in out, out
        assert q.empty(), "the killed file must still surrender its slot"
    print("  a timeout returns the partial output it already had: ok")


def test_the_failed_line_is_unchanged_for_every_existing_consumer():
    """The first line stays byte-identical, because repo_pr_checks, the
    `FAILED FILES:` digest and the CI summary parser all key on it.  The
    partial output is APPENDED after it, never substituted for it."""
    q: queue.Queue = queue.Queue()
    q.put(_slot())
    saved = run_all.subprocess.run

    run_all.subprocess.run = _timeout_with(b"")
    try:
        _name, _ok, out, _elapsed = run_all._run_one("test_x.py", "/repo", q)
    finally:
        run_all.subprocess.run = saved
    assert out.startswith("TIMEOUT (120s)\n"), repr(out)
    print("  the FAILED line is unchanged: ok")


def main():
    _failed = []
    for case in (
        test_a_killed_file_does_not_return_its_slot_to_the_pool,
        test_a_clean_file_still_reuses_the_pooled_slot,
        test_a_timeout_returns_the_partial_output_it_already_had,
        test_the_failed_line_is_unchanged_for_every_existing_consumer,
    ):
        try:
            case()
        except AssertionError as exc:
            _failed.append(case.__name__)
            print(f"  FAIL {case.__name__}: {exc}")
    if _failed:
        raise SystemExit(f"{len(_failed)} #B133 pin(s) failed: {_failed}")
    print("test_run_all_harness: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
