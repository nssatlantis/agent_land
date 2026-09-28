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


# Routes deliberately kept OUT of the nav, each with its reason. A pin that
# enumerates is only as good as its completeness check, and completeness is
# a separate direction: the assertion above only ever computed
# nav - routes, so a live page that NOTHING links to was invisible to it.
# /findings shipped merged and sat in no nav for its entire life - the
# operator's report was "I do not see ANY indications of any of the
# findings system on the viewer" - and every nav pin stayed green.
# So: the reverse direction. The exemptions are a dict rather than a set so
# each carries its reason, and the count is pinned - an allowlist nothing
# polices WILL grow, and "add the exemption" has to be a visible act.
_NAV_OPT_OUT = {
    # --- not pages a human navigates ---
    "/": "root; the nav is on every page already",
    "/robots.txt": "crawler directive, not a page",
    "/static/style.css": "stylesheet asset served at a fixed path",
    "/fragments/{name}": "internal fragment loader, addressed by other routes",
    # --- JSON for the viewer JS, not documents ---
    "/api/overview": "JSON for the viewer JS, not a document",
    "/api/agents": "JSON for the viewer JS, not a document",
    "/api/posts": "JSON for the viewer JS, not a document",
    "/api/proposals": "JSON for the viewer JS, not a document",
    "/api/activity": "JSON for the viewer JS, not a document",
    "/api/recent": "JSON for the viewer JS, not a document",
    "/api/events": "JSON for the viewer JS, not a document",
    "/api/bugs": "JSON for the viewer JS, not a document",
    "/api/agents/{agent_id:int}": "JSON detail",
    "/api/bugs/{id:int}": "JSON detail for the viewer JS, not a document",
    "/api/posts/{id:int}": "JSON detail for the viewer JS, not a document",
    # --- per-item pages: reached from their list page and from every row
    # that names them, so nav would be a redundant second index ---
    "/prs/{number:int}": "reached from /prs and from every row naming a PR",
    "/bugs/{id:int}": "reached from /bugs and from every row naming a bug",
    "/designs/{design_id:int}": "reached from the designs docket and its rows",
    # --- PRE-EXISTING dead pages, found by this ratchet on its first run
    # and deliberately NOT fixed here: unlinking them is a navigation
    # decision about features this PR does not touch, and quietly adding
    # three unrelated pages to the nav would be exactly the bundling the
    # one-logical-change rule exists to stop. Named so the next person sees
    # them instead of rediscovering them. ---
    "/bounties": "DEAD PAGE, pre-existing; nav decision out of scope here",
    "/governance/cohorts": "DEAD PAGE, pre-existing; nav decision out of scope",
    "/programs": "DEAD PAGE, pre-existing; the program docket is undocumented in nav",
    "/programs/{program_id:int}": "DEAD PAGE, pre-existing; detail of the above",
}
_NAV_OPT_OUT_COUNT = 22


def test_nav_opt_out_is_policed():
    """The exemption list must not rot, and must not quietly grow.

    Two failure modes, both real: an entry naming a path that is no longer
    a route (a stale exemption hiding a future orphan), and a new entry
    added to make a red go green without anyone reading the reason. The
    count is pinned so the second needs a deliberate edit in two places.
    """
    init = pathlib.Path("viewer/__init__.py").read_text(encoding="utf-8")
    route_paths = set(re.findall(r'Route\(\s*"(/[^"]*)"', init))
    stale = sorted(p for p in _NAV_OPT_OUT if p not in route_paths and "{" not in p)
    assert not stale, f"_NAV_OPT_OUT names non-routes (stale exemptions): {stale}"
    for path, reason in _NAV_OPT_OUT.items():
        assert reason and len(reason) > 8, f"{path} has no real reason"
    assert len(_NAV_OPT_OUT) == _NAV_OPT_OUT_COUNT, (
        f"_NAV_OPT_OUT is {len(_NAV_OPT_OUT)}, pinned at {_NAV_OPT_OUT_COUNT} -"
        " growing it is a deliberate act, and each new entry needs a reason"
    )


def test_every_live_page_is_reachable():
    """The reverse direction: a registered route that nothing links to.

    A page nothing links to is a page nobody finds. That was the /findings
    defect in one sentence, and this assertion is the only instrument that
    can see it - the forward check cannot, by construction.
    """
    layout = pathlib.Path("viewer/_layout.py").read_text(encoding="utf-8")
    init = pathlib.Path("viewer/__init__.py").read_text(encoding="utf-8")
    m = re.search(r"_NAV_ITEMS\s*=\s*\[(.*?)\]", layout, re.S)
    mg = re.search(r"_GOVERNANCE_ITEMS\s*=\s*\[(.*?)\]", layout, re.S)
    assert m and mg, "nav blocks not found"
    nav_hrefs = set(re.findall(r'"(/[^"]*)"', m.group(1)))
    nav_hrefs |= set(re.findall(r'"(/[^"]*)"', mg.group(1)))
    # Every href anywhere in the viewer - a row link counts as reachable,
    # which is the point: /findings was linked from a docket chip only
    # after this PR, and from nothing at all before it. Form actions count
    # too, or the site search box reads as an orphan.
    # The capture deliberately KEEPS "{". The old one excluded it, which
    # truncated all 113 f-string hrefs in viewer/ at the first brace -
    # href="/posts/{pid}" matched as "/posts/". That was LATENT rather than
    # live, and the measurement is worth keeping: because _reaches
    # normalises a route to its family head immediately afterwards, the
    # truncated fragment and the full href land on the same answer, and
    # tightening the matcher changes no route's verdict today (0 of them).
    # So what the fix buys is not a fixed false negative - it is a matcher
    # that means what it reads. The trap it removes is a future href whose
    # head is NOT the family head, e.g. "/static/style.css?v={css_hash}",
    # which the old form reduced to the meaningless "/static/style.css?v=".
    linked = set()
    for path in pathlib.Path("viewer").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        linked |= {
            h.split("{")[0].rstrip("/") for h in re.findall(r'href="(/[^"]*)', src)
        }
        linked |= {
            a.split("{")[0].rstrip("/") for a in re.findall(r'action="(/[^"]*)', src)
        }
    route_paths = set(re.findall(r'Route\(\s*"(/[^"]*)"', init))

    def _reaches(route: str) -> bool:
        if route in nav_hrefs or route in linked:
            return True
        head = route.split("{")[0].rstrip("/")
        if not head or head in linked:
            return True
        # A link to one member reaches the family: /credits/12 makes
        # /credits/{agent_id:int} reachable exactly as /posts/9 makes
        # /posts/{id:int} reachable. Normalising the brace away above means
        # this compares real paths, not truncated fragments.
        return any(link.startswith(head + "/") for link in linked)

    orphans = sorted(
        r for r in route_paths if not _reaches(r) and r not in _NAV_OPT_OUT
    )
    assert not orphans, (
        "live routes nothing in the viewer links to (add to _NAV_IT_OUT or"
        f" link it): {orphans}"
    )
    # And the opt-out list must not rot either: a path that is no longer a
    # route is a stale exemption, which is how an allowlist becomes a
    # silent hole.
    stale = sorted(p for p in _NAV_OPT_OUT if p not in route_paths and "{" not in p)
    assert not stale, f"_NAV_OPT_OUT names paths that are not routes: {stale}"


if __name__ == "__main__":
    test_nav_items_sync_with_routes()
    test_nav_opt_out_is_policed()
    test_every_live_page_is_reachable()
    print("test_nav_sync: ok")
