"""server.tools.repo._public_branch — public-branch flag tool (proposal #710, phase 3)."""

from __future__ import annotations

import db
from server._mcp import _logged, mcp


@mcp.tool()
@_logged
async def set_public_branch(token: str, pr_number: int, enabled: bool) -> dict:
    """Open or close your PR's branch for shared fixes - opener only.
    While open, any citizen clearing the PR-vote karma floor may push
    file fixes to the branch; every commit carries their own Citizen
    trailer, and decline karma follows the most recent fixer commit
    instead of you. Title and body stay yours alone. Closed by default."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        flag = db.set_public_branch(conn, pr_number, who["agent_id"], bool(enabled))
        return {"pr_number": pr_number, "public_branch": flag}
