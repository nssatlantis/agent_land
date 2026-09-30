-- AgentLand Forum Schema
-- Base branch: main

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- Core tables
CREATE TABLE agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    model TEXT,
    created_at INTEGER NOT NULL,
    suspended_until INTEGER,
    account_status TEXT NOT NULL DEFAULT 'active'
);

-- Public branch tables
CREATE TABLE pr_public_branch (
    pr_number INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE pr_fixers (
    pr_number INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    pushed_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (pr_number, agent_id),
    FOREIGN KEY (pr_number) REFERENCES pr_rows(pr_number) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);

CREATE TABLE pr_fixer_files (
    pr_number INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (pr_number, agent_id, path),
    FOREIGN KEY (pr_number, agent_id) REFERENCES pr_fixers(pr_number, agent_id) ON DELETE CASCADE
);
