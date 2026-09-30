# PR Operations - repo_update_pr, etc.
import sqlite3
from ..github import update_pr as github_update_pr
from ._public_branch import record_pr_fixer_files

def repo_update_pr(token: str, number: int, files: list[dict], title: str = None, body: str = None, dry_run: bool = False, expect_shas: dict = None) -> dict:
    """Update a PR with files, title, body changes."""
    pass
