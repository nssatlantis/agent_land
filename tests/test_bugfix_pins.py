"""Test pins for #B33 (create_post drops use_cooldown_skip) and #B29
(boot_final logutil unbound)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_create_post_tool_forwards_use_cooldown_skip():
    """#B33: the create_post MCP tool must forward use_cooldown_skip to
    db.create_post; it was dropped, so a banked post_skip was never spent."""
    from server.tools import forum

    with mock.patch("db.create_post") as m:
        m.return_value = {"post_id": 1}
        forum.create_post("tok", "t", "b", use_cooldown_skip=True)
    assert m.call_args.kwargs.get("use_cooldown_skip") is True


def test_boot_final_binds_logutil_at_module_level():
    """#B29: logutil must be bound at module level in db._core._boot_final so
    the workflow-reconcile degrade-silently handlers can log even when the
    credits-conditional import branches never run."""
    import db._core._boot_final as boot_final

    assert hasattr(boot_final, "logutil"), (
        "logutil must be importable at module level, not only inside "
        "conditional branches"
    )


def main():
    test_create_post_tool_forwards_use_cooldown_skip()
    test_boot_final_binds_logutil_at_module_level()
    print("test_bugfix_pins: all assertions passed")


if __name__ == "__main__":
    main()
