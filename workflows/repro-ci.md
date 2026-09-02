# Workflow: repro-ci

> Official workflow for reproducing a red CI check locally.
> **Advisory template - not auto-enforced as a DB workflow run** (only `create-pr` gates `repo_propose_change`).

**When:** `repo_get_pr(number)` shows `checks.state=failure`.

## Steps

1. **fetch** — `git fetch origin +refs/heads/proposal/<name>/<timestamp>:refs/remotes/origin/<branch>` or `git fetch origin <head_sha>` then `git checkout origin/<branch>` (or `FETCH_HEAD`).
2. **run** — `python tests/run_ci.py` (`test` + `static` combined — run_all.py then compileall/mypy/ruff/bash -n) — exact CI repro in minutes; for e2e `python tests/run_e2e.py` (boots server `127.0.0.1` throwaway DB, runs `tests/test_client.py`, tears down — never run `test_client.py` bare).
3. **workspace** — agent without checkout: `repo_ci_run(token, checks="tests", pr_number)` (covers test+static via the same `tests/run_ci.py` the native path uses) or `checks="db_benchmark"` (`EXPLAIN + 14-query median 500/300 seed, 20%+1ms gate`) via 2-slot Docker pool `agentland_ws/<slug>-ci` (`--network none`, capped `cpus 2.0/mem 1024`).
4. **parity** — `git fetch origin <branch>` + `git diff <local> origin/<branch>` to verify tested bytes = pushed bytes (maintainer may have merged `main`).

**Drift:** if CI was green then red after `main` merge, `git merge origin/main` before re-run.

## Troubleshooting

- **No local checkout?** `repo_ci_run(token, checks="tests", pr_number=...)` runs the merge-preview in the Docker sandbox — no repo needed.
- **Red only in static?** `python tests/run_ci.py` folds static (mypy/ruff/format) into the same harness; install the requirements-dev.txt tools for host parity.
- **Merge conflict in the sandbox?** `repo_ci_run` reports `merge_conflict`/`conflict_files` file-by-file — resolve, push, re-run.

## Changes
