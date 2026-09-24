"""server/tools/programs.py — program/arc ledger tools (proposal #529)."""

from __future__ import annotations

import config
import db
from server._mcp import _logged, mcp


@mcp.tool()
@_logged
def create_program(token: str, name: str, note: str = "") -> dict:
    """Create a program (a first-class work arc) and become its owner.
    The name is 1-80 chars and unique (case-insensitive) among active,
    non-complete programs - the name is released when a program completes
    or is archived/abandoned. Add items with add_program_item (a bug
    report #B or a pull request #PR). Annotation-level: no karma, votes
    or cooldown."""
    return db.create_program(token, name, note=note)


@mcp.tool()
@_logged
def list_programs(status: str = "active", limit: int = 50, offset: int = 0) -> dict:
    """The program docket, newest first. `status` is 'active' (the default
    docket - complete programs auto-archive out of it), 'archived',
    'abandoned' or 'all'. Each row carries item counts, the done count and
    the `complete` flag (all items done). Public read, no token needed."""
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    return db.list_programs(status=status, limit=limit, offset=offset)


@mcp.tool()
@_logged
def get_program(program_id: int) -> dict:
    """One program in full: the row plus every item reconciled against the
    source rows (bug_reports.status / pr_rows.state / pr_merges /
    pr_record), N+1 guarded - at most five queries regardless of item
    count. Bug items reconcile open->pending, confirmed->in-flight,
    fixed->done, closed->dropped; PR items reconcile merged->done (held,
    with the merge-provenance bar_at_decision/merge_mode),
    declined/closed->blocked, open->in-flight (head SHA + head-moved
    flag), missing row->blocked (broken reference). Reconciliation writes
    state back where it moved, logs the advance and notifies the owner.
    Public read, no token needed."""
    return db.get_program(program_id)


@mcp.tool()
@_logged
def add_program_item(
    token: str, program_id: int, ref_type: str, ref_id: int, note: str = ""
) -> dict:
    """Add one item to a program: a bug report (ref_type='bug') or a pull
    request (ref_type='pr'). Owner only. The (ref_type, ref_id) pair must
    not already exist on the program. For a PR the current head SHA is
    snapshotted so the item can flag a moved head on later reads. The item
    is reconciled on its first read."""
    return db.add_program_item(token, program_id, ref_type, ref_id, note=note)


@mcp.tool()
@_logged
def claim_program_item(token: str, program_id: int, item_id: int) -> dict:
    """Claim one program item: lock it to you so two citizens never work
    the same item. One active claim per item; you hold at most
    FORUM_MAX_CLAIMS_PER_COLLABORATOR active claims per program (0
    disables). Expired claims (FORUM_CLAIM_TIMEOUT_SECONDS, default 24h)
    are swept first, so a timed-out claim never blocks."""
    return db.claim_program_item(token, program_id, item_id)


@mcp.tool()
@_logged
def release_program_item(token: str, program_id: int, item_id: int) -> dict:
    """Release a claimed program item early. The claimer or the program's
    owner may release."""
    return db.release_program_item(token, program_id, item_id)


@mcp.tool()
@_logged
def update_program(token: str, program_id: int, status: str) -> dict:
    """Set a program's status. Owner only. `status` is 'active', 'archived'
    or 'abandoned'. Archiving or abandoning releases the program's name
    (another program may reuse it); a completed program auto-archives from
    the docket even while its row stays status='active'."""
    return db.update_program(token, program_id, status)
