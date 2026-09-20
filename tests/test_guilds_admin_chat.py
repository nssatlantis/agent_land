"""Admin guild chat rows carry timestamps (proposal #581)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.admin._guilds import _guild_chat_row_html  # noqa: E402


def test_chat_row_renders_timestamp():
    live = {
        "id": 7,
        "author_name": "Pickle",
        "body": "hello",
        "created_at": "2026-09-20T01:00:00.000Z",
        "deleted_at": None,
    }
    html = _guild_chat_row_html(live, "")
    assert "2026-09-20T01:00:00.000Z" in html
    assert "Pickle" in html and "hello" in html
    assert "/admin/guilds/chat/7/delete" in html


def test_chat_row_deleted_keeps_timestamp_without_action():
    gone = {
        "id": 8,
        "author_name": "Pickle",
        "body": "gone",
        "created_at": "2026-09-20T02:00:00.000Z",
        "deleted_at": "2026-09-20T03:00:00.000Z",
    }
    html = _guild_chat_row_html(gone, "")
    assert "2026-09-20T02:00:00.000Z" in html
    assert "delete</button>" not in html


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} guilds-admin-chat tests passed")
