"""Regression suite for the legacy-table migrations in db/_core.py.

Born from the 2026-08-26 prod outage: the karma_spends CHECK-widen
created its scratch table with a bare autocommit DDL statement, so a
boot interrupted mid-swap left karma_spends_new behind and every later
init_db died on "table karma_spends_new already exists" - forum down.
These scenarios pin the fixed contract: each swap is transactional,
self-heals stray final-name/scratch tables left behind by a crash
mid-swap, preserves every migrated row, and is a clean no-op once
applied.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_migrations_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db  # noqa: E402


# The pre-Karma-Split shapes, populated with one representative row each.
# karma_spends deliberately carries ONLY the legacy kinds (no
# 'stake_lock'), which is what arms the widen guard.
_LEGACY_SCHEMA = """
CREATE TABLE proposal_bounties (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     INTEGER NOT NULL,
    staker_agent_id INTEGER,
    per_pr          INTEGER NOT NULL,
    max_prs         INTEGER NOT NULL,
    paid_count      INTEGER NOT NULL DEFAULT 0,
    locked_count    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    admin_funded    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE bounty_locks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bounty_id      INTEGER NOT NULL,
    pr_number      INTEGER NOT NULL,
    agent_id       INTEGER NOT NULL,
    amount         INTEGER NOT NULL,
    status         TEXT NOT NULL,
    karma_spend_id INTEGER,
    created_at     TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE bounty_rewards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bounty_id  INTEGER NOT NULL,
    pr_number  INTEGER NOT NULL,
    agent_id   INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE karma_spends (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('tag_create', 'tag_apply', 'bounty_lock')),
    amount   INTEGER NOT NULL CHECK (amount > 0),
    ref_id   INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT 'legacy'
);
INSERT INTO proposal_bounties (proposal_id, staker_agent_id, per_pr,
    max_prs, paid_count, locked_count, status)
    VALUES (1, 2, 3, 2, 1, 0, 'completed');
INSERT INTO bounty_locks (bounty_id, pr_number, agent_id, amount, status)
    VALUES (1, 5, 2, 3, 'paid');
INSERT INTO bounty_rewards (bounty_id, pr_number, agent_id, amount)
    VALUES (1, 5, 2, 3);
INSERT INTO karma_spends (agent_id, kind, amount, ref_id)
    VALUES (2, 'bounty_lock', 3, 1);
"""

# What a crash mid-swap leaves behind: final-name tables created but the
# old ones never dropped (rename swaps), plus the widen's scratch table
# - the exact wedge that took production down.
_STRAY_PARTIALS = """
CREATE TABLE proposal_stakes (id INTEGER PRIMARY KEY);
CREATE TABLE stake_locks (id INTEGER PRIMARY KEY);
CREATE TABLE stake_rewards (id INTEGER PRIMARY KEY);
CREATE TABLE karma_spends_new (id INTEGER PRIMARY KEY);
"""


def _replant(extra: str) -> None:
    path = Path(db.DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_SCHEMA + extra)
        conn.commit()
    finally:
        conn.close()


def _query(sql: str, *params):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _assert_migrated() -> None:
    names = {
        r[0] for r in _query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for gone in (
        "proposal_bounties", "bounty_locks", "bounty_rewards",
        "karma_spends_new",
    ):
        assert gone not in names, f"{gone} should be gone"
    for present in (
        "proposal_stakes", "stake_locks", "stake_rewards", "karma_spends",
    ):
        assert present in names, f"{present} missing"

    ddl = _query(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'karma_spends'"
    )[0][0] or ""
    assert "stake_lock" in ddl, "karma_spends widen did not run"

    rows = _query("SELECT agent_id, kind, amount, ref_id FROM karma_spends")
    assert rows == [(2, "bounty_lock", 3, 1)], f"ledger rows lost: {rows}"

    stakes = _query(
        "SELECT proposal_id, staker_agent_id, per_pr, max_prs,"
        " currency, status FROM proposal_stakes"
    )
    assert stakes == [(1, 2, 3, 2, "karma", "completed")], stakes

    locks = _query(
        "SELECT stake_id, pr_number, agent_id, amount, status FROM stake_locks"
    )
    assert locks == [(1, 5, 2, 3, "paid")], locks

    rewards = _query("SELECT stake_id, pr_number, amount FROM stake_rewards")
    assert rewards == [(1, 5, 3)], rewards

    indexes = {
        r[0] for r in _query(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    for idx in (
        "idx_proposal_stakes_proposal", "idx_proposal_stakes_staker",
        "idx_stake_locks_pr", "idx_stake_rewards_agent",
        "idx_karma_spends_agent",
    ):
        assert idx in indexes, f"index {idx} missing"


def test_full_upgrade_from_clean_legacy():
    """Legacy database upgrades end-to-end with every row intact."""
    _replant("")
    db.init_db()
    _assert_migrated()


def test_second_boot_is_noop():
    """Already-migrated database boots again without error or duplication."""
    db.init_db()
    _assert_migrated()
    assert _query("SELECT COUNT(*) FROM karma_spends")[0][0] == 1


def test_wedge_from_interrupted_boot_self_heals():
    """THE PROD OUTAGE: scratch/final-name strays from a crashed swap are
    dropped and redone instead of dying on 'table already exists'."""
    _replant(_STRAY_PARTIALS)
    db.init_db()
    _assert_migrated()


if __name__ == "__main__":
    test_full_upgrade_from_clean_legacy()
    print("full upgrade from clean legacy: ok")
    test_second_boot_is_noop()
    print("second boot noop: ok")
    test_wedge_from_interrupted_boot_self_heals()
    print("wedge self-heal (prod outage repro): ok")
    print("test_migrations: all scenarios passed")
