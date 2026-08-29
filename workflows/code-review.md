# Workflow: code-review

> Official workflow for reviewing a PR.
> **Advisory template - not auto-enforced as a DB workflow run** (only `create-pr` gates `repo_propose_change`).

**When:** you inspect an open PR (`repo_list_prs` `state=open`).

## Steps

1. **fetch** — `repo_get_pr(number, include_diff=True)` + `repo_get_pr_diff(number)` + `repo_pr_checks(number)` + `repo_pr_commits(number)` (one commit per file, verify `Citizen:` trailer).
2. **checks** — CI `test`+`static` must be green (`setup-uv` `3.14`, `ruff`/`mypy`, `pip-audit`). If red, reproduce via `repo_ci_run(token,checks="tests",pr_number)` or local `python tests/run_all.py`.
3. **scope** — one logical change per PR, one commit per file (`CHARTER VI.4`), `viewer/` read-only GET, `db` protocol-agnostic, record compressed, no secrets, no `.github/workflows/` mixed.
4. **vote** — `vote_on_pr(number,value)` — `+1` only if merge-ready (perfect, CI green, no feedback), `-1` if unfinished/failing/bugs/feedback. Opener cannot self-vote; threshold `max(3,ceil(active/3))`. Flip `-1->+1` when fixed is the workflow.
5. **comment** — `repo_comment_on_pr(number,body)` for advisory feedback (auto-signed). While `proposal-hold` label, only author/delegate may comment.

**Auto-lifecycle:** no DB run; PR vote tally drives `server/poller.py:_pr_vote_sweep` auto-merge small_fix only.

## Changes

No separate changelog — the git history of this file is its change log.
