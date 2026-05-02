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
from datetime import UTC, datetime, timedelta
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


@dataclass(frozen=True, slots=True)
class OneShotItem:
    title: str | None
    canonical_url: str
    ingested_at: str
    output_path: str | None


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


def list_oneshots(conn: sqlite3.Connection, limit: int = 20) -> list[OneShotItem]:
    """Return the most recent one-shot ingestions (subscription_name IS NULL).

    Items written by `pulp sync` carry their subscription_name and don't show
    up here - those are summarized at the subscription level in `pulp list`.
    """
    rows = conn.execute(
        "SELECT title, canonical_url, ingested_at, output_path FROM items "
        "WHERE subscription_name IS NULL "
        "ORDER BY ingested_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        OneShotItem(
            title=row["title"],
            canonical_url=row["canonical_url"],
            ingested_at=row["ingested_at"],
            output_path=row["output_path"],
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class LibraryItem:
    title: str | None
    canonical_url: str
    subscription_name: str | None
    ingested_at: str
    pub_date: str | None
    output_path: str | None


def list_items(
    conn: sqlite3.Connection,
    limit: int = 500,
    filter_text: str | None = None,
) -> list[LibraryItem]:
    """Return recent items, optionally filtered by case-insensitive title/url match."""
    sql = (
        "SELECT title, canonical_url, subscription_name, ingested_at, pub_date, output_path "
        "FROM items"
    )
    params: list[object] = []
    if filter_text:
        sql += " WHERE LOWER(title) LIKE ? OR LOWER(canonical_url) LIKE ?"
        like = f"%{filter_text.lower()}%"
        params.extend([like, like])
    sql += " ORDER BY ingested_at DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        LibraryItem(
            title=row["title"],
            canonical_url=row["canonical_url"],
            subscription_name=row["subscription_name"],
            ingested_at=row["ingested_at"],
            pub_date=row["pub_date"],
            output_path=row["output_path"],
        )
        for row in rows
    ]


def count_total(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()
    return int(row["n"]) if row else 0


def count_since(conn: sqlite3.Connection, since_iso: str) -> int:
    """Count items where `ingested_at >= since_iso` (lexicographic on ISO 8601)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE ingested_at >= ?",
        (since_iso,),
    ).fetchone()
    return int(row["n"]) if row else 0


def count_by_subscription(conn: sqlite3.Connection) -> dict[str, int]:
    """Items grouped by subscription_name. NULL is bucketed as '[one-shot]'."""
    rows = conn.execute(
        "SELECT COALESCE(subscription_name, '[one-shot]') AS bucket, COUNT(*) AS n "
        "FROM items GROUP BY bucket ORDER BY n DESC"
    ).fetchall()
    return {row["bucket"]: int(row["n"]) for row in rows}


def count_by_extension(conn: sqlite3.Connection) -> dict[str, int]:
    """Items grouped by file extension (extracted from output_path)."""
    rows = conn.execute("SELECT output_path FROM items WHERE output_path IS NOT NULL").fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        path = row["output_path"]
        if not isinstance(path, str):
            continue
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "(none)"
        counts[ext] = counts.get(ext, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def items_per_day(conn: sqlite3.Connection, days: int = 30) -> list[tuple[str, int]]:
    """Return [(YYYY-MM-DD, count), ...] for the last `days` days, including zeros."""
    rows = conn.execute(
        "SELECT substr(ingested_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM items GROUP BY day ORDER BY day DESC LIMIT ?",
        (days,),
    ).fetchall()
    by_day = {row["day"]: int(row["n"]) for row in rows}

    today = datetime.now(tz=UTC).date()
    out: list[tuple[str, int]] = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        out.append((day.isoformat(), by_day.get(day.isoformat(), 0)))
    return out


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")
