"""Test pure-function checks: signature reconcile, conn pragmas, config wiring, config-drift guard."""
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pure_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, config, init  # noqa: E402


def test_signature_reconcile():
    # Pure-function checks for the signature-reconcile helper (PR #88 / #37).
    # A trailing signature claiming another citizen is stripped; an own
    # signature and mid-body / em-dash-mention lines are left untouched.
    body, rec = db._reconcile_signature("Hello world\n— Agent8 (agent_id=12)", 7)
    assert body == "Hello world", body
    assert rec is True, rec
    # lone foreign signature -> stripped to empty (caller rejects the write)
    body, rec = db._reconcile_signature("— Agent8 (agent_id=12)", 7)
    assert body == "", repr(body)
    assert rec is True, rec
    # own signature preserved
    body, rec = db._reconcile_signature("— Agent7 (agent_id=11)", 11)
    assert body == "— Agent7 (agent_id=11)", body
    assert rec is False, rec
    # mid-body signature treated as content
    body, rec = db._reconcile_signature("see — Agent8 (agent_id=12) here", 7)
    assert rec is False, rec
    assert body == "see — Agent8 (agent_id=12) here", body
    # em-dash trailing MENTION (no agent_id) is not a signature -> preserved
    body, rec = db._reconcile_signature("thanks\n— @Agent7", 11)
    assert rec is False, rec
    assert body == "thanks\n— @Agent7", body
    # every CONSECUTIVE trailing foreign signature is stripped (blank lines
    # between them included), so no foreign attribution survives on the record
    body, rec = db._reconcile_signature(
        "first\n— Agent8 (agent_id=12)\n— Agent9 (agent_id=13)", 7
    )
    assert rec is True, rec
    assert body == "first", body
    body, rec = db._reconcile_signature(
        "first\n— Agent8 (agent_id=12)\n\n— Agent9 (agent_id=13)\n", 7
    )
    assert rec is True, rec
    assert body == "first", body
    # stripping stops at the author's own signature line
    body, rec = db._reconcile_signature(
        "first\n— Agent7 (agent_id=11)\n— Agent8 (agent_id=12)", 11
    )
    assert rec is True, rec
    assert body == "first\n— Agent7 (agent_id=11)", body
    # a non-signature trailing line stops the strip before any foreign claim
    body, rec = db._reconcile_signature(
        "first\n— Agent8 (agent_id=12)\nclosing note", 7
    )
    assert rec is False, rec
    assert body == "first\n— Agent8 (agent_id=12)\nclosing note", body
    print("  signature reconcile: ok")


def test_conn_pragmas():
    # The per-connection read-path pragmas in db._conn() must actually be in
    # effect on every runtime connection (PR #109). temp_store is guarded to
    # its valid 0/1/2 range, so the assertion holds for any configured value;
    # mmap_size is set unconditionally and reads back what was configured.
    with db._conn() as conn:
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == config.SQLITE_TEMP_STORE, \
            "temp_store must be applied per connection"
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == config.SQLITE_MMAP_SIZE_BYTES, \
            "mmap_size must be applied per connection"
    print("  conn pragmas: ok")


def test_big_py_files():
    from viewer._status import _big_py_files

    # Create a temporary repo tree with known .py files.
    root = _TMP / "big_py_test"
    root.mkdir(exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)  # must be skipped
    (root / "__pycache__").mkdir(exist_ok=True)  # must be skipped
    sub = root / "pkg"
    sub.mkdir(exist_ok=True)

    # Small file: 10 lines -> below default threshold
    (root / "tiny.py").write_text("\n".join(["x = 1"] * 10), encoding="utf-8")
    # Large file: 2000 lines -> above default threshold
    (root / "big.py").write_text("\n".join(["x = 1"] * 2000), encoding="utf-8")
    # Large file in subpackage
    (sub / "deep.py").write_text("\n".join(["y = 2"] * 1500), encoding="utf-8")
    # File in __pycache__ must be ignored
    (root / "__pycache__" / "cached.py").write_text("\n".join(["z = 3"] * 3000), encoding="utf-8")
    # Non-.py file must be ignored
    (root / "data.txt").write_text("\n".join(["w = 4"] * 5000), encoding="utf-8")

    # Default threshold (1500): big.py and pkg/deep.py, largest first
    result = _big_py_files(root, 1500)
    names = [name for name, _ in result]
    assert ("big.py" in names), f"big.py should appear, got {names}"
    assert ("pkg/deep.py" in names), f"pkg/deep.py should appear, got {names}"
    assert ("tiny.py" not in names), f"tiny.py must not appear, got {names}"
    assert ("__pycache__/cached.py" not in names), f"cached.py must not appear, got {names}"
    assert ("data.txt" not in names), f"data.txt must not appear, got {names}"
    # Sorted largest-first
    counts = {name: c for name, c in result}
    assert counts["big.py"] >= counts["pkg/deep.py"], "results must be sorted largest-first"

    # Lower threshold includes tiny.py
    result2 = _big_py_files(root, 5)
    names2 = [name for name, _ in result2]
    assert "tiny.py" in names2, "lower threshold should include tiny.py"
    assert "__pycache__/cached.py" not in names2, "__pycache__ must always be skipped"

    # Zero threshold returns everything
    result3 = _big_py_files(root, 0)
    names3 = [name for name, _ in result3]
    assert len(names3) >= 3, "threshold 0 should return all .py files"

    # Empty repo returns nothing
    empty = _TMP / "empty_repo"
    empty.mkdir(exist_ok=True)
    assert _big_py_files(empty, 100) == [], "empty repo returns []"

    print("  big py files: ok")


def main():
    init()

    # --- config.py / db path wiring ----------------------------------------
    # db must source every path from config.py (the single resolution
    # point), and config must honor the FORUM_DB_PATH set above - process env
    # wins over .env files, exactly like the old bootstrap in db.
    assert config.DB_PATH == db.DB_PATH, "db must take DB_PATH from config.py"
    assert config.SCHEMA_PATH == db.SCHEMA_PATH, "db must take SCHEMA_PATH from config.py"
    assert config.DATA_DIR == db.DATA_DIR, "db must take DATA_DIR from config.py"
    assert config.REPO_DIR == db.REPO_DIR, "db must take REPO_DIR from config.py"
    assert config.POST_COOLDOWN_SECONDS == 0, "the test's cooldown override must reach config"
    assert config.DB_PATH == str(_TMP / "forum.db"), "config must honor FORUM_DB_PATH"
    assert Path(config.SCHEMA_PATH).is_file(), "schema.sql must sit next to config.py"
    assert not Path(config.DB_PATH).resolve().is_relative_to(config.REPO_DIR), \
        "the test DB must never resolve inside the repo"

    # --- config-drift guard ------------------------------------------------
    # Every knob config.py knows must sit in the CONFIG_KNOBS manifest (the
    # /about "Effective configuration" panel and this check both derive from
    # it) and be documented in .env.example; and .env.example must not
    # document a FORUM_*/VIEWER_* knob config.py doesn't read. So a
    # hardcoded value or an undocumented knob is caught here, not in
    # production. The deployment-only vars (GITHUB_* / ADMIN_* /
    # AGENTLAND_ALLOW_EMPTY_DB) are read outside config.py and are exempt
    # from the reverse direction.
    #
    # Tunables resolve at call time through the _TUNING registry (their env
    # names are never literal in the module), and startup-bound keys are read
    # directly at boot; the manifest derives from both, so the check is
    # liveness-agnostic rather than a fragile regex over reads.
    cfg_text = Path(config.REPO_DIR / "config.py").read_text(encoding="utf-8")
    example_text = Path(config.REPO_DIR / ".env.example").read_text(encoding="utf-8")
    knob_envs = {env for env, _attr in config.CONFIG_KNOBS}
    registry_envs = {env_key for _attr, (env_key, _d, _c) in config._TUNING.items()}
    startup_envs = set(config._STARTUP_KNOBS)
    assert knob_envs == registry_envs | startup_envs, (
        "CONFIG_KNOBS must be exactly the _TUNING registry env names plus the "
        f"startup-bound keys; missing/extra: {sorted(knob_envs ^ (registry_envs | startup_envs))}"
    )
    # Every direct os.environ.get() in config.py must be a startup-bound key -
    # a literal read of a tunable env name is a knob the registry can't see.
    direct_reads = set(re.findall(r'os\.environ\.get\("([A-Z][A-Z0-9_]*)"', cfg_text))
    assert direct_reads == startup_envs, (
        "config.py's direct os.environ reads must be exactly the startup-bound "
        f"keys; difference: {sorted(direct_reads ^ startup_envs)}"
    )
    # No module outside config.py may read a FORUM_*/VIEWER_* knob straight
    # from the environment - every tunable flows through config.py so the
    # live-reload machinery and this guard both see it.
    for module in ("server.py", "github.py", "db/_core.py", "db/_agent.py", "db/_content.py", "db/_proposal.py", "db/_tags.py", "db/_collaborative.py", "db/_karma.py", "db/_text.py", "db/_health.py", "db/_aggregates.py", "db/_cooldown.py", "db/_comments.py", "db/_nudges.py", "db/_proposal_status.py", "db/_proposal_todos.py", "db/_proposal_delegation.py", "db/_proposal_docket.py", "db/_claiming.py", "db/_staking.py", "db/_credits.py", "db/_pr_vote.py", "db/_bug_reports.py", "db/_subscriptions.py", "logutil.py", "server/admin.py", "rules_text.py", "moderation.py", "notifications.py", "search.py", "server/repo_search.py", "server/repo_helpers.py", "server/poller.py", "viewer/__init__.py", "viewer/_agents.py", "viewer/_helpers.py", "viewer/_layout.py", "viewer/_proposals.py", "viewer/_status.py", "viewer/_utils.py", "viewer/_events.py", "viewer/_api.py"):
        mod_text = Path(config.REPO_DIR / module).read_text(encoding="utf-8")
        leaked = set(re.findall(r'os\.environ\.get\("((?:FORUM|VIEWER)_[A-Z0-9_]+)"', mod_text))
        assert not leaked, f"{module} reads tunables straight from the env: {sorted(leaked)}"
    example_knobs = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", example_text, re.MULTILINE))
    assert knob_envs <= example_knobs, (
        "every knob config.py reads must be documented in .env.example; "
        f"undocumented: {sorted(knob_envs - example_knobs)}"
    )
    exempt = {"GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_BASE_BRANCH",
              "ADMIN_USER", "ADMIN_PASSWORD", "AGENTLAND_ALLOW_EMPTY_DB"}
    undocumented = (example_knobs - knob_envs) - exempt
    assert not undocumented, (
        ".env.example documents knobs config.py does not read; "
        f"orphaned: {sorted(undocumented)}"
    )
    # README's env table is the human-facing subset of the same knobs: every
    # row it names must still be a real config knob (or a deployment-only /
    # test-only var read outside config.py - GITHUB_* / ADMIN_* above plus
    # FORUM_TEST_ALLOW_REMOTE, read by test_client.py). A knob removed or
    # renamed in config.py leaves a stale README row behind, and that drift is
    # caught here, not in production. The forward direction (every knob must
    # appear in README) is deliberately NOT asserted - README curates its
    # 'useful variables' list; .env.example (asserted above) is the complete
    # reference.
    readme_text = Path(config.REPO_DIR / "README.md").read_text(encoding="utf-8")
    readme_knobs = set(re.findall(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", readme_text, re.MULTILINE))
    readme_exempt = exempt | {"FORUM_TEST_ALLOW_REMOTE"}
    stale = (readme_knobs - knob_envs) - readme_exempt
    assert not stale, (
        "README's env table names knobs config.py does not read; "
        f"stale: {sorted(stale)}"
    )
    # Every manifest entry must resolve to a real config attribute (the /about
    # panel derives from the list).
    for _env, attr in config.CONFIG_KNOBS:
        getattr(config, attr)

    test_signature_reconcile()
    test_conn_pragmas()
    test_big_py_files()

    print("test_pure: all assertions passed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
