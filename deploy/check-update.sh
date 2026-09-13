#!/bin/bash
set -u
REPO_DIR="/opt/agent_land"
DATA_DIR="${AGENTLAND_DATA_DIR:-/opt/agent_land_data}"
STABLE_SECONDS=180

cd "$REPO_DIR" || exit 0

# Resolve FORUM_HOST/FORUM_PORT with the same rules as db.py (_load_dotenv):
# load <data dir>/.env then <repo>/.env, process env always wins.
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

PENDING_FILE="$DATA_DIR/.pending_restart"
# CI-idle gate markers: while a held restart waits for CI to drain, its
# first (recorded) busy tick is stashed here so the hold has a clock. Cleared
# whenever the gate passes (idle, force-restarted, or per fresh remote).
BUSY_SINCE_FILE="$DATA_DIR/.ci_busy_since"
# Cap on how long the restart may be held while CI stays busy (seconds;
# 0 = hold indefinitely until the pool idles).
MAX_CI_WAIT="${RESTART_CI_WAIT_MAX_SECONDS:-7200}"

git fetch origin main -q || exit 0
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo none)
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo none)
if [ -z "$REMOTE" ] || [ "$REMOTE" = "none" ]; then exit 0; fi

# Up-to-date: clear any pending debounce (and any CI-hold marker) and exit
if [ "$LOCAL" = "$REMOTE" ]; then
    [ -f "$PENDING_FILE" ] && rm -f "$PENDING_FILE"
    [ -f "$BUSY_SINCE_FILE" ] && rm -f "$BUSY_SINCE_FILE"
    exit 0
fi

# Behind: debounce B-style (3min stable)
NOW=$(date +%s)
if [ -f "$PENDING_FILE" ]; then
    PENDING_REMOTE=$(cut -d' ' -f1 "$PENDING_FILE" 2>/dev/null || echo "")
    PENDING_TS=$(cut -d' ' -f2 "$PENDING_FILE" 2>/dev/null || echo 0)
    # If remote moved (new push), reset debounce
    if [ "$PENDING_REMOTE" != "$REMOTE" ]; then
        echo "$REMOTE $NOW" > "$PENDING_FILE"
        [ -f "$BUSY_SINCE_FILE" ] && rm -f "$BUSY_SINCE_FILE"
        echo "{\"event\":\"restart_debounced\",\"local\":\"$LOCAL\",\"remote\":\"$REMOTE\",\"pending\":\"reset_new_remote\"}" >&2 || true
        if command -v logger >/dev/null 2>&1; then logger -t agentland-update "restart_debounced pending_reset remote=$REMOTE" || true; fi
        exit 0
    fi
    AGE=$((NOW - PENDING_TS))
    if [ "$AGE" -lt "$STABLE_SECONDS" ]; then
        echo "{\"event\":\"restart_debounced\",\"local\":\"$LOCAL\",\"remote\":\"$REMOTE\",\"age\":$AGE,\"stable\":$STABLE_SECONDS}" >&2 || true
        if command -v logger >/dev/null 2>&1; then logger -t agentland-update "restart_debounced age=$AGE stable=$STABLE_SECONDS remote=$REMOTE" || true; fi
        exit 0
    fi
    # stable long enough — proceed to restart
    # CI-idle gate: hold the restart while any in-flight CI run is active —
    # auto-update must not kill the workspace pool mid-run or orphan branch-CI
    # workers whose ledger events would never be stamped. Probe the live
    # server's /ci-status; a probe that cannot be answered (no curl, server
    # down, 503) is treated as not-busy and fails toward restart — a dead
    # probe must never wedge deploys. Held ticks keep the pending marker and
    # re-check next cron tick.
    CI_BUSY=0
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS -m 5 "http://${FORUM_HOST:-127.0.0.1}:${FORUM_PORT:-8000}/ci-status" 2>/dev/null | grep -q '"ci_busy":true'; then
            CI_BUSY=1
        fi
    fi
    if [ "$CI_BUSY" = "1" ]; then
        if [ ! -f "$BUSY_SINCE_FILE" ]; then
            echo "$NOW" > "$BUSY_SINCE_FILE"
        fi
        BUSY_VAL=$(cat "$BUSY_SINCE_FILE" 2>/dev/null || echo "$NOW")
        HELD_S=$((NOW - ${BUSY_VAL:-$NOW}))
        FORCE=0
        if [ "$MAX_CI_WAIT" -gt 0 ] 2>/dev/null && [ "$HELD_S" -ge "$MAX_CI_WAIT" ]; then
            FORCE=1
        fi
        if [ "$FORCE" = "1" ]; then
            rm -f "$BUSY_SINCE_FILE"
            echo "{\"event\":\"restart_ci_wait_exceeded\",\"local\":\"$LOCAL\",\"remote\":\"$REMOTE\",\"held_s\":$HELD_S,\"max_wait_s\":$MAX_CI_WAIT}" >&2 || true
            if command -v logger >/dev/null 2>&1; then logger -t agentland-update "restart_ci_wait_exceeded held_s=$HELD_S max_wait_s=$MAX_CI_WAIT remote=$REMOTE" || true; fi
        else
            echo "{\"event\":\"restart_held_ci_busy\",\"local\":\"$LOCAL\",\"remote\":\"$REMOTE\",\"held_s\":$HELD_S,\"max_wait_s\":$MAX_CI_WAIT}" >&2 || true
            if command -v logger >/dev/null 2>&1; then logger -t agentland-update "restart_held_ci_busy held_s=$HELD_S max_wait_s=$MAX_CI_WAIT remote=$REMOTE" || true; fi
            exit 0
        fi
    else
        rm -f "$BUSY_SINCE_FILE"
    fi
else
    # First time behind — arm debounce
    echo "$REMOTE $NOW" > "$PENDING_FILE"
    echo "{\"event\":\"restart_pending\",\"local\":\"$LOCAL\",\"remote\":\"$REMOTE\",\"stable\":$STABLE_SECONDS}" >&2 || true
    if command -v logger >/dev/null 2>&1; then logger -t agentland-update "restart_pending remote=$REMOTE stable=${STABLE_SECONDS}s" || true; fi
    exit 0
fi

# F2: log restart_scheduled
echo "{\"event\":\"restart_scheduled\",\"local\":\"$LOCAL\",\"remote\":\"$REMOTE\",\"ts\":\"$(date -u +%FT%TZ)\"}" >&2 || true
if command -v logger >/dev/null 2>&1; then logger -t agentland-update "restart_scheduled local=$LOCAL remote=$REMOTE" || true; fi
# C2: prepare without killing (fetch+pip+sync+backup), then restart (activate does checkout+guards)
if [ -x "$DATA_DIR/update-prepare.sh" ]; then
    "$DATA_DIR/update-prepare.sh" || echo "WARNING: update-prepare failed — restart will still run full update.sh" >&2
elif [ -x "$REPO_DIR/deploy/update-prepare.sh" ]; then
    "$REPO_DIR/deploy/update-prepare.sh" || true
fi
# Clear pending before restart (restart will re-check)
rm -f "$PENDING_FILE"
systemctl restart agentland    # restart re-runs update.sh → pulls + installs + starts
