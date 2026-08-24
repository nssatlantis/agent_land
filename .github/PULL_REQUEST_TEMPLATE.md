## Summary
<!-- One sentence: what this PR does and why. -->

## Changes
<!-- Per-file bullets: file / change / why. One bullet per file. -->
- `file.py` —

## Verification
<!-- What you ran and the result. -->
- [ ] `python tests/run_all.py` passes
- [ ] `python tests/test_admin_http.py` passes
- [ ] `python tests/test_deploy.py` passes
- [ ] `python tests/run_e2e.py` passes
- [ ] ruff + mypy clean

## Compliance
- [ ] I read `AGENTS.md` before opening this PR.
- [ ] `db` stays protocol-agnostic; rules are enforced server-side in `db`.
- [ ] `viewer/` remains read-only (GET only).
- [ ] Record/text changes stay compressed (the shortest true version).
- [ ] No secrets, tokens, or API keys in code or commits.
- [ ] No `.github/workflows/` or branch-protection changes mixed into app code.
