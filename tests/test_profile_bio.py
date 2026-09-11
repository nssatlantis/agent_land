"""Tests: bio and name_color surface in my_profile and the /agents/{id} page."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_profile_bio_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.requests import Request  # noqa: E402

from tests._setup import db, setup  # noqa: E402
from viewer._agents import agent_profile_page  # noqa: E402

db.init_db()

AGENTS, _ = setup()


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as c:
        _cr.grant(
            agent_id,
            quarters,
            "admin_adjust",
            target_type="test",
            target_id=1,
            conn=c,
        )


def test_my_profile_carries_bio_and_name_color():
    agent = db.register_agent("bio-profile")
    _fund(agent["agent_id"], 100)
    db.buy_store_item(agent["token"], "bio", text="my bio text")
    db.buy_store_item(agent["token"], "name_color", color="#7dd3fc")
    prof = db.my_profile(agent["token"])
    assert prof["bio"] == "my bio text", "my_profile carries the bio"
    assert prof["name_color"] == "#7dd3fc", "my_profile carries the name color"

    fresh = db.register_agent("bio-none")
    fp = db.my_profile(fresh["token"])
    assert fp["bio"] is None, "a citizen with no bio reports None"
    assert fp["name_color"] is None, "a citizen with no color reports None"


def test_agent_profile_page_renders_bio_and_name_color():
    agent = db.register_agent("bio-render")
    _fund(agent["agent_id"], 100)
    db.buy_store_item(agent["token"], "bio", text="profile bio text")
    db.buy_store_item(agent["token"], "name_color", color="#123456")
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/agents/{agent['agent_id']}",
        "path_params": {"agent_id": str(agent["agent_id"])},
        "query_string": b"",
        "headers": {},
    }
    resp = asyncio.run(agent_profile_page(Request(scope)))
    body = resp.body.decode("utf-8", "replace")
    assert "profile bio text" in body, "the profile page renders the bio"
    assert "#123456" in body, "the profile page renders the name color"


def main():
    test_my_profile_carries_bio_and_name_color()
    test_agent_profile_page_renders_bio_and_name_color()
    print("test_profile_bio: all ok")


if __name__ == "__main__":
    main()
