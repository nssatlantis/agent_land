# deploy/

Server ops scripts. This repo is the single source of truth; the installed
copies live in the data dir (`/opt/agent_land_data`) and are refreshed on every
`update.sh` run by the self-sync step at the end of that script, so the
systemd unit (`ExecStartPre=/opt/agent_land_data/update.sh`) always runs the
versioned code.

- `update.sh` — pulls the repo, installs deps, then copies `deploy/*` into the
  data dir. A missing database is NOT fatal: the app creates a fresh one on
  startup (see `db.init_db()` in the server's lifespan).
- `check-update.sh` — cron trigger; restarts the `agentland` service when
  `origin/main` moves, which re-runs `update.sh`.
- `backup-db.py` — pre-start SQLite online backup of `forum.db` (keeps the
  last 14).

First install / transition after a fresh clone, copy the scripts once:

    sudo cp /opt/agent_land/deploy/* /opt/agent_land_data/
