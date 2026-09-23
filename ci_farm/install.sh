#!/usr/bin/env bash
# CI farm runner install (proposal #667, PR 1).
# Target: Ubuntu Server 24.04/26.04. Installs git + docker, clones the
# public repo, builds the venv, and starts the runner under systemd.
#
# Usage:
#   CIFARM_TOKEN=secret ./ci_farm/install.sh [repo_dir] [bind] [port]
set -euo pipefail

REPO_DIR="${1:-$HOME/agent_land_farm}"
BIND="${2:-0.0.0.0}"
PORT="${3:-8731}"
TOKEN="${CIFARM_TOKEN:?set CIFARM_TOKEN to the runner's bearer token}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git docker.io python3 python3-venv

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone https://github.com/nssatlantis/agent_land.git "$REPO_DIR"
fi
git -C "$REPO_DIR" fetch origin main
git -C "$REPO_DIR" checkout -B main origin/main

python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install \
  -r "$REPO_DIR/requirements.txt" \
  -r "$REPO_DIR/requirements-dev.txt"

DATA_DIR="$REPO_DIR/ci_farm/data"
mkdir -p "$DATA_DIR"

UNIT=agentland-ci-farm.service
cat > "/etc/systemd/system/$UNIT" <<EOF
[Unit]
Description=AgentLand CI farm runner
After=network-online.target docker.service

[Service]
User=root
Environment=CIFARM_TOKEN=$TOKEN
Environment=CIFARM_REPO_DIR=$REPO_DIR
Environment=AGENTLAND_DATA_DIR=$DATA_DIR
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/ci_farm/runner.py --bind $BIND --port $PORT
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$UNIT"
echo "CI farm runner installed: $UNIT on $BIND:$PORT"
