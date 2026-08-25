# Dependency image for the sandboxed CI runner (server/ci_runner.py).
# Only requirements are baked - repository code is mounted read-only at
# run time, so this image rebuilds solely when main's requirements.txt
# changes (content-hash-tagged by _ensure_image).
FROM python:3.14-slim
WORKDIR /repo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
