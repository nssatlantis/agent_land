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
image is warm). Exit 0 when the parity-relevant keys agree; 1
otherwise. summary is compared without the run-specific wall-clock keys
(slowest_s, timings_median_ms, regressions) that can never agree across
two machines, and failed_files is compared as a set.
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


# _parse_summary (server/ci_runner/_sandbox.py) enriches `summary` with
# run-specific keys that two executions of the same tree cannot agree on
# across machines: per-file wall times (slowest_s), bench medians
# (timings_median_ms) and the timing-derived regression count. They prove
# nothing about parity, so they are dropped before the diff - the harness
# compares what the runner got right, not how fast.
_RUN_SPECIFIC_SUMMARY_KEYS = ("slowest_s", "timings_median_ms", "regressions")


def _parity_summary(value: object) -> object:
    """The summary without keys that cannot agree across two machines."""
    if not isinstance(value, dict):
        return value
    return {
        key: val for key, val in value.items() if key not in _RUN_SPECIFIC_SUMMARY_KEYS
    }


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
    parser.add_argument("--base-ref", help="local-mode rehearsal base ref")
    args = parser.parse_args()
    if args.base_ref is not None and args.mode != "local":
        raise SystemExit("--base-ref is local-mode only")

    payload: dict = {"checks": args.checks, "mode": args.mode}
    if args.mode == "local":
        files = []
        for spec in args.file:
            path, sep, content = spec.partition("=")
            if not sep:
                raise SystemExit("--file expects path=content")
            files.append({"path": path, "content": content})
        payload["files"] = files
    if args.base_ref is not None:
        payload["base_ref"] = args.base_ref

    host_result = runner._run_job(payload)
    runner_result = post_runner(args.url, args.token, payload)

    keys = (
        "ok",
        "exit_code",
        "timed_out",
        "summary",
        "failed_files",
        "error",
        "base_ref",
        "base_sha",
        "executed_base_sha",
        "local",
    )
    diffs = []
    for key in keys:
        a = _parity_summary(host_result.get(key))
        b = _parity_summary(runner_result.get(key))
        if key == "failed_files" and isinstance(a, list) and isinstance(b, list):
            a, b = set(a), set(b)
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
    # head_sha drift check: if the two sides executed different code,
    # parity is meaningless. Warn (not fail) since the runner may have
    # a slightly different checkout.
    h_sha = host_result.get("head_sha")
    r_sha = runner_result.get("head_sha")
    if h_sha and r_sha and h_sha != r_sha:
        drift = f"head_sha drift: host={h_sha} runner={r_sha}"
        if args.base_ref is not None:
            diffs.append(drift)
        else:
            print(f"WARNING: {drift}")
    if diffs:
        print("PARITY FAIL:")
        for line in diffs:
            print(f"  {line}")
        sys.exit(1)
    print("PARITY OK: shared keys agree")
    sys.exit(0)


if __name__ == "__main__":
    main()
