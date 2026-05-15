"""Tests for SQLite state: dedup ledger + per-subscription run state."""

from __future__ import annotations

from pathlib import Path

from pulpline.state import (
    ItemRecord,
    connect,
    get_subscription_state,
    is_seen,
    record_item,
    update_subscription_state,
)


def _record(dedup: str = "k1", path: str = "/tmp/x.epub") -> ItemRecord:
    return ItemRecord(
        subscription_name="sub",
        source_url="https://feed",
        dedup_key=dedup,
        canonical_url="https://example.com/x",
        title="Title",
        pub_date=None,
        output_path=path,
    )


def test_init_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r["name"] for r in rows}
    assert "subscription_state" in names
    assert "items" in names


def test_is_seen_returns_path_after_record(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        assert is_seen(conn, "k1") is None
        record_item(conn, _record(dedup="k1", path="/tmp/article.epub"))
        assert is_seen(conn, "k1") == "/tmp/article.epub"


def test_record_item_upserts_on_duplicate_dedup_key(tmp_path: Path) -> None:
    """Re-recording with an existing dedup_key updates the row in place.

    This handles re-ingestion after a soft delete: `pulp add <url>` on a
    previously-deleted article should not fail on the UNIQUE constraint.
    """
    from pulpline.state import is_seen

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path="/tmp/old.epub"))
        record_item(conn, _record(dedup="k1", path="/tmp/new.epub"))
        # Single row, with the latest output_path.
        rows = conn.execute("SELECT output_path FROM items WHERE dedup_key = ?", ("k1",)).fetchall()
        assert len(rows) == 1
        assert is_seen(conn, "k1") == "/tmp/new.epub"


def test_was_ingested_distinguishes_from_is_seen(tmp_path: Path) -> None:
    """`was_ingested` is True even after a soft delete; `is_seen` is not."""
    from pulpline.state import delete_item, is_seen, was_ingested

    db = tmp_path / "state.db"
    target_file = tmp_path / "kept.epub"
    target_file.write_bytes(b"x")

    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path=str(target_file)))
        assert is_seen(conn, "k1") == str(target_file)
        assert was_ingested(conn, "k1") is True

        item_id = conn.execute("SELECT id FROM items WHERE dedup_key = ?", ("k1",)).fetchone()["id"]
        delete_item(conn, item_id)

        # File is gone, output_path is null.
        assert not target_file.exists()
        assert is_seen(conn, "k1") is None
        assert was_ingested(conn, "k1") is True


def test_delete_item_then_record_revives(tmp_path: Path) -> None:
    """Soft-deleted items can be re-ingested via the record_item upsert."""
    from pulpline.state import delete_item, is_seen

    db = tmp_path / "state.db"
    f1 = tmp_path / "v1.epub"
    f1.write_bytes(b"x")
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path=str(f1)))
        item_id = conn.execute("SELECT id FROM items WHERE dedup_key = ?", ("k1",)).fetchone()["id"]
        delete_item(conn, item_id)

        # Re-ingest with new path.
        f2 = tmp_path / "v2.epub"
        f2.write_bytes(b"y")
        record_item(conn, _record(dedup="k1", path=str(f2)))

        assert is_seen(conn, "k1") == str(f2)


def test_list_items_excludes_soft_deleted(tmp_path: Path) -> None:
    from pulpline.state import delete_item, list_items

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path="/tmp/a.epub"))
        record_item(conn, _record(dedup="k2", path="/tmp/b.epub"))
        item_id = conn.execute("SELECT id FROM items WHERE dedup_key = ?", ("k1",)).fetchone()["id"]
        delete_item(conn, item_id)

        items = list_items(conn)
    assert len(items) == 1
    assert items[0].output_path == "/tmp/b.epub"


def test_list_items_returns_recent_first(tmp_path: Path) -> None:
    from pulpline.state import list_items

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path="/tmp/a.epub"))
        record_item(conn, _record(dedup="k2", path="/tmp/b.epub"))
        items = list_items(conn, limit=10)

    assert len(items) == 2
    # Most recent first - both inserted close together but k2 second.
    assert items[0].output_path == "/tmp/b.epub"


def test_list_items_filter_text(tmp_path: Path) -> None:
    from pulpline.state import ItemRecord, list_items

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(
            conn,
            ItemRecord(
                subscription_name=None,
                source_url="https://x.example/",
                dedup_key="k1",
                canonical_url="https://x.example/about-cats",
                title="Cats are great",
                pub_date=None,
                output_path="/tmp/cats.epub",
            ),
        )
        record_item(
            conn,
            ItemRecord(
                subscription_name=None,
                source_url="https://y.example/",
                dedup_key="k2",
                canonical_url="https://y.example/dogs",
                title="Dogs are okay",
                pub_date=None,
                output_path="/tmp/dogs.epub",
            ),
        )
        items = list_items(conn, filter_text="cats")

    assert len(items) == 1
    assert items[0].title == "Cats are great"


def test_count_helpers(tmp_path: Path) -> None:
    from pulpline.state import count_by_extension, count_by_subscription, count_total

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path="/tmp/a.epub"))
        record_item(conn, _record(dedup="k2", path="/tmp/b.pdf"))
        record_item(conn, _record(dedup="k3", path="/tmp/c.epub"))

        assert count_total(conn) == 3
        # All three have subscription_name="sub" via _record default
        assert count_by_subscription(conn) == {"sub": 3}
        assert count_by_extension(conn) == {"epub": 2, "pdf": 1}


def test_items_per_day_includes_zeros(tmp_path: Path) -> None:
    from pulpline.state import items_per_day

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1"))
        per_day = items_per_day(conn, days=7)

    assert len(per_day) == 7
    # Today's entry should have count 1; other days should be 0.
    counts = {day: n for day, n in per_day}
    assert sum(counts.values()) == 1


def test_delete_item_records_deleted_at(tmp_path: Path) -> None:
    """Soft-delete writes a timestamp so we can group deletions by day later."""
    from pulpline.state import delete_item

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path="/tmp/x.epub"))
        item_id = conn.execute("SELECT id FROM items WHERE dedup_key = ?", ("k1",)).fetchone()["id"]
        delete_item(conn, item_id)

        row = conn.execute(
            "SELECT output_path, deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["output_path"] is None
        assert row["deleted_at"] is not None  # ISO timestamp; format-checked elsewhere
        # The format is the same isoformat _now_iso uses; loose-validate it starts with year.
        assert row["deleted_at"][:4].isdigit()


def test_record_item_clears_deleted_at_on_revive(tmp_path: Path) -> None:
    """`pulp add <url>` on a deleted item must clear the tombstone, else we'd
    have a row with both output_path AND deleted_at set - logically inconsistent."""
    from pulpline.state import delete_item

    db = tmp_path / "state.db"
    f1 = tmp_path / "v1.epub"
    f1.write_bytes(b"x")
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path=str(f1)))
        item_id = conn.execute("SELECT id FROM items WHERE dedup_key = ?", ("k1",)).fetchone()["id"]
        delete_item(conn, item_id)

        # Confirm the tombstone is set.
        assert (
            conn.execute("SELECT deleted_at FROM items WHERE id = ?", (item_id,)).fetchone()[
                "deleted_at"
            ]
            is not None
        )

        # Re-ingest via the upsert path.
        f2 = tmp_path / "v2.epub"
        f2.write_bytes(b"y")
        record_item(conn, _record(dedup="k1", path=str(f2)))

        row = conn.execute(
            "SELECT output_path, deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["output_path"] == str(f2)
        assert row["deleted_at"] is None


def test_activity_per_day_counts_added_and_deleted(tmp_path: Path) -> None:
    from pulpline.state import activity_per_day, delete_item

    db = tmp_path / "state.db"
    with connect(db) as conn:
        # Add three items today.
        for k in ("k1", "k2", "k3"):
            record_item(conn, _record(dedup=k, path=f"/tmp/{k}.epub"))
        # Delete one today.
        item_id = conn.execute("SELECT id FROM items WHERE dedup_key = 'k1'").fetchone()["id"]
        delete_item(conn, item_id)

        per_day = activity_per_day(conn, days=7)

    assert len(per_day) == 7
    _, added, deleted = per_day[0]
    assert added == 3
    assert deleted == 1
    # Other days should be (0, 0)
    for _, a, d in per_day[1:]:
        assert (a, d) == (0, 0)


def test_activity_per_day_preserves_added_count_after_delete(tmp_path: Path) -> None:
    """Deleting an item should NOT retroactively shrink the 'added' count.

    Otherwise a tidied-up library would look like nothing was ever ingested
    on the days you cleaned up - the historical record should be stable.
    """
    from pulpline.state import activity_per_day, delete_item

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path="/tmp/a.epub"))
        record_item(conn, _record(dedup="k2", path="/tmp/b.epub"))

        before = activity_per_day(conn, days=7)
        added_before = before[0][1]

        item_id = conn.execute("SELECT id FROM items WHERE dedup_key = 'k1'").fetchone()["id"]
        delete_item(conn, item_id)

        after = activity_per_day(conn, days=7)
        added_after = after[0][1]

    assert added_before == 2
    assert added_after == 2  # unchanged - delete doesn't undo the add
    assert after[0][2] == 1  # but it shows up as a delete


def test_list_recently_deleted_excludes_live_items(tmp_path: Path) -> None:
    from pulpline.state import delete_item, list_recently_deleted

    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1", path="/tmp/live.epub"))
        record_item(conn, _record(dedup="k2", path="/tmp/dead.epub"))
        dead_id = conn.execute("SELECT id FROM items WHERE dedup_key = 'k2'").fetchone()["id"]
        delete_item(conn, dead_id)

        deleted = list_recently_deleted(conn)

    assert len(deleted) == 1
    assert deleted[0].id == dead_id
    assert deleted[0].output_path is None
    assert deleted[0].deleted_at is not None


def test_list_recently_deleted_legacy_null_timestamp_sorts_last(tmp_path: Path) -> None:
    """An item soft-deleted before deleted_at shipped has NULL there; it
    should still appear in the listing but sort after items with timestamps."""
    from pulpline.state import list_recently_deleted

    db = tmp_path / "state.db"
    with connect(db) as conn:
        # Two soft-deleted items via direct SQL (simulating legacy state where
        # the column existed but wasn't being written). Today's-tombstone has
        # deleted_at; the legacy one has NULL.
        record_item(conn, _record(dedup="k1", path="/tmp/legacy.epub"))
        record_item(conn, _record(dedup="k2", path="/tmp/today.epub"))
        # Legacy: null out path AND deleted_at to mimic old behavior.
        conn.execute(
            "UPDATE items SET output_path = NULL, deleted_at = NULL WHERE dedup_key = 'k1'"
        )
        # Today: use the proper helper.
        today_id = conn.execute("SELECT id FROM items WHERE dedup_key = 'k2'").fetchone()["id"]
        from pulpline.state import delete_item

        delete_item(conn, today_id)

        deleted = list_recently_deleted(conn)

    # Both appear; the one with deleted_at set comes first.
    assert len(deleted) == 2
    assert deleted[0].deleted_at is not None
    assert deleted[1].deleted_at is None


def test_subscription_state_upsert(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        assert get_subscription_state(conn, "sub") is None
        update_subscription_state(conn, "sub", "ok", None)
        state = get_subscription_state(conn, "sub")
        assert state is not None
        assert state.name == "sub"
        assert state.last_status == "ok"
        assert state.last_error is None

        update_subscription_state(conn, "sub", "error", "boom")
        state = get_subscription_state(conn, "sub")
        assert state is not None
        assert state.last_status == "error"
        assert state.last_error == "boom"
