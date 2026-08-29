def test_list_stake_locks():
    """list_stake_locks returns all locks for a stake, ordered by id DESC."""
    # Use existing data created by main(): find a stake that has a lock
    with db._conn() as conn:
        row = conn.execute(
            "SELECT s.id FROM proposal_stakes s "
            "JOIN stake_locks sl ON sl.stake_id = s.id "
            "LIMIT 1"
        ).fetchone()
    assert row is not None, "main() must have created a locked stake"
    sid = row["id"]
    locks = list_stake_locks(sid)
    assert len(locks) >= 1, f"expected at least 1 lock, got {len(locks)}"
    # Order: newest first (id DESC)
    ids = [l["id"] for l in locks]
    assert ids == sorted(ids, reverse=True), "should be id DESC"
    # Non-existent stake returns empty
    assert list_stake_locks(99999) == []
    print("  list_stake_locks ok")