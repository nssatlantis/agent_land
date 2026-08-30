"""Regression guard: server/admin facade must keep its public re-exports.

If server/admin/__init__.py is ever committed with its re-export surface deleted
(the same "file-gutted-on-push" failure class that hit db in PR #425, +2/-346,
and schema.sql in PR #423, +3/-933), this test fails immediately and locally
instead of waiting for admin routes to 404 at runtime.

server/admin.py was split into the server/admin/ package (3,718 lines → 9
leaves + facade), following the viewer/ and github/ pattern (PR #434). Like
db and server/__init__.py, server/admin/__init__.py is a facade that
re-exports the public API, so it needs the same ratchet as PR #431/#437.

This guard checks the facade two ways:
  1. Statically (primary, side-effect-free) -- parse server/admin/__init__.py
     and require every EXPECTED name to appear in a `from server.admin... import`
     re-export line. This targets the gutting failure class directly (it is a
     text deletion) WITHOUT importing the whole app stack, so it cannot be
     masked by an unrelated import-time crash and stays fast.
  2. Dynamically (secondary) -- `import server.admin` and require the same names
     to be present AND to point at the real leaf objects (not a placeholder).

If a name is legitimately removed or renamed from the facade, update EXPECTED
to match -- that is the contract. Do NOT delete expectations to silence the test.

Part of the #163 resilience ratchet applied to the source tree itself.
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

FACADE_PATH = os.path.join(REPO_ROOT, "server", "admin", "__init__.py")

# Representative slice of the public admin facade. Every name is a real
# re-export from server/admin/__init__.py; a gutted facade drops most of
# them, so the test fails before merge. Keep at least one name per leaf.
EXPECTED = [
    # core auth (server/admin/_auth)
    "ADMIN_USER",
    "ADMIN_PASSWORD",
    "_CSRF_COOKIE",
    "_authorized",
    "_admin_user",
    "_denied",
    "_csrf_token",
    "_csrf_field",
    "_csrf_ok",
    "_admin_page",
    "_flash",
    "_safe_referer",
    "_admin_nav",
    "_mutate",
    # reports
    "admin_page",
    "reports_index",
    "report_detail",
    "resolve_report",
    # posts
    "posts_index",
    "admin_update_post_settings",
    "delete_post",
    # agents
    "agent_detail",
    "ban_agent",
    "unban_agent",
    "delete_agent",
    # jobs
    "jobs_manager_page",
    "create_official_job",
    "admin_close_job",
    "admin_review_job",
    "create_stake",
    "delete_stake",
    # workflows
    "workflows_admin_page",
    "workflow_restart",
    "workflow_close_stale",
    # ci
    "ci_admin_page",
    "ci_clear_pending",
    "ci_prune_images",
    "ci_restart_ticker",
    "ci_gc_workspaces",
    # economy
    "economy_adjust",
    # bugs
    "bugs_index",
    "bug_detail",
    "admin_confirm_bug",
    "admin_fix_bug",
    # routes
    "ROUTES",
]

# Leaf module -> (facade name, leaf attribute) pairs used for the identity
# check. Each name must be the SAME object on the facade and in its leaf.
_IDENTITY = {
    "server.admin._auth": ["_authorized", "_admin_user"],
    "server.admin._reports": ["admin_page", "reports_index"],
    "server.admin._posts": ["posts_index", "admin_update_post_settings"],
    "server.admin._agents": ["agent_detail", "ban_agent"],
    "server.admin._jobs": ["jobs_manager_page", "admin_close_job"],
    "server.admin._workflows": ["workflows_admin_page", "workflow_restart"],
    "server.admin._ci": ["ci_admin_page", "ci_clear_pending"],
    "server.admin._economy": ["economy_adjust"],
    "server.admin._bugs": ["bugs_index", "admin_confirm_bug"],
}


def _facade_source() -> str:
    with open(FACADE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _re_exported_names(source: str) -> set:
    """Names the facade re-exports via `from server.admin... import`."""
    names = set()
    # Multi-line: from server.admin.x import (a, b, c)
    for m in re.finditer(r"from\s+server\.admin[\w.]*\s+import\s*\(([^)]*)\)", source):
        for item in re.findall(r"[\w]+", m.group(1)):
            names.add(item)
    # Single-line: from server.admin.x import y, z  or from server.admin._auth import FOO
    for m in re.finditer(r"from\s+server\.admin[\w.]*\s+import\s+([^\n(]+)", source):
        for item in re.split(r"[,\s]+", m.group(1)):
            # Strip trailing comments
            item = item.split("#")[0].strip()
            if item and item.isidentifier():
                names.add(item)
    # Also handle single-import lines without parens — already covered, but add bare import check for ROUTES
    if "ROUTES" in source:
        names.add("ROUTES")
    return names


def test_admin_facade_exports_present_in_source():
    """Static ratchet: every EXPECTED name must be re-exported in server/admin/__init__.py."""
    exported = _re_exported_names(_facade_source())
    missing = [name for name in EXPECTED if name not in exported]
    assert not missing, (
        f"server/admin facade (server/admin/__init__.py) is missing re-exports: {missing}"
    )


def test_admin_facade_exports_present_at_runtime():
    """Dynamic ratchet: `import server.admin` exposes every EXPECTED name and
    re-export points at the real leaf object."""
    import server.admin

    missing = [name for name in EXPECTED if not hasattr(server.admin, name)]
    assert not missing, f"server.admin facade is missing re-exports: {missing}"

    for module_name, attrs in _IDENTITY.items():
        leaf = __import__(module_name, fromlist=["__name__"])
        for attr in attrs:
            assert getattr(server.admin, attr, None) is getattr(leaf, attr, None), (
                f"server.admin.{attr} is not the real {module_name}.{attr} object"
            )
