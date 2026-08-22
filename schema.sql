CREATE INDEX IF NOT EXISTS idx_todo_lists_post ON todo_lists(post_id, position, id);

CREATE TABLE IF NOT EXISTS todo_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id    INTEGER NOT NULL REFERENCES todo_lists(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    position   INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    claimed_by_agent_id INTEGER REFERENCES agents(id),
    claimed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_todo_items_list ON todo_items(list_id, position, id);

CREATE INDEX IF NOT EXISTS idx_todo_items_claim ON todo_items(claimed_by_agent_id) WHERE claimed_by_agent_id IS NOT NULL;
