"""Monolith size guard for viewer/__init__.py (4707).

Verifies the 167768B monolith does not bloat before the real split to
viewer/_routes.py + _render.py (see AGENTS.md, like server.py 455B shim).
"""

import pathlib


def test_viewer_monolith_size():
    p = pathlib.Path("viewer/__init__.py")
    assert p.exists(), "viewer/__init__.py missing"
    size = p.stat().st_size
    # 167768B at filing, allow small growth but catch 10% bloat — 180k ceiling
    assert size < 180000, f"viewer/__init__.py {size} exceeds 180k — split plan 4707"
