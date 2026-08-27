# syntax=docker/dockerfile:1
# Dependency image for the sandboxed CI runner (server/ci_runner.py).
# Only requirements are baked - and only MAIN's requirements.txt, read via
# `git show <main_sha>:requirements.txt` so an unmerged PR can never choose
# what this host-side build installs.  Repository code is mounted read-only
# at run time, so the image rebuilds solely when main's requirements change
# (content-hash-tagged by _ensure_image).
# requirements.txt pins uvicorn[standard]==0.52.1 (with httptools/uvloop) — the
# image therefore carries uvicorn[standard], not plain uvicorn, exactly like
# the host venv (pip install -r requirements.txt) and workspaces (no separate
# install, they share the host's venv via sys.executable).
# BuildKit cache mount for pip (host keeps /root/.cache/pip in Docker's
# build cache, not in agentland_ws — faster rebuilds when requirements.txt
# changes, no extra I/O in the workspace pool).
FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /repo
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
