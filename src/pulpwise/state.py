"""SQLite state: dedup ledger + per-subscription run state.

Schema lives here, not in TOML. TOML answers "what should pulpwise subscribe to?";
SQLite answers "what has pulpwise already done?". The ledger is load-bearing in
a way it wasn't when output was files on disk: remote deduplication is not a
substitute for remembering what the user already processed or deleted. The
tombstone rows here prevent a deleted article from being resurrected on the
next sync, and local skips avoid spending a destination API call per item.
Legacy column names `readwise_id` / `readwise_url` now store the remote result
for either destination; `destination` identifies which provider owns it.

Liveness: an item row is live while `deleted_at IS NULL`. (The old pulpline
schema encoded liveness as `output_path IS NOT NULL`; a migration below
backfills `deleted_at` for legacy tombstones so drop-in databases keep their
deletion history.)
"""

from __future__ import annotations

import contextlib
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
    last_error TEXT,
    total_items INTEGER
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
    readwise_id TEXT,
    readwise_url TEXT,
    submission_kind TEXT,
    destination TEXT NOT NULL DEFAULT 'readwise',
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_subscription ON items(subscription_name);
"""

# Forward migrations: adapt older DBs (including drop-in pulpline ledgers) in
# place. SQLite has no IF NOT EXISTS for ADD COLUMN, so each statement is
# tried and OperationalError swallowed in `connect()` below. The final UPDATE
# backfills `deleted_at` for rows soft-deleted under the old scheme (liveness
# was `output_path IS NOT NULL` there); on fresh databases it errors on the
# missing `output_path` column and is skipped, which is correct. We use
# `ingested_at` as the stand-in timestamp - the real deletion time was never
# recorded for those rows. The `readwise_id IS NULL` guard is load-bearing:
# this UPDATE re-runs on every connect to a legacy DB, and rows written by
# pulpwise itself also have output_path NULL - without the guard, the next
# connect would tombstone every document pushed since the migration. Every
# pulpwise-written row has a readwise_id; only pre-fork rows have none.
_MIGRATIONS = [
    "ALTER TABLE subscription_state ADD COLUMN total_items INTEGER",
    "ALTER TABLE items ADD COLUMN deleted_at TEXT",
    "ALTER TABLE items ADD COLUMN readwise_id TEXT",
    "ALTER TABLE items ADD COLUMN readwise_url TEXT",
    "ALTER TABLE items ADD COLUMN submission_kind TEXT",
    "ALTER TABLE items ADD COLUMN destination TEXT NOT NULL DEFAULT 'readwise'",
    "UPDATE items SET dedup_key = 'shiori:' || dedup_key "
    "WHERE destination = 'shiori' AND dedup_key NOT LIKE 'shiori:%'",
    "UPDATE items SET deleted_at = ingested_at "
    "WHERE output_path IS NULL AND deleted_at IS NULL AND readwise_id IS NULL",
]


@dataclass(frozen=True, slots=True)
class ItemRecord:
    subscription_name: str | None
    source_url: str
    dedup_key: str
    canonical_url: str
    title: str | None
    pub_date: str | None
    readwise_id: str
    readwise_url: str
    submission_kind: str  # 'url' | 'html'
    destination: str = "readwise"


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    name: str
    last_synced_at: str | None
    last_status: str | None
    last_error: str | None
    total_items: int | None = None  # source-reported total, when the source knows one


@dataclass(frozen=True, slots=True)
class OneShotItem:
    title: str | None
    canonical_url: str
    ingested_at: str
    readwise_url: str | None


def default_state_path() -> Path:
    """Honors `PULPWISE_STATE_PATH`; otherwise XDG default."""
    override = os.environ.get("PULPWISE_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "pulpwise" / "state.db"


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or default_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        for migration in _MIGRATIONS:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(migration)
        # The backfill UPDATE above opens an implicit write transaction;
        # commit it so read-only sessions don't hold a reserved lock for
        # their whole lifetime (and roll the migration back on close).
        conn.commit()
        yield conn
    finally:
        conn.close()


def is_seen(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    """Return the existing Reader URL if `dedup_key` has a live, pushed row.

    Live = not soft-deleted (`deleted_at IS NULL`) AND actually in Readwise
    (`readwise_url` set). Used by `add_once` for idempotency: re-adding a URL
    that's already in Reader just returns its document URL. Soft-deleted rows
    return None so re-running `pulpwise add` re-pushes them; so do legacy
    file-era rows that were never pushed to Readwise.

    For "should sync skip this URL?" use `was_ingested` instead - that's a
    broader check that includes soft-deleted and legacy rows (we don't want
    sync to re-push what the user deleted or already read as a file).
    """
    row = conn.execute(
        "SELECT readwise_url, deleted_at FROM items WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    if row is None or row["deleted_at"] is not None or row["readwise_url"] is None:
        return None
    return str(row["readwise_url"])


def was_ingested(conn: sqlite3.Connection, dedup_key: str) -> bool:
    """True if `dedup_key` has any row at all, including soft-deleted ones.

    Used by sync to decide "skip this item" without re-pushing things the
    user explicitly deleted (in the TUI or in Reader itself).
    """
    return (
        conn.execute("SELECT 1 FROM items WHERE dedup_key = ? LIMIT 1", (dedup_key,)).fetchone()
        is not None
    )


def record_item(conn: sqlite3.Connection, item: ItemRecord) -> None:
    """Insert a new item, or update the existing row on dedup_key conflict.

    The `ON CONFLICT` upsert handles re-ingestion after soft delete:
    `pulpwise add <url>` on a previously-deleted article updates the existing
    row in place (clearing the tombstone) rather than failing on the
    UNIQUE constraint. It also upgrades legacy file-era rows with their
    Readwise document identifiers on re-add.
    """
    conn.execute(
        """
        INSERT INTO items (
            subscription_name, source_url, dedup_key, canonical_url,
            title, pub_date, ingested_at, readwise_id, readwise_url,
            submission_kind, destination
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dedup_key) DO UPDATE SET
            subscription_name = excluded.subscription_name,
            source_url = excluded.source_url,
            canonical_url = excluded.canonical_url,
            title = excluded.title,
            pub_date = excluded.pub_date,
            ingested_at = excluded.ingested_at,
            readwise_id = excluded.readwise_id,
            readwise_url = excluded.readwise_url,
            submission_kind = excluded.submission_kind,
            destination = excluded.destination,
            deleted_at = NULL
        """,
        (
            item.subscription_name,
            item.source_url,
            item.dedup_key,
            item.canonical_url,
            item.title,
            item.pub_date,
            _now_iso(),
            item.readwise_id,
            item.readwise_url,
            item.submission_kind,
            item.destination,
        ),
    )
    conn.commit()


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    """Soft-delete: tombstone the row. The Reader document is left alone.

    Pulp Wise is push-only - it never deletes from Readwise; removing the
    document there is the user's call, made in Reader. What the tombstone
    buys is the other direction: the row stays in `items` so sync never
    re-pushes the item (which matters - Readwise would happily recreate a
    document deleted on its side). To bring an item back, `pulpwise
    add <url>` re-pushes via the upsert path in `record_item`, which
    clears `deleted_at`.
    """
    conn.execute(
        "UPDATE items SET deleted_at = ? WHERE id = ?",
        (_now_iso(), item_id),
    )
    conn.commit()


def update_subscription_state(
    conn: sqlite3.Connection,
    name: str,
    status: str,
    error: str | None = None,
    total_items: int | None = None,
) -> None:
    """Upsert the per-subscription run state. status is 'ok' or 'error'.

    `total_items` is preserved when None - we only update it when the source
    reports a fresh total.
    """
    if total_items is None:
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
    else:
        conn.execute(
            """
            INSERT INTO subscription_state (
                name, last_synced_at, last_status, last_error, total_items
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                last_synced_at = excluded.last_synced_at,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                total_items = excluded.total_items
            """,
            (name, _now_iso(), status, error, total_items),
        )
    conn.commit()


def get_subscription_state(conn: sqlite3.Connection, name: str) -> SubscriptionState | None:
    row = conn.execute(
        "SELECT name, last_synced_at, last_status, last_error, total_items "
        "FROM subscription_state WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    total = row["total_items"]
    return SubscriptionState(
        name=row["name"],
        last_synced_at=row["last_synced_at"],
        last_status=row["last_status"],
        last_error=row["last_error"],
        total_items=int(total) if total is not None else None,
    )


def list_oneshots(conn: sqlite3.Connection, limit: int = 20) -> list[OneShotItem]:
    """Return the most recent one-shot ingestions (subscription_name IS NULL).

    Items pushed by `pulpwise sync` carry their subscription_name and don't
    show up here - those are summarized at the subscription level in
    `pulpwise list`.
    """
    rows = conn.execute(
        "SELECT title, canonical_url, ingested_at, readwise_url FROM items "
        "WHERE subscription_name IS NULL AND deleted_at IS NULL "
        "ORDER BY ingested_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        OneShotItem(
            title=row["title"],
            canonical_url=row["canonical_url"],
            ingested_at=row["ingested_at"],
            readwise_url=row["readwise_url"],
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class LibraryItem:
    id: int
    title: str | None
    canonical_url: str
    subscription_name: str | None
    ingested_at: str
    pub_date: str | None
    # None for legacy file-era rows that were never pushed to Readwise.
    readwise_url: str | None
    submission_kind: str | None = None
    deleted_at: str | None = None


_LIBRARY_COLUMNS = (
    "id, title, canonical_url, subscription_name, ingested_at, pub_date, "
    "readwise_url, submission_kind, deleted_at"
)


def _library_item(row: sqlite3.Row) -> LibraryItem:
    return LibraryItem(
        id=int(row["id"]),
        title=row["title"],
        canonical_url=row["canonical_url"],
        subscription_name=row["subscription_name"],
        ingested_at=row["ingested_at"],
        pub_date=row["pub_date"],
        readwise_url=row["readwise_url"],
        submission_kind=row["submission_kind"],
        deleted_at=row["deleted_at"],
    )


def list_items(
    conn: sqlite3.Connection,
    limit: int | None = 500,
    filter_text: str | None = None,
) -> list[LibraryItem]:
    """Return recent live items, optionally filtered by case-insensitive title/url match.

    `limit=None` returns every live item - the library view uses that so
    the list is complete rather than a sliding most-recent window.
    """
    sql = f"SELECT {_LIBRARY_COLUMNS} FROM items WHERE deleted_at IS NULL"
    params: list[object] = []
    if filter_text:
        sql += " AND (LOWER(title) LIKE ? OR LOWER(canonical_url) LIKE ?)"
        like = f"%{filter_text.lower()}%"
        params.extend([like, like])
    sql += " ORDER BY ingested_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_library_item(row) for row in rows]


def count_total(conn: sqlite3.Connection) -> int:
    """Count of live items (excludes soft-deleted)."""
    row = conn.execute("SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NULL").fetchone()
    return int(row["n"]) if row else 0


def count_since(conn: sqlite3.Connection, since_iso: str) -> int:
    """Count live items ingested on or after `since_iso`."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE ingested_at >= ? AND deleted_at IS NULL",
        (since_iso,),
    ).fetchone()
    return int(row["n"]) if row else 0


def count_by_subscription(conn: sqlite3.Connection) -> dict[str, int]:
    """Live items grouped by subscription_name."""
    rows = conn.execute(
        "SELECT COALESCE(subscription_name, '[one-shot]') AS bucket, COUNT(*) AS n "
        "FROM items WHERE deleted_at IS NULL "
        "GROUP BY bucket ORDER BY n DESC"
    ).fetchall()
    return {row["bucket"]: int(row["n"]) for row in rows}


def count_by_kind(conn: sqlite3.Connection) -> dict[str, int]:
    """Live items grouped by how they were delivered.

    'url' = bare URL save (Readwise fetched it), 'html' = content submission
    (we pushed the body), 'file' = legacy pulpline rows written to disk
    before the Readwise era.
    """
    rows = conn.execute(
        "SELECT COALESCE(submission_kind, 'file') AS kind, COUNT(*) AS n "
        "FROM items WHERE deleted_at IS NULL "
        "GROUP BY kind ORDER BY n DESC"
    ).fetchall()
    return {row["kind"]: int(row["n"]) for row in rows}


def items_per_day(conn: sqlite3.Connection, days: int = 30) -> list[tuple[str, int]]:
    """Return [(YYYY-MM-DD, count), ...] for the last `days` days, including zeros."""
    rows = conn.execute(
        "SELECT substr(ingested_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM items WHERE deleted_at IS NULL "
        "GROUP BY day ORDER BY day DESC LIMIT ?",
        (days,),
    ).fetchall()
    by_day = {row["day"]: int(row["n"]) for row in rows}

    today = datetime.now(tz=UTC).date()
    out: list[tuple[str, int]] = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        out.append((day.isoformat(), by_day.get(day.isoformat(), 0)))
    return out


def activity_per_day(conn: sqlite3.Connection, days: int = 30) -> list[tuple[str, int, int]]:
    """Return [(YYYY-MM-DD, added, deleted), ...] for the last `days` days.

    `added` counts items first ingested on that day (uses `ingested_at`),
    including any that were later soft-deleted - so the historical view
    doesn't retroactively shrink when you tidy up.

    `deleted` counts items soft-deleted on that day.
    """
    # Two independent aggregates, joined in Python so days with adds-but-no-deletes
    # (or vice versa) still appear.
    added_rows = conn.execute(
        "SELECT substr(ingested_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM items WHERE ingested_at >= ? "
        "GROUP BY day",
        ((datetime.now(tz=UTC) - timedelta(days=days)).date().isoformat(),),
    ).fetchall()
    added_by_day = {row["day"]: int(row["n"]) for row in added_rows}

    deleted_rows = conn.execute(
        "SELECT substr(deleted_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM items WHERE deleted_at IS NOT NULL AND deleted_at >= ? "
        "GROUP BY day",
        ((datetime.now(tz=UTC) - timedelta(days=days)).date().isoformat(),),
    ).fetchall()
    deleted_by_day = {row["day"]: int(row["n"]) for row in deleted_rows}

    today = datetime.now(tz=UTC).date()
    out: list[tuple[str, int, int]] = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        out.append((day, added_by_day.get(day, 0), deleted_by_day.get(day, 0)))
    return out


def list_recently_deleted(
    conn: sqlite3.Connection,
    limit: int | None = 200,
    filter_text: str | None = None,
) -> list[LibraryItem]:
    """Return soft-deleted items, most-recently-deleted first.

    `limit=None` returns every tombstone. Returns the same `LibraryItem`
    dataclass as `list_items`, with `deleted_at` set; the library view
    branches on `deleted_at` rather than on type.
    """
    sql = f"SELECT {_LIBRARY_COLUMNS} FROM items WHERE deleted_at IS NOT NULL"
    params: list[object] = []
    if filter_text:
        sql += " AND (LOWER(title) LIKE ? OR LOWER(canonical_url) LIKE ?)"
        like = f"%{filter_text.lower()}%"
        params.extend([like, like])
    sql += " ORDER BY deleted_at DESC, ingested_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_library_item(row) for row in rows]


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")
