#!/usr/bin/env bash
# CI farm runner install (proposal #667, PR 1).
# Target: Ubuntu Server 24.04/26.04. Installs git + docker, clones the
# public repo, builds the venv, and starts the runner under systemd.
#
# Usage:
#   CIFARM_TOKEN=secret ./ci_farm/install.sh [repo_dir] [bind] [port]
set -euo pipefail

# Root guard: apt/systemd writes require root.
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: must run as root (apt + systemd writes)" >&2
  exit 1
fi

REPO_DIR="${1:-$HOME/agent_land_farm}"
BIND="${2:-0.0.0.0}"
PORT="${3:-8731}"
TOKEN="${CIFARM_TOKEN:?set CIFARM_TOKEN to the runner's bearer token}"

# Token charset gate: only [A-Za-z0-9_-] allowed. A newline or space
# in the token would inject extra env lines into the heredoc.
if [[ ! "$TOKEN" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "ERROR: CIFARM_TOKEN may only contain [A-Za-z0-9_-]" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git docker.io python3 python3-venv

# Enable docker (the unit's After=docker.service requires it running).
systemctl enable --now docker

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

# The bearer token is a secret: it lands in a root-only 0600 env file
# (proposal #667 P0-2: "env file (port, token, data dir)") referenced via
# EnvironmentFile=. A world-readable systemd unit would leak it to every
# local user and echo it back through `systemctl show`.
# BIND/PORT are not secrets and are baked into ExecStart at install time
# (operator-chosen, not runtime-toggled).
ENV_FILE=/etc/agentland-ci-farm.env
umask 077
cat > "$ENV_FILE" <<EOF
CIFARM_TOKEN=$TOKEN
CIFARM_REPO_DIR=$REPO_DIR
AGENTLAND_DATA_DIR=$DATA_DIR
EOF
chmod 600 "$ENV_FILE"
umask 022

UNIT=agentland-ci-farm.service
cat > "/etc/systemd/system/$UNIT" <<EOF
[Unit]
Description=AgentLand CI farm runner
After=network-online.target docker.service

[Service]
User=root
EnvironmentFile=$ENV_FILE
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/ci_farm/runner.py --bind $BIND --port $PORT
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$UNIT"
# enable --now only starts an inactive unit: without this, re-running the
# installer (new checkout and/or fresh venv) would leave the OLD process
# serving stale code and stale dependencies.
systemctl restart "$UNIT"
echo "CI farm runner installed: $UNIT on $BIND:$PORT"
