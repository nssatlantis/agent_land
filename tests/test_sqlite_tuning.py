"""Tests for the SQLite observability & maintenance tuning set: slow-block
logging, event_total memoization, the WAL checkpoint guard, and the
sqlite_version surfacing that grounds engine upgrades."""
import os
import sqlite3
import sys
import tempfile
import unittest.mock
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_sqlite_tuning_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, config  # noqa: E402
import events  # noqa: E402
import logutil  # noqa: E402


def _set_env(key: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


def test_knob_defaults():
    assert config.SQLITE_SLOW_BLOCK_MS == 100
    assert config.EVENT_TOTAL_CACHE_SECONDS == 5
    assert config.WAL_CHECKPOINT_BYTES == 8 * 1024 * 1024


def test_slow_block_threshold_gating():
    from db import _core

    old = os.environ.get("FORUM_SQLITE_SLOW_BLOCK_MS")
    try:
        os.environ["FORUM_SQLITE_SLOW_BLOCK_MS"] = "0"
        with unittest.mock.patch.object(logutil, "log") as spy:
            _core._log_slow_block_if_needed(99_999.0, False)
            assert spy.call_count == 0, "0 must disable the logger entirely"
        os.environ["FORUM_SQLITE_SLOW_BLOCK_MS"] = "50"
        with unittest.mock.patch.object(logutil, "log") as spy:
            _core._log_slow_block_if_needed(40.0, True)
            assert spy.call_count == 0, "below threshold stays silent"
            _core._log_slow_block_if_needed(60.5, True)
            assert spy.call_count == 1
            kwargs = spy.call_args.kwargs
            assert kwargs["ms"] == 60.5 and kwargs["immediate"] is True
    finally:
        _set_env("FORUM_SQLITE_SLOW_BLOCK_MS", old)


def test_event_total_cache_staleness_window():
    db.init_db()
    old = os.environ.get("FORUM_EVENT_TOTAL_CACHE_SECONDS")
    try:
        os.environ["FORUM_EVENT_TOTAL_CACHE_SECONDS"] = "60"
        events._total_cache.clear()
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO events (kind, created_at) VALUES ('test_kind', ?)",
                (db._now_iso(),),
            )
        first = events.event_total()
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO events (kind, created_at) VALUES ('test_kind', ?)",
                (db._now_iso(),),
            )
        cached = events.event_total()
        assert cached == first, "within the TTL the count must be memoized"

        os.environ["FORUM_EVENT_TOTAL_CACHE_SECONDS"] = "0"
        live = events.event_total()
        assert live == first + 1, "0 disables memoization - recomputed live"
    finally:
        _set_env("FORUM_EVENT_TOTAL_CACHE_SECONDS", old)
        events._total_cache.clear()


def test_wal_guard_runs_and_degrades_quietly():
    from server import poller

    old = os.environ.get("FORUM_WAL_CHECKPOINT_BYTES")
    try:
        db.init_db()
        wal_path = str(db.DB_PATH) + "-wal"
        os.environ["FORUM_WAL_CHECKPOINT_BYTES"] = "10"
        Path(wal_path).write_bytes(b"x" * 64)
        poller._maybe_truncate_wal()  # must not raise on a healthy db
        os.environ["FORUM_WAL_CHECKPOINT_BYTES"] = "0"
        poller._maybe_truncate_wal()  # disabled: early return even without a wal
        Path(wal_path).unlink(missing_ok=True)
        poller._maybe_truncate_wal()  # missing file degrades quietly
    finally:
        _set_env("FORUM_WAL_CHECKPOINT_BYTES", old)


def test_storage_stats_names_the_engine():
    stats = db.storage_stats()
    assert stats["sqlite_version"] == sqlite3.sqlite_version
    assert isinstance(stats["wal_bytes"], (int, type(None))), (
        "wal_bytes must be a byte count or None when no -wal file exists"
    )


def test_slow_block_counters_and_process_info():
    import platform

    from db import _core

    db.init_db()
    old = os.environ.get("FORUM_SQLITE_SLOW_BLOCK_MS")
    try:
        before = _core.slow_block_stats()
        os.environ["FORUM_SQLITE_SLOW_BLOCK_MS"] = "1"
        with unittest.mock.patch.object(logutil, "log"):
            _core._log_slow_block_if_needed(5.0, True)
        after = _core.slow_block_stats()
        assert after["count"] == before["count"] + 1
        assert after["last"]["ms"] == 5.0 and after["last"]["immediate"] is True

        info = db.process_info()
        assert info["python_version"] == platform.python_version()
        assert info["pid"] == os.getpid()
        assert info["uptime_seconds"] >= 0
        # init_db() ran above - the planner-stats stamp must be set.
        assert info["stats_refreshed_at"], "init_db must stamp the refresh time"
        assert info["count"] == after["count"]
    finally:
        _set_env("FORUM_SQLITE_SLOW_BLOCK_MS", old)


def test_effective_configuration_panel_starts_closed():
    from viewer import _utils

    html = _utils._collapsible("X", "<p>y</p>", "probe")
    assert 'class="panel" open' in html, "default panels stay open"
    html = _utils._collapsible("X", "<p>y</p>", "probe", open=False)
    assert 'class="panel"' in html and " open" not in html.split(">", 1)[0], (
        "open=False must render a details element without the open attribute"
    )


def main():
    test_knob_defaults()
    test_slow_block_threshold_gating()
    test_event_total_cache_staleness_window()
    test_wal_guard_runs_and_degrades_quietly()
    test_storage_stats_names_the_engine()
    test_slow_block_counters_and_process_info()
    test_effective_configuration_panel_starts_closed()
    print("test_sqlite_tuning: all ok")


if __name__ == "__main__":
    main()
