"""test_deploy.py — exercise the deploy backup / restore / wipe-guard scripts.

Run: python test_deploy.py   (stdlib only, no server needed)

Runs the REAL deploy/backup-db.py, deploy/restore-db.py and
deploy/check-db-boot.py as subprocesses against throwaway scratch
databases seeded with the real schema (db.init_db + register_agent /
create_post). It never touches the local agent_land_data forum.db or any
other live database.

Scenarios:
- fresh install (no db, no backups) -> boot check passes (exit 0)
- healthy db with a backup -> boot check passes
- wiped db (deleted, or replaced by an empty file) -> boot check fires (1);
  the empty-file case runs the full update.sh sequence (pre-start backup then
  guard) and asserts the guard names the content backup, never the empty newest
- escape hatch AGENTLAND_ALLOW_EMPTY_DB=1 -> boot check passes
- backup-db.py on a missing db -> nonzero (update.sh's WARNING path)
- restore refuses to overwrite a non-empty live db without --force
- restore --force snapshots the live db (.pre-restore.db) then restores
- restore onto a missing live db; the result boots through db.init_db()
- --file restores a named older backup
- restore rejects a non-snapshot / path --file name
- --list shows the backups with counts
- a db path inside the repo is refused and nothing is created
- a broken config.py (syntax error) makes check-db-boot / restore / backup
  ALL fail closed (exit 2, refuse to run) - the guard never acts on a guessed
  path because config.py - its single source of path resolution - won't load
- config.py resolves AGENTLAND_DATA_DIR + a scratch .env override +
  FORUM_DB_PATH (process env wins), and warns when FORUM_DB_PATH points
  inside the repo
- update.sh installs the scripts (self-sync) BEFORE the guard runs, and its
  hint and the guard's message restore a named backup via --file, never the
  newest snapshot with --force
"""

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent
DEPLOY = REPO / "deploy"
PY = sys.executable


def run(script, *args, env=None):
    """Run one deploy script as a subprocess. Env is per-call; the real
    environment never leaks AGENTLAND_ALLOW_EMPTY_DB into a scenario."""
    e = dict(os.environ)
    e.pop("AGENTLAND_ALLOW_EMPTY_DB", None)
    if env:
        e.update(env)
    proc = subprocess.run(
        [PY, str(DEPLOY / script), *args],
        env=e, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _python(code):
    proc = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr}\n---\n{code}"
    return proc.stdout


def seed(db_path, names, posts=0):
    """Build a real forum.db at db_path via db.init_db + register_agent /
    create_post. FORUM_DB_PATH and the cooldown are set before importing db.
    `names` is the full citizen list (each seed call must use fresh names)."""
    code = (
        "import os, sys\n"
        "os.environ['FORUM_DB_PATH'] = {db_path!r}\n"
        "os.environ['FORUM_POST_COOLDOWN_SECONDS'] = '0'\n"
        "sys.path.insert(0, {repo!r})\n"
        "import db\n"
        "db.init_db()\n"
        "tokens = [db.register_agent(n, 'test-model')['token'] for n in {names!r}]\n"
        "for i in range({posts}):\n"
        "    db.create_post(tokens[0], 'seed post ' + str(i), 'seed body ' + str(i))\n"
        "print('SEEDED', len(db.list_agents()))\n"
    ).format(db_path=str(db_path), repo=str(REPO), names=names, posts=posts)
    _python(code)


def boot_agents(db_path):
    """Run db.init_db() against db_path and print the agent count - proves a
    restored database boots cleanly and keeps its citizens."""
    code = (
        "import os, sys\n"
        f"os.environ['FORUM_DB_PATH'] = {str(db_path)!r}\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import db\n"
        "db.init_db()\n"
        "print('BOOT_AGENTS', len(db.list_agents()))\n"
    )
    return _python(code)


def count(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def backups_of(td):
    return sorted((pathlib.Path(td) / "backups").glob("forum.*.db"))


def _find(lines, needle):
    for i, line in enumerate(lines):
        if needle in line:
            return i
    raise AssertionError(f"no line in update.sh contains {needle!r}")


def wipe(db_path):
    for p in (db_path, pathlib.Path(str(db_path) + "-wal"), pathlib.Path(str(db_path) + "-shm")):
        if p.exists():
            p.unlink()


def main():
    # == fresh install: no db, no backups ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, (rc, err)
        assert "first run" in out, out
    print("== fresh install -> check passes ==")

    # == healthy: live citizens + a backup ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        assert len(backups_of(td)) == 1
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, (rc, err)
        assert "ok" in out, out
    print("== healthy db -> check passes ==")

    # == wiped: live db deleted, backup still has citizens ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta", "gamma"], posts=2)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        backup = backups_of(td)[-1]
        wipe(db_path)
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 1, (rc, out, err)
        assert backup.name in err, err
    print("== wiped db -> guard fires, names the backup ==")

    # == wiped but the live file exists as an empty 0-byte file ==
    # The FULL update.sh sequence: the pre-start backup runs BEFORE the guard,
    # so a post-wipe empty snapshot becomes the newest backup. The guard must
    # name the content-bearing backup (A) via --file - never the empty newest
    # (restoring that would leave the forum wiped, and a bare `restore-db.py`
    # would pick it as the default newest).
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        content = backups_of(td)[-1]
        wipe(db_path)
        db_path.write_bytes(b"")
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, "the pre-start backup must succeed even on an empty file"
        empty = backups_of(td)[-1]
        assert empty != content, "the post-wipe snapshot is a distinct file"
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 1, (rc, err)
        assert content.name in err, f"must name the content backup, got: {err}"
        assert empty.name not in err, f"must not name the empty post-wipe snapshot: {err}"
        assert f"--file {content.name}" in err, \
            f"hint must restore the content backup by name, got: {err}"
        assert "restore-db.py" in err, f"hint must reference restore-db.py, got: {err}"
        assert "--force" not in err, f"an empty live DB needs no --force: {err}"
    print("== wiped with an empty file -> guard names the content backup ==")

    # == escape hatch: a deliberate new age ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        wipe(db_path)
        rc, out, err = run("check-db-boot.py",
                           env={"FORUM_DB_PATH": str(db_path),
                                "AGENTLAND_ALLOW_EMPTY_DB": "1"})
        assert rc == 0, (rc, err)
        assert "skipping" in out, out
    print("== escape hatch -> check passes ==")

    # == backup-db.py on a missing db is nonzero (update.sh's WARNING path) ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        rc, out, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc != 0, "backup of a missing db must fail loudly"
        assert "no database" in (out + err), (out, err)
    print("== backup-db on a missing db -> nonzero ==")

    # == restore refuses to overwrite a non-empty live db ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        seed(db_path, ["gamma", "delta"])  # live now has 4 citizens
        assert count(db_path, "agents") == 4
        rc, out, err = run("restore-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 2, (rc, out, err)
        assert "REFUSING" in err, err
        assert count(db_path, "agents") == 4, "the live db must be untouched"
    print("== restore refuses a non-empty live db ==")

    # == restore --force snapshots the live db, then restores ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)  # snapshot holds 2 citizens
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        seed(db_path, ["gamma", "delta", "epsilon"])  # live now has 5
        assert count(db_path, "agents") == 5
        rc, out, err = run("restore-db.py", "--force", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, (rc, out, err)
        assert count(db_path, "agents") == 2, "the live db is back to the snapshot"
        pres = sorted((pathlib.Path(td) / "backups").glob("forum.*.pre-restore.db"))
        assert len(pres) == 1, pres
        assert count(pres[0], "agents") == 5, "the replaced live db is preserved"
    print("== restore --force snapshots then restores ==")

    # == restore onto a missing live db; the result boots through init_db ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta", "gamma"], posts=2)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        wipe(db_path)
        rc, out, err = run("restore-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, (rc, out, err)
        assert "restored" in out, out
        assert count(db_path, "agents") == 3
        out = boot_agents(db_path)
        assert "BOOT_AGENTS 3" in out, out
    print("== restore onto a missing live db -> boots ==")

    # == --file restores a named older backup ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=0)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        older = backups_of(td)[-1]
        seed(db_path, ["gamma"], posts=0)  # live now has 3
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        rc, _, err = run("restore-db.py", "--file", older.name, "--force",
                         env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        assert count(db_path, "agents") == 2, "the named older backup is restored"
    print("== --file restores a named older backup ==")

    # == restore rejects a non-snapshot / path --file name ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        rc, out, err = run("restore-db.py", "--file", "..\\evil.db",
                           env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 2, (rc, out, err)
        assert "not a backup snapshot name" in err, err
    print("== restore rejects a non-snapshot --file name ==")

    # == --list shows the backups with counts ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        seed(db_path, ["gamma"], posts=0)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        rc, out, err = run("restore-db.py", "--list", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        for b in backups_of(td):
            assert b.name in out, (b, out)
        assert "agents=2" in out and "agents=3" in out, out
    print("== --list shows the backups with counts ==")

    # == a db path inside the repo is refused; nothing is created ==
    forbidden = REPO / "_test_deploy_must_not_exist"
    db_path = forbidden / "forum.db"
    try:
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 2, (rc, out, err)
        assert "inside the repo" in err, err
        rc, out, err = run("restore-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 2, (rc, out, err)
        assert "inside the repo" in err, err
        assert not db_path.exists(), "refused before touching the filesystem"
    finally:
        shutil.rmtree(forbidden, ignore_errors=True)
    print("== db path inside the repo -> refused, nothing created ==")

    # == a broken config.py makes every deploy script fail closed ==
    # config.py is now the deploy scripts' single source of path resolution,
    # so a config that cannot be imported must never let backup/guard/restore
    # act on a guessed path. Run the REAL scripts from a fake checkout whose
    # config.py is a syntax error: _find_repo() finds the fake repo (it has
    # schema.sql + db.py), importing its config.py fails, and every script
    # must refuse to run with exit 2.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        fake = pathlib.Path(td) / "repo"
        (fake / "deploy").mkdir(parents=True)
        (fake / "schema.sql").write_text("-- broken-repo fixture\n", encoding="utf-8")
        (fake / "db.py").write_text("# stub - config.py fails before db is ever needed\n", encoding="utf-8")
        (fake / "config.py").write_text("this is not valid python :(\n", encoding="utf-8")
        for script in ("check-db-boot.py", "restore-db.py", "backup-db.py"):
            shutil.copy(DEPLOY / script, fake / "deploy" / script)
        env = dict(os.environ)
        env.pop("AGENTLAND_ALLOW_EMPTY_DB", None)
        for script in ("check-db-boot.py", "restore-db.py", "backup-db.py"):
            proc = subprocess.run(
                [PY, str(fake / "deploy" / script)],
                env=env, capture_output=True, text=True,
            )
            assert proc.returncode == 2, (script, proc.returncode, proc.stdout, proc.stderr)
            assert "config.py" in proc.stderr, (script, proc.stderr)
            assert "refusing to run" in proc.stderr, (script, proc.stderr)
    print("== broken config.py -> every deploy script refuses (exit 2) ==")

    # == config.py resolves env + .env paths (the db.py bootstrap, moved) ==
    # AGENTLAND_DATA_DIR picks the data dir; a scratch .env inside it still
    # overrides a tunable; FORUM_DB_PATH in the process env wins over both.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        scratch = pathlib.Path(td) / "data"
        scratch.mkdir()
        (scratch / ".env").write_text("FORUM_POST_COOLDOWN_SECONDS=5\n", encoding="utf-8")
        env = dict(os.environ)
        for k in ("AGENTLAND_DATA_DIR", "FORUM_DB_PATH", "FORUM_POST_COOLDOWN_SECONDS"):
            env.pop(k, None)
        env["AGENTLAND_DATA_DIR"] = str(scratch)
        env["FORUM_DB_PATH"] = str(scratch / "custom.db")
        code = (
            "import os, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "import config\n"
            f"assert config.DATA_DIR == {str(scratch)!r}, config.DATA_DIR\n"
            f"assert config.DB_PATH == {str(scratch / 'custom.db')!r}, config.DB_PATH\n"
            "assert config.POST_COOLDOWN_SECONDS == 5, config.POST_COOLDOWN_SECONDS\n"
            "assert Path(config.SCHEMA_PATH).is_file(), config.SCHEMA_PATH\n"
            "assert not Path(config.DB_PATH).resolve().is_relative_to(config.REPO_DIR)\n"
            "print('CONFIG_OK')\n"
        )
        proc = subprocess.run([PY, "-c", code], env=env, capture_output=True, text=True)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert "CONFIG_OK" in proc.stdout, proc.stdout

        # FORUM_DB_PATH inside the repo: config loads but warns (non-fatal;
        # the scripts' own hard checks still refuse to run on such a path).
        env.pop("FORUM_DB_PATH")
        env["FORUM_DB_PATH"] = str(REPO / "_config_probe_forum.db")
        code_warn = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "import config\n"
            "print('CONFIG_OK')\n"
        )
        proc = subprocess.run([PY, "-c", code_warn], env=env, capture_output=True, text=True)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert "CONFIG_OK" in proc.stdout, proc.stdout
        assert "inside the repo" in proc.stderr, proc.stderr
        assert "wiped" in proc.stderr, proc.stderr
    print("== config.py resolves env + .env paths, warns on inside-repo DB ==")

    # == two backups in the same second must not overwrite each other ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=0)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        seed(db_path, ["gamma"], posts=0)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        backups = backups_of(td)
        assert len(backups) == 2, f"same-second backups must not overwrite: {[b.name for b in backups]}"
        assert sorted(count(b, "agents") for b in backups) == [2, 3], "both snapshots survive intact"
    print("== same-second backups don't overwrite each other ==")

    # == update.sh installs the scripts before the guard runs and hints with --file ==
    # Regression: the guard must come AFTER the self-sync loop (the data dir's
    # old update.sh self-syncs only three scripts, so on the transition deploy
    # the new check-db-boot.py / restore-db.py would otherwise be missing when
    # the guard's first run executes). And the hint must not tell the operator
    # to restore the NEWEST snapshot with --force (that is the empty post-wipe
    # backup in the empty-file wipe case).
    text = (REPO / "deploy" / "update.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    sync = _find(lines, "for f in update.sh check-update.sh backup-db.py restore-db.py check-db-boot.py")
    guard = _find(lines, 'check-db-boot.py"; then')
    assert sync < guard, f"scripts must be installed (line {sync}) before the guard runs (line {guard})"
    assert "restore-db.py --list" in text, "update.sh must document --list"
    assert "--force" not in text, "update.sh must not suggest restoring the newest snapshot with --force"
    assert "WIPE-CHECK" in text, "update.sh must point at the guard's own restore command"
    boot = (REPO / "deploy" / "check-db-boot.py").read_text(encoding="utf-8")
    assert "restore-db.py" in boot, "the guard must reference the restore script"
    cmd_line = next(l for l in boot.splitlines() if "--file" in l and "backup.name" in l)
    assert "--force" not in cmd_line, f"the guard's restore command must not use --force: {cmd_line!r}"
    print("== update.sh wiring ==")

    print("test_deploy: all assertions passed")


if __name__ == "__main__":
    main()
