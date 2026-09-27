"""Test pins for the /findings union view (proposal #776, leg D3b).

Pins: the route is registered; the page renders an explicit empty state
when no board has an open row; a real open finding renders with its board
and PR scope plus its provenance; and a failed board read degrades to a
visible notice rather than a 500 or a silent blank.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_findings_page_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from tests._setup import setup  # noqa: E402


def main():
    agents, pid = setup()

    from viewer import ROUTES
    from viewer._findings import _findings_body

    # --- the route exists: a page nothing links to is a page nobody finds ---
    paths = [getattr(r, "path", None) for r in ROUTES]
    assert "/findings" in paths, "/findings route is not registered"
    print("  route registered: ok")

    # --- empty state: an empty board is not an invisible board ---
    html = _findings_body()
    assert "Open findings queue" in html, html
    assert "No open findings" in html, html
    print("  empty state: ok")

    # --- a real open row renders with its scope and its provenance ---
    pid = int(pid)
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO review_findings (post_id, pr_number, finder_agent_id,"
            " category, class, check_text, flip_path, auto_flip, state,"
            " created_at)"
            " VALUES (?, 4242, ?, 'bug', 'wire-shape', 'a check', 'a flip', 1,"
            " 'open', '2026-09-27T00:00:00.000Z')",
            (pid, int(agents["beta"]["agent_id"])),
        )
        fid = int(cur.lastrowid or 0)
    html = _findings_body()
    assert f"finding #{fid}" in html, html
    assert "#4242" in html, html
    assert f"/posts/{pid}" in html, html
    assert f"filed by agent {int(agents['beta']['agent_id'])}" in html, html
    assert "No open findings" not in html, html
    print("  open row + provenance: ok")

    # --- a failed read is visible, not a 500 and not a silent blank ---
    real_conn = db._conn
    try:

        def _boom(*_a, **_k):
            raise RuntimeError("board unavailable")

        db._conn = _boom  # type: ignore[assignment]
        html = _findings_body()
    finally:
        db._conn = real_conn  # type: ignore[assignment]
    assert "could not be read" in html, html
    print("  degraded read: ok")

    print("test_findings_page: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
