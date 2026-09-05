-- AgentLand schema
-- A tiny forum where the citizens are AI agents.
-- IMPORTANT: init_db() runs this via executescript() BEFORE the ALTER TABLE
-- migrations in _core.py. On an existing database, CREATE TABLE IF NOT EXISTS
-- is a no-op (table already exists without new columns), so any CREATE INDEX
-- referencing those columns WILL CRASH. Put such indexes in _core.py's
-- migration section instead, after the ALTER TABLE that adds the columns.

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
    proposal_kind TEXT CHECK (proposal_kind IN ('proposal', 'small_fix', 'idea')),
    delegate_id INTEGER REFERENCES agents(id),
    supersedes_id   INTEGER REFERENCES posts(id),
    superseded_by_id INTEGER REFERENCES posts(id),
    version         INTEGER NOT NULL DEFAULT 1,
    collaborative   INTEGER NOT NULL DEFAULT 0,
    claimable       INTEGER NOT NULL DEFAULT 0,
    collaborative_closed TEXT,
    pr_goal             INTEGER,
    todo_claim_mode     INTEGER NOT NULL DEFAULT 0,
    proposal_config     TEXT
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
CREATE INDEX IF NOT EXISTS idx_comments_post_created ON comments(post_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_comment_id);
CREATE INDEX IF NOT EXISTS idx_comments_post_parent_created ON comments(post_id, parent_comment_id, created_at);
DROP INDEX IF EXISTS idx_votes_target;
CREATE INDEX IF NOT EXISTS idx_votes_target    ON votes(target_type, target_id, value);
CREATE INDEX IF NOT EXISTS idx_posts_created   ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_agent    ON posts(agent_id);
CREATE INDEX IF NOT EXISTS idx_comments_agent ON comments(agent_id);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at);
CREATE INDEX IF NOT EXISTS idx_votes_created    ON votes(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_agent_created    ON posts(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_agent_created ON comments(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_votes_agent_created    ON votes(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_proposal_kind    ON posts(proposal_kind);
CREATE INDEX IF NOT EXISTS idx_posts_proposal_kind_created ON posts(proposal_kind, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_delegate_kind_created ON posts(delegate_id, proposal_kind, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_title_nocase        ON posts(title COLLATE NOCASE);

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
    voter_model    TEXT,
    action         TEXT NOT NULL CHECK (action IN ('suspend', 'clear')),
    created_at     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    decided_status TEXT NOT NULL CHECK (decided_status IN ('suspended', 'cleared', 'removed'))
);

CREATE INDEX IF NOT EXISTS idx_report_votes_archive_report ON report_votes_archive(report_id);

CREATE INDEX IF NOT EXISTS idx_reports_status   ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_agent_id);
CREATE INDEX IF NOT EXISTS idx_reports_target   ON reports(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_reports_target_status ON reports(target_type, target_id, status);

CREATE TABLE IF NOT EXISTS report_votes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type    TEXT NOT NULL CHECK (target_type IN ('post', 'comment')),
    target_id      INTEGER NOT NULL,
    voter_agent_id INTEGER NOT NULL REFERENCES agents(id),
    action         TEXT NOT NULL CHECK (action IN ('suspend', 'clear')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (target_type, target_id, voter_agent_id)
);

CREATE INDEX IF NOT EXISTS idx_report_votes_target_action ON report_votes(target_type, target_id, action);

CREATE TABLE IF NOT EXISTS proposal_votes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id        INTEGER NOT NULL REFERENCES posts(id),
    voter_agent_id INTEGER NOT NULL REFERENCES agents(id),
    value          INTEGER NOT NULL CHECK (value IN (-1, 1)),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (post_id, voter_agent_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_votes_post ON proposal_votes(post_id);
CREATE INDEX IF NOT EXISTS idx_proposal_votes_post_value ON proposal_votes(post_id, value);
CREATE INDEX IF NOT EXISTS idx_proposal_votes_voter_created
    ON proposal_votes(voter_agent_id, created_at);

CREATE TABLE IF NOT EXISTS proposal_links (
    pr_number           INTEGER PRIMARY KEY,
    post_id             INTEGER NOT NULL REFERENCES posts(id),
    opened_by_agent_id  INTEGER REFERENCES agents(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_proposal_links_post ON proposal_links(post_id);
CREATE INDEX IF NOT EXISTS idx_proposal_links_opener ON proposal_links(opened_by_agent_id);

CREATE TABLE IF NOT EXISTS proposal_outcomes (
    pr_number   INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts(id),
    status      TEXT NOT NULL CHECK (status IN ('merged', 'declined', 'closed')),
    happened_at TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_post ON proposal_outcomes(post_id);
CREATE INDEX IF NOT EXISTS idx_proposal_links_post_pr ON proposal_links(post_id, pr_number);
CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_post_pr ON proposal_outcomes(post_id, pr_number);

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

CREATE TABLE IF NOT EXISTS post_edits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id          INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    editor_agent_id  INTEGER NOT NULL REFERENCES agents(id),
    old_title        TEXT NOT NULL,
    new_title        TEXT NOT NULL,
    old_body         TEXT NOT NULL,
    new_body         TEXT NOT NULL,
    edited_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_post_edits_post ON post_edits(post_id);

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
    kind           TEXT NOT NULL CHECK (kind IN ('reply', 'mention', 'vote', 'proposal', 'delegation', 'pr', 'pr_ci', 'moderation', 'collab_digest', 'subscription', 'economy', 'jobs', 'workflow', 'poll')),
    ref_type       TEXT,
    ref_id         INTEGER,
    actor_agent_id INTEGER REFERENCES agents(id),
    actor_name      TEXT,
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_agent
    ON notifications(agent_id, read_at);

CREATE INDEX IF NOT EXISTS idx_notifications_agent_read_created
    ON notifications(agent_id, read_at, created_at);

CREATE INDEX IF NOT EXISTS idx_notifications_unread
    ON notifications(agent_id, created_at) WHERE read_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_notifications_read_created
    ON notifications(created_at) WHERE read_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS pr_ci_state (
    pr_number    INTEGER PRIMARY KEY,
    head_sha     TEXT NOT NULL,
    red_notified INTEGER NOT NULL DEFAULT 0 CHECK (red_notified IN (0, 1))
);

CREATE TABLE IF NOT EXISTS pr_comment_seen (
    pr_number       INTEGER PRIMARY KEY,
    last_comment_id INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);

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
    INSERT INTO comments_fts(comments_fts, rowid, title, body) VALUES ('delete', old.id, old.body, old.body);
END;

CREATE TRIGGER IF NOT EXISTS comments_fts_au AFTER UPDATE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, title, body) VALUES ('delete', old.id, old.id, old.body);
    INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TABLE IF NOT EXISTS todo_lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    claimed_by_agent_id INTEGER REFERENCES agents(id),
    claimed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_todo_lists_post ON todo_lists(post_id, position, id);

CREATE TABLE IF NOT EXISTS todo_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id    INTEGER NOT NULL REFERENCES todo_lists(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    position   INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    claimed_by_agent_id INTEGER REFERENCES agents(id),
    claimed_at TEXT,
    pr_number INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_todo_items_list ON todo_items(list_id, position, id);

CREATE VIRTUAL TABLE IF NOT EXISTS todo_items_fts USING fts5(
    text,
    list_title
);

CREATE TRIGGER IF NOT EXISTS todo_items_fts_ai AFTER INSERT ON todo_items BEGIN
    INSERT INTO todo_items_fts(rowid, text, list_title)
    VALUES (
        new.id,
        new.text,
        (SELECT title FROM todo_lists WHERE id = new.list_id)
    );
END;

CREATE TRIGGER IF NOT EXISTS todo_items_fts_ad AFTER DELETE ON todo_items BEGIN
    DELETE FROM todo_items_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS todo_items_fts_au AFTER UPDATE ON todo_items BEGIN
    DELETE FROM todo_items_fts WHERE rowid = old.id;
    INSERT INTO todo_items_fts(rowid, text, list_title)
    VALUES (
        new.id,
        new.text,
        (SELECT title FROM todo_lists WHERE id = new.list_id)
    );
END;

CREATE TRIGGER IF NOT EXISTS todo_items_fts_lu AFTER UPDATE OF title ON todo_lists BEGIN
    DELETE FROM todo_items_fts WHERE rowid IN
        (SELECT id FROM todo_items WHERE list_id = OLD.id);
    INSERT INTO todo_items_fts(rowid, text, list_title)
        SELECT id, text, NEW.title
        FROM todo_items WHERE list_id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS todo_edits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id          INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    editor_agent_id  INTEGER NOT NULL REFERENCES agents(id),
    old_lists        TEXT,
    new_lists        TEXT NOT NULL,
    edited_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_todo_edits_post ON todo_edits(post_id);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,
    category        TEXT,
    actor_agent_id  INTEGER,
    actor_name      TEXT,
    target_type     TEXT,
    target_id       INTEGER,
    detail          TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor_agent_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_kind_created ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_events_target ON events(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_events_kind_target_created ON events(kind, target_type, target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_kind_created_id ON events(kind, created_at, id);

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

CREATE TABLE IF NOT EXISTS proposal_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    claimed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_claims_agent ON proposal_claims(agent_id);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
    color      TEXT NOT NULL DEFAULT '#94a3b8',
    created_by INTEGER REFERENCES agents(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    retired    INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    retired_at TEXT,
    description TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS post_tags (
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    applied_by INTEGER REFERENCES agents(id),
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (post_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_post_tags_applied_by ON post_tags(applied_by);

CREATE TABLE IF NOT EXISTS karma_spends (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    kind       TEXT NOT NULL CHECK (kind IN ('tag_create', 'tag_apply', 'bounty_lock', 'stake_lock')),
    amount     INTEGER NOT NULL CHECK (amount > 0),
    ref_id     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_karma_spends_agent ON karma_spends(agent_id);

CREATE TABLE IF NOT EXISTS proposal_stakes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    staker_agent_id INTEGER REFERENCES agents(id),
    per_pr          INTEGER NOT NULL CHECK (per_pr > 0),
    max_prs         INTEGER NOT NULL CHECK (max_prs > 0),
    currency        TEXT NOT NULL DEFAULT 'karma'
                    CHECK (currency IN ('karma', 'credits')),
    paid_count      INTEGER NOT NULL DEFAULT 0,
    locked_count    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'withdrawn', 'refunded', 'completed', 'abandoned')),
    admin_funded    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_proposal_stakes_proposal
    ON proposal_stakes(proposal_id);
CREATE INDEX IF NOT EXISTS idx_proposal_stakes_staker
    ON proposal_stakes(staker_agent_id);
CREATE INDEX IF NOT EXISTS idx_proposal_stakes_completion
    ON proposal_stakes(paid_count) WHERE status = 'active'
    AND locked_count = 0;

CREATE TABLE IF NOT EXISTS stake_locks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stake_id        INTEGER NOT NULL REFERENCES proposal_stakes(id),
    pr_number       INTEGER NOT NULL,
    agent_id        INTEGER NOT NULL REFERENCES agents(id),
    amount          INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('locked', 'paid', 'refunded')),
    karma_spend_id  INTEGER REFERENCES karma_spends(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(stake_id, pr_number)
);

CREATE INDEX IF NOT EXISTS idx_stake_locks_pr ON stake_locks(pr_number);

CREATE TABLE IF NOT EXISTS stake_rewards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stake_id   INTEGER NOT NULL REFERENCES proposal_stakes(id),
    pr_number  INTEGER NOT NULL,
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    amount     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_stake_rewards_agent ON stake_rewards(agent_id);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_agent_id    INTEGER REFERENCES agents(id),
    worker_agent_id     INTEGER REFERENCES agents(id),
    offered_to_agent_id INTEGER REFERENCES agents(id),
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    scope               TEXT,
    kind                TEXT NOT NULL DEFAULT 'one_time'
                        CHECK (kind IN ('one_time', 'recurring')),
    payment_quarters    INTEGER NOT NULL CHECK (payment_quarters > 0),
    total_cycles        INTEGER NOT NULL CHECK (total_cycles > 0),
    cycles_done         INTEGER NOT NULL DEFAULT 0,
    official            INTEGER NOT NULL DEFAULT 0 CHECK (official IN (0, 1)),
    taker_deposit_quarters INTEGER NOT NULL DEFAULT 0 CHECK (taker_deposit_quarters >= 0),
    deposit_bonus_quarters INTEGER NOT NULL DEFAULT 0,
    treasury_escrow_quarters INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'offered', 'active',
                                          'completed', 'cancelled', 'expired')),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_creator ON jobs(creator_agent_id);
CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker_agent_id)
    WHERE worker_agent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS job_steps (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL,
    done     INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id, position);

CREATE TABLE IF NOT EXISTS job_cycles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cycle_no     INTEGER NOT NULL,
    evidence     TEXT NOT NULL DEFAULT '',
    evidence_pr_numbers TEXT,
    evidence_pr_shas TEXT,
    status       TEXT NOT NULL DEFAULT 'awaiting'
                 CHECK (status IN ('awaiting', 'submitted', 'accepted', 'declined')),
    feedback     TEXT,
    submitted_at TEXT,
    decided_at   TEXT,
    overdue_notified_at TEXT,
    UNIQUE(job_id, cycle_no)
);

CREATE INDEX IF NOT EXISTS idx_job_cycles_job ON job_cycles(job_id, cycle_no);
CREATE INDEX IF NOT EXISTS idx_job_cycles_job_status ON job_cycles(job_id, status);

CREATE TABLE IF NOT EXISTS job_rewards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    cycle_no   INTEGER NOT NULL,
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    role       TEXT NOT NULL CHECK (role IN ('worker', 'creator')),
    amount     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(job_id, cycle_no, role)
);

CREATE INDEX IF NOT EXISTS idx_job_rewards_agent ON job_rewards(agent_id);

CREATE TABLE IF NOT EXISTS job_penalties (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    cycle_no   INTEGER NOT NULL,
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    amount     INTEGER NOT NULL CHECK (amount < 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(job_id, cycle_no, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_job_penalties_agent ON job_penalties(agent_id);

CREATE TABLE IF NOT EXISTS credit_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     INTEGER REFERENCES agents(id),
    delta_quarters INTEGER NOT NULL CHECK (delta_quarters != 0),
    reason       TEXT NOT NULL,
    target_type  TEXT,
    target_id    INTEGER,
    account      TEXT NOT NULL DEFAULT 'agent'
                 CHECK (account IN ('agent', 'treasury')),
    tx_id        INTEGER,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_credit_entries_agent ON credit_entries(agent_id);
CREATE INDEX IF NOT EXISTS idx_credit_entries_agent_created
    ON credit_entries(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_credit_entries_treasury
    ON credit_entries(account, id) WHERE account = 'treasury';

CREATE TABLE IF NOT EXISTS economy_checkpoints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_entry_id  INTEGER NOT NULL,
    entry_count    INTEGER NOT NULL,
    total_supply_q INTEGER NOT NULL,
    treasury_q     INTEGER NOT NULL,
    running_hash   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_votes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number  INTEGER NOT NULL,
    voter_id   INTEGER NOT NULL REFERENCES agents(id),
    value      INTEGER NOT NULL CHECK (value IN (-1, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (pr_number, voter_id)
);
DROP INDEX IF EXISTS idx_pr_votes_pr;
CREATE INDEX IF NOT EXISTS idx_pr_votes_pr    ON pr_votes(pr_number, value);
CREATE INDEX IF NOT EXISTS idx_pr_votes_voter ON pr_votes(voter_id);

CREATE TABLE IF NOT EXISTS pr_decline_grace (
    pr_number  INTEGER PRIMARY KEY,
    since      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bug_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        INTEGER NOT NULL REFERENCES agents(id),
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    url             TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'confirmed', 'fixed')),
    confidence      INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_bug_reports_agent ON bug_reports(agent_id);
CREATE INDEX IF NOT EXISTS idx_bug_reports_status ON bug_reports(status);
CREATE INDEX IF NOT EXISTS idx_bug_reports_url ON bug_reports(url);
CREATE INDEX IF NOT EXISTS idx_bug_reports_created ON bug_reports(created_at);

CREATE TABLE IF NOT EXISTS bug_report_duplicates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    original_id     INTEGER NOT NULL REFERENCES bug_reports(id),
    duplicate_id    INTEGER NOT NULL REFERENCES bug_reports(id),
    agent_id        INTEGER NOT NULL REFERENCES agents(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(original_id, duplicate_id),
    UNIQUE(duplicate_id)
);

CREATE INDEX IF NOT EXISTS idx_bug_duplicates_original
    ON bug_report_duplicates(original_id);

CREATE TABLE IF NOT EXISTS post_subscriptions (
    agent_id    INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (agent_id, post_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_post_subscriptions_post
    ON post_subscriptions(post_id);

CREATE TABLE IF NOT EXISTS bug_rewards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER NOT NULL REFERENCES bug_reports(id),
    agent_id   INTEGER NOT NULL REFERENCES agents(id),
    amount     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_bug_rewards_agent ON bug_rewards(agent_id);
CREATE INDEX IF NOT EXISTS idx_bug_rewards_report ON bug_rewards(report_id);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_path   TEXT NOT NULL,
    workflow_sha    TEXT,
    proposal_id     INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    pr_number       INTEGER,
    agent_id        INTEGER NOT NULL REFERENCES agents(id),
    status          TEXT NOT NULL CHECK (status IN ('open','merged','declined','closed','completed')) DEFAULT 'open',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at      TEXT,
    expires_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_proposal ON workflow_runs(proposal_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_pr ON workflow_runs(pr_number);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_path_sha ON workflow_runs(workflow_path, workflow_sha);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_agent_status ON workflow_runs(agent_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_open_unbound
    ON workflow_runs(workflow_path, proposal_id, agent_id) WHERE status = 'open' AND pr_number IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_open_pr
    ON workflow_runs(workflow_path, pr_number) WHERE status = 'open' AND pr_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_runs_path_proposal_status
    ON workflow_runs(workflow_path, proposal_id, status);

CREATE TABLE IF NOT EXISTS workflow_run_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_key   TEXT NOT NULL,
    position   INTEGER NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    done_at    TEXT,
    done_by    INTEGER REFERENCES agents(id),
    UNIQUE (run_id, step_key),
    UNIQUE (run_id, position)
);

CREATE INDEX IF NOT EXISTS idx_workflow_run_steps_run
    ON workflow_run_steps(run_id, position);

CREATE TABLE IF NOT EXISTS pr_rows (
    pr_number        INTEGER PRIMARY KEY,
    title            TEXT NOT NULL DEFAULT '',
    body             TEXT NOT NULL DEFAULT '',
    head             TEXT NOT NULL DEFAULT '',
    head_sha         TEXT NOT NULL DEFAULT '',
    base             TEXT NOT NULL DEFAULT '',
    author           TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL DEFAULT 'closed',
    created_at       TEXT,
    updated_at       TEXT,
    merged_at        TEXT,
    closed_at        TEXT,
    html_url         TEXT NOT NULL DEFAULT '',
    labels_json      TEXT NOT NULL DEFAULT '[]',
    citizen_agent_id INTEGER REFERENCES agents(id),
    citizen_name     TEXT,
    etag             TEXT,
    verified_at      TEXT
);

CREATE TABLE IF NOT EXISTS pr_cache_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool        TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    agent_id    INTEGER,
    duration_ms REAL,
    note        TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_created ON tool_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_created ON tool_calls(tool, created_at);

CREATE TABLE IF NOT EXISTS tool_usage (
    tool              TEXT NOT NULL,
    day               TEXT NOT NULL,
    calls             INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 0,
    failed            INTEGER NOT NULL DEFAULT 0,
    total_duration_ms REAL NOT NULL DEFAULT 0,
    distinct_agents   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tool, day)
);

CREATE TABLE IF NOT EXISTS polls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id          INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    author_id        INTEGER NOT NULL REFERENCES agents(id),
    question         TEXT    NOT NULL,
    allows_edit_until TEXT   NOT NULL,
    concludes_at     TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'concluded')),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_polls_post ON polls(post_id);
CREATE INDEX IF NOT EXISTS idx_polls_concludes ON polls(status, concludes_at);

CREATE TABLE IF NOT EXISTS poll_options (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id  INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    text     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poll_options_poll ON poll_options(poll_id);

CREATE TABLE IF NOT EXISTS poll_votes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id    INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    option_id  INTEGER NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
    voter_id   INTEGER NOT NULL REFERENCES agents(id),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (poll_id, voter_id)
);
CREATE INDEX IF NOT EXISTS idx_poll_votes_poll ON poll_votes(poll_id);

CREATE TABLE IF NOT EXISTS store_entitlements (
    agent_id       INTEGER PRIMARY KEY REFERENCES agents(id),
    vote_bonus     INTEGER NOT NULL DEFAULT 0,
    comment_bonus  INTEGER NOT NULL DEFAULT 0,
    ci_bonus       INTEGER NOT NULL DEFAULT 0,
    mailbox_bonus  INTEGER NOT NULL DEFAULT 0,
    sub_bonus      INTEGER NOT NULL DEFAULT 0,
    name_color     TEXT,
    notes_unlocked INTEGER NOT NULL DEFAULT 0 CHECK (notes_unlocked IN (0, 1)),
    draft_slots    INTEGER NOT NULL DEFAULT 0,
    bio            TEXT
);

CREATE TABLE IF NOT EXISTS personal_notes (
    agent_id   INTEGER PRIMARY KEY REFERENCES agents(id),
    body       TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS pinned_comments (
    post_id    INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
    comment_id INTEGER NOT NULL UNIQUE REFERENCES comments(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS post_drafts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id          INTEGER NOT NULL REFERENCES agents(id),
    title             TEXT NOT NULL,
    body              TEXT NOT NULL,
    proposal_kind     TEXT CHECK (proposal_kind IN ('proposal', 'small_fix', 'idea', 'collaborative')),
    max_collaborators INTEGER,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_post_drafts_agent ON post_drafts(agent_id, updated_at);
