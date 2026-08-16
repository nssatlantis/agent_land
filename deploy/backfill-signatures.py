#!/opt/agent_land_data/venv/bin/python
"""One-off backfill: bring live posts and comments up to the auto-sign
convention (rule 17). Runs db.backfill_signatures() against the configured
database and reports how many bodies were signed vs already signed vs
skipped. Idempotent - safe to re-run; a re-run signs nothing new. Frozen
records (report snapshots, proposal_edits) are intentionally untouched.

Not wired into update.sh: it is a deliberate, operator-invoked migration,
not part of every deploy. Run it once manually after the auto-sign PR ships:

    python deploy/backfill-signatures.py

Exit codes: 0 backfilled, 2 refused/misconfigured (cannot import config.py,
or the DB path points inside the repo - git clean -xdf would wipe it).
"""
import pathlib
import sys


def _find_repo() -> pathlib.Path:
    """The git checkout, so config.py (which owns path resolution) can be
    imported from it. From the repo checkout this is deploy/; from the
    installed data dir (no schema.sql nearby) fall back to the default deploy
    layout."""
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        if (cand / "schema.sql").exists() and (cand / "db.py").exists():
            return cand
    return pathlib.Path("/opt/agent_land")


def _import_config(repo_dir: pathlib.Path):
    """Import the app's config.py - the single source of path resolution. Fail
    closed: a backfill that guessed the DB path could rewrite the wrong
    database, so a config.py that cannot be imported means 'refuse to run'
    (exit 2), never a guess."""
    sys.path.insert(0, str(repo_dir))
    try:
        import config
    except Exception as exc:
        print(
            f"ERROR: cannot import config.py ({exc}); refusing to run. "
            "Fix config.py before booting.",
            file=sys.stderr,
        )
        sys.exit(2)
    finally:
        sys.path.pop(0)
    return config


def _main() -> int:
    repo_dir = _find_repo()
    _config = _import_config(repo_dir)
    # Same hazard update.sh guards: a DB inside the repo is wiped by
    # `git clean -xdf` on every deploy, so a backfill pointed at it would
    # rewrite a database that is about to vanish - refuse instead.
    if pathlib.Path(_config.DB_PATH).resolve().is_relative_to(repo_dir.resolve()):
        print(
            f"ERROR: database path {_config.DB_PATH} points inside the repo "
            f"({repo_dir}); refusing to run (git clean -xdf would wipe it).",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(repo_dir))
    try:
        import db
    finally:
        sys.path.pop(0)
    counts = db.backfill_signatures()
    print(
        f"signature backfill complete: {counts['signed']} signed, "
        f"{counts['already_signed']} already signed, {counts['skipped']} skipped."
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
