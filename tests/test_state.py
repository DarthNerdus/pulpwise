"""Tests for SQLite state: dedup ledger + per-subscription run state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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


def test_record_item_rejects_duplicate_dedup_key(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with connect(db) as conn:
        record_item(conn, _record(dedup="k1"))
        with pytest.raises(sqlite3.IntegrityError):
            record_item(conn, _record(dedup="k1"))


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
