#!/bin/bash
set -u
REPO_DIR="/opt/agent_land"
DATA_DIR="${AGENTLAND_DATA_DIR:-/opt/agent_land_data}"
cd "$REPO_DIR" || { echo "FATAL: $REPO_DIR does not exist - clone the repo first" >&2; exit 1; }

# DB safety #1: resolve the DB path with the SAME rules as db.py, but without
# importing from the checkout (the checkout may be stale before the first pull).
if [ -n "${FORUM_DB_PATH:-}" ]; then
    DB_FILE="$FORUM_DB_PATH"
else
    DB_FILE="$DATA_DIR/forum.db"
fi
# A missing DB is not fatal: the app's lifespan calls db.init_db(), which creates
# a fresh database + schema on startup. Warn loudly just in case it's a surprise
# (e.g. a wrong FORUM_DB_PATH / AGENTLAND_DATA_DIR).
if [ ! -f "$DB_FILE" ]; then
    echo "WARNING: no database at $DB_FILE - the app will create a fresh one on startup." >&2
    echo "         If this is unexpected, check FORUM_DB_PATH / AGENTLAND_DATA_DIR." >&2
fi

# DB safety #2: snapshot the DB as it is BEFORE the new code runs.
"$DATA_DIR/venv/bin/python" "$DATA_DIR/backup-db.py" \
    || echo "WARNING: pre-start backup failed - continuing" >&2

# DB safety #3: repo is disposable; data lives outside it.
if git fetch origin main; then
    git checkout -f main
    git reset --hard origin/main
    git clean -xdf
    "$DATA_DIR/venv/bin/pip" install -q -r requirements.txt
else
    echo "WARNING: git fetch failed - starting with the existing code" >&2
fi

# Sync deploy scripts from the (fresh) checkout into the data dir so installed
# copies always match the repo. tmp+mv keeps the overwrite atomic in case
# update.sh replaces itself while running.
for f in update.sh check-update.sh backup-db.py; do
    cp "$REPO_DIR/deploy/$f" "$DATA_DIR/$f.tmp" && mv "$DATA_DIR/$f.tmp" "$DATA_DIR/$f"
    chmod 755 "$DATA_DIR/$f"
done
