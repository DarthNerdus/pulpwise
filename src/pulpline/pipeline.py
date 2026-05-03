"""Pipeline orchestration: compose sources, renderers, sinks, dedup, state."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import httpx

from pulpline.config import Config, Subscription, default_output_dir, load_config
from pulpline.models import ExtractionError, FetchError
from pulpline.sinks.filesystem import FilesystemSink
from pulpline.sources import get_source, pick_source_for_url
from pulpline.sources.base import Source
from pulpline.state import (
    ItemRecord,
    connect,
    is_seen,
    record_item,
    update_subscription_state,
    was_ingested,
)
from pulpline.util.dedup import dedup_key
from pulpline.util.logging import get_logger

_log = get_logger("pipeline")


@dataclass(frozen=True, slots=True)
class SyncReport:
    name: str
    new_items: int
    skipped: int
    errors: int
    error_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncTotal:
    reports: tuple[SyncReport, ...]

    @property
    def total_new(self) -> int:
        return sum(r.new_items for r in self.reports)

    @property
    def total_skipped(self) -> int:
        return sum(r.skipped for r in self.reports)

    @property
    def total_errors(self) -> int:
        return sum(r.errors for r in self.reports)


class ProgressReporter(Protocol):
    """Callback protocol for `sync()` progress events.

    Implemented by the CLI's rich-progress wrapper. None-passed when no
    progress UI is desired (tests, scripted use). All methods are optional
    in spirit: missing methods may be implemented as no-ops by callers.
    """

    def subscription_discovered(self, name: str, item_total: int) -> None: ...

    def item_started(self, name: str, title: str) -> None: ...

    def item_finished(self, name: str, *, skipped: bool = False) -> None: ...

    def subscription_finished(self, name: str, report: SyncReport) -> None: ...


def add_once(
    url: str,
    output_dir: Path | None = None,
    client: httpx.Client | None = None,
    state_path: Path | None = None,
    config: Config | None = None,
) -> Path:
    """Fetch a single URL, render to its source's format, write to disk. Dedup-aware.

    The source is picked by URL pattern (`Source.matches_url`); URLSource is
    the fallback. So `arxiv.org/abs/...` routes to ArXivSource (PDF),
    everything else routes to URLSource (EPUB via trafilatura).

    `config` is threaded into the source via `from_config` so config-aware
    sources (Anna's Archive's API key) work on the one-shot path. Loaded
    lazily from disk if not passed.

    If `url` has already been ingested, returns the existing path without
    re-fetching. Re-runs are idempotent.
    """
    base = (output_dir or default_output_dir()).expanduser()
    target = base / "oneshots"
    key = dedup_key(url)

    source_cls = pick_source_for_url(url)
    cfg = config if config is not None else load_config()

    _log.info("add_once start url=%s source=%s", url, source_cls.name)

    with connect(state_path) as conn:
        existing = is_seen(conn, key)
        if existing is not None:
            _log.info("add_once dedup-hit url=%s path=%s", url, existing)
            return Path(existing)

        with source_cls.from_config(cfg, client=client) as source:
            refs = list(source.discover(url))
            if len(refs) != 1:
                raise RuntimeError(
                    f"{source_cls.__name__}.discover yielded {len(refs)} items, expected exactly 1"
                )
            article = source.fetch(refs[0])
            content = source.render(article)

        sink = FilesystemSink(target)
        path = sink.write(article, content, source.extension)

        record_item(
            conn,
            ItemRecord(
                subscription_name=None,
                source_url=url,
                dedup_key=key,
                canonical_url=article.canonical_url,
                title=article.title,
                pub_date=_iso_or_none(article.pub_date),
                output_path=str(path),
            ),
        )
        _log.info("add_once wrote url=%s path=%s", url, path)
        return path


def sync(
    config: Config | None = None,
    state_path: Path | None = None,
    client: httpx.Client | None = None,
    progress: ProgressReporter | None = None,
) -> SyncTotal:
    """Run all subscriptions; for each, write EPUBs for any not-yet-seen items.

    Errors at feed level (network, parse) and item level (fetch, extract) are
    skip+log+continue per the spec; they end up in `SyncReport.error_messages`.

    `progress` is an optional reporter that receives subscription/item-level
    events as they happen. Pass None for silent operation (tests, scripts).
    """
    cfg = config or load_config()
    reports: list[SyncReport] = []
    with connect(state_path) as conn:
        for sub in cfg.subscriptions:
            reports.append(_sync_subscription(sub, cfg, conn, client, progress))
    return SyncTotal(reports=tuple(reports))


def _sync_subscription(
    sub: Subscription,
    cfg: Config,
    conn: sqlite3.Connection,
    client: httpx.Client | None,
    progress: ProgressReporter | None,
) -> SyncReport:
    _log.info("sync %s starting (source=%s url=%s)", sub.name, sub.source, sub.url)
    try:
        source_cls = get_source(sub.source)
    except ValueError as exc:
        _log.error("sync %s unknown source: %s", sub.name, exc)
        update_subscription_state(conn, sub.name, "error", str(exc))
        report = SyncReport(sub.name, 0, 0, 1, (str(exc),))
        if progress is not None:
            progress.subscription_finished(sub.name, report)
        return report

    try:
        with source_cls.from_config(cfg, client=client, subscription=sub) as source:
            report = _sync_with_source(sub, cfg, conn, source, progress)
    except (FetchError, ExtractionError) as exc:
        _log.error("sync %s aborted: %s", sub.name, exc)
        update_subscription_state(conn, sub.name, "error", str(exc))
        report = SyncReport(sub.name, 0, 0, 1, (str(exc),))

    _log.info(
        "sync %s done: new=%d skipped=%d errors=%d",
        sub.name,
        report.new_items,
        report.skipped,
        report.errors,
    )

    if progress is not None:
        progress.subscription_finished(sub.name, report)
    return report


def _sync_with_source(
    sub: Subscription,
    cfg: Config,
    conn: sqlite3.Connection,
    source: Source,
    progress: ProgressReporter | None,
) -> SyncReport:
    refs = list(source.discover(sub.url))
    if progress is not None:
        progress.subscription_discovered(sub.name, len(refs))

    output_dir = cfg.output_dir_for(sub)
    sink = FilesystemSink(output_dir)

    new_items = 0
    skipped = 0
    errors = 0
    error_msgs: list[str] = []

    for ref in refs:
        key = dedup_key(ref.url)
        if was_ingested(conn, key):
            # Includes soft-deleted items - don't re-fetch what the user deleted.
            skipped += 1
            if progress is not None:
                progress.item_finished(sub.name, skipped=True)
            continue
        if progress is not None:
            progress.item_started(sub.name, ref.title or ref.url)
        try:
            article = source.fetch(ref)
            article = replace(article, subscription_name=sub.name)
            content = source.render(article)
            path = sink.write(article, content, source.extension)
            record_item(
                conn,
                ItemRecord(
                    subscription_name=sub.name,
                    source_url=sub.url,
                    dedup_key=key,
                    canonical_url=article.canonical_url,
                    title=article.title,
                    pub_date=_iso_or_none(article.pub_date),
                    output_path=str(path),
                ),
            )
            new_items += 1
        except (FetchError, ExtractionError) as exc:
            _log.warning("sync %s item failed url=%s err=%s", sub.name, ref.url, exc)
            errors += 1
            error_msgs.append(f"{ref.url}: {exc}")
        if progress is not None:
            progress.item_finished(sub.name)

    status = "ok" if errors == 0 else "error"
    error_summary = "; ".join(error_msgs) if error_msgs else None
    update_subscription_state(
        conn,
        sub.name,
        status,
        error_summary,
        total_items=source.last_known_total,
    )
    return SyncReport(sub.name, new_items, skipped, errors, tuple(error_msgs))


@dataclass(frozen=True, slots=True)
class MigrationReport:
    moved: int
    skipped_already_correct: int
    skipped_collision: int
    missing_on_disk: int
    orphaned: int  # subscription was removed; file left alone


def migrate(
    config: Config | None = None,
    state_path: Path | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Move existing items into their new subscription-named subfolders.

    For each item in `items`:
      - subscription items go to `<output_dir>/<sub.name>/` (or the per-sub
        explicit override if set)
      - one-shot items go to `<output_dir>/oneshots/`

    Skips items whose file is gone, whose target already has a same-named
    file, or whose subscription has been removed from config. Updates
    `items.output_path` for each successful move.
    """
    cfg = config or load_config()
    moved = 0
    skipped_already = 0
    skipped_collision = 0
    missing = 0
    orphaned = 0

    global_default = Path(cfg.paths.output_dir).expanduser()

    with connect(state_path) as conn:
        rows = conn.execute(
            "SELECT id, subscription_name, output_path FROM items WHERE output_path IS NOT NULL"
        ).fetchall()

        for row in rows:
            old_path = Path(str(row["output_path"]))
            if not old_path.exists():
                missing += 1
                continue

            sub_name = row["subscription_name"]
            if sub_name is None:
                target_dir = global_default / "oneshots"
            else:
                sub = cfg.find(sub_name)
                if sub is None:
                    orphaned += 1
                    continue
                target_dir = cfg.output_dir_for(sub)

            new_path = target_dir / old_path.name
            if new_path == old_path:
                skipped_already += 1
                continue
            if new_path.exists():
                skipped_collision += 1
                continue

            if not dry_run:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_path), str(new_path))
                conn.execute(
                    "UPDATE items SET output_path = ? WHERE id = ?",
                    (str(new_path), int(row["id"])),
                )
            moved += 1

        if not dry_run:
            conn.commit()

    return MigrationReport(
        moved=moved,
        skipped_already_correct=skipped_already,
        skipped_collision=skipped_collision,
        missing_on_disk=missing,
        orphaned=orphaned,
    )


def _iso_or_none(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else None
