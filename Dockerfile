# Dependency image for the sandboxed CI runner (server/ci_runner.py).
# Only requirements are baked - and only MAIN's requirements.txt, read via
# `git show <main_sha>:requirements.txt` so an unmerged PR can never choose
# what this host-side build installs.  Repository code is mounted read-only
# at run time, so the image rebuilds solely when main's requirements change
# (content-hash-tagged by _ensure_image).
FROM python:3.14-slim
WORKDIR /repo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
