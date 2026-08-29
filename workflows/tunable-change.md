# Workflow: tunable-change

> Official workflow for changing a live tunable (e.g., `GZIP_*`) — prevents `750↔700` thrash (`17` commits on #590) and enforces one commit per file.
> **Advisory template - not auto-enforced as a DB workflow run** (only `create-pr` gates `repo_propose_change`).

**When:** you change `config.py:_TUNING` (`FORUM_*` default/converter) + `.env.example` docs + behavior in `server/gzip_tunable.py`.

## Steps

1. **single-source** — edit one `config.py` entry `(env_key,default,converter)` + one `.env.example` entry + one behavior file (e.g., `server/gzip_tunable.py` clamp `9-15` window). No mixed `.github/workflows/` change.
2. **content** — whole-file `content` write (not `edits` with `occurrence` unless patch), check `content_manifest` byte/sha in `repo_propose_change --dry-run`.
3. **live-reload** — relies on `config.__getattr__` + `reload_dotenv` watcher `ENV_POLL_SECONDS 60` (`server/_app.py:122` `spawn_env_watcher`) — no restart needed; validate via `_valid_reload_value` (bad `.env` skipped, not 500).
4. **verify** — `ruff check` + `ruff format --check` + `mypy` (`warn_unused_ignores`) + `python tests/run_all.py` before push.

**Auto-lifecycle:** no DB run; single PR, single commit per file (`CHARTER VI.4`).

## Changes

No separate changelog — the git history of this file is its change log.
