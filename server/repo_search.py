"""Local filesystem search over the repo's checked-out working tree.

Searches only the record, code, schema, CI config and deploy scripts
(allowlist by extension + named files). The database, .env secrets,
dependency manifests and binaries are excluded by construction.

Read-only, no GitHub API calls - the same tree the viewer's record
routes trust.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import config
import db
from github import RepoError
from github._core import _validate_ref

SEARCH_EXTENSIONS = {".py", ".md", ".sql", ".sh", ".yml", ".yaml"}
SEARCH_SPECIAL_FILES = {".env.example", ".gitignore", "CODEOWNERS"}
_SEARCH_SKIP_DIRS = {".git", "__pycache__"}
# git grep emits "<rev>:<path>:<line>:<text>" - path and text may both
# hold ":" (odd names, type hints, URLs), so the line number anchors the
# split: greedy path, digits, rest is text.
_GREP_LINE_RE = re.compile(r"^(?P<path>.+):(?P<lineno>\d+):(?P<text>.*)$")


def _searchable_file(path: Path) -> bool:
    """A file is searchable only if the extension allowlist or the named
    specials list says so - the DB, .env, .txt manifests and binaries never
    are."""
    return path.name in SEARCH_SPECIAL_FILES or path.suffix.lower() in SEARCH_EXTENSIONS


def _trim_search_line(line: str) -> str:
    """Cap a matched line so a single huge line can't bloat a result."""
    ellipsis = "..."
    if len(line) <= config.REPO_SEARCH_LINE_TRIM:
        return line
    return line[: config.REPO_SEARCH_LINE_TRIM - len(ellipsis)] + ellipsis


def _resolve_ref_commit(ref: str, repo_dir: str | None = None) -> tuple[str, str]:
    """Resolve ref to (winning candidate, commit SHA) via local git.

    Tries `ref` then `origin/<ref>`; the winning candidate is returned so
    callers can echo provenance (`origin/<ref>` when fallback resolved).
    """
    if repo_dir is None:
        repo_dir = str(Path(db.REPO_DIR).resolve())
    else:
        repo_dir = str(Path(repo_dir).resolve())
    for candidate in (ref, f"origin/{ref}"):
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    repo_dir,
                    "rev-parse",
                    "--verify",
                    f"{candidate}^{{commit}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:  # domain: degrade-silently - rev-parse timeout retries next candidate (origin/)
            continue
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            if sha:
                return candidate, sha
    raise RepoError(
        f"unknown ref {ref!r} - no such branch, tag or commit in the local checkout."
    )


def _search_with_ref(
    query: str,
    max_results: int,
    ref: str,
    repo_dir: str | None = None,
    allowlist: bool = True,
    budget_bytes: int | None = None,
) -> dict:
    """Search the committed tree at `ref` via `git grep` — no checkout, no API.

    `repo_dir` roots the search (defaults to the server checkout, so
    `repo_search` is unaffected); `allowlist=False` searches every
    tracked blob regardless of extension (the workspace file universe),
    while True keeps the record/code allowlist. `budget_bytes` caps the
    raw grep output held before parsing (defaults to the transfer cap):
    response caps bound the reply, but only this budget bounds the
    subprocess — an over-budget match refuses instead of bloating the
    worker. The winning ref candidate rides `ref` in the reply
    (`origin/<ref>` when fallback resolved, so provenance is auditable).
    """
    ref = _validate_ref(ref)
    resolved, commit = _resolve_ref_commit(ref, repo_dir)
    if repo_dir is None:
        repo_dir = str(Path(db.REPO_DIR).resolve())
    else:
        repo_dir = str(Path(repo_dir).resolve())
    if budget_bytes is None:
        from github._workspaces import _transfer_file_cap_bytes

        budget_bytes = _transfer_file_cap_bytes()
    try:
        budget = int(budget_bytes)
    except (TypeError, ValueError):
        budget = 1 << 20
    if budget <= 0:
        budget = 1 << 20
    # Fixed-string, case-insensitive, no binary, line numbers.
    # `git grep -n -i -F -I` at a rev outputs "<rev>:<path>:<line>:<text>".
    try:
        proc = subprocess.Popen(
            [
                "git",
                "-C",
                repo_dir,
                "grep",
                "-n",
                "-i",
                "-F",
                "-I",
                "-e",
                query,
                commit,
                "--",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:  # domain: fail-loudly - no git surfaces
        raise RepoError("git is not installed or not in PATH") from None
    except OSError as exc:  # domain: fail-loudly - unspawnable git surfaces
        raise RepoError(f"repo_search could not start git grep: {exc}") from None
    assert proc.stdout is not None and proc.stderr is not None
    # Bounded queue: backpressure into git's pipe, so the held output can
    # never exceed the budget plus a few chunks no matter how the reader
    # thread races ahead; the finally-block drains it before reaping.
    pieces: queue.Queue = queue.Queue(maxsize=8)

    def _drain() -> None:
        try:
            while True:
                part = proc.stdout.read(65536)
                if not part:
                    break
                pieces.put(part)
        except Exception as exc:  # domain: fail-loudly - reader errors surface
            pieces.put(exc)
        finally:
            pieces.put(None)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + 30
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise RepoError("repo_search timed out while searching the branch.")
            try:
                item = pieces.get(timeout=wait)
            except queue.Empty:
                raise RepoError(
                    "repo_search timed out while searching the branch."
                ) from None
            if item is None:
                break
            if isinstance(item, Exception):
                raise RepoError(f"repo_search failed on ref {ref!r}: {item}") from None
            total += len(item)
            if total > budget:
                raise RepoError(
                    f"search at ref {resolved!r} produced over the "
                    f"{budget / (1 << 20):g}MB output cap - narrow the query."
                )
            chunks.append(item)
        text_out = b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        try:
            err_text = (proc.stderr.read() or b"").decode("utf-8", errors="replace")
        except OSError:
            err_text = ""
        proc.stdout.close()
        proc.stderr.close()
        try:
            while True:
                pieces.get_nowait()
        except queue.Empty:
            pass
        reader.join(timeout=5)
    if proc.returncode not in (0, 1):
        # 1 = no matches (not an error), 128 = rev not found, else error
        raise RepoError(f"repo_search failed on ref {ref!r}: {err_text.strip()[:300]}")
    if proc.returncode == 1 or not text_out:
        return {"query": query, "matches": [], "ref": resolved}
    try:
        per_file = int(config.REPO_SEARCH_MAX_PER_FILE)
    except (
        Exception
    ):  # domain: degrade-silently - bad knob falls back, like the live search path
        per_file = 50
    # Parse git grep output: "<commit>:<path>:<line>:<text>"
    by_file: dict[str, list[dict]] = {}
    order: list[str] = []
    for raw_line in text_out.splitlines():
        # Split rev prefix off: first colon separates rev from path.
        try:
            _, rest = raw_line.split(":", 1)
        except ValueError:  # domain: degrade-silently - malformed grep line skipped
            continue
        match = _GREP_LINE_RE.match(rest)
        if match is None:  # domain: degrade-silently - malformed path:line:text skipped
            continue
        path_str, lineno_str, text = match.group("path", "lineno", "text")
        try:
            lineno = int(lineno_str)
        except ValueError:  # domain: degrade-silently - non-numeric line number skipped
            continue
        # Allowlist — same as walk path (skipped for workspace
        # file-universe searches, where every tracked blob is fair game).
        if allowlist:
            p = Path(path_str)
            if (
                p.name not in SEARCH_SPECIAL_FILES
                and p.suffix.lower() not in SEARCH_EXTENSIONS
            ):
                continue
        # Per-file cap
        lst = by_file.get(path_str)
        if lst is None:
            if len(order) >= max_results:
                continue
            lst = []
            by_file[path_str] = lst
            order.append(path_str)
        if len(lst) >= per_file:
            continue
        lst.append({"line_number": lineno, "text": _trim_search_line(text)})
    results = [{"path": p, "matches": by_file[p]} for p in order if by_file[p]]
    return {"query": query, "matches": results, "ref": ref}


def search_files(
    query: str,
    max_results: int = config.REPO_SEARCH_DEFAULT_MAX_FILES,
    root=None,
    ref: str | None = None,
) -> dict:
    """Search the repo's checked-out working tree for a case-insensitive
    substring, restricted to the record and code files (SEARCH_EXTENSIONS +
    SEARCH_SPECIAL_FILES) so the database, secrets and manifests are never
    read. Returns {query, matches: [{path, matches: [{line_number, text}]}]}
    with paths relative to the repo root, bounded to max_results files (each
    capped at _SEARCH_MAX_PER_FILE lines). `root` is only for tests; it
    defaults to the repository checkout. `ref` (optional) names the git ref
    to search — a branch, tag or commit SHA; when given, the committed tree
    at that ref is searched via `git grep` instead of the working tree, so
    a branch can be audited before it is merged. The response echoes the
    ref it searched when provided."""
    query = (query or "").strip()
    if not query:
        raise RepoError("repo_search needs a non-empty query.")
    if len(query) < 2:
        raise RepoError("repo_search query too short - use at least 2 characters.")
    if len(query) > config.MAX_QUERY_LENGTH:
        raise RepoError(
            f"repo_search query too long - keep it under {config.MAX_QUERY_LENGTH} characters."
        )
    max_results = max(1, min(int(max_results), config.REPO_SEARCH_MAX_FILES))
    if ref is not None:
        # Branch-aware path — no root, no working-tree walk.
        return _search_with_ref(query, max_results, ref)
    root = Path(root).resolve() if root else Path(db.REPO_DIR).resolve()
    needle = query.lower()
    db_path = Path(db.DB_PATH).resolve()
    data_dir = Path(db.DATA_DIR).resolve()
    skip_data_dir = data_dir.is_relative_to(root)
    results: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SEARCH_SKIP_DIRS)
        current = Path(dirpath).resolve()
        for name in sorted(filenames):
            full = current / name
            if not _searchable_file(full):
                continue
            if full == db_path or (skip_data_dir and full.is_relative_to(data_dir)):
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits = []
            for lineno, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(
                        {"line_number": lineno, "text": _trim_search_line(line)}
                    )
                    if len(hits) >= config.REPO_SEARCH_MAX_PER_FILE:
                        break
            if hits:
                results.append(
                    {"path": full.relative_to(root).as_posix(), "matches": hits}
                )
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    return {"query": query, "matches": results}
