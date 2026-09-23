"""Manual parity harness for the CI farm (proposal #667, PR 1).

Runs one job two ways and diffs the shared result keys:
  host    - the runner's own _run_job in-process (same modules, same
            pipeline - what the host dispatch layer will reuse)
  runner  - POST /run to a running farm runner over HTTP

Usage:
  python ci_farm/dispatch_test.py \
      --url http://192.168.0.41:8731 --token SECRET \
      [--checks format] [--mode main] [--file path=content]

Default: checks=format, mode=main (seconds; no docker build if the
image is warm). Exit 0 when ok/exit_code/summary agree; 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

import ci_farm.runner as runner


def post_runner(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url + "/run",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="CI farm parity harness")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--checks", default="format")
    parser.add_argument("--mode", default="main")
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="path=content (repeatable; local mode only)",
    )
    args = parser.parse_args()

    payload: dict = {"checks": args.checks, "mode": args.mode}
    if args.mode == "local":
        files = []
        for spec in args.file:
            path, sep, content = spec.partition("=")
            if not sep:
                raise SystemExit("--file expects path=content")
            files.append({"path": path, "content": content})
        payload["files"] = files

    host_result = runner._run_job(payload)
    runner_result = post_runner(args.url, args.token, payload)

    keys = ("ok", "exit_code", "timed_out", "summary", "failed_files")
    diffs = []
    for key in keys:
        a = host_result.get(key)
        b = runner_result.get(key)
        if a != b:
            diffs.append(f"{key}: host={a!r} runner={b!r}")
    print(
        f"host:   ok={host_result.get('ok')} "
        f"exit={host_result.get('exit_code')} "
        f"duration={host_result.get('duration_seconds')}s"
    )
    print(
        f"runner: ok={runner_result.get('ok')} "
        f"exit={runner_result.get('exit_code')} "
        f"duration={runner_result.get('duration_seconds')}s"
    )
    if diffs:
        print("PARITY FAIL:")
        for line in diffs:
            print(f"  {line}")
        sys.exit(1)
    print("PARITY OK: shared keys agree")
    sys.exit(0)


if __name__ == "__main__":
    main()
