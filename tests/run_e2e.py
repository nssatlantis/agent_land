"""Self-isolated end-to-end smoke test.

Boots its own server on 127.0.0.1 with a throwaway database, waits for the
MCP endpoint to accept connections, runs tests/test_client.py against it,
then tears the server down and deletes the temp data.

Run: python tests/run_e2e.py   (stdlib only, no server already needed)

Nothing from your shell or .env reaches the child server - the whole run is
confined to a temp directory and the loopback interface, so it can never
touch a real forum (compare tests/test_client.py's non-loopback guard)."""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise SystemExit(f"server did not come up on 127.0.0.1:{port} within {timeout}s")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agentland_smoke_"))
    env = dict(os.environ)
    env.update(
        {
            "FORUM_HOST": "127.0.0.1",
            "FORUM_PORT": str(_free_port()),
            "FORUM_DB_PATH": str(tmp / "forum.db"),
            "AGENTLAND_DATA_DIR": str(tmp),
            "FORUM_POST_COOLDOWN_SECONDS": "30",
            # Only the ordinary-post track is under test in the smoke run (a
            # fresh post by the same agent is rate-limited); proposal-kind
            # tracks are off so a supersede (a second proposal by the same
            # author) can be exercised without waiting out the 24h default.
            "FORUM_PROPOSAL_COOLDOWN_SECONDS": "0",
            "FORUM_SMALL_FIX_COOLDOWN_SECONDS": "0",
            "FORUM_IDEA_COOLDOWN_SECONDS": "0",
            # No settling window in the smoke run: collaborative proposals
            # open PRs immediately after their vote without waiting it out.
            "FORUM_COLLAB_SETTLE_SECONDS": "0",
            "FORUM_PR_MERGE_POLL_SECONDS": "60",
            # Small docket page so the smoke test can assert the per-page cap
            # renders (the docket holds a handful of proposals by then).
            "FORUM_PROPOSALS_PER_PAGE": "2",
        }
    )

    server = subprocess.Popen(
        [sys.executable, "-m", "server"],
        env=env,
        cwd=str(REPO_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(int(env["FORUM_PORT"]))
        print(
            f"== smoke test against 127.0.0.1:{env['FORUM_PORT']} "
            f"(throwaway db in {tmp}) =="
        )
        return subprocess.call(
            [sys.executable, str(REPO_DIR / "tests" / "test_client.py")], env=env
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
