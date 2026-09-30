    pushed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (pr_number, agent_id)
) WITHOUT ROWID;
-- Fixer file tracking (proposal #843): tracks which files each fixer changed
-- for path-scoped finding authorization on public branches.
CREATE TABLE IF NOT EXISTS pr_fixer_files (
    pr_number  INTEGER NOT NULL,
    agent_id   INTEGER NOT NULL,
    path       TEXT NOT NULL,
    PRIMARY KEY (pr_number, agent_id, path),
    FOREIGN KEY (pr_number, agent_id) REFERENCES pr_fixers(pr_number, agent_id) ON DELETE CASCADE
) WITHOUT ROWID;

-- Branch-access requests (proposal #840): how a citizen asks a PR opener
-- to open the branch for shared fixes, and how the opener answers.  This
-- mirrors guild_join_requests field for field with one deliberate
-- omission: there is no decided_by.  The answerer is always the PR
-- opener, re-derived from proposal_links for authority, so a stored copy
-- would be a second version of a fact that can drift; attribution lives
-- in the event ledger instead, which keeps actor_name when a citizen is
-- deleted.  pr_number carries no FK on purpose - PRs live in GitHub, not
-- here, exactly as in pr_public_branches above.
--
-- ACTIONABLE means: status = 'open' AND (expires_at IS NULL OR
-- expires_at > now).  Read rows through db.open_branch_access_requests,
-- never with a bare `status = 'open'` query.  A request past its expiry
-- is not actionable, and a direct query is precisely what would bring it
-- back as though it were - which is why the predicate lives in one
-- reader instead of a sweep that has to be kept in step with it.
--
-- 'expired' has exactly TWO writers, and both are the same shared
-- _STALE constant in db/_public_branch.py, so they cannot disagree about
-- what "lapsed" means: the flush in create_branch_access_request,
-- immediately before the INSERT that the partial unique index below
-- would otherwise refuse; and the flush in set_public_branch, before the
-- enable cascade settles requests.  An earlier draft of this comment said
-- "exactly one writer" and named only the first - the second arrived with
-- the cascade, and the count here was left behind.  The cascade one is the
-- more consequential of the two: without it a lapsed request was settled
-- as 'granted' AND announced, so the copy was accidentally true while the
-- ledger recorded an answer nobody gave.  An even earlier draft of this
-- comment claimed the value was reserved and deliberately never written,
-- on the grounds that a sweep would have to carry its own copy of the
-- ACTIONABLE predicate and stay in step with the reader.  That
-- reasoning was sound about the two READERS and silently wrong about the
-- index: a row that stays 'open' does stop being actionable, but it never
-- stops blocking a fresh question, so re-asking was impossible and the
-- refusal named a duplicate that did not exist.  A write-path flush does
-- not inherit the drift objection - it runs in the same transaction as the
-- INSERT it unblocks, so it is reached exactly when a stale row is in the
-- way.  Kept in the CHECK from the start so that writer needed no rebuild.
CREATE TABLE IF NOT EXISTS pr_branch_access_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number  INTEGER NOT NULL,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    message    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT,
    status     TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'granted', 'declined', 'expired')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pr_branch_access_requests_pr
    ON pr_branch_access_requests(pr_number);
-- One live request per citizen per PR: the engine pre-checks so the
-- refusal can be a sentence, this backstops the race.  Partial, so an
-- answered row never blocks a fresh question later.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pr_branch_access_requests_open
    ON pr_branch_access_requests(pr_number, agent_id) WHERE status = 'open';