"""db._tool_inventory - tool-surface inventory for agentland://tools/changes.

The server records one snapshot per boot (post-deploy state) of every
registered MCP tool as (name, params_hash, desc_hash); this module persists
that inventory and answers "what changed in the last N days" as
added / signature-changed / description-updated / removed. Protocol-agnostic
by design (plain rows in, plain dicts out - the server layer owns the MCP
registry reads and the resource rendering).

Semantics: one row per tool ever seen (history is kept, nothing is
deleted). first_seen/last_seen bracket observation; last_params_change /
last_desc_change stamp the newest fingerprint change per axis. A tool
missing from the latest snapshot but seen recently counts as removed -
removal time is approximate (last boot where it was present), which the
resource page states. All timestamps are UTC ISO like the rest of the app.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from db._core import _conn, _now_iso


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_tool_inventory(items: list[tuple[str, str, str]]) -> int:
    """Upsert one boot snapshot: (name, params_json, description) per tool.

    New names are inserted with first_seen = last_seen = now. Known names
    refresh last_seen and stamp last_params_change / last_desc_change when
    that axis's fingerprint moved. Idempotent per snapshot - recording the
    same inventory twice changes only last_seen. Returns rows touched.
    """
    now = _now_iso()
    touched = 0
    with _conn() as conn:
        for name, params_json, desc in items:
            params_hash = _fingerprint(params_json or "")
            desc_hash = _fingerprint(desc or "")
            row = conn.execute(
                "SELECT params_hash, desc_hash FROM tool_inventory WHERE tool = ?",
                (name,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tool_inventory (tool, params_hash, desc_hash,"
                    " first_seen, last_seen, last_params_change,"
                    " last_desc_change) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                    (name, params_hash, desc_hash, now, now),
                )
            else:
                params_changed = row["params_hash"] != params_hash
                desc_changed = row["desc_hash"] != desc_hash
                conn.execute(
                    "UPDATE tool_inventory SET params_hash = ?, desc_hash = ?,"
                    " last_seen = ?,"
                    " last_params_change = CASE WHEN ? THEN ?"
                    " ELSE last_params_change END,"
                    " last_desc_change = CASE WHEN ? THEN ?"
                    " ELSE last_desc_change END WHERE tool = ?",
                    (
                        params_hash,
                        desc_hash,
                        now,
                        1 if params_changed else 0,
                        now,
                        1 if desc_changed else 0,
                        now,
                        name,
                    ),
                )
            touched += 1
    return touched


def _cutoff(days: int) -> str:
    """ISO timestamp `days` ago (UTC) - changes at or after this count."""
    return _now_iso(datetime.now(timezone.utc) - timedelta(days=days))


def tool_inventory_changes(days: int = 5, present: set[str] | None = None) -> dict:
    """Added / signature-changed / description-updated / removed tool names.

    `days` bounds the window (first_seen / last_*_change >= cutoff).
    `present` is the live registry's name set: rows absent from it but
    seen within the window count as removed (pass None to skip the
    removed classification). A tool changed on both axes lists under
    signature_changed only; description_updated means description-only.
    Every list is sorted. Meta carries generated_at, tracking_since
    (oldest first_seen, None when empty), snapshot_at (newest last_seen,
    None when empty) and recorded_tools.
    """
    cutoff = _cutoff(max(0, int(days)))
    present = set(present or ())
    with _conn() as conn:
        rows = conn.execute(
            "SELECT tool, first_seen, last_seen, last_params_change,"
            " last_desc_change FROM tool_inventory"
        ).fetchall()
        snapshot_at = conn.execute(
            "SELECT MAX(last_seen) FROM tool_inventory"
        ).fetchone()[0]
    added: list[str] = []
    sig_changed: list[str] = []
    desc_updated: list[str] = []
    removed: list[str] = []
    firsts: list[str] = []
    for r in rows:
        name = r["tool"]
        firsts.append(r["first_seen"])
        if r["first_seen"] >= cutoff:
            added.append(name)
            continue
        if name not in present:
            if r["last_seen"] >= cutoff:
                removed.append(name)
            continue
        if (r["last_params_change"] or "") >= cutoff:
            sig_changed.append(name)
        elif (r["last_desc_change"] or "") >= cutoff:
            desc_updated.append(name)
    return {
        "days": int(days),
        "generated_at": _now_iso(),
        "tracking_since": min(firsts) if firsts else None,
        "snapshot_at": snapshot_at,
        "recorded_tools": len(rows),
        "added": sorted(added),
        "signature_changed": sorted(sig_changed),
        "description_updated": sorted(desc_updated),
        "removed": sorted(removed),
    }
