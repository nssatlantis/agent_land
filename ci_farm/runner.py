"""Standalone CI runner for the CI farm (proposal #667, PR 1).

Runs on a spare machine and serves one JSON API (POST /run, GET
/health). It binds the exact execution path of server/ci_runner: it
imports _trees/_sandbox/_slots/_runs from the pinned repo checkout, so
the bytes executed are identical to the host's (the parity pin).

Modes:
  "main"  - reference run on origin/main (host mode name: "native")
  "local" - overlay of payload["files"] onto origin/main

Usage:
  CIFARM_TOKEN=secret python ci_farm/runner.py --bind 0.0.0.0 --port 8731

Env:
  CIFARM_TOKEN       bearer token required on POST /run (required)
  CIFARM_REPO_DIR    repo checkout to import from (default: the repo
                     root containing this file)
  AGENTLAND_DATA_DIR data dir (default: <repo>/ci_farm/data)
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RUNNER_VERSION = 1

# Body cap: 10 MB max request body (prevents OOM/disk-fill from a
# token holder). Files in local mode are additionally capped.
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_FILES_COUNT = 50
MAX_FILES_TOTAL_BYTES = 5 * 1024 * 1024
_BASE_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")

# extra_env allowlist: only these keys may be injected into the sandbox.
_EXTRA_ENV_ALLOWLIST = frozenset(
    {
        "AGENTLAND_BENCH_ANCHOR",
        "AGENTLAND_BENCH_BASE",
        "AGENTLAND_BENCH_LABEL",
    }
)
_EXTRA_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXTRA_ENV_MAX_VALUE_LEN = 8192  # widened from 256 for anchor medians round-trip

_CHECKS_TO_SCRIPT = {
    "tests": "tests/run_ci.py",
    "static": "tests/run_static.py",
    "format": "tests/run_format.py",
    "benchmarks": "tests/benchmark_github.py",
    "db_benchmark": "tests/test_benchmark.py",
    "db_bench": "tests/test_benchmark.py",
}

_mods: dict | None = None


def _repo_root() -> str:
    env = os.environ.get("CIFARM_REPO_DIR")
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir() -> str:
    dd = os.environ.get("AGENTLAND_DATA_DIR")
    if not dd:
        root = _repo_root()
        dd = os.path.join(root, "ci_farm", "data")
        os.makedirs(dd, exist_ok=True)
        os.environ["AGENTLAND_DATA_DIR"] = dd
    return dd


def _bootstrap() -> dict:
    """Import the host CI modules from the pinned checkout (idempotent).

    Returns {"config", "runs", "sandbox", "slots", "trees"} - the same
    module objects the host server uses, so the runner delegates to the
    host's exact functions (the parity pin).
    """
    global _mods
    if _mods is not None:
        return _mods
    root = _repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    _data_dir()
    import config
    import server.ci_runner._runs as runs
    import server.ci_runner._sandbox as sandbox
    import server.ci_runner._slots as slots
    import server.ci_runner._trees as trees

    _mods = {
        "config": config,
        "runs": runs,
        "sandbox": sandbox,
        "slots": slots,
        "trees": trees,
    }
    return _mods


def _check_token(token: str, expected: str) -> bool:
    if not token or not expected:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def _validate_extra_env(extra_env: object) -> dict[str, str] | None:
    """Validate and sanitize the extra_env dict. Returns None if absent,
    a clean dict if valid, raises ValueError if invalid."""
    if extra_env is None:
        return None
    if not isinstance(extra_env, dict):
        raise ValueError("extra_env must be a dict or absent")
    clean: dict[str, str] = {}
    for k, v in extra_env.items():
        if not isinstance(k, str) or not _EXTRA_ENV_KEY_RE.match(k):
            raise ValueError(f"extra_env key {k!r} is not a valid identifier")
        if k not in _EXTRA_ENV_ALLOWLIST:
            raise ValueError(f"extra_env key {k!r} is not in the allowlist")
        if not isinstance(v, str):
            raise ValueError(f"extra_env value for {k!r} must be a string")
        if len(v) > _EXTRA_ENV_MAX_VALUE_LEN:
            raise ValueError(
                f"extra_env value for {k!r} exceeds {_EXTRA_ENV_MAX_VALUE_LEN} chars"
            )
        clean[k] = v
    return clean


def _validate_files(files: object) -> list[dict]:
    """Validate the files list for local mode. Raises ValueError if invalid."""
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a non-empty list")
    if len(files) > MAX_FILES_COUNT:
        raise ValueError(f"files exceeds max count {MAX_FILES_COUNT}")
    total = 0
    for i, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"files[{i}] must be a dict")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"files[{i}].path must be a non-empty string")
        content = entry.get("content", "")
        if not isinstance(content, str):
            raise ValueError(f"files[{i}].content must be a string")
        total += len(content.encode("utf-8"))
        if total > MAX_FILES_TOTAL_BYTES:
            raise ValueError(f"files total bytes exceeds {MAX_FILES_TOTAL_BYTES}")
    return files


def _run_job(payload: dict) -> dict:
    """Run one CI job exactly the way the host would.

    Returns the host-shaped result dict (ok, exit_code, summary,
    output_tail, ... plus mode/base_sha keys) so the host dispatch layer
    can map it 1:1 onto a local run result.
    """
    mods = _bootstrap()
    config = mods["config"]
    runs = mods["runs"]
    sandbox = mods["sandbox"]
    trees = mods["trees"]

    checks = str(payload.get("checks", "tests"))
    script_rel = _CHECKS_TO_SCRIPT.get(checks)
    if script_rel is None:
        return {"ok": False, "error": f"unknown checks: {checks}"}
    mode = str(payload.get("mode", "main"))
    base_sha = payload.get("base_sha")
    if base_sha is not None:
        if not isinstance(base_sha, str) or _BASE_SHA_RE.fullmatch(base_sha) is None:
            return {"ok": False, "error": "base_sha must be a 40-char hex string"}
    try:
        extra_env = _validate_extra_env(payload.get("extra_env"))
    except ValueError as exc:
        return {"ok": False, "error": f"invalid extra_env: {exc}"}

    data_dir = _data_dir()
    tmp_root = tempfile.mkdtemp(prefix="agentland_farm_", dir=data_dir)
    try:
        if mode == "local":
            if payload.get("base_sha") is not None:
                # A pinned base is main-mode only: silently overwriting it
                # with latest-main would measure the wrong tree.
                return {"ok": False, "error": "base_sha is main-mode only"}
            try:
                files = _validate_files(payload.get("files"))
            except ValueError as exc:
                return {"ok": False, "error": f"invalid files: {exc}"}
            tree, head_sha, merge_info = trees._prepare_local_tree(files, slot=0)
            sandboxed = True
            image_tag = sandbox._ensure_image(tree, merge_info["base"])
            sandbox._ensure_tree_traversable(tree, head_sha)
            argv, container_name = sandbox._sandbox_argv(
                tree,
                image_tag,
                script_rel,
                extra_env=extra_env,
                mypy_cache_host_dir=sandbox._mypy_host_dir(0),
                ruff_cache_host_dir=sandbox._ruff_host_dir(0),
            )
            env = runs._child_env(tmp_root)
            base_sha = merge_info.get("base") or head_sha
        elif mode == "main":
            tree, head_sha = trees._prepare_tree(slot=0)
            if base_sha is not None:
                check = subprocess.run(
                    ["git", "-C", tree, "cat-file", "-e", base_sha],
                    capture_output=True,
                )
                if check.returncode != 0:
                    return {"ok": False, "error": f"base_sha {base_sha} not found"}
                reset = subprocess.run(
                    ["git", "-C", tree, "reset", "--hard", base_sha],
                    capture_output=True,
                )
                if reset.returncode != 0:
                    return {"ok": False, "error": f"could not reset to {base_sha}"}
                head_sha = base_sha
            sandboxed = bool(
                config.CI_RUN_NATIVE_SANDBOX
                and config.CI_RUN_BRANCH_ENABLED
                and sandbox._docker_available()
            )
            if sandboxed:
                image_tag = sandbox._ensure_image(tree, head_sha)
                sandbox._ensure_tree_traversable(tree, head_sha)
                argv, container_name = sandbox._sandbox_argv(
                    tree,
                    image_tag,
                    script_rel,
                    extra_env=extra_env,
                    mypy_cache_host_dir=sandbox._mypy_host_dir(0),
                    ruff_cache_host_dir=sandbox._ruff_host_dir(0),
                )
            else:
                argv = [sys.executable, script_rel]
                container_name = None
            env = runs._child_env(tmp_root)
        else:
            return {"ok": False, "error": f"unknown mode: {mode}"}

        pieces = sandbox._execute(
            argv,
            tree,
            config.CI_RUN_TIMEOUT_SECONDS,
            config.CI_RUN_TAIL_BYTES,
            config.CI_RUN_MAX_RETAINED_BYTES,
            env=env,
            container_name=container_name,
        )
        result: dict = {
            "checks": checks,
            "mode": mode,
            "sandboxed": sandboxed,
        }
        if mode == "local":
            result["base_sha"] = base_sha
            result["merge_conflict"] = False
            result["local"] = True
        if base_sha is not None:
            result["executed_base_sha"] = base_sha
        result.update(pieces)
        if mode == "main" and checks == "tests":
            static_result = (
                (result.get("summary") or {}).get("static", {}).get("result")
            )
            if static_result == "skipped":
                result["host_fallback_static_skipped"] = True
        result["head_sha"] = head_sha
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


class FarmHandler(BaseHTTPRequestHandler):
    """One JSON response per request; single-flight execution."""

    expected_token: str = ""
    lock: threading.Lock = threading.Lock()

    def log_message(self, fmt: str, *args) -> None:
        # domain: degrade-silently - the runner stays quiet; errors ride
        # the JSON payload, not stderr
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            docker_ok = False
            try:
                mods = _bootstrap()
                docker_ok = mods["sandbox"]._docker_available()
            except Exception:
                pass
            self._json(
                200,
                {
                    "ok": True,
                    "version": RUNNER_VERSION,
                    "busy": self.lock.locked(),
                    "docker_available": docker_ok,
                    "active_runs": 1 if self.lock.locked() else 0,
                },
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._json(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not _check_token(
            auth[7:], self.expected_token
        ):
            self._json(401, {"error": "unauthorized"})
            return
        # Acquire the lock BEFORE reading the body: a busy runner
        # rejects immediately without consuming the upload.
        if not self.lock.acquire(blocking=False):
            self._json(409, {"error": "runner busy; retry later"})
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    self._json(400, {"error": "bad json body"})
                    return
                if length > MAX_BODY_BYTES:
                    self._json(413, {"error": "body exceeds 10 MB limit"})
                    return
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._json(400, {"error": "bad json body"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "body must be a JSON object"})
                return
            try:
                self._json(200, _run_job(payload))
            except Exception as exc:
                sys.stderr.write(f"ci_farm runner error: {exc}\n")
                self._json(500, {"error": "internal runner error"})
        finally:
            self.lock.release()


class FarmRunner:
    """ThreadingHTTPServer wrapper: one runner, single-flight execution."""

    def __init__(self, host: str, port: int, token: str) -> None:
        if not token:
            raise ValueError("token required")
        FarmHandler.expected_token = token
        FarmHandler.lock = threading.Lock()
        self.server = ThreadingHTTPServer((host, port), FarmHandler)
        self.port = self.server.server_address[1]

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CI farm runner")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--token", default=os.environ.get("CIFARM_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("CIFARM_TOKEN (or --token) is required")
    farm = FarmRunner(args.bind, args.port, args.token)
    print(f"CI farm runner v{RUNNER_VERSION} listening on {args.bind}:{farm.port}")
    farm.serve_forever()


if __name__ == "__main__":
    main()
