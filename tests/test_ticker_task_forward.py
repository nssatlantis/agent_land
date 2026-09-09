"""The facade must forward the rebound ticker task live, not freeze None.

_ensure/_cancel_ticker REBIND _TICKER_TASK (global), so a static
from-import in the package facade would pin None forever while the leaf
holds the live task - defeating the shutdown-await in server/_app and the
/admin/ci ticker panel. Regression test for the repo.py split: the facade
(and a fresh from-import, the shape both readers use) must track the
leaf on ensure AND on cancel.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ticker_task_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.tools.repo as repo_pkg  # noqa: E402
import server.tools.repo._ticker as ticker_mod  # noqa: E402


async def _lifecycle():
    assert ticker_mod._TICKER_TASK is None, "ticker starts idle"
    assert repo_pkg._TICKER_TASK is None, "facade starts idle"
    repo_pkg._ensure_ticker()
    await asyncio.sleep(0)
    assert ticker_mod._TICKER_TASK is not None, "leaf holds the task"
    assert repo_pkg._TICKER_TASK is ticker_mod._TICKER_TASK, (
        "facade must forward the live task, not a frozen None"
    )
    from server.tools.repo import _TICKER_TASK as fresh_read

    assert fresh_read is ticker_mod._TICKER_TASK, (
        "a fresh from-import (the _app/admin shape) must see the live task"
    )
    task = ticker_mod._TICKER_TASK
    repo_pkg._cancel_ticker()
    assert ticker_mod._TICKER_TASK is None, "cancel clears the leaf"
    assert repo_pkg._TICKER_TASK is None, "facade tracks the cancel too"
    # _cancel_ticker cancels but cannot await (sync function) - await the
    # corpse here so no "Task was destroyed but it is pending" escapes.
    try:
        await task
    except asyncio.CancelledError:
        pass
    print("  ticker task forwarding: ok")


def main():
    asyncio.run(_lifecycle())
    print("test_ticker_task_forward: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
