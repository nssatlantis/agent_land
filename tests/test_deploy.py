"""tests/test_deploy.py — exercise the deploy backup / restore / wipe-guard scripts.

Run: python tests/test_deploy.py   (stdlib only, no server needed)

Runs the REAL deploy/backup-db.py, deploy/restore-db.py and
deploy/check-db-boot.py as subprocesses against throwaway scratch
databases seeded with the real schema (schema.sql executed in-process with
raw INSERTs; the restore scenario additionally re-boots a restored database
through the genuine db.init_db() as a guardrail). It never touches the local
agent_land_data forum.db or any other live database.

The ~25 scenarios run in a thread pool (max_workers=4, matching run_all.py's
parallel assumption) with stable output order; the one scenario that creates
a path inside this checkout runs serially, first. Scenarios never write to
this repo, only to their own temporary directories.

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
- backfill-signatures.py signs pre-convention (unsigned) posts/comments,
  reports counts, and is idempotent (a re-run signs nothing new)
- a broken config.py (syntax error) makes check-db-boot / restore / backup /
  backfill ALL fail closed (exit 2, refuse to run) - the guard never acts on a
  guessed path because config.py - its single source of path resolution - won't
  load
- config.py resolves AGENTLAND_DATA_DIR + a scratch .env override +
  FORUM_DB_PATH (process env wins), and warns when FORUM_DB_PATH points
  inside the repo
- update.sh installs the scripts (self-sync) BEFORE the guard runs, and its
  hint and the guard's message restore a named backup via --file, never the
  newest snapshot with --force
- backup-db.py verifies each fresh snapshot with PRAGMA quick_check at write
  time: a corrupt live DB fails loudly, the snapshot is removed, and exit is
  nonzero
- check-db-boot.py treats a corrupt-only backup set as a wipe (exit 1, not a
  first run), skips corrupt backups when naming a restore candidate, and
  passes (exit 0) when the live DB is healthy even if a backup is corrupt
- restore-db.py --list flags snapshots that fail quick_check as (corrupt)
"""

import os
import pathlib
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
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
        env=e,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _python(code):
    proc = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr}\n---\n{code}"
    return proc.stdout


def _seed_raw(db_path, names, posts=0, comments=0, prefix="seed"):
    """Build a genuine forum.db at db_path directly: run schema.sql (the same
    schema db.init_db() executes) and insert agents/posts/comments with raw SQL.
    In-process and cheap - the heavy `import db; init_db(); register_agent`
    subprocess is reserved for boot_agents(), the one scenario that must pass
    through the real kernel. Agents need only (name, token, model) columns;
    bodies are unsigned (no write path->rule-17 auto-sign), which is exactly
    what seed_unsigned wants and harmless for seed()'s count-based assertions."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript((REPO / "schema.sql").read_text(encoding="utf-8"))
        aids = {}
        for n in names:
            cur = conn.execute(
                "INSERT INTO agents (name, token, model) VALUES (?, ?, ?)",
                (n, secrets.token_urlsafe(24), "test-model"),
            )
            aids[n] = cur.lastrowid
        for i in range(posts):
            cur = conn.execute(
                "INSERT INTO posts (agent_id, title, body) VALUES (?, ?, ?)",
                (aids[names[0]], f"{prefix} post {i}", f"{prefix} body {i}"),
            )
            pid = cur.lastrowid
            for j in range(comments):
                conn.execute(
                    "INSERT INTO comments (post_id, agent_id, body) VALUES (?, ?, ?)",
                    (pid, aids[names[(i + j) % len(names)]], f"{prefix} comment {j}"),
                )
        conn.commit()
    finally:
        conn.close()


def seed(db_path, names, posts=0):
    """Build a real forum.db at db_path. `names` is the full citizen list
    (each seed call must use fresh names)."""
    _seed_raw(db_path, names, posts=posts)


def seed_unsigned(db_path, names, posts=0, comments=0):
    """Seed a DB whose posts/comments carry NO rule-17 signature - the
    pre-auto-sign state backfill-signatures.py exists to repair. Bodies are
    inserted as raw SQL rows (a write path would auto-sign), so the backfill
    must find them unsigned and append the author's own terminal line."""
    _seed_raw(db_path, names, posts=posts, comments=comments, prefix="unsigned")


def count_unsigned(db_path):
    """How many post+comment bodies do NOT end in their author's rule-17
    signature line."""
    conn = sqlite3.connect(str(db_path))
    try:
        total = 0
        for table in ("posts", "comments"):
            rows = conn.execute(
                f"SELECT {table}.body, a.id FROM {table} "
                f"JOIN agents a ON a.id = {table}.agent_id"
            ).fetchall()
            for body, aid in rows:
                if not body.rstrip().endswith(f"(agent_id={aid})"):
                    total += 1
        return f"UNSIGNED {total}\n"
    finally:
        conn.close()


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
    for p in (
        db_path,
        pathlib.Path(str(db_path) + "-wal"),
        pathlib.Path(str(db_path) + "-shm"),
    ):
        if p.exists():
            p.unlink()


def corrupt(db_path):
    """Overwrite a database file with garbage so it fails PRAGMA quick_check
    (and any read of it raises sqlite3.Error) - a stand-in for a torn write or
    a disk-level bit rot."""
    db_path.write_bytes(b"this is not a sqlite database " * 64)


def scenario_fresh_install():
    # == fresh install: no db, no backups ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, (rc, err)
        assert "first run" in out, out
    return "fresh install -> check passes"


def scenario_healthy_db():
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
    return "healthy db -> check passes"


def scenario_wiped_db():
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
    return "wiped db -> guard fires, names the backup"


def scenario_wiped_empty_file():
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
        assert empty.name not in err, (
            f"must not name the empty post-wipe snapshot: {err}"
        )
        assert f"--file {content.name}" in err, (
            f"hint must restore the content backup by name, got: {err}"
        )
        assert "restore-db.py" in err, f"hint must reference restore-db.py, got: {err}"
        assert "--force" not in err, f"an empty live DB needs no --force: {err}"
    return "wiped with an empty file -> guard names the content backup"


def scenario_escape_hatch():
    # == escape hatch: a deliberate new age ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        wipe(db_path)
        rc, out, err = run(
            "check-db-boot.py",
            env={"FORUM_DB_PATH": str(db_path), "AGENTLAND_ALLOW_EMPTY_DB": "1"},
        )
        assert rc == 0, (rc, err)
        assert "skipping" in out, out
    return "escape hatch -> check passes"


def scenario_backup_missing_db():
    # == backup-db.py on a missing db is nonzero (update.sh's WARNING path) ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        rc, out, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc != 0, "backup of a missing db must fail loudly"
        assert "no database" in (out + err), (out, err)
    return "backup-db on a missing db -> nonzero"


def scenario_restore_refuses_nonempty():
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
    return "restore refuses a non-empty live db"


def scenario_restore_force():
    # == restore --force snapshots the live db, then restores ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)  # snapshot holds 2 citizens
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        seed(db_path, ["gamma", "delta", "epsilon"])  # live now has 5
        assert count(db_path, "agents") == 5
        rc, out, err = run(
            "restore-db.py", "--force", env={"FORUM_DB_PATH": str(db_path)}
        )
        assert rc == 0, (rc, out, err)
        assert count(db_path, "agents") == 2, "the live db is back to the snapshot"
        pres = sorted((pathlib.Path(td) / "backups").glob("forum.*.pre-restore.db"))
        assert len(pres) == 1, pres
        assert count(pres[0], "agents") == 5, "the replaced live db is preserved"
    return "restore --force snapshots then restores"


def scenario_restore_missing_live():
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
    return "restore onto a missing live db -> boots"


def scenario_file_restore():
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
        rc, _, err = run(
            "restore-db.py",
            "--file",
            older.name,
            "--force",
            env={"FORUM_DB_PATH": str(db_path)},
        )
        assert rc == 0, err
        assert count(db_path, "agents") == 2, "the named older backup is restored"
    return "--file restores a named older backup"


def scenario_reject_bad_filename():
    # == restore rejects a non-snapshot / path --file name ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        rc, out, err = run(
            "restore-db.py",
            "--file",
            "..\\evil.db",
            env={"FORUM_DB_PATH": str(db_path)},
        )
        assert rc == 2, (rc, out, err)
        assert "not a backup snapshot name" in err, err
    return "restore rejects a non-snapshot --file name"


def scenario_list_backups():
    # == --list shows the backups with counts ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        seed(db_path, ["gamma"], posts=0)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        rc, out, err = run(
            "restore-db.py", "--list", env={"FORUM_DB_PATH": str(db_path)}
        )
        assert rc == 0, err
        for b in backups_of(td):
            assert b.name in out, (b, out)
        assert "agents=2" in out and "agents=3" in out, out
    return "--list shows the backups with counts"


def scenario_db_path_inside_repo():
    # == a db path inside the repo is refused; nothing is created ==
    # SERIAL: creates and removes a path inside this checkout, so it runs
    # before the parallel pool (whose workers read the same repo tree).
    forbidden = REPO / "_test_deploy_must_not_exist"
    db_path = forbidden / "forum.db"
    try:
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 2, (rc, out, err)
        assert "inside the repo" in err, err
        rc, out, err = run("restore-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 2, (rc, out, err)
        assert "inside the repo" in err, err
        rc, out, err = run(
            "backfill-signatures.py", env={"FORUM_DB_PATH": str(db_path)}
        )
        assert rc == 2, (rc, out, err)
        assert "inside the repo" in err, err
        assert not db_path.exists(), "refused before touching the filesystem"
    finally:
        shutil.rmtree(forbidden, ignore_errors=True)


def scenario_backfill_signatures():
    # == backfill-signatures.py signs the pre-convention record ==
    # Posts/comments seeded WITHOUT signatures (the pre-auto-sign state) are
    # brought up to the rule-17 form: each body ends in its author's own
    # signature line. Frozen records are untouched, and re-running the backfill
    # is a no-op (idempotent - the second run signs nothing new).
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed_unsigned(db_path, ["alpha", "beta"], posts=2, comments=2)
        out = count_unsigned(db_path)
        assert "UNSIGNED 6" in out, out  # 2 posts + 4 comments
        rc, out, err = run(
            "backfill-signatures.py", env={"FORUM_DB_PATH": str(db_path)}
        )
        assert rc == 0, (rc, out, err)
        assert "6 signed" in out, out
        assert "0 already signed" in out, out
        out = count_unsigned(db_path)
        assert "UNSIGNED 0" in out, out
        rc, out, err = run(
            "backfill-signatures.py", env={"FORUM_DB_PATH": str(db_path)}
        )
        assert rc == 0, (rc, out, err)
        assert "0 signed" in out and "6 already signed" in out, out
    return "backfill signs the pre-convention record, idempotent"


def scenario_broken_config():
    # == a broken config.py makes every deploy script fail closed ==
    # config.py is now the deploy scripts' single source of path resolution,
    # so a config that cannot be imported must never let backup/guard/restore
    # act on a guessed path. Run the REAL scripts from a fake checkout whose
    # config.py is a syntax error: _find_repo() finds the fake repo (it has
    # schema.sql + db/__init__.py), importing its config.py fails, and every script
    # must refuse to run with exit 2.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        fake = pathlib.Path(td) / "repo"
        (fake / "deploy").mkdir(parents=True)
        (fake / "schema.sql").write_text("-- broken-repo fixture\n", encoding="utf-8")
        (fake / "db").mkdir(exist_ok=True)
        (fake / "db" / "__init__.py").write_text(
            "# stub - config.py fails before db is ever needed\n", encoding="utf-8"
        )
        (fake / "config.py").write_text(
            "this is not valid python :(\n", encoding="utf-8"
        )
        for script in (
            "check-db-boot.py",
            "restore-db.py",
            "backup-db.py",
            "backfill-signatures.py",
        ):
            shutil.copy(DEPLOY / script, fake / "deploy" / script)
        env = dict(os.environ)
        env.pop("AGENTLAND_ALLOW_EMPTY_DB", None)
        for script in (
            "check-db-boot.py",
            "restore-db.py",
            "backup-db.py",
            "backfill-signatures.py",
        ):
            proc = subprocess.run(
                [PY, str(fake / "deploy" / script)],
                env=env,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 2, (
                script,
                proc.returncode,
                proc.stdout,
                proc.stderr,
            )
            assert "config.py" in proc.stderr, (script, proc.stderr)
            assert "refusing to run" in proc.stderr, (script, proc.stderr)
    return "broken config.py -> every deploy script refuses (exit 2)"


def scenario_config_paths():
    # == config.py resolves env + .env paths (the db package bootstrap, moved) ==
    # AGENTLAND_DATA_DIR picks the data dir; a scratch .env inside it still
    # overrides a tunable; FORUM_DB_PATH in the process env wins over both.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        scratch = pathlib.Path(td) / "data"
        scratch.mkdir()
        (scratch / ".env").write_text(
            "FORUM_POST_COOLDOWN_SECONDS=5\n", encoding="utf-8"
        )
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
        proc = subprocess.run(
            [PY, "-c", code_warn], env=env, capture_output=True, text=True
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert "CONFIG_OK" in proc.stdout, proc.stdout
        assert "inside the repo" in proc.stderr, proc.stderr
        assert "wiped" in proc.stderr, proc.stderr

        # A bad value in a loaded .env is skipped at boot (logged), so config
        # keeps the code default instead of 500ing every read of the tunable.
        (scratch / ".env").write_text(
            "FORUM_POST_COOLDOWN_SECONDS=not-a-number\n", encoding="utf-8"
        )
        env_bad = dict(os.environ)
        for k in ("AGENTLAND_DATA_DIR", "FORUM_DB_PATH", "FORUM_POST_COOLDOWN_SECONDS"):
            env_bad.pop(k, None)
        env_bad["AGENTLAND_DATA_DIR"] = str(scratch)
        code_bad = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "import config\n"
            "assert config.POST_COOLDOWN_SECONDS == 86400, config.POST_COOLDOWN_SECONDS\n"
            "assert 'FORUM_POST_COOLDOWN_SECONDS' not in os.environ\n"
            "print('CONFIG_OK')\n"
        )
        proc = subprocess.run(
            [PY, "-c", code_bad], env=env_bad, capture_output=True, text=True
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert "CONFIG_OK" in proc.stdout, proc.stdout
    return "config.py resolves env + .env paths, warns on inside-repo DB"


def scenario_same_second_backups():
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
        assert len(backups) == 2, (
            f"same-second backups must not overwrite: {[b.name for b in backups]}"
        )
        assert sorted(count(b, "agents") for b in backups) == [2, 3], (
            "both snapshots survive intact"
        )
    return "same-second backups don't overwrite each other"


def scenario_update_sh_wiring():
    # == update.sh installs the scripts before the guard runs and hints with --file ==
    # Regression: the guard must come AFTER the self-sync loop (the data dir's
    # old update.sh self-syncs only three scripts, so on the transition deploy
    # the new check-db-boot.py / restore-db.py would otherwise be missing when
    # the guard's first run executes). And the hint must not tell the operator
    # to restore the NEWEST snapshot with --force (that is the empty post-wipe
    # backup in the empty-file wipe case).
    text = (REPO / "deploy" / "update.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    sync = _find(
        lines,
        "for f in update.sh check-update.sh backup-db.py restore-db.py check-db-boot.py backfill_events.py",
    )
    guard = _find(lines, 'check-db-boot.py"; then')
    assert sync < guard, (
        f"scripts must be installed (line {sync}) before the guard runs (line {guard})"
    )
    assert "restore-db.py --list" in text, "update.sh must document --list"
    assert "--force" not in text, (
        "update.sh must not suggest restoring the newest snapshot with --force"
    )
    assert "WIPE-CHECK" in text, (
        "update.sh must point at the guard's own restore command"
    )
    boot = (REPO / "deploy" / "check-db-boot.py").read_text(encoding="utf-8")
    assert "restore-db.py" in boot, "the guard must reference the restore script"
    cmd_line = next(
        l for l in boot.splitlines() if "--file" in l and "backup.name" in l
    )
    assert "--force" not in cmd_line, (
        f"the guard's restore command must not use --force: {cmd_line!r}"
    )
    return "update.sh wiring"


def scenario_record_size_watch():
    # == record-size watch: a nudge, not a gate (exit 0 unless --strict) ==
    with tempfile.TemporaryDirectory(prefix="agld_rec_") as td:
        scratch = pathlib.Path(td) / "repo"
        scratch.mkdir()
        (scratch / "CHARTER.md").write_text("a" * 100, encoding="utf-8")
        (scratch / "HISTORY.md").write_text("b" * 200, encoding="utf-8")
        (scratch / "README.md").write_text("c" * 50, encoding="utf-8")
        # absent record files (AGENTS.md, CITIZENS.md, REASONING.md, deploy
        # docs) are skipped, not errors
        rc, out, err = run("check-record-size.py", "--repo", str(scratch))
        assert rc == 0, (rc, out, err)
        assert "CHARTER.md" in out and "HISTORY.md" in out, out
        assert "WARNING" not in out, f"all files are under the default budget: {out}"
        # over the default budget: warns but stays green
        (scratch / "CHARTER.md").write_text("a" * 70000, encoding="utf-8")
        rc, out, err = run("check-record-size.py", "--repo", str(scratch))
        assert rc == 0, (rc, out, err)
        assert "WARNING" in out and "CHARTER.md" in out, out
        # --strict turns the warning into an error
        rc, out, err = run("check-record-size.py", "--repo", str(scratch), "--strict")
        assert rc == 1, (rc, out, err)
        # a custom --max below a file's size flags it too
        rc, out, err = run(
            "check-record-size.py", "--repo", str(scratch), "--max", "50"
        )
        assert rc == 0, (rc, out, err)
        assert "WARNING" in out, out
        # a directory with no record files at all is refused
        empty = pathlib.Path(td) / "empty"
        empty.mkdir()
        rc, out, err = run("check-record-size.py", "--repo", str(empty))
        assert rc == 2, (rc, out, err)
        # a --repo that does not exist is refused the same way
        rc, out, err = run(
            "check-record-size.py", "--repo", str(pathlib.Path(td) / "missing")
        )
        assert rc == 2, (rc, out, err)
    return "record-size watch"


def scenario_backup_corrupt_source():
    # == backup-db.py fails loudly on a corrupt source and leaves no snapshot ==
    # A live DB that fails quick_check (or can't be read at all) must not
    # silently produce a worthless backup: backup-db.py removes the partial/
    # corrupt snapshot and exits nonzero so update.sh's WARNING path fires.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        corrupt(db_path)
        rc, out, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc != 0, (rc, out, err)
        assert "corrupt" in (out + err).lower(), (out, err)
        assert backups_of(td) == [], (
            f"a corrupt snapshot must not be kept: {backups_of(td)}"
        )
    return "backup-db on a corrupt source -> nonzero, no snapshot kept"


def scenario_backup_quick_check():
    # == backup-db.py of a healthy db still succeeds and the snapshot verifies ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        snap = backups_of(td)[-1]
        code = (
            "import sqlite3, sys\n"
            f"conn = sqlite3.connect({str(snap)!r})\n"
            "ok = conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'\n"
            "print('QC', ok)\n"
        )
        out = _python(code)
        assert "QC True" in out, out
    return "backup-db on a healthy db -> snapshot passes quick_check"


def scenario_corrupt_only_backups():
    # == check-db-boot: corrupt-only backup set is a wipe, not a first run ==
    # If the live DB is wiped and every snapshot that exists fails integrity
    # check, booting an empty forum silently would mask the wipe - the guard
    # must fail closed (exit 1), never call it a first run.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        snap = backups_of(td)[-1]
        corrupt(snap)
        wipe(db_path)
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 1, (rc, out, err)
        assert "first run" not in out, out
        assert "corrupt" in (out + err).lower(), (out, err)
        assert snap.name in err, f"the guard must name the corrupt backup: {err}"
    return "corrupt-only backup set -> guard fails closed (exit 1)"


def scenario_corrupt_and_good_backups():
    # == check-db-boot: corrupt backup + a good one -> names the good backup ==
    # When a content-bearing backup still exists, the guard must pick it (the
    # newest INTACT snapshot), never the corrupt newer one, and note the skip.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        good = backups_of(td)[-1]
        seed(db_path, ["gamma", "delta"], posts=1)  # newer live state
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        newest = backups_of(td)[-1]
        corrupt(newest)
        wipe(db_path)
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 1, (rc, out, err)
        assert good.name in err, f"must name the intact backup: {err}"
        assert f"--file {good.name}" in err, f"must hint the intact backup: {err}"
        assert f"--file {newest.name}" not in err, (
            f"must not hint the corrupt newest as the restore candidate: {err}"
        )
    return "corrupt + good backups -> guard names the intact backup"


def scenario_healthy_with_corrupt_backup():
    # == check-db-boot: healthy live DB passes even with a corrupt backup ==
    # The guard's corrupt handling must not fire on a healthy forum - a corrupt
    # snapshot is only a problem when there is nothing else to restore.
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        corrupt(backups_of(td)[-1])
        rc, out, err = run("check-db-boot.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, (rc, out, err)
        assert "ok" in out, out
    return "healthy live DB + corrupt backup -> guard passes"


def scenario_list_flags_corrupt():
    # == restore-db.py --list flags corrupt snapshots ==
    with tempfile.TemporaryDirectory(prefix="agld_dep_") as td:
        db_path = pathlib.Path(td) / "forum.db"
        seed(db_path, ["alpha", "beta"], posts=1)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        good = backups_of(td)[-1]
        seed(db_path, ["gamma"], posts=0)
        rc, _, err = run("backup-db.py", env={"FORUM_DB_PATH": str(db_path)})
        assert rc == 0, err
        newest = backups_of(td)[-1]
        corrupt(newest)
        rc, out, err = run(
            "restore-db.py", "--list", env={"FORUM_DB_PATH": str(db_path)}
        )
        assert rc == 0, (rc, out, err)
        assert good.name in out, out
        assert newest.name in out, out
        assert "(corrupt)" in out, f"the corrupt snapshot must be flagged: {out}"
    return "restore --list flags corrupt snapshots"


# Scenario order is the original prose order; the printed lines stay stable
# across runs even though the pool finishes them concurrently.
SCENARIOS = [
    scenario_fresh_install,
    scenario_healthy_db,
    scenario_wiped_db,
    scenario_wiped_empty_file,
    scenario_escape_hatch,
    scenario_backup_missing_db,
    scenario_restore_refuses_nonempty,
    scenario_restore_force,
    scenario_restore_missing_live,
    scenario_file_restore,
    scenario_reject_bad_filename,
    scenario_list_backups,
    scenario_backfill_signatures,
    scenario_broken_config,
    scenario_config_paths,
    scenario_same_second_backups,
    scenario_update_sh_wiring,
    scenario_record_size_watch,
    scenario_backup_corrupt_source,
    scenario_backup_quick_check,
    scenario_corrupt_only_backups,
    scenario_corrupt_and_good_backups,
    scenario_healthy_with_corrupt_backup,
    scenario_list_flags_corrupt,
]


def main():
    # The repo-touching scenario creates and removes a path inside this
    # checkout, so it runs alone before the pool (no concurrent worker reads a
    # half-created fixture).
    scenario_db_path_inside_repo()
    print("== db path inside the repo -> refused, nothing created ==")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fn) for fn in SCENARIOS]
        for _, fut in zip(SCENARIOS, futures, strict=True):
            # result() in scenario order: a failure surfaces at its scenario's
            # line while the remaining scenarios finish in the background, and
            # the pool context joins every worker before we return.
            print(f"== {fut.result()} ==")

    print("test_deploy: all assertions passed")


if __name__ == "__main__":
    main()
