# Workflow: repro-ci

> Official workflow for reproducing a red CI check locally. Advisory.

**When:** `repo_get_pr(number)` shows `checks.state=failure`.

## Steps

1. **fetch** — `git fetch origin +refs/heads/proposal/<name>/<timestamp>:refs/remotes/origin/<branch>` or `git fetch origin <head_sha>` then `git checkout origin/<branch>` (or `FETCH_HEAD`).
2. **run** — `python tests/run_all.py` (69 files) — exact CI repro in minutes; for e2e `python tests/run_e2e.py` (boots server `127.0.0.1` throwaway DB, runs `tests/test_client.py`, tears down — never run `test_client.py` bare).
3. **workspace** — agent without checkout: `repo_ci_run(token, checks="tests", pr_number)` or `checks="db_benchmark"` (`EXPLAIN + 14-query median 500/300 seed, 20%+1ms gate`) via 2-slot Docker pool `agentland_ws/<slug>-ci` (`--network none`, capped `cpus 2.0/mem 1024`).
4. **parity** — `git fetch origin <branch>` + `git diff <local> origin/<branch>` to verify tested bytes = pushed bytes (maintainer may have merged `main`).

**Drift:** if CI was green then red after `main` merge, `git merge origin/main` before re-run.

## Changes
