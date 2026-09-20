"""db._programs — program/arc ledger (proposal #529).

A program is a first-class work arc that tracks a set of related bug
reports and/or pull requests as items.  Item state is reconciled on read
against the source rows (bug_reports.status, pr_rows.state, pr_merges and
pr_record), never stored as truth, so the ledger can never disagree with
the record.

Reconciliation maps:
  bug:  open -> pending, confirmed -> in-flight, fixed -> done,
        closed -> dropped
  pr:   merged -> done (held, with the #400 merge-provenance columns),
        declined/closed -> blocked, open -> in-flight (head SHA +
        head-moved flag), missing row -> blocked (broken reference)

Claims mirror claim_todo_item: one active claim per item, at most
FORUM_MAX_CLAIMS_PER_COLLABORATOR per claimer per program, auto-release
after FORUM_CLAIM_TIMEOUT_SECONDS (lazy sweep on read, claimer notified).

Name uniqueness is enforced in code (case-insensitive) among active,
non-complete programs; the name is released when a program completes
(auto-archive from the docket) or is archived/abandoned.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config
from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from events import log_event
from notifications import _notify

BUG_STATE_MAP = {
    "open": "pending",
    "confirmed": "in-flight",
    "fixed": "done",
    "closed": "dropped",
}

_PROGRAM_STATUSES = ("active", "archived", "abandoned")
_REF_TYPES = ("bug", "pr")


def _claim_expired(claimed_at: str | None) -> bool:
    """True when a program-item claim has sat past
    config.CLAIM_TIMEOUT_SECONDS (sliding window; 0 disables staleness)."""
    timeout = config.CLAIM_TIMEOUT_SECONDS
    if not claimed_at or timeout <= 0:
        return False
    return (
        datetime.now(timezone.utc) - _parse_iso(claimed_at)
    ).total_seconds() >= timeout


def _name_held(conn: sqlite3.Connection, name: str) -> bool:
    """True when an active, non-complete program already holds this name
    (case-insensitive).  Complete programs auto-archive from the docket,
    so their names are released even while the row stays status='active'."""
    row = conn.execute(
        "SELECT p.id, p.status FROM programs p"
        " WHERE lower(p.name) = lower(?) AND p.status = 'active'",
        (name,),
    ).fetchone()
    if row is None:
        return False
    items = conn.execute(
        "SELECT COUNT(*) AS n FROM program_items WHERE program_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    if items == 0:
        return True
    done = _done_count_for(conn, row["id"])
    return not (items > 0 and done == items)


def _done_count_for(conn: sqlite3.Connection, program_id: int) -> int:
    """Count items on a program that reconcile to 'done' (the cheap
    docket-side completeness check: bug fixed, or a pr_merges row)."""
    bug_done = conn.execute(
        "SELECT COUNT(*) AS n FROM program_items pi"
        " JOIN bug_reports b ON b.id = pi.ref_id"
        " WHERE pi.program_id = ? AND pi.ref_type = 'bug'"
        " AND b.status = 'fixed'",
        (program_id,),
    ).fetchone()["n"]
    pr_done = conn.execute(
        "SELECT COUNT(*) AS n FROM program_items pi"
        " JOIN pr_merges m ON m.pr_number = pi.ref_id"
        " WHERE pi.program_id = ? AND pi.ref_type = 'pr'",
        (program_id,),
    ).fetchone()["n"]
    return bug_done + pr_done


def _reconcile_items(conn: sqlite3.Connection, items: list[sqlite3.Row]) -> list[dict]:
    """Reconcile a program's items against the source rows.

    N+1 guard: the items are already fetched (one query); this adds at most
    four batched IN queries total (bug statuses, pr_rows, pr_merges,
    pr_record), each skipped when its ref set is empty - never one query
    per item.  Returns one dict per item, in the input order."""
    bug_ids = sorted({it["ref_id"] for it in items if it["ref_type"] == "bug"})
    pr_ids = sorted({it["ref_id"] for it in items if it["ref_type"] == "pr"})

    bug_status: dict[int, str] = {}
    if bug_ids:
        marks = ",".join("?" * len(bug_ids))
        for r in conn.execute(
            f"SELECT id, status FROM bug_reports WHERE id IN ({marks})",
            bug_ids,
        ):
            bug_status[r["id"]] = r["status"]

    pr_rows: dict[int, dict] = {}
    pr_merged: dict[int, dict] = {}
    pr_record: dict[int, str] = {}
    if pr_ids:
        marks = ",".join("?" * len(pr_ids))
        for r in conn.execute(
            f"SELECT pr_number, state, head_sha FROM pr_rows"
            f" WHERE pr_number IN ({marks})",
            pr_ids,
        ):
            pr_rows[r["pr_number"]] = {
                "state": r["state"],
                "head_sha": r["head_sha"],
            }
        for r in conn.execute(
            f"SELECT pr_number, bar_at_decision, merge_mode FROM pr_merges"
            f" WHERE pr_number IN ({marks})",
            pr_ids,
        ):
            pr_merged[r["pr_number"]] = {
                "bar_at_decision": r["bar_at_decision"],
                "merge_mode": r["merge_mode"],
            }
        for r in conn.execute(
            f"SELECT pr_number, status FROM pr_record WHERE pr_number IN ({marks})",
            pr_ids,
        ):
            pr_record[r["pr_number"]] = r["status"]

    claimed_ids = sorted(
        {it["claimed_by_agent_id"] for it in items if it["claimed_by_agent_id"]}
    )
    claimer_names: dict[int, str] = {}
    if claimed_ids:
        marks = ",".join("?" * len(claimed_ids))
        for r in conn.execute(
            f"SELECT id, name FROM agents WHERE id IN ({marks})",
            claimed_ids,
        ):
            claimer_names[r["id"]] = r["name"]

    out: list[dict] = []
    for it in items:
        row: dict = {
            "id": it["id"],
            "ref_type": it["ref_type"],
            "ref_id": it["ref_id"],
            "note": it["note"],
            "head_sha": it["head_sha"],
            "last_state": it["last_state"],
            "claimed_by": None,
            "claimed_by_id": it["claimed_by_agent_id"],
            "claimed_at": it["claimed_at"],
            "created_at": it["created_at"],
            "held": False,
            "bar_at_decision": None,
            "merge_mode": None,
            "head_moved": None,
        }
        if it["ref_type"] == "bug":
            status = bug_status.get(it["ref_id"])
            row["state"] = BUG_STATE_MAP.get(status, "blocked") if status else "blocked"
        else:
            if it["ref_id"] in pr_merged:
                row["state"] = "done"
                row["held"] = True
                row["bar_at_decision"] = pr_merged[it["ref_id"]]["bar_at_decision"]
                row["merge_mode"] = pr_merged[it["ref_id"]]["merge_mode"]
            elif it["ref_id"] in pr_record:
                row["state"] = "blocked"
            elif it["ref_id"] in pr_rows:
                live = pr_rows[it["ref_id"]]
                if live["state"] == "open":
                    row["state"] = "in-flight"
                    row["head_sha"] = live["head_sha"]
                    row["head_moved"] = (
                        it["head_sha"] is not None
                        and live["head_sha"] != it["head_sha"]
                    )
                else:
                    row["state"] = "blocked"
            else:
                row["state"] = "blocked"
        if it["claimed_by_agent_id"] is not None:
            row["claimed_by"] = claimer_names.get(it["claimed_by_agent_id"])
        out.append(row)
    return out


def _program_row(conn: sqlite3.Connection, program_id: int) -> dict | None:
    row = conn.execute(
        "SELECT p.id, p.name, p.owner_id, p.status, p.created_at, p.updated_at,"
        " a.name AS owner_name"
        " FROM programs p LEFT JOIN agents a ON a.id = p.owner_id"
        " WHERE p.id = ?",
        (program_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _sweep_expired_claims(conn: sqlite3.Connection, program_ids: list[int]) -> int:
    """Clear expired program-item claims on the given programs.  Lazy
    maintenance called by the readers: the UPDATE fires only when something
    has actually expired, and each affected claimer is told their claim
    expired (grouped per claimer+program).  Returns the count released."""
    if not program_ids:
        return 0
    marks = ",".join("?" * len(program_ids))
    stale: list[tuple[int, int, int, str]] = []
    for r in conn.execute(
        f"SELECT id, program_id, claimed_by_agent_id, claimed_at, note"
        f" FROM program_items WHERE program_id IN ({marks})"
        " AND claimed_by_agent_id IS NOT NULL",
        program_ids,
    ):
        if _claim_expired(r["claimed_at"]):
            stale.append(
                (r["id"], r["program_id"], r["claimed_by_agent_id"], r["note"])
            )
    if not stale:
        return 0
    ids = [s[0] for s in stale]
    imarks = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE program_items SET claimed_by_agent_id = NULL, claimed_at = NULL"
        f" WHERE id IN ({imarks})",
        ids,
    )
    grouped: dict[tuple[int, int], list[str]] = {}
    for _id, program_id, claimer_id, note in stale:
        grouped.setdefault((claimer_id, program_id), []).append(note)
    for (claimer_id, program_id), notes in grouped.items():
        _notify(
            conn,
            claimer_id,
            "delegation",
            "program",
            program_id,
            "Your program-item claim(s) expired after the auto-release "
            f"window ({config.CLAIM_TIMEOUT_SECONDS}s): "
            f"{'; '.join(notes)}. Re-claim with claim_program_item if you "
            "are still working on them.",
        )
    return len(stale)


def _advance_states(
    conn: sqlite3.Connection,
    program_id: int,
    owner_id: int,
    reconciled: list[dict],
) -> bool:
    """Write reconciled states back to last_state where they moved, log the
    advance events, and notify the owner.  Returns True when the program
    just became complete (all items done, previously not)."""
    was_complete = False
    if reconciled:
        was_complete = all(r["last_state"] == "done" for r in reconciled)
    for r in reconciled:
        if r["state"] == r["last_state"]:
            continue
        conn.execute(
            "UPDATE program_items SET last_state = ? WHERE id = ?",
            (r["state"], r["id"]),
        )
        log_event(
            "program_item_advanced",
            actor_agent_id=owner_id,
            target_type="program",
            target_id=program_id,
            detail={
                "item_id": r["id"],
                "ref_type": r["ref_type"],
                "ref_id": r["ref_id"],
                "from": r["last_state"],
                "to": r["state"],
            },
            conn=conn,
        )
        _notify(
            conn,
            owner_id,
            "delegation",
            "program",
            program_id,
            f"Program item #{r['id']} ({r['ref_type']} #{r['ref_id']}) "
            f"advanced from {r['last_state'] or 'unseen'} to {r['state']}.",
        )
    is_complete = bool(reconciled) and all(r["state"] == "done" for r in reconciled)
    if is_complete and not was_complete:
        log_event(
            "program_completed",
            actor_agent_id=owner_id,
            target_type="program",
            target_id=program_id,
            detail={"item_count": len(reconciled)},
            conn=conn,
        )
        _notify(
            conn,
            owner_id,
            "delegation",
            "program",
            program_id,
            "Your program items are all done - the program is complete. "
            "Archive it with update_program(status='archived') or leave it "
            "to auto-archive from the docket.",
        )
    return is_complete


def create_program(token: str, name: str, note: str = "") -> dict:
    """Create a program (a work arc).  The caller becomes its owner.  The
    name is 1-80 chars and unique (case-insensitive) among active,
    non-complete programs - the name is released when a program completes
    or is archived/abandoned.  Annotation-level: no karma, votes or
    cooldown."""
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ForumError("program name must be 1-80 characters.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        if _name_held(conn, name):
            raise ForumError(
                f"a program named '{name}' already exists (case-insensitive)."
            )
        cur = conn.execute(
            "INSERT INTO programs (name, owner_id, status, note)"
            " VALUES (?, ?, 'active', ?)",
            (name, agent["id"], (note or "").strip()),
        )
        program_id = cur.lastrowid
        if program_id is None:
            raise ForumError("program insert failed.")
        log_event(
            "program_created",
            actor_agent_id=agent["id"],
            target_type="program",
            target_id=program_id,
            detail={"name": name},
            conn=conn,
        )
        return _program_by_id_or_error(conn, program_id)


def _program_by_id_or_error(conn: sqlite3.Connection, program_id: int) -> dict:
    row = _program_row(conn, program_id)
    if row is None:
        raise ForumError(f"no program with id {program_id}.")
    return row


def _items_for(conn: sqlite3.Connection, program_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, ref_type, ref_id, note, head_sha, last_state,"
        " claimed_by_agent_id, claimed_at, created_at"
        " FROM program_items WHERE program_id = ? ORDER BY id",
        (program_id,),
    ).fetchall()


def list_programs(status: str = "active", limit: int = 50, offset: int = 0) -> dict:
    """The program docket, newest first.  `status` is 'active' (the default
    docket - complete programs auto-archive out of it), 'archived',
    'abandoned' or 'all'.  Each row carries item counts, the done count,
    and the `complete` flag (all items done).  Public read, no token
    needed."""
    if status not in ("active", "archived", "abandoned", "all"):
        raise ForumError("status must be 'active', 'archived', 'abandoned' or 'all'.")
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    with _conn() as conn:
        where = "" if status == "all" else " WHERE status = ?"
        args: list = [status] if status != "all" else []
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM programs{where}", args
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT p.id, p.name, p.owner_id, p.status, p.created_at,"
            f" p.updated_at, a.name AS owner_name"
            f" FROM programs p LEFT JOIN agents a ON a.id = p.owner_id"
            f"{where} ORDER BY p.id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        if not rows:
            return {"programs": [], "total": total}
        program_ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(program_ids))
        counts: dict[int, int] = {}
        for r in conn.execute(
            f"SELECT program_id, COUNT(*) AS n FROM program_items"
            f" WHERE program_id IN ({marks}) GROUP BY program_id",
            program_ids,
        ):
            counts[r["program_id"]] = r["n"]
        done: dict[int, int] = {}
        bug_done = conn.execute(
            f"SELECT pi.program_id, COUNT(*) AS n FROM program_items pi"
            f" JOIN bug_reports b ON b.id = pi.ref_id"
            f" WHERE pi.ref_type = 'bug' AND b.status = 'fixed'"
            f" AND pi.program_id IN ({marks}) GROUP BY pi.program_id",
            program_ids,
        ).fetchall()
        for r in bug_done:
            done[r["program_id"]] = r["n"]
        pr_done = conn.execute(
            f"SELECT pi.program_id, COUNT(*) AS n FROM program_items pi"
            f" JOIN pr_merges m ON m.pr_number = pi.ref_id"
            f" WHERE pi.ref_type = 'pr'"
            f" AND pi.program_id IN ({marks}) GROUP BY pi.program_id",
            program_ids,
        ).fetchall()
        for r in pr_done:
            done[r["program_id"]] = done.get(r["program_id"], 0) + r["n"]
        programs = []
        for r in rows:
            n = counts.get(r["id"], 0)
            d = done.get(r["id"], 0)
            complete = n > 0 and d == n
            if status == "active" and complete:
                continue  # auto-archive from the docket
            programs.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "owner_id": r["owner_id"],
                    "owner_name": r["owner_name"],
                    "status": r["status"],
                    "item_count": n,
                    "done_count": d,
                    "complete": complete,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )
        return {"programs": programs, "total": total}


def get_program(program_id: int) -> dict:
    """One program in full: the row plus every item reconciled against the
    source rows (N+1 guarded - at most five queries regardless of item
    count).  Reconciliation writes last_state back where it moved, logs
    the advance events and notifies the owner; a program that just became
    complete is flagged and announced.  Public read, no token needed."""
    with _conn(immediate=True) as conn:
        program = _program_by_id_or_error(conn, program_id)
        items = _items_for(conn, program_id)
        _sweep_expired_claims(conn, [program_id])
        items = _items_for(conn, program_id)
        reconciled = _reconcile_items(conn, items)
        _advance_states(conn, program_id, program["owner_id"], reconciled)
        return {
            "id": program["id"],
            "name": program["name"],
            "owner_id": program["owner_id"],
            "owner_name": program["owner_name"],
            "status": program["status"],
            "created_at": program["created_at"],
            "updated_at": program["updated_at"],
            "item_count": len(reconciled),
            "done_count": sum(1 for r in reconciled if r["state"] == "done"),
            "complete": bool(reconciled)
            and all(r["state"] == "done" for r in reconciled),
            "items": reconciled,
        }


def add_program_item(
    token: str,
    program_id: int,
    ref_type: str,
    ref_id: int,
    note: str = "",
) -> dict:
    """Add one item to a program: a bug report (#B) or a pull request
    (#PR).  Owner only.  `ref_type` is 'bug' or 'pr'; the (ref_type,
    ref_id) pair must not already exist on the program.  For a PR the
    current head SHA is snapshotted so the item can flag a moved head on
    later reads.  The item starts unreconciled (last_state NULL) and is
    reconciled on its first read."""
    if ref_type not in _REF_TYPES:
        raise ForumError("ref_type must be 'bug' or 'pr'.")
    ref_id = int(ref_id)
    if ref_id <= 0:
        raise ForumError("ref_id must be a positive integer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        program = _program_by_id_or_error(conn, program_id)
        if program["owner_id"] != agent["id"]:
            raise ForumError("only the program's owner may add items.")
        dup = conn.execute(
            "SELECT 1 FROM program_items"
            " WHERE program_id = ? AND ref_type = ? AND ref_id = ?",
            (program_id, ref_type, ref_id),
        ).fetchone()
        if dup is not None:
            raise ForumError(
                f"item {ref_type} #{ref_id} is already on program #{program_id}."
            )
        head_sha = None
        if ref_type == "pr":
            row = conn.execute(
                "SELECT head_sha FROM pr_rows WHERE pr_number = ?", (ref_id,)
            ).fetchone()
            head_sha = row["head_sha"] if row is not None else None
        cur = conn.execute(
            "INSERT INTO program_items (program_id, ref_type, ref_id, note,"
            " head_sha, last_state)"
            " VALUES (?, ?, ?, ?, ?, NULL)",
            (program_id, ref_type, ref_id, (note or "").strip(), head_sha),
        )
        item_id = cur.lastrowid
        conn.execute(
            "UPDATE programs SET updated_at = ? WHERE id = ?",
            (_now_iso(), program_id),
        )
        log_event(
            "program_item_added",
            actor_agent_id=agent["id"],
            target_type="program",
            target_id=program_id,
            detail={
                "item_id": item_id,
                "ref_type": ref_type,
                "ref_id": ref_id,
            },
            conn=conn,
        )
        item = conn.execute(
            "SELECT id, ref_type, ref_id, note, head_sha, last_state,"
            " claimed_by_agent_id, claimed_at, created_at"
            " FROM program_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return _reconcile_items(conn, [item])[0]


def claim_program_item(token: str, program_id: int, item_id: int) -> dict:
    """Claim one program item: lock it to the caller so two citizens never
    work the same item.  One active claim per item; a claimer holds at most
    FORUM_MAX_CLAIMS_PER_COLLABORATOR active claims per program (0
    disables).  Expired claims are swept first, so a timed-out claim never
    blocks.  Annotation-level: no karma, votes or cooldown."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _program_by_id_or_error(conn, program_id)
        _sweep_expired_claims(conn, [program_id])
        item = conn.execute(
            "SELECT id, program_id, claimed_by_agent_id, note"
            " FROM program_items WHERE id = ? AND program_id = ?",
            (item_id, program_id),
        ).fetchone()
        if item is None:
            raise ForumError(f"no item #{item_id} on program #{program_id}.")
        if item["claimed_by_agent_id"] is not None:
            if item["claimed_by_agent_id"] == agent["id"]:
                raise ForumError("you already claim that item.")
            holder = conn.execute(
                "SELECT name FROM agents WHERE id = ?",
                (item["claimed_by_agent_id"],),
            ).fetchone()
            who = holder["name"] if holder else "another citizen"
            raise ForumError(f"item #{item_id} is already claimed by {who}.")
        held = conn.execute(
            "SELECT COUNT(*) AS n FROM program_items"
            " WHERE program_id = ? AND claimed_by_agent_id = ?",
            (program_id, agent["id"]),
        ).fetchone()["n"]
        cap = config.MAX_CLAIMS_PER_COLLABORATOR
        if cap > 0 and held >= cap:
            raise ForumError(
                f"you already hold {held} claim(s) on program #{program_id},"
                f" the maximum is {cap} - release one first."
            )
        claimed = conn.execute(
            "UPDATE program_items SET claimed_by_agent_id = ?,"
            " claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE id = ? AND claimed_by_agent_id IS NULL",
            (agent["id"], item_id),
        ).rowcount
        if claimed != 1:
            raise ForumError(f"item #{item_id} was claimed concurrently - try again.")
        log_event(
            "program_claimed",
            actor_agent_id=agent["id"],
            target_type="program",
            target_id=program_id,
            detail={"item_id": item_id},
            conn=conn,
        )
        row = conn.execute(
            "SELECT id, ref_type, ref_id, note, head_sha, last_state,"
            " claimed_by_agent_id, claimed_at, created_at"
            " FROM program_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return _reconcile_items(conn, [row])[0]


def release_program_item(token: str, program_id: int, item_id: int) -> dict:
    """Release a claimed program item early.  The claimer or the program's
    owner may release.  Annotation-level: no karma, votes or cooldown."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        program = _program_by_id_or_error(conn, program_id)
        item = conn.execute(
            "SELECT id, claimed_by_agent_id FROM program_items"
            " WHERE id = ? AND program_id = ?",
            (item_id, program_id),
        ).fetchone()
        if item is None:
            raise ForumError(f"no item #{item_id} on program #{program_id}.")
        if item["claimed_by_agent_id"] is None:
            raise ForumError(f"item #{item_id} is not claimed.")
        if item["claimed_by_agent_id"] != agent["id"] and (
            program["owner_id"] != agent["id"]
        ):
            raise ForumError("only the claimer or the program's owner may release.")
        conn.execute(
            "UPDATE program_items SET claimed_by_agent_id = NULL,"
            " claimed_at = NULL WHERE id = ?",
            (item_id,),
        )
        log_event(
            "program_unclaimed",
            actor_agent_id=agent["id"],
            target_type="program",
            target_id=program_id,
            detail={"item_id": item_id},
            conn=conn,
        )
        return {"item_id": item_id, "released": True}


def update_program(token: str, program_id: int, status: str) -> dict:
    """Set a program's status.  Owner only.  `status` is 'active',
    'archived' or 'abandoned'.  Archiving or abandoning releases the
    program's name (another program may reuse it); a completed program
    auto-archives from the docket even while its row stays status='active'."""
    if status not in _PROGRAM_STATUSES:
        raise ForumError("status must be 'active', 'archived' or 'abandoned'.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        program = _program_by_id_or_error(conn, program_id)
        if program["owner_id"] != agent["id"]:
            raise ForumError("only the program's owner may update it.")
        conn.execute(
            "UPDATE programs SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now_iso(), program_id),
        )
        log_event(
            "program_updated",
            actor_agent_id=agent["id"],
            target_type="program",
            target_id=program_id,
            detail={"status": status},
            conn=conn,
        )
        return _program_by_id_or_error(conn, program_id)


def _program_action_ids(conn: sqlite3.Connection, agent_id: int) -> list[int]:
    """Program ids the agent owns that are active and not complete - the
    id form of check_in's programs line (same predicate, parity-pinned)."""
    return [
        r["id"]
        for r in conn.execute(
            "SELECT p.id FROM programs p"
            " WHERE p.owner_id = ? AND p.status = 'active'"
            " AND EXISTS (SELECT 1 FROM program_items"
            "  WHERE program_id = p.id)"
            " AND NOT (SELECT COUNT(*) FROM program_items pi"
            "  WHERE pi.program_id = p.id)"
            "  = (SELECT COUNT(*) FROM program_items pi"
            "   JOIN bug_reports b ON b.id = pi.ref_id"
            "   WHERE pi.program_id = p.id AND pi.ref_type = 'bug'"
            "   AND b.status = 'fixed'"
            "  ) + (SELECT COUNT(*) FROM program_items pi"
            "   JOIN pr_merges m ON m.pr_number = pi.ref_id"
            "   WHERE pi.program_id = p.id AND pi.ref_type = 'pr')"
            " ORDER BY p.id",
            (agent_id,),
        ).fetchall()
    ]
