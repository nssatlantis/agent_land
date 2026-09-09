"""Tests for the db_benchmark baseline blessing path (--reset-baseline).

Isolated-subprocess file (the run_all.py convention): importing
tests.test_benchmark has module-level side effects (mkdtemp + DB env),
so this file points its own throwaway env first, exactly like every
other behavior-test file.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bench_flags_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_benchmark import _build_baseline  # noqa: E402


def test_build_update_merges_and_prunes_ghosts():
    old = {"keep": 1.0, "gone": 2.0}
    new = {"keep": 1.5, "fresh": 3.0}
    merged, ghosts = _build_baseline(old, new, reset=False)
    assert merged["keep"] == 1.5
    assert merged["fresh"] == 3.0
    assert "gone" not in merged
    assert ghosts == ["gone"]
    assert merged["_meta"]["note"]
    assert "reset" not in merged["_meta"]
    # inputs untouched (no aliasing surprises for the caller)
    assert old == {"keep": 1.0, "gone": 2.0}


def test_build_reset_discards_old_file_entirely():
    old = {"stale": 9.0, "older": 8.0}
    new = {"now": 1.0}
    fresh, ghosts = _build_baseline(old, new, reset=True)
    assert set(fresh.keys()) == {"now", "_meta"}
    assert fresh["now"] == 1.0
    assert ghosts == []
    assert fresh["_meta"]["reset"] is True
    assert fresh["_meta"]["date"].endswith("Z")


def main():
    test_build_update_merges_and_prunes_ghosts()
    test_build_reset_discards_old_file_entirely()
    print("test_benchmark_flags: all ok")


if __name__ == "__main__":
    main()
