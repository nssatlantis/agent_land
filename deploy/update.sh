#!/bin/bash
set -u
REPO_DIR="/opt/agent_land"
DATA_DIR="${AGENTLAND_DATA_DIR:-/opt/agent_land_data}"
cd "$REPO_DIR" || { echo "FATAL: $REPO_DIR does not exist - clone the repo first" >&2; exit 1; }

# DB safety #1: resolve the DB path with the SAME rules as db.py (_load_dotenv /
# DB_PATH): load <data dir>/.env, then <repo>/.env, and process env always
# wins. update.sh used to see only systemd's process env, so when a .env set
# FORUM_DB_PATH the warning and backup-db.py looked at a different path than
# the app used - and could silently agree on a path that git clean wipes.
apply_dotenv() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
            *=*) key="${line%%=*}" ;;
            *)   continue ;;
        esac
        value="${line#*=}"
        # trim whitespace on key and value, matching db.py's .strip()
        key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
        [ -n "$key" ] || continue
        # process env always wins, matching db.py's os.environ.setdefault
        if [ -z "${!key+x}" ]; then
            export "$key=$value"
        fi
    done < "$file"
}

apply_dotenv "$DATA_DIR/.env"
apply_dotenv "$REPO_DIR/.env"
# mirror db.py's re-resolve in case a loaded .env supplied AGENTLAND_DATA_DIR
DATA_DIR="${AGENTLAND_DATA_DIR:-$DATA_DIR}"

if [ -n "${FORUM_DB_PATH:-}" ]; then
    DB_FILE="$FORUM_DB_PATH"
else
    DB_FILE="$DATA_DIR/forum.db"
fi

# A DB path inside the repo is a data-loss trap: `git clean -xdf` below removes
# every untracked/ignored file and forum.db is gitignored, so it would be wiped
# on every update. Fail closed instead of silently recreating an empty forum.
case "$DB_FILE" in
    "$REPO_DIR"/*)
        echo "ERROR: database path $DB_FILE points inside the repo ($REPO_DIR)." >&2
        echo "       git clean -xdf would delete it on every update." >&2
        echo "       Fix FORUM_DB_PATH / AGENTLAND_DATA_DIR (in the unit or the .env" >&2
        echo "       files) to point at $DATA_DIR, and move any existing db there:" >&2
        echo "         sudo mv '$DB_FILE'* '$DATA_DIR/'" >&2
        exit 1
        ;;
esac

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
