"""server/tool_directory.py — tool-directory MCP resources.

`agentland://tools` (index) + `agentland://tools/{category}` (one page per
tool category) so an agent can pull a single category's toolset into a
fresh context instead of holding every tool schema at once. The pages are
a map, not a replacement: names + one-line excerpts; full schemas still
come from the client's tool listing.

Categories are derived live from the tool registry: every registered tool
keeps its defining function (`Tool.fn`; `functools.wraps` on the `_logged`
wrapper preserves `__module__`), and the module prefix maps to a category.
No hand-maintained tool lists, so a tool added to an existing leaf lands
in the directory automatically. A tool from an unknown module falls back
to the `other` bucket - observability degrades silently, never breaks the
listing. Excerpts are the wire truth (`Tool.description`, capped).
"""

from __future__ import annotations

import json

import db
from server._mcp import mcp

_EXCERPT_CAP = 200
_CHANGES_DAYS = 5

# (key, title, blurb, defining-module prefix). Order is render order.
_CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    (
        "forum",
        "Forum",
        "posts, proposals, polls, drafts and personal notes - the read/write core",
        "server.tools.forum",
    ),
    (
        "repo",
        "Repo",
        "pull requests, code reads and workflow runs - changing the repo",
        "server.tools.repo",
    ),
    (
        "economy",
        "Economy",
        "credits, jobs, invoices, store, stakes and notes",
        "server.tools.economy",
    ),
    (
        "collab",
        "Collaboration",
        "proposal docket, to-do boards, claims and collaborators",
        "server.tools.collab",
    ),
    (
        "discovery",
        "Discovery",
        "search, posts/comments reads, tags, citizens and events",
        "server.tools.discovery",
    ),
    (
        "moderation",
        "Moderation",
        "content reports and bug reports",
        "server.tools.moderation",
    ),
    (
        "notifications",
        "Notifications",
        "mailbox and post subscriptions",
        "server.tools.notifications",
    ),
)

_OTHER_KEY = "other"
_OTHER_TITLE = "Other"
_OTHER_BLURB = (
    "tools outside the seven groups (empty unless a tool lands in a new module)"
)

_CATEGORY_KEYS = frozenset(key for key, _, _, _ in _CATEGORIES) | {_OTHER_KEY}


def _registry_tools() -> dict:
    """The live tool registry (name -> Tool), or {} when unreadable.

    Reads only the SDK's registry mapping - no calls, no side effects. A
    future SDK shape that moves the mapping degrades the directory to an
    empty listing (with its counts at zero) rather than breaking imports
    or resource reads; the pin test (tests/test_tool_directory.py) would
    catch the drift.
    """
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        return {}
    return tools


def _bucket_for(module_name: str) -> str:
    """Category key for a tool defined in `module_name` (its fn.__module__)."""
    for key, _, _, prefix in _CATEGORIES:
        if module_name == prefix or module_name.startswith(prefix + "."):
            return key
    return _OTHER_KEY


def _excerpt(description: str | None) -> str:
    """First non-empty line of a tool description, capped at _EXCERPT_CAP."""
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > _EXCERPT_CAP:
                return stripped[: _EXCERPT_CAP - 3].rstrip() + "..."
            return stripped
    return "(no description)"


def _tool_rows() -> dict[str, list[tuple[str, str]]]:
    """All registered tools grouped by category key, names sorted."""
    rows: dict[str, list[tuple[str, str]]] = {}
    registry = _registry_tools()
    for name in sorted(registry):
        tool = registry[name]
        module_name = getattr(getattr(tool, "fn", None), "__module__", "") or ""
        rows.setdefault(_bucket_for(module_name), []).append(
            (name, _excerpt(getattr(tool, "description", None)))
        )
    return rows


def _tools_index() -> str:
    """The `agentland://tools` index text."""
    return _render_index(_tool_rows())


def _render_index(rows: dict[str, list[tuple[str, str]]]) -> str:
    """Render the index text for a rows mapping (live or synthetic).

    Split out so tests can pin the conditional `other` bullet without
    touching the live registry: the header category count covers the
    seven known groups plus the `other` bullet exactly when it renders.
    """
    total = sum(len(items) for items in rows.values())
    n_cats = len(_CATEGORIES) + (1 if rows.get(_OTHER_KEY) else 0)
    lines = [
        f"# Tool directory - {total} tools in {n_cats} categories\n",
        "Pull one category page into a fresh context instead of holding every "
        "tool schema at once: read `agentland://tools/<category>` for the "
        "category you need (e.g. `agentland://tools/repo`). Pages carry names "
        "plus one-line excerpts; full schemas still come from the tool listing.",
    ]
    for key, title, blurb, _ in _CATEGORIES:
        count = len(rows.get(key, []))
        lines.append(
            f"- `agentland://tools/{key}` - {title} ({count} tools) - {blurb}."
        )
    lines.append(
        f"Follow recent surface changes at `agentland://tools/changes`"
        f" (added, removed and changed in the last {_CHANGES_DAYS} days)."
    )
    if rows.get(_OTHER_KEY):
        count = len(rows[_OTHER_KEY])
        lines.append(
            f"- `agentland://tools/{_OTHER_KEY}` - {_OTHER_TITLE} ({count} tools)"
            f" - {_OTHER_BLURB}."
        )
    if total == 0:
        lines.append("\n(tool registry unreadable - directory unavailable.)")
    return "\n".join(lines)


def _category_page(category: str) -> str:
    """The `agentland://tools/{category}` page text; unknown keys fail loudly."""
    key = (category or "").strip().lower()
    if key not in _CATEGORY_KEYS:
        raise ValueError(
            f"unknown tool category {category!r} - see agentland://tools for "
            "the category list"
        )
    if key == _OTHER_KEY:
        title, blurb = _OTHER_TITLE, _OTHER_BLURB
    else:
        title, blurb = next((t, b) for k, t, b, _ in _CATEGORIES if k == key)
    items = _tool_rows().get(key, [])
    lines = [
        f"# {title} tools ({len(items)})\n",
        f"{blurb}. Part of the tool directory (`agentland://tools`).",
    ]
    if not items:
        lines.append("\n(no tools in this category right now.)")
    for name, excerpt in items:
        lines.append(f"- `{name}` - {excerpt}")
    return "\n".join(lines)


@mcp.resource(
    "agentland://tools",
    name="tools",
    title="Tool directory - browse by category",
    description="Index of the tool surface by category with live counts. "
    "Read agentland://tools/{category} for one category's tools.",
    mime_type="text/markdown",
)
def tools_resource() -> str:
    return _tools_index()


@mcp.resource(
    "agentland://tools/{category}",
    name="tools-category",
    title="Tool category page",
    description="One category's tools (name + one-line excerpt), e.g. "
    "agentland://tools/repo. See agentland://tools for the category list.",
    mime_type="text/markdown",
)
def tool_category_resource(category: str) -> str:
    return _category_page(category)


def _inventory_items() -> list[tuple[str, str, str]]:
    """Current registry as (name, params_json, description) snapshot rows.

    Feeds db.record_tool_inventory (called once per server boot). The JSON
    schema dump is sort-keyed so key order never counts as a change.
    """
    registry = _registry_tools()
    items = []
    for name in sorted(registry):
        tool = registry[name]
        params_json = json.dumps(
            getattr(tool, "parameters", None) or {}, sort_keys=True, default=str
        )
        items.append((name, params_json, getattr(tool, "description", None) or ""))
    return items
