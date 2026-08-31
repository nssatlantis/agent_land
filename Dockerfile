# syntax=docker/dockerfile:1
# Dependency image for the sandboxed CI runner (server/ci_runner.py).
# Only requirements are baked - and only MAIN's requirements.txt and
# requirements-dev.txt, read via `git show <main_sha>:...` so an unmerged PR
# can never choose what this host-side build installs.  Repository code is
# mounted read-only at run time, so the image rebuilds solely when main's
# requirements change (content-hash-tagged by _ensure_image, which folds both
# files into the tag so a dev-deps bump also invalidates the image).
# requirements.txt pins uvicorn[standard]==0.52.1 (with httptools/uvloop) — the
# image therefore carries uvicorn[standard], not plain uvicorn, exactly like
# the host venv (pip install -r requirements.txt) and workspaces (no separate
# install, they share the host's venv via sys.executable).  requirements-dev.txt
# pins the static-check tooling (mypy/ruff/coverage/pip-audit) so the combined
# `tests` harness (tests + static) can reproduce the GitHub `static` job inside
# the sandbox at the exact pinned versions; bash is installed for `bash -n`.
# BuildKit cache mount for uv (host keeps /root/.cache/uv in Docker's
# build cache, not in agentland_ws — faster rebuilds when requirements*.txt
# change, no extra I/O in the workspace pool; --no-cache keeps image lean).
FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends git bash && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv
WORKDIR /repo
COPY requirements.txt .
COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system --no-cache -r requirements.txt -r requirements-dev.txt
