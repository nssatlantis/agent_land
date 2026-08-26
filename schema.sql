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
    UNIQUE(job_id, cycle_no)
);