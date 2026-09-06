"""Pin the #315 unified-viewer-cache migration for the /analytics and
/agents/{id}/activity panels: the shared _cached helper (pinned in
tests/test_viewer_cache.py) really serves these pages - cold, warm and
re-rendered bytes are identical, and the panel keys live in the shared
cache. DB-backed, so the underlying queries genuinely run."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_viewer_panels_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, fresh_db, init, setup  # noqa: E402
from viewer import _activity, _analytics, _cache  # noqa: E402


def test_analytics_served_from_shared_cache():
    _cache._reset_for_tests()
    setup()
    first = _analytics._analytics_html()
    assert ("analytics",) in _cache._CACHE
    assert "Society analytics" in first
    assert "No citizen data." not in first  # setup() created real agents
    assert _analytics._analytics_html() == first  # warm serve, no re-fetch
    _cache._reset_for_tests()
    assert _analytics._analytics_html() == first  # fresh render is byte-identical
    _cache._reset_for_tests()
    assert _analytics._fetch_analytics_html() == first  # direct == cached


def test_activity_served_from_shared_cache():
    _cache._reset_for_tests()
    agents, post_id = setup()
    alpha_id = agents["alpha"]["agent_id"]
    a = db.agent_card(alpha_id)
    any_tab = _activity._activity_body(a, "all", 1)
    assert ("activity", alpha_id, "all", 1) in _cache._CACHE
    assert "Activity" in any_tab
    assert _activity._activity_body(a, "all", 1) == any_tab
    posts_tab = _activity._activity_body(a, "posts", 1)
    assert ("activity", alpha_id, "posts", 1) in _cache._CACHE
    assert str(post_id) in posts_tab  # post_created event row renders #P{post_id}
    assert _activity._activity_body(a, "posts", 1) == posts_tab


if __name__ == "__main__":
    init()
    test_analytics_served_from_shared_cache()
    fresh_db()  # isolate the second test's dataset (B2 pattern)
    test_activity_served_from_shared_cache()
    print("all tests passed")
