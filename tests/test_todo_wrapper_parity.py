"""Wrapper signature-parity pin for tick_todo_item (#B89).

PR #1379's batch rewrite dropped ``progress`` from the MCP wrapper while
the db layer kept it - README and AGENTS still document the parameter.
Four reviewers verified batch-vs-single semantics and none diffed the
parameter lists (diff the wrapper's SIGNATURE, not just its behavior).
Three ratchets, all driven through ``server.tools.collab``:

1. parity - every non-token parameter of ``db.tick_todo_item`` and
   ``db.tick_todo_items`` must exist on the wrapper; a vanished argument
   fails here instead of shipping.
2. forward - a single tick through the wrapper with ``progress=`` must
   persist end-to-end (through ``_logged``).
3. refuse - ``ticks`` with ``progress`` refuses before any write, and an
   over-cap note still carries the db's own length error.

run_all spawns each test file as a bare subprocess, so the repo
convention applies: ``main()`` plus an ``if __name__ == "__main__"``
driver - without it this pin exits 0 having asserted nothing (#320).
"""

import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_todo_wrapper_parity_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.tools.collab as collab_tools  # noqa: E402
from tests._setup import db, setup  # noqa: E402


def _expect_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - helper surfaces the message
        return str(exc)
    raise AssertionError(f"{fn.__name__} did not raise")


def main():
    agents, _ = setup()
    author = agents["alpha"]

    # -- 1. signature parity - the #B89 ratchet ----------------------------
    fn = collab_tools.tick_todo_item
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    wrap_params = set(inspect.signature(fn).parameters)
    for callee in (db.tick_todo_item, db.tick_todo_items):
        db_params = set(inspect.signature(callee).parameters)
        missing = sorted(db_params - wrap_params - {"token"})
        assert not missing, (
            f"{callee.__name__}: wrapper lost {missing} - "
            "diff the wrapper's SIGNATURE, not just its behavior (#B89)"
        )

    # -- 2. forward end-to-end through the wrapper -------------------------
    proposal = db.create_proposal(author["token"], "Wrapper parity", "Body.")
    pid = proposal["post_id"]
    db.set_todos_for_post(
        author["token"],
        pid,
        [{"title": "Work", "items": [{"text": "ship A"}, {"text": "ship B"}]}],
    )
    items = [it["id"] for it in db.get_todos_for_post(pid)[0]["items"]]
    note = "wrapper forwards me"
    out = collab_tools.tick_todo_item(
        token=author["token"],
        post_id=pid,
        item_id=items[0],
        done=True,
        progress=note,
    )
    assert out["progress"] == note, out
    board = db.get_todos_for_post(pid)[0]["items"]
    assert board[0]["progress"] == note, board

    # -- 3. over-cap note still carries the db's length error ---------------
    err = _expect_error(
        collab_tools.tick_todo_item,
        token=author["token"],
        post_id=pid,
        item_id=items[1],
        done=True,
        progress="x" * 225,
    )
    assert "224" in err, err

    # -- 4. ticks + progress refuses before any write ----------------------
    err = _expect_error(
        collab_tools.tick_todo_item,
        token=author["token"],
        post_id=pid,
        item_id=None,
        done=True,
        ticks=[{"item_id": items[1]}],
        progress="not with ticks",
    )
    assert "progress applies to a single tick only" in err, err
    after = db.get_todos_for_post(pid)[0]["items"]
    assert after[1]["done"] is False, after  # refusal flipped nothing.

    print("test_todo_wrapper_parity: all ok")


if __name__ == "__main__":
    main()
