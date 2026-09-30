# PR Views - read surfaces for PR data
import sqlite3
from .github import get_pr as github_get_pr

def repo_get_pr(conn: sqlite3.Connection, pr_number: int, token: str = None) -> dict:
    """Get PR with full details including public_branch flag and fixers."""
    pr = github_get_pr(pr_number, token)
    if not pr:
        return None
    
    c = conn.cursor()
    row = c.execute("SELECT enabled FROM pr_public_branch WHERE pr_number = ?", (pr_number,)).fetchone()
    pr['public_branch'] = row is not None and row[0] == 1
    
    fixers = c.execute("""
        SELECT pf.agent_id, a.name, pf.pushed_at
        FROM pr_fixers pf
        JOIN agents a ON a.id = pf.agent_id
        WHERE pf.pr_number = ?
        ORDER BY pf.pushed_at
    """, (pr_number,)).fetchall()
    pr['pr_fixers'] = [
        {'agent_id': f[0], 'name': f[1], 'pushed_at': f[2]}
        for f in fixers
    ]
    
    return pr

def repo_my_prs(conn: sqlite3.Connection, token: str) -> dict:
    """Get current user's PRs with fixers info."""
    pass
