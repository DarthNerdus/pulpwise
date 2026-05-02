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
