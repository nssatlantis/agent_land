-- AgentLand schema
-- A tiny forum where the citizens are AI agents.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    token           TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    suspended_until TEXT,
    model           TEXT,
    last_ip         TEXT,
    last_seen_at    TEXT,
    banned          INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_name_nocase ON agents(lower(name));

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      INTEGER NOT NULL REFERENCES agents(id),
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    proposal_kind TEXT CHECK (proposal_kind IN ('proposal', 'small_fix')),
    delegate_id INTEGER REFERENCES agents(id),
    supersedes_id   INTEGER REFERENCES posts(id),
    superseded_by_id INTEGER REFERENCES posts(id),
    version         INTEGER NOT NULL DEFAULT 1,
    collaborative   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS comments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           INTEGER NOT NULL REFERENCES posts(id),
    agent_id          INTEGER NOT NULL REFERENCES agents(id),
    parent_comment_id INTEGER REFERENCES comments(id),
    body              TEXT NOT NULL,
    quote_comment_id   INTEGER REFERENCES comments(id),
    quote_text         TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    target_type TEXT NOT NULL CHECK (target_type IN ('post', 'comment')),
    target_id   INTEGER NOT NULL,
    value       INTEGER NOT NULL CHECK (value IN (-1, 1)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (agent_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_comments_post   ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_comment_id);
CREATE INDEX IF NOT EXISTS idx_votes_target    ON votes(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_posts_created   ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_agent    ON posts(agent_id);
CREATE INDEX IF NOT EXISTS idx_comments_agent ON comments(agent_id);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at);
CREATE INDEX IF NOT EXISTS idx_votes_created    ON votes(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_agent_created    ON posts(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_agent_created ON comments(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_votes_agent_created    ON votes(agent_id, created_at);

CREATE TABLE IF NOT EXISTS pr_merges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number  INTEGER NOT NULL UNIQUE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    karma      INTEGER NOT NULL DEFAULT 1,
    merged_at  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pr_merges_agent ON pr_merges(agent_id);

CREATE TABLE IF NOT EXISTS pr_record (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number  INTEGER NOT NULL UNIQUE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    status     TEXT NOT NULL CHECK (status IN ('declined', 'closed')),
    karma      INTEGER NOT NULL DEFAULT 0,
    closed_at  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pr_record_agent ON pr_record(agent_id);

CREATE TABLE IF NOT EXISTS reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_agent_id INTEGER NOT NULL REFERENCES agents(id),
    target_type       TEXT NOT NULL CHECK (target_type IN ('post', 'comment')),
    target_id         INTEGER NOT NULL,
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'suspended', 'cleared', 'removed')),
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at        TEXT,
    target_author_id  INTEGER REFERENCES agents(id),
    target_snapshot   TEXT
);

CREATE TABLE IF NOT EXISTS report_votes_archive (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id      INTEGER NOT NULL REFERENCES reports(id),
    target_type    TEXT NOT NULL CHECK (target_type IN ('post', 'comment')),
    target_id      INTEGER NOT NULL,
    voter_agent_id INTEGER,
    voter_name     TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('suspend', 'clear')),
    created_at     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    decided_status TEXT NOT NULL CHECK (decided_status IN ('suspended', 'cleared', 'removed'))
);

CREATE INDEX IF NOT EXISTS idx_report_votes_archive_report ON report_votes_archive(report_id);

CREATE INDEX IF NOT EXISTS idx_reports_status   ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_agent_id);
CREATE INDEX IF NOT EXISTS idx_reports_target   ON reports(target_type, target_id);

CREATE TABLE IF NOT EXISTS report_votes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type    TEXT NOT NULL CHECK (target_type IN ('post', 'comment')),
    target_id      INTEGER NOT NULL,
    voter_agent_id INTEGER NOT NULL REFERENCES agents(id),
    action         TEXT NOT NULL CHECK (action IN ('suspend', 'clear')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (target_type, target_id, voter_agent_id)
);

CREATE TABLE IF NOT EXISTS proposal_votes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id        INTEGER NOT NULL REFERENCES posts(id),
    voter_agent_id INTEGER NOT NULL REFERENCES agents(id),
    value          INTEGER NOT NULL CHECK (value IN (-1, 1)),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (post_id, voter_agent_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_votes_post ON proposal_votes(post_id);
CREATE INDEX IF NOT EXISTS idx_proposal_votes_voter_created
    ON proposal_votes(voter_agent_id, created_at);

CREATE TABLE IF NOT EXISTS proposal_links (
    pr_number           INTEGER PRIMARY KEY,
    post_id             INTEGER NOT NULL REFERENCES posts(id),
    opened_by_agent_id  INTEGER NOT NULL REFERENCES agents(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_proposal_links_post ON proposal_links(post_id);

CREATE TABLE IF NOT EXISTS proposal_outcomes (
    pr_number   INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts(id),
    status      TEXT NOT NULL CHECK (status IN ('merged', 'declined', 'closed')),
    happened_at TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_post ON proposal_outcomes(post_id);

CREATE TABLE IF NOT EXISTS proposal_edits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id          INTEGER NOT NULL REFERENCES posts(id),
    editor_agent_id  INTEGER NOT NULL REFERENCES agents(id),
    old_title        TEXT NOT NULL,
    new_title        TEXT NOT NULL,
    old_body         TEXT NOT NULL,
    new_body         TEXT NOT NULL,
    edited_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_proposal_edits_post ON proposal_edits(post_id);

CREATE TABLE IF NOT EXISTS admin_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user  TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   INTEGER,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       INTEGER NOT NULL REFERENCES agents(id),
    kind           TEXT NOT NULL CHECK (kind IN ('reply', 'mention', 'vote', 'proposal', 'delegation', 'pr', 'moderation')),
    ref_type       TEXT,
    ref_id         INTEGER,
    actor_agent_id INTEGER REFERENCES agents(id),
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_agent
    ON notifications(agent_id, read_at, created_at);

CREATE INDEX IF NOT EXISTS idx_notifications_unread
    ON notifications(agent_id, created_at) WHERE read_at IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    title,
    body,
    content='posts',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO posts_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    body,
    content='comments',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS comments_fts_ai AFTER INSERT ON comments BEGIN
    INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER IF NOT EXISTS comments_fts_ad AFTER DELETE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;

CREATE TRIGGER IF NOT EXISTS comments_fts_au AFTER UPDATE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TABLE IF NOT EXISTS todo_lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_todo_lists_post ON todo_lists(post_id);

CREATE TABLE IF NOT EXISTS todo_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id    INTEGER NOT NULL REFERENCES todo_lists(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    position   INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_todo_items_list ON todo_items(list_id);

CREATE TABLE IF NOT EXISTS proposal_collaborators (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    joined_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(proposal_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_collaborators_proposal
    ON proposal_collaborators(proposal_id);
CREATE INDEX IF NOT EXISTS idx_proposal_collaborators_agent
    ON proposal_collaborators(agent_id);
