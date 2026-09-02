"""Nav-route sync ratchet for viewer/_layout._NAV_ITEMS vs viewer ROUTES (4708)."""

import pathlib
import re


def test_nav_items_sync_with_routes():
    layout = pathlib.Path("viewer/_layout.py").read_text(encoding="utf-8")
    init = pathlib.Path("viewer/__init__.py").read_text(encoding="utf-8")
    m = re.search(r"_NAV_ITEMS\s*=\s*\[(.*?)\]", layout, re.S)
    assert m, "_NAV_ITEMS not found"
    nav_hrefs = set(re.findall(r'"(/[^"]*)"', m.group(1)))
    mg = re.search(r"_GOVERNANCE_ITEMS\s*=\s*\[(.*?)\]", layout, re.S)
    assert mg, "_GOVERNANCE_ITEMS not found"
    gov_hrefs = set(re.findall(r'"(/[^"]*)"', mg.group(1)))
    route_paths = set(re.findall(r'Route\(\s*"(/[^"]*)"', init))
    missing = sorted(nav_hrefs - route_paths)
    assert not missing, f"_NAV_ITEMS hrefs missing in ROUTES: {missing}"
    missing_gov = sorted(gov_hrefs - route_paths)
    assert not missing_gov, f"_GOVERNANCE_ITEMS hrefs missing: {missing_gov}"
    assert nav_hrefs.issubset(route_paths | {"/api/overview"}), (
        f"unexpected nav hrefs: {sorted(nav_hrefs - route_paths)}"
    )
