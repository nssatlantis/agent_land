"""Boot VACUUM: init_db reclaims freelist pages past a threshold, never else."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_boot_vacuum_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db._core._boot_vacuum import maybe_vacuum  # noqa: E402
from tests._setup import config, db, setup  # noqa: E402

_ENV_KEY = "FORUM_SQLITE_VACUUM_THRESHOLD_BYTES"


def _freelist_bytes() -> int:
    with db._conn() as conn:
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return int(freelist) * int(page_size)


def _fragment_db(mb: int = 10) -> None:
    """Leave ~mb megabytes of dead pages: bulk insert, then delete all."""
    blob = b"\0" * (25 * 1024)
    with db._conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS vacuum_probe (data BLOB)")
        conn.execute("DELETE FROM vacuum_probe")
        conn.executemany(
            "INSERT INTO vacuum_probe (data) VALUES (?)",
            [(blob,) for _ in range(mb * 1024 * 1024 // len(blob))],
        )
    with db._conn() as conn:
        conn.execute("DELETE FROM vacuum_probe")


def main():
    agents, _post_id = setup()
    assert config.SQLITE_VACUUM_THRESHOLD_BYTES == 0, (
        "behavior tests must boot with the vacuum disabled"
    )

    # Disabled path: threshold 0 never touches the file (and opens nothing).
    _fragment_db()
    assert _freelist_bytes() >= 1024 * 1024, "fixture must leave real freelist"
    assert maybe_vacuum() == "disabled", "threshold 0 vacuums nothing"
    assert _freelist_bytes() >= 1024 * 1024, "disabled run must not reclaim"

    # Skip path: a threshold above the freelist is a no-op.
    os.environ[_ENV_KEY] = str(10**12)
    try:
        assert maybe_vacuum() == "skipped", "clean file must skip"
        assert _freelist_bytes() >= 1024 * 1024, "skipped run must not reclaim"
    finally:
        os.environ[_ENV_KEY] = "0"

    # Fire path: a tripped threshold reclaims through the real boot wiring.
    # gc first: this engine skips file truncation while any handle to the
    # file is still open, and long-lived processes can hold an unreferenced
    # connection short of a collection cycle. Real boots have no handles
    # yet (the vacuum runs before init_db opens its own); here setup() ran
    # first, so collect to reproduce boot conditions.
    import gc

    gc.collect()
    size_before = os.path.getsize(config.DB_PATH)
    os.environ[_ENV_KEY] = "1"
    try:
        db.init_db()
    finally:
        os.environ[_ENV_KEY] = "0"
    assert _freelist_bytes() == 0, "fired vacuum must empty the freelist"
    assert os.path.getsize(config.DB_PATH) < size_before, (
        "fired vacuum must shrink the file"
    )
    # The rewrite must not eat live data (init_db's own quick_check also ran).
    with db._conn() as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] >= 9, (
            "vacuum must preserve rows"
        )

    print("test_boot_vacuum: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
