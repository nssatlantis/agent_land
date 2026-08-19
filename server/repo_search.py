"""Local filesystem search over the repo's checked-out working tree.

Searches only the record, code, schema, CI config and deploy scripts
(allowlist by extension + named files). The database, .env secrets,
dependency manifests and binaries are excluded by construction.

Read-only, no GitHub API calls - the same tree the viewer's record
routes trust.
"""

from __future__ import annotations

import os
from pathlib import Path

import config
import db
from github import RepoError

SEARCH_EXTENSIONS = {".py", ".md", ".sql", ".sh", ".yml", ".yaml"}
SEARCH_SPECIAL_FILES = {".env.example", ".gitignore", "CODEOWNERS"}
_SEARCH_SKIP_DIRS = {".git", "__pycache__"}
_SEARCH_MAX_PER_FILE = config.REPO_SEARCH_MAX_PER_FILE
_SEARCH_MAX_FILES = config.REPO_SEARCH_MAX_FILES
_SEARCH_LINE_TRIM = config.REPO_SEARCH_LINE_TRIM


def _searchable_file(path: Path) -> bool:
    """A file is searchable only if the extension allowlist or the named
    specials list says so - the DB, .env, .txt manifests and binaries never
    are."""
    return path.name in SEARCH_SPECIAL_FILES or path.suffix.lower() in SEARCH_EXTENSIONS


def _trim_search_line(line: str) -> str:
    """Cap a matched line so a single huge line can't bloat a result."""
    ellipsis = "..."
    if len(line) <= _SEARCH_LINE_TRIM:
        return line
    return line[: _SEARCH_LINE_TRIM - len(ellipsis)] + ellipsis


def search_files(query: str, max_results: int = config.REPO_SEARCH_DEFAULT_MAX_FILES, root=None) -> dict:
    """Search the repo's checked-out working tree for a case-insensitive
    substring, restricted to the record and code files (SEARCH_EXTENSIONS +
    SEARCH_SPECIAL_FILES) so the database, secrets and manifests are never
    read. Returns {query, matches: [{path, matches: [{line_number, text}]}]}
    with paths relative to the repo root, bounded to max_results files (each
    capped at _SEARCH_MAX_PER_FILE lines). `root` is only for tests; it
    defaults to the repository checkout."""
    query = (query or "").strip()
    if not query:
        raise RepoError("repo_search needs a non-empty query.")
    if len(query) < 2:
        raise RepoError("repo_search query too short - use at least 2 characters.")
    if len(query) > config.MAX_QUERY_LENGTH:
        raise RepoError(
            f"repo_search query too long - keep it under {config.MAX_QUERY_LENGTH} characters."
        )
    max_results = max(1, min(int(max_results), _SEARCH_MAX_FILES))
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
                    hits.append({"line_number": lineno, "text": _trim_search_line(line)})
                    if len(hits) >= _SEARCH_MAX_PER_FILE:
                        break
            if hits:
                results.append({"path": full.relative_to(root).as_posix(), "matches": hits})
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    return {"query": query, "matches": results}
