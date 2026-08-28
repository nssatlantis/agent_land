#!/bin/bash
set -u
REPO_DIR="/opt/agent_land"
DATA_DIR="${AGENTLAND_DATA_DIR:-/opt/agent_land_data}"
STABLE_SECONDS=180
PENDING_FILE="$DATA_DIR/.pending_restart"

cd "$REPO_DIR" || exit 0
git fetch origin main -q || exit 0
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo none)
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo none)
if [ -z "$REMOTE" ] || [ "$REMOTE" = "none" ]; then exit 0; fi

# Up-to-date: clear any pending debounce and exit
if [ "$LOCAL" = "$REMOTE" ]; then
    [ -f "$PENDING_FILE" ] && rm -f "$PENDING_FILE"
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
