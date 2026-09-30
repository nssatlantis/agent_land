# Findings Tools
import sqlite3
from ._public_branch import pr_fixer_ids_for_paths

def finding_mark_resolved(token: str, finding_id: int, note: str) -> dict:
    """Mark a finding as resolved (fix shipped)."""
    pass

def finding_dispute(token: str, finding_id: int, note: str) -> dict:
    """Dispute a finding."""
    pass
