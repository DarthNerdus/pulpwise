"""Tests for SQLite state: dedup ledger, tombstones, legacy migration, counters."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pulpwise.state import (
    ItemRecord,
    connect,
    count_by_kind,
    count_by_subscription,
    count_total,
    delete_item,
    get_subscription_state,
    is_seen,
    items_per_day,
    list_items,
    list_oneshots,
    list_recently_deleted,
    record_item,
    update_subscription_state,
    was_ingested,
)


def _record(
    dedup: str = "k1",
    *,
    sub: str | None = "sub",
    url: str = "https://example.com/x",
    title: str | None = "Title",
    rw_id: str = "doc-1",
    rw_url: str = "https://read.readwise.io/read/doc-1",
    kind: str = "url",
    destination: str = "readwise",
) -> ItemRecord:
    return ItemRecord(
        subscription_name=sub,
        source_url="https://feed",
        dedup_key=dedup,
        canonical_url=url,
        title=title,
        pub_date=None,
        readwise_id=rw_id,
        readwise_url=rw_url,
        submission_kind=kind,
        destination=destination,
    )


def _item_id(conn: sqlite3.Connection, dedup: str) -> int:
    row = conn.execute("SELECT id FROM items WHERE dedup_key = ?", (dedup,)).fetchone()
    return int(row["id"])


def test_init_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r["name"] for r in rows}
    assert "subscription_state" in names
    assert "items" in names


def test_record_then_is_seen_returns_reader_url(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        assert is_seen(conn, "k1") is None
        record_item(conn, _record(dedup="k1", rw_url="https://read.readwise.io/read/doc-1"))
        assert is_seen(conn, "k1") == "https://read.readwise.io/read/doc-1"


def test_record_item_upserts_on_duplicate_dedup_key(tmp_path: Path) -> None:
    """Re-recording with an existing dedup_key updates the row in place."""
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", rw_id="old", rw_url="https://r/old", kind="url"))
        record_item(
            conn,
            _record(
                dedup="k1",
                rw_id="new",
                rw_url="https://r/new",
                kind="html",
                destination="shiori",
            ),
        )

        rows = conn.execute(
            "SELECT readwise_id, readwise_url, submission_kind, destination "
            "FROM items WHERE dedup_key = 'k1'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["readwise_id"] == "new"
        assert rows[0]["submission_kind"] == "html"
        assert rows[0]["destination"] == "shiori"
        assert is_seen(conn, "k1") == "https://r/new"


def test_is_seen_vs_was_ingested_after_soft_delete(tmp_path: Path) -> None:
    """`was_ingested` is True even after a soft delete; `is_seen` is not."""
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1"))
        assert is_seen(conn, "k1") is not None
        assert was_ingested(conn, "k1") is True

        delete_item(conn, _item_id(conn, "k1"))

        assert is_seen(conn, "k1") is None
        assert was_ingested(conn, "k1") is True


def test_is_seen_none_for_unpushed_legacy_rows(tmp_path: Path) -> None:
    """A live row without a readwise_url (legacy file-era) is not 'seen' but
    is 'ingested' - add can re-push it, sync must not."""
    db = tmp_path / "state.db"
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO items (subscription_name, source_url, dedup_key, canonical_url, "
            "ingested_at) VALUES ('sub', 'https://feed', 'legacy', 'https://x', "
            "'2025-01-01T00:00:00+00:00')"
        )
        conn.commit()

        assert is_seen(conn, "legacy") is None
        assert was_ingested(conn, "legacy") is True


def test_delete_item_tombstones_but_keeps_readwise_fields(tmp_path: Path) -> None:
    """Push-only contract: delete tombstones the row locally and touches
    nothing remote - the Reader document identifiers stay on the row."""
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", rw_id="doc-9"))
        item_id = _item_id(conn, "k1")

        delete_item(conn, item_id)

        row = conn.execute(
            "SELECT deleted_at, readwise_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["deleted_at"] is not None
        assert row["deleted_at"][:4].isdigit()  # ISO timestamp
        assert row["readwise_id"] == "doc-9"


def test_delete_item_tombstones_legacy_rows(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO items (subscription_name, source_url, dedup_key, canonical_url, "
            "ingested_at) VALUES (NULL, 'https://x', 'legacy', 'https://x', "
            "'2025-01-01T00:00:00+00:00')"
        )
        conn.commit()
        item_id = _item_id(conn, "legacy")

        delete_item(conn, item_id)
        assert was_ingested(conn, "legacy") is True  # tombstoned, not erased


def test_delete_item_on_missing_id_is_a_noop(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        delete_item(conn, 12345)  # must not raise


def test_record_item_revives_tombstone(tmp_path: Path) -> None:
    """Re-add after delete clears deleted_at and installs the new Reader doc."""
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", rw_id="doc-1", rw_url="https://r/1"))
        item_id = _item_id(conn, "k1")
        delete_item(conn, item_id)

        record_item(conn, _record(dedup="k1", rw_id="doc-2", rw_url="https://r/2", kind="html"))

        row = conn.execute(
            "SELECT id, deleted_at, readwise_id, readwise_url, submission_kind "
            "FROM items WHERE dedup_key = 'k1'"
        ).fetchone()
        assert row["id"] == item_id  # same row, revived in place
        assert row["deleted_at"] is None
        assert row["readwise_id"] == "doc-2"
        assert row["readwise_url"] == "https://r/2"
        assert row["submission_kind"] == "html"
        assert is_seen(conn, "k1") == "https://r/2"


# ---- legacy pulpline migration --------------------------------------------------


def _create_legacy_pulpline_db(path: Path) -> None:
    """The old pulpline schema: file-sink era, liveness = output_path IS NOT NULL."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE subscription_state (
                name TEXT PRIMARY KEY,
                last_synced_at TEXT,
                last_status TEXT,
                last_error TEXT
            );
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_name TEXT,
                source_url TEXT NOT NULL,
                dedup_key TEXT NOT NULL UNIQUE,
                canonical_url TEXT NOT NULL,
                title TEXT,
                pub_date TEXT,
                ingested_at TEXT NOT NULL,
                output_path TEXT
            );
            CREATE INDEX idx_items_subscription ON items(subscription_name);
            """
        )
        conn.execute(
            "INSERT INTO items (subscription_name, source_url, dedup_key, canonical_url, "
            "title, ingested_at, output_path) VALUES "
            "('sub', 'https://feed', 'live-key', 'https://x/live', 'Live', "
            "'2025-03-01T10:00:00+00:00', '/tmp/live.epub')"
        )
        conn.execute(
            "INSERT INTO items (subscription_name, source_url, dedup_key, canonical_url, "
            "title, ingested_at, output_path) VALUES "
            "('sub', 'https://feed', 'dead-key', 'https://x/dead', 'Dead', "
            "'2025-02-01T10:00:00+00:00', NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_pulpline_db_migrates_in_place(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _create_legacy_pulpline_db(db)

    with connect(db) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        assert {
            "readwise_id",
            "readwise_url",
            "submission_kind",
            "destination",
            "deleted_at",
        } <= columns
        sub_columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscription_state)")}
        assert "total_items" in sub_columns

        # The legacy tombstone (output_path NULL) got deleted_at backfilled
        # with its ingested_at - the real deletion time was never recorded.
        dead = conn.execute("SELECT * FROM items WHERE dedup_key = 'dead-key'").fetchone()
        assert dead["deleted_at"] == "2025-02-01T10:00:00+00:00"
        assert dead["destination"] == "readwise"

        # The live row stayed live.
        live = conn.execute("SELECT * FROM items WHERE dedup_key = 'live-key'").fetchone()
        assert live["deleted_at"] is None
        assert live["destination"] == "readwise"

        # Sync must skip both; add must consider neither already-in-Reader.
        assert was_ingested(conn, "live-key") is True
        assert was_ingested(conn, "dead-key") is True
        assert is_seen(conn, "live-key") is None
        assert is_seen(conn, "dead-key") is None


def test_legacy_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _create_legacy_pulpline_db(db)

    with connect(db):
        pass
    with connect(db) as conn:  # second connect re-runs the migration list
        dead = conn.execute("SELECT deleted_at FROM items WHERE dedup_key = 'dead-key'").fetchone()
        assert dead["deleted_at"] == "2025-02-01T10:00:00+00:00"


def test_legacy_migration_never_tombstones_pulpwise_rows(tmp_path: Path) -> None:
    """New rows written into a migrated legacy DB have output_path NULL too -
    the backfill UPDATE re-runs on every connect and must not sweep them up
    (that would soft-delete every document pushed since the migration)."""
    db = tmp_path / "state.db"
    _create_legacy_pulpline_db(db)

    with connect(db) as conn:  # first connect migrates
        record_item(
            conn,
            ItemRecord(
                subscription_name="sub",
                source_url="https://feed",
                dedup_key="new-key",
                canonical_url="https://x/new",
                title="New",
                pub_date=None,
                readwise_id="doc-1",
                readwise_url="https://read.readwise.io/read/doc-1",
                submission_kind="url",
            ),
        )

    with connect(db) as conn:  # second connect re-runs the backfill UPDATE
        row = conn.execute("SELECT deleted_at FROM items WHERE dedup_key = 'new-key'").fetchone()
        assert row["deleted_at"] is None
        assert is_seen(conn, "new-key") == "https://read.readwise.io/read/doc-1"


# ---- listings + counters --------------------------------------------------------


def test_list_items_excludes_soft_deleted(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", url="https://x/a"))
        record_item(conn, _record(dedup="k2", url="https://x/b"))
        delete_item(conn, _item_id(conn, "k1"))

        items = list_items(conn)
    assert len(items) == 1
    assert items[0].canonical_url == "https://x/b"
    assert items[0].readwise_url == "https://read.readwise.io/read/doc-1"


def test_list_items_limit_none_returns_everything(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        for i in range(600):
            record_item(conn, _record(dedup=f"k{i}"))

        assert len(list_items(conn)) == 500  # default cap still applies
        assert len(list_items(conn, limit=3)) == 3
        assert len(list_items(conn, limit=None)) == 600


def test_list_items_filter_text(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", title="Cats are great", url="https://x/about-cats"))
        record_item(conn, _record(dedup="k2", title="Dogs are okay", url="https://x/dogs"))

        items = list_items(conn, filter_text="cats")
    assert len(items) == 1
    assert items[0].title == "Cats are great"


def test_list_recently_deleted(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", url="https://x/live"))
        record_item(conn, _record(dedup="k2", url="https://x/dead"))
        dead_id = _item_id(conn, "k2")
        delete_item(conn, dead_id)

        deleted = list_recently_deleted(conn)
    assert len(deleted) == 1
    assert deleted[0].id == dead_id
    assert deleted[0].deleted_at is not None


def test_list_oneshots_only_shows_live_unsubscribed_items(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", sub=None, url="https://x/once"))
        record_item(conn, _record(dedup="k2", sub="sub", url="https://x/synced"))
        record_item(conn, _record(dedup="k3", sub=None, url="https://x/gone"))
        delete_item(conn, _item_id(conn, "k3"))

        oneshots = list_oneshots(conn)
    assert [o.canonical_url for o in oneshots] == ["https://x/once"]
    assert oneshots[0].readwise_url == "https://read.readwise.io/read/doc-1"


def test_count_by_kind_buckets_url_html_and_legacy_file(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", kind="url"))
        record_item(conn, _record(dedup="k2", kind="html"))
        record_item(conn, _record(dedup="k3", kind="url"))
        # Legacy file-era row: live, no submission_kind.
        conn.execute(
            "INSERT INTO items (subscription_name, source_url, dedup_key, canonical_url, "
            "ingested_at) VALUES ('sub', 'https://feed', 'k4', 'https://x/file', "
            "'2025-01-01T00:00:00+00:00')"
        )
        # Deleted rows don't count.
        record_item(conn, _record(dedup="k5", kind="html"))
        delete_item(conn, _item_id(conn, "k5"))

        assert count_by_kind(conn) == {"url": 2, "html": 1, "file": 1}


def test_count_helpers(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1"))
        record_item(conn, _record(dedup="k2"))
        record_item(conn, _record(dedup="k3", sub=None))

        assert count_total(conn) == 3
        assert count_by_subscription(conn) == {"sub": 2, "[one-shot]": 1}


def test_items_per_day_includes_zeros(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1"))
        per_day = items_per_day(conn, days=7)

    assert len(per_day) == 7
    counts = {day: n for day, n in per_day}
    assert sum(counts.values()) == 1


# ---- subscription state ----------------------------------------------------------


def test_subscription_state_upsert(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        assert get_subscription_state(conn, "sub") is None
        update_subscription_state(conn, "sub", "ok", None)
        state = get_subscription_state(conn, "sub")
        assert state is not None
        assert state.last_status == "ok"
        assert state.last_error is None

        update_subscription_state(conn, "sub", "error", "boom")
        state = get_subscription_state(conn, "sub")
        assert state is not None
        assert state.last_status == "error"
        assert state.last_error == "boom"


def test_subscription_state_preserves_total_items_when_none(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        update_subscription_state(conn, "sub", "ok", None, total_items=42)
        update_subscription_state(conn, "sub", "ok", None)  # no fresh total

        state = get_subscription_state(conn, "sub")
        assert state is not None
        assert state.total_items == 42
