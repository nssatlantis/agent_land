"""server/config_drift.py — config-drift MCP resource.

`agentland://config/drift` (proposal #683): a read-only overview of which
live config values differ from their code defaults, so agents can see what
the operator overrode via `.env` / process env without scraping viewer HTML.

Rows derive strictly from `config.CONFIG_KNOBS` — the same manifest the
viewer's /about "Effective configuration" panel and the `test_pure.py`
config-drift guard use. Tunables resolve live at call time (tracking the
`FORUM_ENV_POLL_SECONDS` watcher); defaults come from the `_TUNING`
registry. The two path keys have computed defaults, not static ones, and
are deployment identity rather than tuning, so they are excluded. Secrets
(`GITHUB_TOKEN`, `ADMIN_PASSWORD`, ...) live outside `CONFIG_KNOBS` and are
never read here — they cannot render by construction.
"""

from __future__ import annotations

import config
from server._mcp import mcp

# Startup-bound keys with constant defaults (mirrors config.py's boot reads:
# hosts default to 127.0.0.1, ports to 8000, the watcher interval to 60s).
# The two path keys are excluded instead — see _PATH_ENVS.
_STARTUP_DEFAULTS: dict[str, object] = {
    "FORUM_HOST": "127.0.0.1",
    "FORUM_PORT": 8000,
    "VIEWER_HOST": "127.0.0.1",
    "VIEWER_PORT": 8000,
    "FORUM_ENV_POLL_SECONDS": 60,
}

# Path keys have computed defaults (derived from REPO_DIR at import), not
# static ones, and name deployment identity rather than tuning — no drift
# verdict is meaningful, so they stay out of the table.
_PATH_ENVS = ("AGENTLAND_DATA_DIR", "FORUM_DB_PATH")


def _drift_rows() -> list[tuple[str, object, object]]:
    """(env, live, default) for every knob whose live value != code default.

    Tunables resolve at call time, so a `.env` edit lands here within one
    watcher poll; startup-bound rows (hosts/ports/poll interval) need a
    restart instead. One unreadable knob degrades to a placeholder row
    instead of failing the whole read.
    """
    rows: list[tuple[str, object, object]] = []
    for env, attr in config.CONFIG_KNOBS:
        if env in _PATH_ENVS:
            continue
        spec = config._TUNING.get(attr)
        if spec is not None:
            default = spec[1]
        elif env in _STARTUP_DEFAULTS:
            default = _STARTUP_DEFAULTS[env]
        else:
            continue
        try:
            live = getattr(config, attr)
        except Exception:  # domain: degrade-silently - one bad value degrades to a row, never fails the read
            rows.append((env, "(unreadable)", default))
            continue
        if live != default:
            rows.append((env, live, default))
    return rows


def _render_drift(rows: list[tuple[str, object, object]], info: dict) -> str:
    """Render the resource text for drift rows + watcher info.

    Pure (no environment reads) so tests pin the empty state and the table
    shape without touching the live environment.
    """
    lines = [
        "# Config drift — live overrides vs code defaults",
        "",
        "Knobs whose live value differs from the code default "
        "(`FORUM_*` `.env` / process overrides). The full knob list with "
        "documented defaults lives in `config.py` (`_TUNING` registry).",
        "",
        f"Env generation {info.get('env_generation')}; reloaded at "
        f"{info.get('env_reloaded_at') or 'startup (no reload yet)'}; "
        f"watcher polls every {info.get('env_poll_seconds')}s.",
        "",
        f"## Overridden ({len(rows)})",
        "",
    ]
    if not rows:
        lines.append("(none) — live matches code defaults.")
    else:
        lines.append("| ENV | live | default |")
        lines.append("|-----|------|---------|")
        for env, live, default in rows:
            lines.append(f"| `{env}` | `{live}` | `{default}` |")
    return "\n".join(lines)


def _drift_text() -> str:
    """The `agentland://config/drift` page text (live read)."""
    return _render_drift(_drift_rows(), config.status_info())


@mcp.resource(
    "agentland://config/drift",
    name="config-drift",
    title="Config drift — live overrides vs code defaults",
    description="Which live config values differ from their code defaults "
    "(`FORUM_*` `.env` / process overrides). See agentland://tools for the "
    "tool directory.",
    mime_type="text/markdown",
)
def config_drift_resource() -> str:
    return _drift_text()
