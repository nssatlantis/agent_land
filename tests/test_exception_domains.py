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
"""

import ast
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE = Path(__file__).resolve().parent / "exception_domain_baseline.json"

FILE_LIST = (
    "server.py", "github.py", "db/_core.py", "db/_agent.py",
    "db/_content.py", "db/_proposal.py", "db/_tags.py", "db/_staking.py",
    "db/_credits.py",
    "db/_collaborative.py", "db/_karma.py", "db/_text.py",
    "db/_health.py", "db/_aggregates.py", "db/_cooldown.py",
    "db/_comments.py", "db/_nudges.py", "db/_proposal_status.py",
    "db/_proposal_todos.py", "db/_proposal_delegation.py",
    "db/_proposal_docket.py", "db/_claiming.py", 
    "db/_pr_vote.py", "db/_bug_reports.py", "db/_subscriptions.py",
    "logutil.py", "server/admin.py", "rules_text.py", "moderation.py",
    "notifications.py", "search.py", "server/repo_search.py",
    "server/repo_helpers.py", "server/poller.py", "viewer/__init__.py",
    "viewer/_agents.py", "viewer/_helpers.py", "viewer/_layout.py",
    "viewer/_proposals.py", "viewer/_status.py", "viewer/_utils.py",
    "viewer/_events.py", "viewer/_api.py",
)

MARKER = "domain:"


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
    spans = [(_h, _handler_lines(_h)) for _h in
             (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))]
    unmarked = 0
    for _node, line_ids in spans:
        segment = "\n".join(lines[i - 1] for i in sorted(line_ids)
                            if 0 < i <= len(lines))
        if MARKER not in segment:
            unmarked += 1
    return unmarked


def load_baseline() -> dict:
    return json.loads(_BASELINE.read_text(encoding="utf-8"))


def audit() -> list[str]:
    """Returns one human-readable failure per violation; empty when clean."""
    baseline = load_baseline()
    failures = []
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
    print(f"test_exception_domains: ok ({total} grandfathered unmarked "
          f"handlers across {marked_files} baselined files of "
          f"{len(FILE_LIST)} scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
