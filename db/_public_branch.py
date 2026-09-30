# Public branch shared-fix lane
import sqlite3
from ._core import get_conn

def set_public_branch(conn: sqlite3.Connection, pr_number: int, enabled: bool) -> None:
    """Enable or disable the public-branch flag for a PR.
    When disabled, clears the fixer roster and file tracking."""
    c = conn.cursor()
    c.execute("""
        INSERT INTO pr_public_branch (pr_number, enabled)
        VALUES (?, ?)
        ON CONFLICT(pr_number) DO UPDATE SET enabled = excluded.enabled
    """, (pr_number, 1 if enabled else 0))
    if not enabled:
        c.execute("DELETE FROM pr_fixers WHERE pr_number = ?", (pr_number,))
        c.execute("DELETE FROM pr_fixer_files WHERE pr_number = ?", (pr_number,))

def is_public_branch(conn: sqlite3.Connection, pr_number: int) -> bool:
    """Check if a PR's branch is open for shared fixes."""
    c = conn.cursor()
    row = c.execute("SELECT enabled FROM pr_public_branch WHERE pr_number = ?", (pr_number,)).fetchone()
    return row is not None and row[0] == 1

def record_pr_fixer_files(conn: sqlite3.Connection, pr_number: int, agent_id: int, paths: list[str]) -> None:
    """Record the files a fixer has changed on a public branch."""
    c = conn.cursor()
    c.execute("""
        INSERT INTO pr_fixers (pr_number, agent_id)
        VALUES (?, ?)
        ON CONFLICT(pr_number, agent_id) DO NOTHING
    """, (pr_number, agent_id))
    for path in paths:
        c.execute("""
            INSERT INTO pr_fixer_files (pr_number, agent_id, path)
            VALUES (?, ?, ?)
            ON CONFLICT(pr_number, agent_id, path) DO NOTHING
        """, (pr_number, agent_id, path))

def pr_fixer_ids_for_paths(conn: sqlite3.Connection, pr_number: int, paths: list[str]) -> list[int]:
    """Get fixer IDs who have changed any of the given paths."""
    if not paths:
        return []
    c = conn.cursor()
    placeholders = ','.join('?' * len(paths))
    query = f"""
        SELECT DISTINCT agent_id FROM pr_fixer_files
        WHERE pr_number = ? AND path IN ({placeholders})
    """
    rows = c.execute(query, [pr_number] + paths).fetchall()
    return [row[0] for row in rows]
