def list_stake_locks(stake_id: int) -> list[dict]:
    """All locks for a single stake, newest first. For the /staking
    locked-stakes drill-down."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, pr_number, agent_id, amount, status,"
            " karma_spend_id, created_at"
            " FROM stake_locks"
            " WHERE stake_id=?"
            " ORDER BY id DESC",
            (stake_id,),
        ).fetchall()
    return [dict(r) for r in rows]
