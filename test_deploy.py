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
