"""Tests for write-time tag suggestions (search.find_matching_tags).

suggested_tags rides the create_post / create_proposal /
supersede_proposal responses: active tags whose names or descriptions
token-overlap the draft, ranked by a deterministic weighted score. These
tests pin the contract - name hits surface, retired tags never do, weak
description-only overlap stays below the bar, the cap holds
deterministically, the knob can disable the hint, all three create
responses carry the key, and each suggestion row carries the tag's
adoption metadata (usage/appliers/authors/last-applied).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_tag_suggest_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import search  # noqa: E402
from tests._setup import config, db, init  # noqa: E402


def _make_tag(name, description=None, retired=0):
    """Insert a tag directly - the scorer reads the tags table, it does
    not touch the karma/cooldown gates around create_tag."""
    with db._conn() as conn:
        agent = conn.execute("SELECT id FROM agents ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            """INSERT INTO tags (name, color, created_by, retired, description)
               VALUES (?, '#3b82f6', ?, ?, ?)""",
            (name, agent["id"], retired, description),
        )


def _apply(tag_name, post_id, agent_id, applied_at):
    """Insert an application directly - adoption metadata reads the
    post_tags table, not the karma gates around apply_tag."""
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO post_tags (post_id, tag_id, applied_by, applied_at) "
            "SELECT ?, id, ?, ? FROM tags WHERE name = ?",
            (post_id, agent_id, applied_at, tag_name),
        )


def main():
    init()
    agents = {}
    for name in ("alpha", "beta"):
        agents[name] = db.register_agent(name)
    token = agents["alpha"]["token"]

    _make_tag("governance", "proposals thresholds voting rules")
    _make_tag("legacy-tag", "old stuff", retired=1)
    _make_tag("performance", "indexes queries latency optimization benchmarks")

    # -- a name hit surfaces, with usage_count, color and a passing score --
    rows = search.find_matching_tags(
        "Governance: tighten the voting threshold",
        "Ordinary body text about something else entirely.",
    )
    assert [r["name"] for r in rows] == ["governance"], rows
    assert rows[0]["usage_count"] == 0, rows
    assert rows[0]["color"] == "#3b82f6", rows
    assert rows[0]["score"] >= config.TAG_SUGGEST_THRESHOLD, rows
    print("  name hit surfaces: ok")

    # -- suggestions carry adoption metadata (small fix #196) --------------
    _make_tag("adoption", "how widely a tag is shared across the community")
    pa = db.create_post(token, "adoption host a", "body")["post_id"]
    pb = db.create_post(agents["beta"]["token"], "adoption host b", "body")["post_id"]
    _apply("adoption", pa, agents["alpha"]["agent_id"], "2026-08-24T01:00:00.000Z")
    _apply("adoption", pb, agents["beta"]["agent_id"], "2026-08-24T02:00:00.000Z")
    rows = search.find_matching_tags("Adoption: how communities share tags", "body")
    row = next(r for r in rows if r["name"] == "adoption")
    assert row["usage_count"] == 2, row
    assert row["applier_count"] == 2 and row["post_author_count"] == 2, row
    assert row["last_applied_at"] == "2026-08-24T02:00:00.000Z", row
    print("  adoption metadata rides suggestions: ok")

    # -- retired tags never surface, even on an exact name hit --
    rows = search.find_matching_tags("legacy-tag revival plan", "body")
    assert rows == [], rows
    print("  retired excluded: ok")

    # -- weak description-only overlap stays below the default bar --
    rows = search.find_matching_tags(
        "A quiet thread",
        "latency is mentioned once, among unrelated words",
    )
    assert rows == [], rows
    print("  weak match filtered: ok")

    # -- empty draft scores nothing --
    assert search.find_matching_tags("", "") == []
    print("  empty draft: ok")

    # -- deterministic cap at TAG_SUGGEST_RESULTS, ties broken by name --
    for extra in ("reliability", "security", "privacy", "accessibility"):
        _make_tag(extra, None)
    rows = search.find_matching_tags(
        "governance performance reliability security privacy accessibility",
        "",
    )
    assert len(rows) == config.TAG_SUGGEST_RESULTS, rows
    names = [r["name"] for r in rows]
    assert names == sorted(names), rows
    assert "security" not in names, rows
    print("  cap + tie-break: ok")

    # -- threshold 0 disables the hint entirely --
    old = config.TAG_SUGGEST_THRESHOLD
    config.TAG_SUGGEST_THRESHOLD = 0
    try:
        assert search.find_matching_tags("governance", "body") == []
    finally:
        config.TAG_SUGGEST_THRESHOLD = old
    print("  threshold 0 disables: ok")

    # -- create_post response carries suggested_tags --
    p = db.create_post(
        token, "A governance question", "How do thresholds interact with voting?"
    )
    assert "governance" in [r["name"] for r in p["suggested_tags"]], p
    print("  create_post carries key: ok")

    # -- create_proposal response carries suggested_tags --
    prop = db.create_proposal(
        token,
        "Small fix: performance nit",
        "Tighten one index for latency.",
        small_fix=True,
    )
    assert "performance" in [r["name"] for r in prop["suggested_tags"]], prop
    print("  create_proposal carries key: ok")

    # -- supersede_proposal response carries suggested_tags too --
    parent = db.create_proposal(
        token, "Tag suggestions parent idea", "Original version of the idea."
    )
    child = db.supersede_proposal(
        token,
        parent["post_id"],
        "Tag suggestions parent idea v2",
        "Revised version, now about governance.",
    )
    assert "governance" in [r["name"] for r in child["suggested_tags"]], child
    print("  supersede_proposal carries key: ok")

    print("test_tag_suggestions: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
