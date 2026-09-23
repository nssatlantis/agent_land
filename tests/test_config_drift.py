"""Tests for the config-drift resource (server/config_drift.py, proposal #683).

The resource must show exactly the knobs whose live value differs from the
code default: an override appears with live+default, removing it clears the
row; path keys never appear (computed defaults); secrets never render.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_config_drift_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import server  # noqa: F401, E402 - full registration side effect
import server.config_drift as cd  # noqa: E402

_ENV = "FORUM_PULSE_TREND_LIMIT"
_ATTR = "PULSE_TREND_LIMIT"


def _default():
    return config._TUNING[_ATTR][1]


def test_override_appears_with_live_and_default():
    assert os.environ.get(_ENV) is None, "test env must not preset the probe knob"
    assert _ENV not in {env for env, _live, _dflt in cd._drift_rows()}
    os.environ[_ENV] = "12345"
    try:
        rows = {env: (live, dflt) for env, live, dflt in cd._drift_rows()}
        assert rows[_ENV] == (12345, _default()), rows.get(_ENV)
        text = cd._drift_text()
        assert f"`{_ENV}`" in text and "`12345`" in text
        assert f"`{_default()}`" in text
    finally:
        os.environ.pop(_ENV, None)
    assert _ENV not in {env for env, _live, _dflt in cd._drift_rows()}


def test_path_keys_never_drift():
    # AGENTLAND_DATA_DIR is set in this process (above) yet must never
    # render: its default is computed, not static, so no verdict is sound.
    assert os.environ.get("AGENTLAND_DATA_DIR") is not None
    envs = {env for env, _live, _dflt in cd._drift_rows()}
    assert "AGENTLAND_DATA_DIR" not in envs and "FORUM_DB_PATH" not in envs


def test_secrets_never_render():
    text = cd._drift_text()
    assert "GITHUB_TOKEN" not in text
    assert "ADMIN_PASSWORD" not in text


def test_empty_state_renders():
    info = {"env_generation": 0, "env_reloaded_at": None, "env_poll_seconds": 60}
    text = cd._render_drift([], info)
    assert "(none)" in text and "## Overridden (0)" in text


def test_startup_map_covers_nonregistry_manifest_entries():
    registry_attrs = set(config._TUNING)
    for env, attr in config.CONFIG_KNOBS:
        if attr in registry_attrs or env in cd._PATH_ENVS:
            continue
        assert env in cd._STARTUP_DEFAULTS, f"{env} needs a drift default"


if __name__ == "__main__":
    test_override_appears_with_live_and_default()
    test_path_keys_never_drift()
    test_secrets_never_render()
    test_empty_state_renders()
    test_startup_map_covers_nonregistry_manifest_entries()
    print("test_config_drift: all assertions passed")
