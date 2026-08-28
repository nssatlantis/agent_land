"""Regression test for the HTTP keep-alive tunable (uvicorn wiring).

Guards three things: the registry default, the env override path, and the
actual launch-site wiring -- including that the installed uvicorn version
still accepts the ``timeout_keep_alive`` parameter name we pass.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
import db  # noqa: F401


def test_default_is_thirty():
    assert config.HTTP_KEEPALIVE_TIMEOUT_SECONDS == 30, (
        "HTTP_KEEPALIVE_TIMEOUT_SECONDS default drifted; keep config.py, "
        ".env.example and README.md in sync"
    )


def test_env_override_reaches_the_call_time_read():
    key = "FORUM_HTTP_KEEPALIVE_TIMEOUT_SECONDS"
    old = os.environ.get(key)
    try:
        os.environ[key] = "7"
        assert config.HTTP_KEEPALIVE_TIMEOUT_SECONDS == 7
        os.environ[key] = "bogus"
        # Process env always wins verbatim: a non-int override raises at the
        # read (the .env-file path is the one that validates and skips).
        raised = False
        try:
            config.HTTP_KEEPALIVE_TIMEOUT_SECONDS  # noqa: B018
        except ValueError:
            raised = True
        assert raised, "a non-int override must fail loudly, not silently pass"
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def test_installed_uvicorn_accepts_the_parameter():
    import uvicorn

    cfg = uvicorn.Config(app=None, timeout_keep_alive=30)
    assert cfg.timeout_keep_alive == 30


def test_all_three_launch_sites_pass_the_tunable():
    repo = Path(config.REPO_DIR)
    expected = "timeout_keep_alive=config.HTTP_KEEPALIVE_TIMEOUT_SECONDS"
    for module in ("server/__main__.py", "viewer/__init__.py", "viewer/__main__.py"):
        text = (repo / module).read_text(encoding="utf-8")
        assert expected in text, f"{module} does not pass the keep-alive tunable"


if __name__ == "__main__":
    test_default_is_thirty()
    test_env_override_reaches_the_call_time_read()
    test_installed_uvicorn_accepts_the_parameter()
    test_all_three_launch_sites_pass_the_tunable()
    print("test_keepalive: all ok")
