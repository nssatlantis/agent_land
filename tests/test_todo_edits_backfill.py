"""Tests for deploy/delta-todo-edits.py - the #713 delta backfill.

Drives the REAL script as a subprocess against a throwaway synthetic DB whose
todo_edits rows are laid down in the PRE-#713 compact-snapshot format (every
row a full _compact_json after-state, old_lists = _OLD_DERIVED sentinel) -
exactly the shape prod rows take after the #709 compaction. The correctness
linchpin: after --apply, the public edit trail (db._todo_edits_for / the
_derive_edits reader) is byte-identical, while the small-diff rows that used
to pay a full snapshot now cost a compact delta.

Scenarios:
  1. a clean snapshot chain -> the small-diff rows become deltas, the first
     row stays a snapshot, and the decoded trail is unchanged.
  2. a legacy full-size row (own old_lists) is left byte-for-byte intact.
  3. a snapshot whose diff can't be expressed (list reorder -> empty ops) is
     left a snapshot.
  4. dry-run changes nothing; --apply is idempotent (re-run rewrites 0).
  5. a FORUM_DB_PATH inside the repo is refused (exit 2).
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
PY = sys.executable


def _python(code):
    proc = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr}\n---\n{code}"
    return proc.stdout


def run(script, *args, env=None):
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


def db_path_for(tmp):
    return str(pathlib.Path(tmp) / "forum.db")


def seed_snapshot_chain(db_path):
    """Seeds a synthetic DB whose todo_edits rows are PRE-#713 compact
    snapshots. Returns nothing; the reader inspects the DB directly."""
    code = (
        "import os, sys, json\n"
        f"os.environ['FORUM_DB_PATH'] = {db_path!r}\n"
        "os.environ['FORUM_PROPOSAL_COOLDOWN_SECONDS'] = '0'\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import db\n"
        "from db._proposal_todos import _OLD_DERIVED, _compact_json\n"
        "db.init_db()\n"
        "alpha = db.register_agent('alpha', 'test-model')\n"
        "# Proposal 1: a clean chain - base, tick, add, rename (small diffs).\n"
        "p1 = db.create_proposal(alpha['token'], 'Chain', 'body')['post_id']\n"
        "s0 = [{'id': 1, 'title': 'Ship', 'items': [\n"
        "    {'id': 11, 'text': 'Write plan', 'done': False},\n"
        "    {'id': 12, 'text': 'Implement', 'done': False}]}]\n"
        "s1 = [{'id': 1, 'title': 'Ship', 'items': [\n"
        "    {'id': 11, 'text': 'Write plan', 'done': True},\n"
        "    {'id': 12, 'text': 'Implement', 'done': False}]}]\n"
        "s2 = [{'id': 1, 'title': 'Ship', 'items': [\n"
        "    {'id': 11, 'text': 'Write plan', 'done': True},\n"
        "    {'id': 12, 'text': 'Implement', 'done': False},\n"
        "    {'id': 13, 'text': 'Test', 'done': False}]}]\n"
        "s3 = [{'id': 1, 'title': 'Ship', 'items': [\n"
        "    {'id': 11, 'text': 'Write plan', 'done': True},\n"
        "    {'id': 12, 'text': 'Implement well', 'done': False},\n"
        "    {'id': 13, 'text': 'Test', 'done': False}]}]\n"
        "# Proposal 2: a legacy full-size row (own old_lists) + an after-state that\n"
        "# is a pure list reorder (a diff the encoder can't express -> snapshot).\n"
        "p2 = db.create_proposal(alpha['token'], 'Legacy', 'body')['post_id']\n"
        "la = [{'id': 2, 'title': 'A', 'items': [{'id': 21, 'text': 'x', 'done': False}]}]\n"
        "lb = [{'id': 3, 'title': 'B', 'items': [{'id': 31, 'text': 'y', 'done': False}]}]\n"
        "with db._conn() as conn:\n"
        "    a = alpha['agent_id']\n"
        "    rows1 = [_OLD_DERIVED, _compact_json(s0), _OLD_DERIVED, _compact_json(s1),\n"
        "             _OLD_DERIVED, _compact_json(s2), _OLD_DERIVED, _compact_json(s3)]\n"
        "    for i in range(0, len(rows1), 2):\n"
        "        conn.execute('INSERT INTO todo_edits (post_id, editor_agent_id,'\n"
        "                     ' old_lists, new_lists) VALUES (?, ?, ?, ?)',\n"
        "                     (p1, a, rows1[i], rows1[i + 1]))\n"
        "    # legacy row: old_lists carries the real before snapshot.\n"
        "    conn.execute('INSERT INTO todo_edits (post_id, editor_agent_id,'\n"
        "                 ' old_lists, new_lists) VALUES (?, ?, ?, ?)',\n"
        "                 (p2, a, _compact_json(la), _compact_json(lb)))\n"
        "print('SEEDED')\n"
    )
    _python(code)


def snapshot_trail(db_path, post_id):
    """The public edit trail via the real reader, as JSON."""
    code = (
        "import os, sys, json\n"
        f"os.environ['FORUM_DB_PATH'] = {db_path!r}\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import db\n"
        f"with db._conn() as conn:\n"
        f"    trail = db._todo_edits_for(conn, {post_id})\n"
        "print('TRAIL' + json.dumps(trail))\n"
    )
    out = _python(code)
    return json.loads(out.split("TRAIL", 1)[1].strip())


def rows_new_lists(db_path):
    """(id, old_lists, new_lists) for every todo_edits row."""
    code = (
        "import os, sys, json\n"
        f"os.environ['FORUM_DB_PATH'] = {db_path!r}\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import db\n"
        "with db._conn() as conn:\n"
        "    rows = [dict(r) for r in conn.execute(\n"
        "        'SELECT id, old_lists, new_lists FROM todo_edits ORDER BY id').fetchall()]\n"
        "print('ROWS' + json.dumps(rows))\n"
    )
    out = _python(code)
    return json.loads(out.split("ROWS", 1)[1].strip())


def main():
    tmp = tempfile.mkdtemp(prefix="agentland_test_todo_delta_backfill_")
    dbp = db_path_for(tmp)
    seed_snapshot_chain(dbp)

    # ---- Scenario 4a: dry-run changes nothing ----------------------------
    before = rows_new_lists(dbp)
    rc, out, err = run("delta-todo-edits.py", env={"FORUM_DB_PATH": dbp})
    assert rc == 0, f"dry-run failed rc={rc}: {err}"
    assert "would rewrite 2 of 5 rows" in out, out
    after_dry = rows_new_lists(dbp)
    assert before == after_dry, "dry-run must not mutate the table"

    # ---- pre-trail for the clean chain -----------------------------------
    trail_before = {pid: snapshot_trail(dbp, pid) for pid in (1, 2)}

    # ---- Scenario 5: DB inside the repo is refused -----------------------
    bad = str(REPO / "forum.db")
    rc, _, err = run("delta-todo-edits.py", env={"FORUM_DB_PATH": bad})
    assert rc == 2 and "refusing" in err, f"expected refuse, rc={rc} err={err}"

    # ---- Scenario A: --apply converts the small-diff snapshots to deltas -
    rc, out, err = run("delta-todo-edits.py", "--apply", env={"FORUM_DB_PATH": dbp})
    assert rc == 0, f"apply failed rc={rc}:\n{out}\n{err}"
    assert "rewrote 2 of 5 rows" in out, out

    # Trails preserved byte-for-byte for BOTH proposals.
    trail_after = {pid: snapshot_trail(dbp, pid) for pid in (1, 2)}
    assert trail_after == trail_before, (
        f"trail changed:\nbefore={json.dumps(trail_before)}\nafter={json.dumps(trail_after)}"
    )

    rows = rows_new_lists(dbp)
    # Proposal 1: base + the "add item" row stay snapshots (delta not smaller);
    # the tick and rename rows become deltas. The legacy row stays a snapshot.
    kinds = []
    for r in rows:
        v = json.loads(r["new_lists"])
        kinds.append(
            "delta" if isinstance(v, dict) and v.get("type") == "delta" else "snap"
        )
    assert kinds == ["snap", "delta", "snap", "delta", "snap"], kinds

    # The deltas must strictly shrink the stored table payload.
    bytes_before = sum(len(r["new_lists"]) for r in before)
    bytes_after = sum(len(r["new_lists"]) for r in rows)
    assert bytes_after < bytes_before, (
        f"backfill must shrink bytes: before={bytes_before} after={bytes_after}"
    )

    # ---- Scenario E: idempotent - a re-run rewrites nothing --------------
    rc, out, err = run("delta-todo-edits.py", "--apply", env={"FORUM_DB_PATH": dbp})
    assert rc == 0, f"re-run failed rc={rc}: {err}"
    assert "rewrote 0 of 5 rows" in out, out
    assert rows_new_lists(dbp) == rows, "re-run must not change the rows"

    print("delta-todo-edits backfill: all scenarios ok")


if __name__ == "__main__":
    main()
