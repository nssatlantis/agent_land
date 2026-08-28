"""PR-diff shrink-floor ratchet (proposal #229, part 2 of file-gutted-on-push).

Catches the second shape of the "file-gutted-on-push" failure class that the
facade-export tests (#431 db, #437 server/) cannot see: a tracked source file
whose line-mass vanishes in a PR with no compensating added/renamed file.

  Poster child: PR #423 -- schema.sql arrived at +3 / -933 (almost the whole
schema vanished). schema.sql has no import facade, so nothing imported it and
CI stayed green. Detected only by human eyes.

How it works
------------
Runs natively in CI against the current checkout. It diffs the working tree
against its merge-base with origin/main (i.e. the PR's own changes) using
`git diff --find-renames -M --numstat`, then for every tracked source file
(.py / .sql / .md) that lost lines:
  * a rename is EXEMPT (git -M detected the content moved),
  * a shrink whose lost lines reappear in a same-PR added file is EXEMPT
    (refactor/split; PR #434 server.py -> server/ is the canonical example),
  * otherwise, if the file shrank by > SHRINK_FRACTION (0.5) of its original
    line count AND lost at least MIN_DELETED lines, the PR FAILS.
Whole-file removals are not failed (they are visible in the diff for review).
Genuinely retired files may carry `# retired` or `# shrink-ratchet-exempt` to
opt out of the failure.

On plain origin/main the merge-base diff is empty, so the test passes
(becomes a no-op); it only bites on PR merges. If git is unavailable the test
skips rather than errors.
"""

from __future__ import annotations

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCE_EXTS = (".py", ".sql", ".md")
SHRINK_FRACTION = 0.5
MIN_DELETED = 50


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _merge_base_diff() -> list[str]:
    base = _git("merge-base", "HEAD", "origin/main")
    if not base:
        return []
    base = base.strip().split("\n", 1)[0]
    if not base:
        return []
    numstat = _git("diff", "--find-renames", "-M", "--numstat", base, "HEAD")
    if numstat is None:
        return []
    return [ln for ln in numstat.splitlines() if ln.strip()]


def _parse_numstat(lines: list[str]):
    added_files: dict[str, int] = {}
    renamed: set[str] = set()
    shrinks: list[tuple[str, int, int]] = []
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        added_s, deleted_s, path = parts[0], parts[1], parts[2]
        added = int(added_s) if added_s not in ("-",) else 0
        deleted = int(deleted_s) if deleted_s not in ("-",) else 0
        if " => " in path:
            new = path.split(" => ", 1)[1].strip()
            renamed.add(new)
            added_files[new] = added
            continue
        if path.endswith(SOURCE_EXTS):
            added_files[path] = added
            if deleted > 0:
                shrinks.append((path, added, deleted))
    return added_files, renamed, shrinks


def _current_lines(path: str) -> int:
    full = os.path.join(REPO_ROOT, path)
    try:
        with open(full, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _has_opt_out_marker(path: str) -> bool:
    full = os.path.join(REPO_ROOT, path)
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            head = "".join(next(fh, "") for _ in range(5))
    except OSError:
        return False
    return "# retired" in head or "# shrink-ratchet-exempt" in head


def test_pr_diff_shrink_floor():
    lines = _merge_base_diff()
    if not lines:
        return
    added_files, renamed, shrinks = _parse_numstat(lines)
    failures = []
    for path, added, deleted in shrinks:
        current = _current_lines(path)
        if current == 0:
            continue
        original = current + deleted - added
        if original <= 0:
            continue
        frac = deleted / original
        if frac <= SHRINK_FRACTION:
            continue
        if deleted < MIN_DELETED:
            continue
        if path in renamed:
            continue
        moved = any(a >= deleted * 0.8 for p, a in added_files.items() if p != path)
        if moved:
            continue
        if _has_opt_out_marker(path):
            continue
        failures.append((path, original, current, added, deleted, round(frac, 2)))
    assert not failures, _format_failures(failures)


def _format_failures(failures):
    msg = [
        "PR-diff shrink-floor ratchet: tracked file(s) lost >50% of their "
        "lines with no move (file-gutted-on-push, part 2):\n"
    ]
    for path, original, current, added, deleted, frac in failures:
        msg.append(
            f"  {path}: ~{original} -> {current} lines "
            f"(+{added}/-{deleted}, {int(frac * 100)}% shrunk)\n"
        )
    msg.append(
        "If this is a legitimate retire/refactor, add `# retired` (or "
        "`# shrink-ratchet-exempt`) to the file, or split into a renamed/added file."
    )
    return "".join(msg)
