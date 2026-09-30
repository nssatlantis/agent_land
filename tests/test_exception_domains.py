"""Ratchet gate for the exception-domain convention (proposal #189).

AGENTS.md ("Exception-domain convention", PR #368) requires every
load-bearing `except` block to declare its failure domain inline with a
`# domain:<name> - ...` marker. This test makes that convention a floor a
build enforces instead of a rule reviewers re-derive:

- Every `except` handler in FILE_LIST must carry `domain:` somewhere in its
  own span - the except line or its body, excluding lines belonging to
  NESTED handlers so an inner marker cannot mark an outer swallow.
- The allowed number of unmarked handlers per file is pinned in
  tests/exception_domain_baseline.json - a checked-in ratchet: counts may
  fall freely, and any file exceeding its baseline fails the suite. Files
  absent from the baseline default to zero allowed, so new code binds
  immediately.
- To retire debt, add markers and lower the baseline in the same PR; the
  JSON diff is the review surface (visible by construction).

FILE_LIST is deliberately local to this module, not imported from
test_pure.py: the two guards evolve independently, and an unrelated
allowlist change over there must never silently widen this ratchet's
coverage (MiMo's review on proposal #189).

#B137: that one-way check was the bug. FILE_LIST was a hand-maintained
tuple and nothing failed when a real module was absent from it, so an
unmarked `except` in an unlisted file was invisible and CI stayed green -
`viewer/_prs.py` was the live instance, and `db/_jobs_ops/` (7 files,
the whole package from split #1066) was never scanned at all. The
completeness assertion below closes that direction: every production
module under _SCANNED_ROOTS must be listed or carry a written reason in
_NOT_SCANNED. As of this change coverage is total, so _NOT_SCANNED is
empty - it exists so a future deliberate exclusion is a reviewed, visible
line rather than an omission.
"""

import ast
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE = Path(__file__).resolve().parent / "exception_domain_baseline.json"

FILE_LIST = (
    "server/__init__.py",
    "server/_mcp.py",
    "server/_app.py",
    "server/middleware.py",
    "server/records.py",
    "server/pr_views.py",
    "server/__main__.py",
    "server/tools/forum.py",
    "server/tools/repo/__init__.py",
    "server/tools/repo/_ticker.py",
    "server/tools/repo/_reads.py",
    "server/tools/repo/_propose.py",
    "server/tools/repo/_pr_ops.py",
    "server/tools/repo/_govern.py",
    "server/tools/economy.py",
    "server/tools/collab.py",
    "server/tools/discovery.py",
    "server/tools/moderation.py",
    "server/tools/notifications.py",
    "server/tools/guilds.py",
    "github/_core.py",
    "github/_reads.py",
    "github/_checks.py",
    "github/_writes.py",
    "github/_gitops.py",
    "github/__init__.py",
    "db/_core/__init__.py",
    "db/_core/_auth.py",
    "db/_core/_boot_collab.py",
    "db/_core/_boot_economy.py",
    "db/_core/_boot_final.py",
    "db/_core/_boot_foundation.py",
    "db/_core/_boot_schema.py",
    "db/_core/_boot_vacuum.py",
    "db/_core/_boot_workflow.py",
    "db/_core/_conn.py",
    "db/_core/_errors.py",
    "db/_core/_init.py",
    "db/_core/_migrate.py",
    "db/_core/_observe.py",
    "db/_core/_paths.py",
    "db/_core/_time.py",
    "db/_agent.py",
    "db/_content.py",
    "db/_proposal.py",
    "db/_tags.py",
    "db/_staking.py",
    "db/_credits.py",
    "db/_collaborative.py",
    "db/_karma.py",
    "db/_text.py",
    "db/_health.py",
    "db/_invoices.py",
    "db/_aggregates.py",
    "db/_ci_usage.py",
    "db/_cooldown.py",
    "db/_comments.py",
    "db/_nudges.py",
    "db/_proposal_status.py",
    "db/_proposal_todos/__init__.py",
    "db/_proposal_todos/_claims.py",
    "db/_proposal_todos/_edits.py",
    "db/_proposal_todos/_mutations.py",
    "db/_proposal_todos/_reads.py",
    "db/_proposal_delegation.py",
    "db/_proposal_docket.py",
    "db/_claiming.py",
    "db/_guilds.py",
    "db/_guilds_grants.py",
    "db/_guilds_lending.py",
    "db/_guilds_money.py",
    "db/_guilds_treasury.py",
    "db/_guilds_views.py",
    "db/_guilds_plans.py",
    "db/_pr_vote.py",
    "db/_bug_reports.py",
    "db/_subscriptions.py",
    "logutil.py",
    "server/admin/__init__.py",
    "server/admin/_auth.py",
    "server/admin/_reports.py",
    "server/admin/_posts.py",
    "server/admin/_agents.py",
    "server/admin/_jobs.py",
    "server/admin/_workflows.py",
    "server/admin/_ci.py",
    "server/admin/_agentwake.py",
    "server/admin/_economy.py",
    "server/admin/_bugs.py",
    "rules_text.py",
    "moderation.py",
    "notifications.py",
    "search.py",
    "server/repo_search.py",
    "server/repo_helpers.py",
    "server/tool_directory.py",
    "server/poller/__init__.py",
    "server/poller/_outcome.py",
    "server/poller/_autolink.py",
    "server/poller/_batches.py",
    "server/poller/_vote.py",
    "server/poller/_wake.py",
    "server/poller/_broadcast.py",
    "server/ci_runner/__init__.py",
    "server/ci_runner/_slots.py",
    "server/ci_runner/_trees.py",
    "server/ci_runner/_sandbox.py",
    "server/ci_runner/_runs.py",
    "viewer/__init__.py",
    "viewer/_agents.py",
    "viewer/_citizens_helpers.py",
    "viewer/_feed_helpers.py",
    "viewer/_guilds.py",
    "viewer/_layout.py",
    "viewer/_pr_helpers.py",
    "viewer/_proposals.py",
    "viewer/_render_helpers.py",
    # viewer/_skills.py (proposal #857) is listed from birth rather than
    # retrofitted: a marker nobody scans is a comment, and #B137 is the
    # instance of a listed set that let an unlisted file pass CI. Its
    # one handler (the board read) carries an in-span domain marker and
    # no baseline entry, so the allowed count is 0.
    "viewer/_skills.py",
    "viewer/_staking_helpers.py",
    "viewer/_status.py",
    "viewer/_utils.py",
    "viewer/_events.py",
    "viewer/_api.py",
    "viewer/_bonds.py",
    # -- repo root (from #B137: the population the one-way check missed) --
    "config.py",
    "events.py",
    "reports.py",
    "server.py",
    # -- db (from #B137: the population the one-way check missed) --
    "db/__init__.py",
    "db/_bench_anchor.py",
    "db/_bench_history.py",
    "db/_bonds.py",
    "db/_bounty.py",
    "db/_designs.py",
    "db/_designs_admin.py",
    "db/_designs_cores.py",
    "db/_designs_discuss.py",
    "db/_designs_flow.py",
    "db/_designs_issues.py",
    "db/_designs_readers.py",
    "db/_drafts.py",
    "db/_economy.py",
    "db/_guilds_bonds.py",
    "db/_guilds_reputation.py",
    "db/_jobs.py",
    "db/_jobs_admin.py",
    "db/_jobs_subsidy.py",
    "db/_notes.py",
    "db/_polls.py",
    "db/_pr_rows.py",
    "db/_pr_state.py",
    "db/_programs.py",
    "db/_public_branch.py",
    "db/_review_findings.py",
    "db/_services.py",
    "db/_skills.py",
    "db/_store.py",
    "db/_threads.py",
    "db/_tool_inventory.py",
    "db/_tool_usage.py",
    "db/_transfer_tickets.py",
    "db/_workflow.py",
    "db/_workspace_claims.py",
    # -- db/_jobs_ops (from #B137: the population the one-way check missed) --
    "db/_jobs_ops/__init__.py",
    "db/_jobs_ops/_auto.py",
    "db/_jobs_ops/_board.py",
    "db/_jobs_ops/_create.py",
    "db/_jobs_ops/_detail.py",
    "db/_jobs_ops/_flow.py",
    "db/_jobs_ops/_helpers.py",
    # -- db/_proposal_todos (from #B137: the population the one-way check missed) --
    "db/_proposal_todos/_flags.py",
    # -- github (from #B137: the population the one-way check missed) --
    "github/_eol.py",
    "github/_workspaces.py",
    # -- server (from #B137: the population the one-way check missed) --
    "server/_merge_gate.py",
    "server/_transfer.py",
    "server/config_drift.py",
    "server/gzip_tunable.py",
    # -- server/admin (from #B137: the population the one-way check missed) --
    "server/admin/_designs.py",
    "server/admin/_guilds.py",
    "server/admin/_invoices.py",
    "server/admin/_notifications.py",
    "server/admin/_usage.py",
    # -- server/ci_runner (from #B137: the population the one-way check missed) --
    "server/ci_runner/_farm.py",
    # -- server/poller (from #B137: the population the one-way check missed) --
    "server/poller/_anchor.py",
    # -- server/tools (from #B137: the population the one-way check missed) --
    "server/tools/__init__.py",
    "server/tools/agent.py",
    "server/tools/designs.py",
    "server/tools/programs.py",
    # -- server/tools/repo (from #B137: the population the one-way check missed) --
    "server/tools/repo/_findings.py",
    "server/tools/repo/_public_branch.py",
    "server/tools/repo/_transfer.py",
    "server/tools/repo/_workspace.py",
    # -- viewer (from #B137: the population the one-way check missed) --
    "viewer/__main__.py",
    "viewer/_activity.py",
    "viewer/_analytics.py",
    "viewer/_bugs.py",
    "viewer/_cache.py",
    "viewer/_ci.py",
    "viewer/_designs.py",
    "viewer/_findings.py",
    "viewer/_governance.py",
    "viewer/_money.py",
    "viewer/_overview.py",
    "viewer/_posts.py",
    "viewer/_programs.py",
    "viewer/_prs.py",
    "viewer/_pulse.py",
    "viewer/_recent.py",
    "viewer/_records.py",
    "viewer/_reports.py",
    "viewer/_search.py",
    "viewer/_services.py",
    "viewer/_static.py",
)

MARKER = "domain:"

# Directories whose *.py files this ratchet governs, plus repo-root modules.
# tests/, deploy/ and ci_farm/ are deliberately out of scope - they are not
# production request paths - so the assertion below never demands they be
# listed. Changing this tuple widens what the gate claims to cover, so treat
# it as a policy edit and say so in the PR.
_SCANNED_ROOTS = ("db", "github", "server", "viewer")

# path -> why this production module is deliberately not scanned. Membership
# here is an explicit, reviewed decision; absence from BOTH this and
# FILE_LIST is the failure the assertion exists to catch.
_NOT_SCANNED: dict = {}


def discovered_modules() -> list:
    """Every production .py the gate claims to govern, derived from disk.

    Derived rather than listed on purpose: a hand-kept second list would
    drift exactly the way FILE_LIST did.
    """
    found = set()
    for _root in _SCANNED_ROOTS:
        found.update(
            p.relative_to(_ROOT).as_posix() for p in (_ROOT / _root).rglob("*.py")
        )
    found.update(p.relative_to(_ROOT).as_posix() for p in _ROOT.glob("*.py"))
    return sorted(found)


def completeness_failures() -> list:
    """The direction #B137 was missing: disk -> FILE_LIST.

    A module that exists, is in scope, and is in neither FILE_LIST nor
    _NOT_SCANNED is unscanned: its handlers are not counted, so the gate
    reports success over a population it never looked at.
    """
    listed = set(FILE_LIST)
    opted_out = set(_NOT_SCANNED)
    failures = []
    for rel in discovered_modules():
        if rel not in listed and rel not in opted_out:
            failures.append(
                f"{rel}: production module is in scope but in neither "
                "FILE_LIST nor _NOT_SCANNED - it is UNSCANNED, so an "
                "unmarked `except` here is invisible and CI is green. Add it "
                "to FILE_LIST (with a baseline entry if it carries debt) or "
                "record it in _NOT_SCANNED with a reason"
            )
    for rel in sorted(opted_out):
        if rel in listed:
            failures.append(
                f"{rel}: in both _NOT_SCANNED and FILE_LIST - keep exactly one"
            )
        elif not (_ROOT / rel).exists():
            failures.append(
                f"{rel}: _NOT_SCANNED entry matches no file on disk - remove it"
            )
    return failures


def _handler_lines(node: ast.ExceptHandler) -> set[int]:
    """The handler's own line span, minus lines owned by handlers nested
    inside it - so an inner block's marker cannot vouch for an outer one."""
    owned = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    for sub in ast.walk(node):
        if sub is not node and isinstance(sub, ast.ExceptHandler):
            owned -= set(range(sub.lineno, (sub.end_lineno or sub.lineno) + 1))
    return owned


def unmarked_handlers(text: str) -> int:
    """Count `except` handlers whose entire own span lacks the marker."""
    tree = ast.parse(text)
    lines = text.splitlines()
    spans = [
        (_h, _handler_lines(_h))
        for _h in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
    ]
    unmarked = 0
    for _node, line_ids in spans:
        segment = "\n".join(
            lines[i - 1] for i in sorted(line_ids) if 0 < i <= len(lines)
        )
        if MARKER not in segment:
            unmarked += 1
    return unmarked


def load_baseline() -> dict:
    return json.loads(_BASELINE.read_text(encoding="utf-8"))


def audit() -> list[str]:
    """Returns one human-readable failure per violation; empty when clean."""
    baseline = load_baseline()
    failures = completeness_failures()
    seen = set()
    for rel in FILE_LIST:
        path = _ROOT / rel
        if not path.exists():
            failures.append(f"{rel}: listed in FILE_LIST but missing on disk")
            continue
        seen.add(rel)
        allowed = int(baseline.get(rel, 0))
        have = unmarked_handlers(path.read_text(encoding="utf-8"))
        if have > allowed:
            failures.append(
                f"{rel}: {have} unmarked except handler(s) exceed baseline "
                f"{allowed} - add '# domain:<domain> - <why>' inline, or "
                "lower the entry in tests/exception_domain_baseline.json "
                "if this debt was retired elsewhere"
            )
    for rel in sorted(baseline):
        if rel not in seen:
            failures.append(
                f"baseline entry '{rel}' matches no scanned file - remove it"
            )
    return failures


def main() -> int:
    failures = audit()
    if failures:
        print("test_exception_domains: FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    baseline = load_baseline()
    total = sum(int(baseline.get(r, 0)) for r in FILE_LIST)
    marked_files = sum(1 for r in FILE_LIST if r in baseline)
    print(
        f"test_exception_domains: ok ({total} grandfathered unmarked "
        f"handlers across {marked_files} baselined files of "
        f"{len(FILE_LIST)} scanned; {len(discovered_modules())} production "
        f"modules discovered, {len(_NOT_SCANNED)} deliberately unscanned)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
