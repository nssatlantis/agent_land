#!/bin/bash
set -u
# C2: prepare phase — runs without killing the old process (called by check-update.sh)
# Fetches new code, installs deps (A1/A2), syncs deploy scripts, snapshots DB.
# The actual restart's ExecStartPre (update.sh) then only does checkout + guards,
# so the killed window is ~2s not 60s. Safe to run repeatedly; idempotent.
REPO_DIR="/opt/agent_land"
DATA_DIR="${AGENTLAND_DATA_DIR:-/opt/agent_land_data}"
cd "$REPO_DIR" || { echo "FATAL: $REPO_DIR does not exist" >&2; exit 1; }

apply_dotenv() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; *=*) key="${line%%=*}" ;; *) continue ;; esac
        value="${line#*=}"
        key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
        [ -n "$key" ] || continue
        if [ -z "${!key+x}" ]; then export "$key=$value"; fi
    done < "$file"
}
apply_dotenv "$DATA_DIR/.env"
apply_dotenv "$REPO_DIR/.env"
DATA_DIR="${AGENTLAND_DATA_DIR:-$DATA_DIR}"
if [ -n "${FORUM_DB_PATH:-}" ]; then DB_FILE="$FORUM_DB_PATH"; else DB_FILE="$DATA_DIR/forum.db"; fi
case "$DB_FILE" in "$REPO_DIR"/*) echo "ERROR: DB inside repo" >&2; exit 1;; esac

# F2: log prepare start (best-effort, never fail the prepare)
if command -v logger >/dev/null 2>&1; then
    logger -t agentland-update "prepare_start repo=$REPO_DIR" || true
fi
# Fallback JSON to stderr for journald (also F2)
echo "{\"event\":\"prepare_start\",\"ts\":\"$(date -u +%FT%TZ)\"}" >&2 || true

REQ_HASH_FILE="$DATA_DIR/.requirements.sha256"
PIP_BIN="$DATA_DIR/venv/bin/pip"
UV_BIN="$DATA_DIR/venv/bin/uv"
UV_CACHE="$DATA_DIR/.uv-cache"

# A3: parallelize fetch + pre-fetch backup-eligible check (fetch needs network, backup needs disk)
# Start fetch in background while we prime cache dir
mkdir -p "$UV_CACHE" 2>/dev/null || true
git fetch origin main -q &
FETCH_PID=$!
# While fetch runs, ensure cache dir exists and hash file readable (no-op but overlaps I/O)
[ -f "$REPO_DIR/requirements.txt" ] || true
wait $FETCH_PID
FETCH_RC=$?
if [ $FETCH_RC -ne 0 ]; then
    echo "WARNING: git fetch failed in prepare — activate will retry" >&2
    echo "{\"event\":\"prepare_fetch_failed\",\"rc\":$FETCH_RC}" >&2 || true
    exit 0
fi
# Now checkout the fetched main (still serving old code from memory)
git checkout -f main >/dev/null 2>&1 || true
git reset --hard origin/main >/dev/null 2>&1 || true
git clean -xdf >/dev/null 2>&1 || true

# A1/A2: pip-if-changed via uv
if [ -f "$REPO_DIR/requirements.txt" ]; then
    NEW_HASH=$(sha256sum "$REPO_DIR/requirements.txt" 2>/dev/null | cut -d' ' -f1 || echo none)
    OLD_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null || echo none)
    if [ "$NEW_HASH" = "$OLD_HASH" ] && [ -x "$PIP_BIN" ]; then
        echo "prepare: requirements.txt unchanged ($NEW_HASH) — skipping pip install" >&2
    else
        echo "prepare: requirements.txt $OLD_HASH -> $NEW_HASH — installing" >&2
        INSTALLED=0
        if [ -x "$UV_BIN" ]; then
            if "$UV_BIN" pip --python "$DATA_DIR/venv/bin/python" --cache-dir "$UV_CACHE" install -q -r requirements.txt; then
                INSTALLED=1
                echo "$NEW_HASH" > "$REQ_HASH_FILE"
            else
                echo "WARNING: uv pip failed, falling back to pip" >&2
            fi
        fi
        if [ "$INSTALLED" -eq 0 ]; then
            if "$PIP_BIN" install -q -r requirements.txt; then
                echo "$NEW_HASH" > "$REQ_HASH_FILE"
            else
                echo "WARNING: pip install failed in prepare — activate will retry and fail-closed if needed" >&2
            fi
        fi
    fi
fi

# Self-sync deploy scripts (atomic tmp+mv) — must be before activate's guard
for f in update.sh check-update.sh backup-db.py restore-db.py check-db-boot.py backfill_events.py check-record-size.py backfill-signatures.py check-registry-drift.py update-prepare.sh; do
    cp "$REPO_DIR/deploy/$f" "$DATA_DIR/$f.tmp" && mv "$DATA_DIR/$f.tmp" "$DATA_DIR/$f"
    chmod 755 "$DATA_DIR/$f"
done

# Snapshot before activate's guard (best-effort)
"$DATA_DIR/venv/bin/python" "$DATA_DIR/backup-db.py" || echo "WARNING: prepare backup failed" >&2

echo "{\"event\":\"prepare_done\",\"ts\":\"$(date -u +%FT%TZ)\"}" >&2 || true
if command -v logger >/dev/null 2>&1; then logger -t agentland-update "prepare_done" || true; fi
