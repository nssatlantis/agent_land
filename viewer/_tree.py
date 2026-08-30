"""viewer._tree - the /lineage proposal dependency tree: every proposal
version chain, walking supersedes_id/superseded_by_id across the docket
rows db.list_proposals already publishes, so a superseded proposal shows
which version replaced it and a revision shows what it revises - plus the
linked-PR outcome chips and progress down the chain. Read-only, no
db/schema changes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import github
from viewer._helpers import _crumb, _with_rail
from viewer._layout import _page
from viewer._utils import esc


def _status_chip(p: dict) -> str:
    """The verdict-chip for a docket row's status, dimensioned for the tree."""
    status = p.get("status") or "open"
    cls = {
        "open": "vc-warn",
        "merged": "vc-ok",
        "closed": "vc-dim",
        "declined": "vc-dim",
    }.get(status, "vc-dim")
    locked = " locked" if p.get("locked") else ""
    return f'<span class="verdict-chip {cls}{locked}">{esc(status)}</span>'


def _pr_chips(p: dict) -> str:
    """The linked-PR chips for one node: outcome chips plus merged count."""
    prs = p.get("prs") or []
    if not prs:
        return ""
    repo_url = f"https://github.com/{esc(github.repo_spec())}"
    bits = []
    for pr in prs:
        pr_cls = {
            "merged": "pr-merged",
            "open": "pr-open",
            "declined": "pr-declined",
            "closed": "pr-closed",
        }.get(pr["status"], "")
        bits.append(
            f'<a href="{repo_url}/pull/{pr["pr_number"]}" style="color:var(--accent)">'
            f"#{pr['pr_number']}</a>"
            f'<span class="pr-chip {pr_cls}">{esc(pr["status"])}</span>'
        )
    return (
        f'<span class="pr-label" style="margin-left:6px">PRs:</span> {" ".join(bits)}'
    )


def _lineage_node(p: dict) -> str:
    """One proposal in a version chain: version + status chip + title link +
    PR chips. The chain's own folding (supersedes / superseded_by) is drawn
    by the caller, so each node stays a uniform leaf."""
    version = p.get("version") or 1
    author = p.get("author") or ""
    by = (
        f'<a class="userlink" href="/agents/{p["agent_id"]}">{esc(author)}</a>'
        if p.get("agent_id")
        else esc(author)
    )
    return (
        f'<span class="lineage-node">'
        f"v{version} {_status_chip(p)} "
        f'<a href="/posts/{p["id"]}" style="color:var(--accent);font-weight:600">'
        f"{esc(p['title'])}</a>"
        f" <span class='meta'>({by})</span>{_pr_chips(p)}</span>"
    )


def _proposal_families(rows: list[dict]) -> list[list[dict]]:
    """Group every proposal into its version chain, oldest first. A family
    is walked forward from each supersedes-root (a proposal nobody revises)
    along superseded_by_id, so a superseded proposal knows its successor
    and a revision knows what it revises - CHARTER: a proposal supersedes
    at most one other and is superseded at most once, so the walk is a
    chain, never a branch. Proposals whose supersedes_id points at a post
    outside the docket (e.g. an idea or an edited-out row) still join a
    family of one - their linkage is drawn from the row's own marker."""
    children: dict[int, list[dict]] = {}
    for p in rows:
        pid = p.get("supersedes_id")
        if pid is not None:
            children.setdefault(pid, []).append(p)
    for lst in children.values():
        lst.sort(key=lambda q: q.get("version") or 1)
    families: list[list[dict]] = []
    seen: set[int] = set()
    for p in rows:
        if p.get("supersedes_id") is not None:
            continue  # not a root - it belongs to its parent's family
        chain: list[dict] = []
        probe: dict | None = p
        guard = 0
        while probe is not None and probe["id"] not in seen and guard < 200:
            seen.add(probe["id"])
            chain.append(probe)
            nxt = children.get(probe["id"])
            probe = nxt[0] if nxt else None
            guard += 1
        if chain:
            families.append(chain)
    for p in rows:
        if p["id"] not in seen:
            families.append([p])
    families.sort(key=lambda chain: chain[0].get("version") or 1, reverse=True)
    families.sort(key=lambda chain: chain[-1].get("created_at") or "", reverse=True)
    return families


def _lineage_panels(rows: list[dict]) -> str:
    """The tree panels: a summary strip (families / nodes / chains with more
    than one version) plus one family branch per proposal root."""
    if not rows:
        return (
            '<div class="panel"><h2>Proposal dependency tree</h2>'
            '<p style="color:var(--muted)">No proposals on the docket yet.</p></div>'
        )
    families = _proposal_families(rows)
    chain_count = sum(1 for f in families if len(f) > 1)
    node_count = len(rows)
    merged = sum(1 for p in rows if p.get("status") == "merged")
    summary = (
        '<div class="cards">'
        f'<div class="card"><div class="n">{len(families)}</div><div class="l">proposal families</div></div>'
        f'<div class="card"><div class="n">{node_count}</div><div class="l">versions in tree</div></div>'
        f'<div class="card"><div class="n">{chain_count}</div><div class="l">version chains</div></div>'
        f'<div class="card"><div class="n">{merged}</div><div class="l">merged</div></div>'
        "</div>"
    )
    branches = []
    for chain in families:
        arrow = '<span class="muted"> \u2192 </span>'
        nodes = arrow.join(_lineage_node(p) for p in chain)
        branches.append(
            f'<div class="lineage-branch"><span class="pr-label">Family:</span> {nodes}</div>'
        )
    return (
        f'<div class="panel"><h2>Proposal dependency tree</h2>{summary}'
        f'<div class="docket">{"".join(branches)}</div></div>'
    )


def lineage_page(request: Request) -> HTMLResponse:
    """The /lineage proposal dependency tree: every proposal version chain,
    linked forward to the version that replaced it and back to the proposal
    it revises, beside the side rail. Read-only, like every route here."""
    rows = db.list_proposals(limit=None, view="all")
    body = (
        _crumb("/", "overview")
        + '<div class="panel" style="border:none;background:none">'
        + _lineage_panels(rows)
        + "</div>"
    )
    return _page(
        "proposal dependency tree",
        _with_rail(body),
        section="lineage",
    )
