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
TOKEN="${CIFARM_TOKEN:?set CIFARM_TOKEN to the runner bearer token}"

# Token charset gate: only [A-Za-z0-9_-] allowed. A newline or space
# in the token would inject extra env lines into the heredoc.
if [[ ! "$TOKEN" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "ERROR: CIFARM_TOKEN may only contain [A-Za-z0-9_-]" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git docker.io python3 python3-venv

# The dependency image is built with a plain `docker build`
# (server/ci_runner/_sandbox.py::_ensure_image). The Dockerfile is
# deliberately free of BuildKit-only instructions so ANY docker build
# works, but the buildx plugin is still worth having: with it the CLI
# routes `docker build` through BuildKit instead of the deprecated legacy
# builder. Ubuntu ships the plugin as `docker-buildx`, Docker's own apt
# repo as `docker-buildx-plugin` - install whichever exists, and never fail
# the install over it: a missing plugin costs a slower build, not a broken
# one. Installing neither is what left a live farm unable to build its
# image, failing every dispatch with "--mount option requires BuildKit".
apt-get install -y docker-buildx 2>/dev/null || apt-get install -y docker-buildx-plugin 2>/dev/null || {
    echo "NOTE: no docker buildx plugin installed - the image will still" >&2
    echo "      build, with the legacy builder (slower, deprecated upstream)." >&2
}
if docker buildx version >/dev/null 2>&1; then
    echo "docker buildx: $(docker buildx version | head -1)"
else
    echo "NOTE: docker buildx unavailable - using the legacy docker builder." >&2
fi

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

# The data dir holds the runner's warm CI trees (agentland_ws/<slug>-ci,
# see server/ci_runner/_trees.py) and per-run tmp roots. It must live
# OUTSIDE the checkout: those trees are untracked and .gitignore does not
# cover them, so any `git clean -xdf` in the repo takes every warm tree with
# it - and an in-checkout data dir is also what makes config.py print its
# "DB_PATH is inside the repo" warning at every start. Mirrors config.py's
# own convention (REPO_DIR.parent / "agent_land_data").
DATA_DIR="${AGENTLAND_DATA_DIR:-$(dirname "$REPO_DIR")/agent_land_farm_data}"
LEGACY_DATA_DIR="$REPO_DIR/ci_farm/data"
if [ -d "$LEGACY_DATA_DIR" ] && [ "$LEGACY_DATA_DIR" != "$DATA_DIR" ]; then
    # One-time move off the in-checkout path. Only rebuildable caches live
    # here (trees + tmp roots), so a failure warns, never blocks.
    if [ -d "$DATA_DIR" ]; then
        # `mv src dst` NESTS src inside an existing dst and still exits 0,
        # which would strand the warm trees at $DATA_DIR/data for good: the
        # legacy path is then gone, so this block never retries. Never nest.
        echo "WARNING: $DATA_DIR already exists - not moving $LEGACY_DATA_DIR." >&2
        echo "         Merge it by hand to keep the warm CI trees:" >&2
        echo "           mv $LEGACY_DATA_DIR/* $DATA_DIR/" >&2
    elif mv "$LEGACY_DATA_DIR" "$DATA_DIR" 2>/dev/null; then
        echo "moved data dir $LEGACY_DATA_DIR -> $DATA_DIR"
    else
        echo "WARNING: could not move $LEGACY_DATA_DIR to $DATA_DIR." >&2
        echo "         Move it by hand: left inside the checkout it is one" >&2
        echo "         `git clean` away from every warm CI tree." >&2
    fi
fi
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
AGENTLAND_DATA_DIR="$DATA_DIR"
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
