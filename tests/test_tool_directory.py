"""Tests for the tool-directory resources (server/tool_directory.py).

The directory must mirror the live tool registry exactly: every registered
tool appears once, in exactly one category, with a capped excerpt. Coverage
is derived from the registry itself (not a hand list), so adding a tool to
an existing leaf is covered without touching this file.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_tool_directory_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: F401, E402 - full registration side effect
import server.tool_directory as td  # noqa: E402
from tests._setup import db  # noqa: E402, I001

db.init_db()


def test_directory_covers_registry_exactly():
    rows = td._tool_rows()
    registry = set(td._registry_tools())
    assert registry, "tool registry must be populated after `import server`"
    listed = [name for items in rows.values() for name, _ in items]
    assert set(listed) == registry, (
        f"directory/registry mismatch: "
        f"missing={sorted(registry - set(listed))} "
        f"extra={sorted(set(listed) - registry)}"
    )
    assert len(listed) == len(set(listed)), "every tool must appear exactly once"


def test_known_categories_present_and_nonempty():
    assert len(td._CATEGORIES) == 7, "seven tool groups"
    rows = td._tool_rows()
    for key, _title, _blurb, _prefix in td._CATEGORIES:
        assert rows.get(key), f"category {key!r} must list at least one tool"


def test_index_lists_every_category_with_counts():
    index = td._tools_index()
    rows = td._tool_rows()
    total = sum(len(items) for items in rows.values())
    assert str(total) in index, "index must carry the live tool total"
    for key, _title, _blurb, _ in td._CATEGORIES:
        assert f"agentland://tools/{key}" in index
        assert f"({len(rows[key])} tools)" in index, f"index count for {key}"


def test_category_pages_carry_names_and_excerpts():
    for key, _title, _blurb, _ in td._CATEGORIES:
        page = td._category_page(key)
        for name, excerpt in td._tool_rows()[key]:
            assert f"`{name}`" in page, f"{name} missing from {key} page"
            assert excerpt in page, f"excerpt for {name} missing"
    assert "`repo_get_pr`" in td._category_page("repo")


def test_unknown_category_fails_loudly():
    for bad in ("nope", "Repo!", "../x", "", "tools/repo", "123"):
        try:
            td._category_page(bad)
        except ValueError as exc:
            assert "unknown tool category" in str(exc), f"{bad!r}: {exc}"
            continue
        raise AssertionError(f"{bad!r} must raise ValueError")
    assert td._category_page("  REPO ") == td._category_page("repo")


def test_excerpts_capped_and_nonempty():
    rows = td._tool_rows()
    assert rows, "directory must not be empty"
    for _key, items in rows.items():
        for name, excerpt in items:
            assert excerpt, f"{name} has an empty excerpt"
            assert len(excerpt) <= td._EXCERPT_CAP, (name, len(excerpt))


def test_excerpt_caps_long_lines():
    out = td._excerpt("x" * 500)
    assert len(out) <= td._EXCERPT_CAP and out.endswith("...")
    assert td._excerpt("") == "(no description)"
    assert td._excerpt(None) == "(no description)"
    assert td._excerpt("\n  \nSecond line first.") == "Second line first."


def test_bucket_fallback_pins_other():
    assert td._bucket_for("server.tools.forum") == "forum"
    assert td._bucket_for("server.tools.repo._reads") == "repo"
    assert td._bucket_for("some.unknown.module") == td._OTHER_KEY
    assert td._bucket_for("server.tools.forumx") == td._OTHER_KEY
    assert td._bucket_for("") == td._OTHER_KEY


def test_other_renders_conditionally():
    assert "(no tools in this category right now.)" in td._category_page("other")
    synthetic = {
        "forum": [("a_tool", "Does things.")],
        "other": [("x_tool", "Does other things.")],
    }
    text = td._render_index(synthetic)
    assert "in 8 categories" in text
    assert "`agentland://tools/other`" in text and "(1 tools)" in text
    text = td._render_index({"forum": [("a_tool", "Does things.")]})
    assert "in 7 categories" in text
    assert "`agentland://tools/other`" not in text


def _iso(days_ago: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


def _wipe_inventory():
    with db._conn() as conn:
        conn.execute("DELETE FROM tool_inventory")


def _seed_inventory_row(
    name,
    first_days_ago,
    last_days_ago,
    params_days_ago=None,
    desc_days_ago=None,
):
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO tool_inventory (tool, params_hash, desc_hash,"
            " first_seen, last_seen, last_params_change, last_desc_change)"
            " VALUES (?, 'ph', 'dh', ?, ?, ?, ?)",
            (
                name,
                _iso(first_days_ago),
                _iso(last_days_ago),
                _iso(params_days_ago) if params_days_ago is not None else None,
                _iso(desc_days_ago) if desc_days_ago is not None else None,
            ),
        )


def test_inventory_windows_and_removal():
    _wipe_inventory()
    _seed_inventory_row("new_tool", 0, 0)
    _seed_inventory_row("old_sig", 10, 0, params_days_ago=1)
    _seed_inventory_row("old_desc", 10, 0, desc_days_ago=2)
    _seed_inventory_row("both_axes", 10, 0, params_days_ago=1, desc_days_ago=1)
    _seed_inventory_row("gone_tool", 10, 1)
    _seed_inventory_row("stale_gone", 20, 10)
    _seed_inventory_row("quiet_tool", 10, 10)
    present = {"new_tool", "old_sig", "old_desc", "both_axes", "quiet_tool"}
    ch = db.tool_inventory_changes(days=5, present=present)
    assert ch["added"] == ["new_tool"], ch["added"]
    assert ch["signature_changed"] == ["both_axes", "old_sig"], ch["signature_changed"]
    assert ch["description_updated"] == ["old_desc"], ch["description_updated"]
    assert ch["removed"] == ["gone_tool"], ch["removed"]
    assert ch["recorded_tools"] == 7
    assert ch["tracking_since"] is not None


def test_inventory_writer_roundtrip():
    _wipe_inventory()
    items = [
        ("t_alpha", '{"a": 1}', "Does alpha."),
        ("t_beta", '{"b": 2}', "Does beta."),
    ]
    assert db.record_tool_inventory(items) == 2
    # Push first_seen outside the window: freshly recorded tools always
    # read as "added", which would mask the axis classification below.
    with db._conn() as conn:
        conn.execute("UPDATE tool_inventory SET first_seen = ?", (_iso(10),))
    present = {"t_alpha", "t_beta"}
    ch = db.tool_inventory_changes(days=5, present=present)
    assert ch["added"] == []
    # Identical re-record: no axis changes.
    assert db.record_tool_inventory(items) == 2
    ch = db.tool_inventory_changes(days=5, present=present)
    assert ch["signature_changed"] == []
    assert ch["description_updated"] == []
    # Description-only edit on t_alpha.
    items[0] = ("t_alpha", '{"a": 1}', "Does alpha, revised.")
    db.record_tool_inventory(items)
    ch = db.tool_inventory_changes(days=5, present=present)
    assert ch["description_updated"] == ["t_alpha"]
    assert ch["signature_changed"] == []
    # Signature edit on t_beta (lists under signature only).
    items[1] = ("t_beta", '{"b": 3}', "Does beta.")
    db.record_tool_inventory(items)
    ch = db.tool_inventory_changes(days=5, present=present)
    assert ch["signature_changed"] == ["t_beta"]
    # Drop t_beta from the snapshot: recently seen + absent = removed.
    ch = db.tool_inventory_changes(days=5, present={"t_alpha"})
    assert ch["removed"] == ["t_beta"]


def test_inventory_live_registry_smoke():
    _wipe_inventory()
    items = td._inventory_items()
    assert len(items) >= 130, f"registry snapshot too small: {len(items)}"
    assert db.record_tool_inventory(items) == len(items)
    names = {name for name, _, _ in items}
    ch = db.tool_inventory_changes(days=365, present=names)
    assert ch["recorded_tools"] == len(items)
    assert len(ch["added"]) == len(items)
    db.record_tool_inventory(items)
    ch = db.tool_inventory_changes(days=365, present=names)
    assert ch["signature_changed"] == []
    assert ch["description_updated"] == []


if __name__ == "__main__":
    for fn in [
        test_directory_covers_registry_exactly,
        test_known_categories_present_and_nonempty,
        test_index_lists_every_category_with_counts,
        test_category_pages_carry_names_and_excerpts,
        test_unknown_category_fails_loudly,
        test_excerpts_capped_and_nonempty,
        test_excerpt_caps_long_lines,
        test_bucket_fallback_pins_other,
        test_other_renders_conditionally,
        test_inventory_windows_and_removal,
        test_inventory_writer_roundtrip,
        test_inventory_live_registry_smoke,
    ]:
        fn()
    print("test_tool_directory all passed")
