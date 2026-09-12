"""Tests for the tool-directory resources (server/tool_directory.py).

The directory must mirror the live tool registry exactly: every registered
tool appears once, in exactly one category, with a capped excerpt. Coverage
is derived from the registry itself (not a hand list), so adding a tool to
an existing leaf is covered without touching this file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: F401, E402 - full registration side effect
import server.tool_directory as td  # noqa: E402


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
    ]:
        fn()
    print("test_tool_directory all passed")
