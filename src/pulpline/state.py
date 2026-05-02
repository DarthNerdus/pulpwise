"""SQLite state: dedup ledger + per-subscription run state.

Schema lives here, not in TOML. TOML answers "what should pulpline subscribe to?";
SQLite answers "what has pulpline already done?". The boundary matters - a corrupt
state DB should be deletable + rebuildable from config without losing what to fetch.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_state (
    name TEXT PRIMARY KEY,
    last_synced_at TEXT,
    last_status TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS items (
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

CREATE INDEX IF NOT EXISTS idx_items_subscription ON items(subscription_name);
"""


@dataclass(frozen=True, slots=True)
class ItemRecord:
    subscription_name: str | None
    source_url: str
    dedup_key: str
    canonical_url: str
    title: str | None
    pub_date: str | None
    output_path: str


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    name: str
    last_synced_at: str | None
    last_status: str | None
    last_error: str | None


def default_state_path() -> Path:
    """Honors `PULPLINE_STATE_PATH`; otherwise XDG default."""
    override = os.environ.get("PULPLINE_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "pulpline" / "state.db"


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or default_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


def is_seen(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    """Return the existing output_path if `dedup_key` is already in items, else None."""
    row = conn.execute("SELECT output_path FROM items WHERE dedup_key = ?", (dedup_key,)).fetchone()
    if row is None:
        return None
    return str(row["output_path"]) if row["output_path"] is not None else None


def record_item(conn: sqlite3.Connection, item: ItemRecord) -> None:
    """Insert a new item. Caller must check `is_seen` first to avoid IntegrityError."""
    conn.execute(
        """
        INSERT INTO items (
            subscription_name, source_url, dedup_key, canonical_url,
            title, pub_date, ingested_at, output_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.subscription_name,
            item.source_url,
            item.dedup_key,
            item.canonical_url,
            item.title,
            item.pub_date,
            _now_iso(),
            item.output_path,
        ),
    )
    conn.commit()


def update_subscription_state(
    conn: sqlite3.Connection,
    name: str,
    status: str,
    error: str | None = None,
) -> None:
    """Upsert the per-subscription run state. status is 'ok' or 'error'."""
    conn.execute(
        """
        INSERT INTO subscription_state (name, last_synced_at, last_status, last_error)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_synced_at = excluded.last_synced_at,
            last_status = excluded.last_status,
            last_error = excluded.last_error
        """,
        (name, _now_iso(), status, error),
    )
    conn.commit()


def get_subscription_state(conn: sqlite3.Connection, name: str) -> SubscriptionState | None:
    row = conn.execute(
        "SELECT name, last_synced_at, last_status, last_error "
        "FROM subscription_state WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return SubscriptionState(
        name=row["name"],
        last_synced_at=row["last_synced_at"],
        last_status=row["last_status"],
        last_error=row["last_error"],
    )


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")
