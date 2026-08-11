#!/bin/bash
set -u
cd /opt/agent_land || exit 0
git fetch origin main -q || exit 0
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo none)
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo none)
if [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
    systemctl restart agentland    # restart re-runs update.sh → pulls + installs + starts
fi
