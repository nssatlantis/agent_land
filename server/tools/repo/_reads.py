# Repository Reads
import sqlite3

def repo_get_pr(conn: sqlite3.Connection, pr_number: int, token: str = None) -> dict:
    """Get PR details including fixers."""
    from ..pr_views import repo_get_pr as pr_views_get_pr
    return pr_views_get_pr(conn, pr_number, token)

def repo_my_prs(conn: sqlite3.Connection, token: str) -> dict:
    """Get current user's PRs."""
    pass
