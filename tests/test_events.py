"""Test the events ledger: query_events, event_total, and the list_events MCP tool."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_events_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402

import events  # noqa: E402


def main():
    agents, post_id = setup()

    # ---- basic: a post creates an event ---------------------------------
    evts = events.query_events(kind="post_created", target_type="post",
                               target_id=post_id)
    assert len(evts) == 1, f"expected 1 post_created event, got {len(evts)}"
    assert evts[0]["actor_agent_id"] == agents["alpha"]["agent_id"]
    assert evts[0]["detail"]["title"] == "Rules proposal"
    assert evts[0]["target_type"] == "post"
    assert evts[0]["target_id"] == post_id
    print("  post_created event OK")

    # ---- total matches --------------------------------------------------
    total = events.event_total(kind="post_created", target_type="post",
                               target_id=post_id)
    assert total == 1, f"expected total 1, got {total}"
    print("  event_total OK")

    # ---- vote events ----------------------------------------------------
    db.vote(agents["beta"]["token"], "post", post_id, 1)
    db.vote(agents["gamma"]["token"], "post", post_id, -1)
    vote_evts = events.query_events(kind="vote_cast", target_type="post",
                                    target_id=post_id)
    assert len(vote_evts) == 2, f"expected 2 vote_cast events, got {len(vote_evts)}"
    actors = {e["actor_agent_id"] for e in vote_evts}
    assert agents["beta"]["agent_id"] in actors
    assert agents["gamma"]["agent_id"] in actors
    print("  vote_cast events OK")

    # ---- filter by actor ------------------------------------------------
    beta_evts = events.query_events(agent_id=agents["beta"]["agent_id"])
    assert any(e["kind"] == "vote_cast" for e in beta_evts)
    assert all(e["actor_agent_id"] == agents["beta"]["agent_id"] for e in beta_evts)
    print("  agent_id filter OK")

    # ---- filter by since ------------------------------------------------
    all_evts = events.query_events()
    assert len(all_evts) >= 3  # at least post_created + 2 votes
    oldest = all_evts[-1]["created_at"]
    recent = events.query_events(since=oldest)
    assert len(recent) >= 1
    print("  since filter OK")

    # ---- pagination: limit + offset -------------------------------------
    page1 = events.query_events(limit=2, offset=0)
    page2 = events.query_events(limit=2, offset=2)
    assert len(page1) == 2
    assert page1[0]["id"] != page2[0]["id"], "pages should differ"
    total_all = events.event_total()
    assert total_all >= 3
    print("  pagination OK")

    # ---- detail parsing -------------------------------------------------
    # agent_registered always carries a detail dict
    reg_evts = events.query_events(kind="agent_registered")
    assert len(reg_evts) >= 1
    assert reg_evts[0]["detail"] is not None
    assert "model" in reg_evts[0]["detail"]
    print("  agent_registered detail OK")

    # ---- filter by target_type only (no target_id) ----------------------
    post_evts = events.query_events(target_type="post")
    assert len(post_evts) >= 1
    assert all(e["target_type"] == "post" for e in post_evts)
    print("  target_type filter OK")

    # ---- filter by kind only --------------------------------------------
    post_kinds = events.query_events(kind="post_created")
    assert all(e["kind"] == "post_created" for e in post_kinds)
    print("  kind-only filter OK")

    # ---- no filter: returns everything ----------------------------------
    all_events = events.query_events()
    total_all = events.event_total()
    assert len(all_events) <= 50  # default limit
    assert total_all >= len(all_events)
    print("  unfiltered query OK")

    # ---- list_events handler shape: verify {events, total} dict the handler returns ----
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_server_main",
        str(Path(__file__).resolve().parent.parent / "server" / "__init__.py"),
    )
    _server_main = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_server_main)
    result = _server_main.list_events()
    assert "events" in result and "total" in result
    assert isinstance(result["events"], list)
    assert isinstance(result["total"], int)
    assert result["total"] >= 3
    assert len(result["events"]) <= 50
    print("  handler shape OK")

    # ---- list_events handler with filters -----------------------------------
    filtered = _server_main.list_events(
        kind="post_created", target_type="post", target_id=post_id, limit=200,
    )
    assert len(filtered["events"]) == 1
    assert filtered["total"] == 1
    print("  handler filtered OK")

    # ---- invalid kind is rejected by log_event, not by query ------------
    # querying a non-existent kind should return empty, not error
    empty = events.query_events(kind="nonexistent_kind_xyz")
    assert empty == [], "nonexistent kind should return empty list"
    print("  nonexistent kind returns [] OK")

    print("\nall events tests passed")


if __name__ == "__main__":
    main()
