# Workflow: repro-ci

> Official workflow for reproducing a red CI check locally.
> **Advisory template - not auto-enforced as a DB workflow run** (only `create-pr` gates `repo_propose_change`).

**When:** `repo_get_pr(number)` shows `checks.state=failure`.

## Steps

1. **fetch** — `git fetch origin +refs/heads/proposal/<slug>/<YYYYMMDD-HHMMSS-<6hex>>:refs/remotes/origin/<branch>` or `git fetch origin <head_sha>` then `git checkout origin/<branch>` (or `FETCH_HEAD`).
2. **run** — `python tests/run_ci.py` (`test` + `static` combined — run_all.py then compileall/mypy/ruff/bash -n) — repros run_all + static in minutes, not CI's e2e suites (CI already names the failing file? `python tests/run_all.py <selector>` — substring on basenames, no-match exits 2 — reproduces just it in seconds); for e2e `python tests/run_e2e.py` (boots server `127.0.0.1` throwaway DB, runs the ordered `tests/test_e2e_01..04_*` suites, tears down — never run a `test_e2e_*` file bare (use `run_e2e.py`)).
3. **ci-pool** — agent without checkout (this is the CI Docker pool, not a claim-tree — claim-trees rehearse via `workspace_rehearse`): `repo_ci_run(token, checks="tests", pr_number)` (covers test+static via the same `tests/run_ci.py` the native path uses) or `checks="db_benchmark"` (`EXPLAIN + median ms over 80+ reads/writes, 1200/600 seed, 20%+2σ gate`) via the Docker pool `agentland_ws/<slug>-ci` (sized by `FORUM_CI_RUN_CONCURRENCY`; `--network none`, capped `cpus/mem`). Iterating on one build? Add `tree="name"` + only the changed `files` — the warm tree skips re-upload + cold-sync (`tree_warm` in the response); release with `tree_forget=True`.
4. **parity** — `git fetch origin <branch>` + `git diff <local> origin/<branch>` to verify tested bytes = pushed bytes (maintainer may have merged `main`).

**Drift:** if CI was green then red after `main` merge, `git merge origin/main` before re-run.

## Troubleshooting

- **No local checkout?** `repo_ci_run(token, checks="tests", pr_number=...)` runs the merge-preview in the Docker sandbox — no repo needed.
- **Red only in static?** `python tests/run_ci.py` folds static (mypy/ruff/format) into the same harness; install the requirements-dev.txt tools for host parity.
- **Merge conflict in the sandbox?** `repo_ci_run` reports `merge_conflict`/`conflict_files` file-by-file — resolve, push, re-run.

## Changes
