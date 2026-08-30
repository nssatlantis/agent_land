"""Tests for the DB-persisted closed-PR cache (db._pr_rows).

Covers both write shapes (feed rows from the outcome poller, raw GitHub
headers from the revalidation seam), the head_sha round-trip that makes a
304 synthetic header possible, the reopened-PR guard, the unpopulated
fallback signal, and the backfill watermark."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pr_rows_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from tests._setup import setup  # noqa: E402

AGENTS, _ = setup()


def _feed_row(number=100, **overrides):
    """A closed-PR row in the outcome-feed shape (github._closed_row_from_raw)."""
    row = {
        "number": number,
        "title": f"PR {number}",
        "body": f"Body {number}",
        "head": f"feature/{number}",
        "head_sha": f"abc123{number}def",
        "base": "main",
        "author": "someone",
        "state": "closed",
        "created_at": f"2026-01-01T00:00:{number % 60:02d}.000Z",
        "updated_at": f"2026-01-01T01:00:{number % 60:02d}.000Z",
        "merged_at": None,
        "closed_at": None,
        "html_url": f"https://github.com/nssatlantis/agent_land/pull/{number}",
        "labels": [],
        "citizen": None,
    }
    row.update(overrides)
    return row


def _clear():
    """Wipe the cache tables so each test starts from a clean slate (the
    module-level DB is shared across tests, like the rest of the suite)."""
    with db._conn() as conn:
        conn.execute("DELETE FROM pr_rows")
        conn.execute("DELETE FROM pr_cache_meta")


def test_feed_upsert_round_trips_head_sha_and_derived_fields():
    _clear()
    with db._conn() as conn:
        db.pr_rows_upsert(
            conn,
            _feed_row(
                101,
                labels=["declined: proof", "maintainer"],
                citizen={"agent_id": AGENTS["alpha"]["agent_id"], "name": "alpha"},
            ),
        )
    row = db.pr_row(101)
    assert row is not None
    assert row["head_sha"] == "abc123101def", "head_sha round-trips through the cache"
    assert row["labels"] == ["declined: proof", "maintainer"]
    assert row["citizen"] == {
        "agent_id": AGENTS["alpha"]["agent_id"],
        "name": "alpha",
    }
    # Declined label wins the outcome derivation (no merged_at).
    assert row["outcome"] == "declined"
    assert row["number"] == 101
    print("  feed upsert round-trips head_sha + derived fields: ok")


def test_outcome_kinds_and_no_citizen():
    _clear()
    with db._conn() as conn:
        db.pr_rows_upsert(conn, _feed_row(102, merged_at="2026-01-01T02:00:00.000Z"))
        db.pr_rows_upsert(conn, _feed_row(103))
        db.pr_rows_upsert(conn, _feed_row(104, state="closed", labels=["declined"]))
    assert db.pr_row(102)["outcome"] == "merged"
    assert db.pr_row(103)["outcome"] == "closed"
    assert db.pr_row(104)["outcome"] == "declined"
    # A feed row without citizen enrichment yields citizen None.
    assert db.pr_row(103)["citizen"] is None
    print("  outcome kinds derive correctly; absent citizen is None: ok")


def test_list_pr_rows_unpopulated_vs_populated():
    _clear()
    # No rows AND no watermark yet -> None (the live-GitHub fallback signal).
    assert db.list_pr_rows() is None
    with db._conn() as conn:
        db.pr_rows_upsert(conn, _feed_row(105))
    # With rows present the cache is populated even before a backfill
    # watermark lands - the watermark only disambiguates a zero-row cache.
    rows = db.list_pr_rows()
    assert isinstance(rows, list) and [r["number"] for r in rows] == [105]
    print("  unpopulated -> None; any row flips the cache on: ok")


def test_list_pr_rows_ordering_and_since():
    _clear()
    with db._conn() as conn:
        db.pr_rows_upsert(
            conn,
            _feed_row(106, updated_at="2026-01-02T00:00:00.000Z"),
        )
        db.pr_rows_upsert(
            conn,
            _feed_row(107, updated_at="2026-01-03T00:00:00.000Z"),
        )
        db.pr_rows_set_watermark(conn)
    rows = db.list_pr_rows()
    assert [r["number"] for r in rows] == [107, 106], "newest updated_at first"
    since = db.list_pr_rows(since="2026-01-03T00:00:00.000Z")
    assert [r["number"] for r in since] == [107]
    print("  list_pr_rows orders by updated_at and filters by since: ok")


def test_feed_upsert_conflict_preserves_missing_etag():
    _clear()
    # Poller feed rows never carry an etag; a revalidation write must stick.
    with db._conn() as conn:
        db.pr_rows_upsert(conn, _feed_row(108))
    # Refresh with changed content, still no etag -> etag stays None.
    with db._conn() as conn:
        db.pr_rows_upsert(conn, _feed_row(108, title="Retitled 108"))
    row = db.pr_row(108)
    assert row["title"] == "Retitled 108"
    assert row["etag"] is None
    # Then a revalidation write lands an etag and survives a feed refresh.
    with db._conn() as conn:
        db.pr_rows_upsert_from_raw(
            conn,
            _raw_payload(108, title="Retitled 108"),
            etag='"v1"',
        )
        db.pr_rows_upsert(conn, _feed_row(108, title="Feed wins content"))
    row = db.pr_row(108)
    assert row["title"] == "Feed wins content"
    assert row["etag"] == '"v1"', "etag survives a later feed refresh (COALESCE)"
    print("  feed refresh keeps a stored etag via COALESCE: ok")


def _raw_payload(number, **overrides):
    """A raw GitHub pull-header shape (the revalidation seam's input)."""
    payload = {
        "number": number,
        "title": f"Raw {number}",
        "body": None,
        "state": "closed",
        "html_url": f"https://github.com/nssatlantis/agent_land/pull/{number}",
        "head": {"ref": f"feature/{number}", "sha": f"deadbeef{number}"},
        "base": {"ref": "main", "label": "nssatlantis:main"},
        "user": {"login": "roamer"},
        "labels": [],
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T01:00:00.000Z",
        "merged_at": None,
        "closed_at": "2026-01-01T02:00:00.000Z",
    }
    payload.update(overrides)
    return payload


def test_raw_upsert_head_sha_labels_and_citizen_preservation():
    _clear()
    # Seed a feed row with citizen attribution (only the poller knows it).
    with db._conn() as conn:
        db.pr_rows_upsert(
            conn,
            _feed_row(
                109,
                citizen={"agent_id": AGENTS["beta"]["agent_id"], "name": "beta"},
            ),
        )
    # Revalidation refreshes content + etag from a raw header.
    with db._conn() as conn:
        db.pr_rows_upsert_from_raw(
            conn,
            _raw_payload(109, labels=[{"name": "declined: infra"}]),
            etag='"abc"',
        )
    row = db.pr_row(109)
    assert row["head_sha"] == "deadbeef109", "head_sha maps from head.sha"
    assert row["labels"] == ["declined: infra"]
    assert row["etag"] == '"abc"'
    assert row["outcome"] == "declined"
    assert row["citizen"] == {
        "agent_id": AGENTS["beta"]["agent_id"],
        "name": "beta",
    }, "raw upsert never erases poller citizen attribution"
    print("  raw upsert stores head_sha; keeps citizen + sets etag: ok")


def test_reopened_pr_is_dropped_from_cache():
    _clear()
    with db._conn() as conn:
        db.pr_rows_upsert(conn, _feed_row(110))
    # A conditional read returns a fresh OPEN payload -> the stale closed row
    # must be deleted so the closed listing never serves it again.
    with db._conn() as conn:
        db.pr_rows_upsert_from_raw(
            conn,
            _raw_payload(110, state="open"),
            etag='"reopened"',
        )
    assert db.pr_row(110) is None, "reopened PR must leave the closed cache"
    print("  reopened PR is dropped from the closed cache: ok")


def test_watermark_explicit_vs_default():
    _clear()
    assert db.pr_rows_watermark() is None
    with db._conn() as conn:
        db.pr_rows_set_watermark(conn, "2026-01-01T00:00:00.000Z")
        assert db.pr_rows_watermark(conn) == "2026-01-01T00:00:00.000Z"
        # Default stamps _now_iso() (a valid ISO timestamp), overwriting.
        db.pr_rows_set_watermark(conn)
        val = db.pr_rows_watermark(conn)
        assert val is not None and "T" in val and val.endswith("Z")
    print("  watermark explicit/default stamp round-trips: ok")


def test_empty_rows_after_watermark_returns_empty_not_none():
    _clear()
    # A completed backfill with a truly empty repo returns [] (zero PRs),
    # not None (unpopulated) - the distinction matters for live fallback.
    with db._conn() as conn:
        db.pr_rows_set_watermark(conn)
    assert db.list_pr_rows() == [], db.list_pr_rows()
    print("  post-watermark zero-row cache reads [], not None: ok")


if __name__ == "__main__":
    test_feed_upsert_round_trips_head_sha_and_derived_fields()
    test_outcome_kinds_and_no_citizen()
    test_list_pr_rows_unpopulated_vs_populated()
    test_list_pr_rows_ordering_and_since()
    test_feed_upsert_conflict_preserves_missing_etag()
    test_raw_upsert_head_sha_labels_and_citizen_preservation()
    test_reopened_pr_is_dropped_from_cache()
    test_watermark_explicit_vs_default()
    test_empty_rows_after_watermark_returns_empty_not_none()
    print("\n== test_pr_rows: all passed ==")
