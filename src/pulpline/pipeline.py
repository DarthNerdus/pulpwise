"""Pipeline orchestration: compose sources, renderers, sinks, dedup, state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from pulpline.config import Config, Subscription, default_output_dir, load_config
from pulpline.models import ExtractionError, FetchError
from pulpline.sinks.filesystem import FilesystemSink
from pulpline.sources import REGISTRY, get_source
from pulpline.sources.base import Source
from pulpline.sources.url import URLSource
from pulpline.state import (
    ItemRecord,
    connect,
    is_seen,
    record_item,
    update_subscription_state,
)
from pulpline.util.dedup import dedup_key


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


def add_once(
    url: str,
    output_dir: Path | None = None,
    client: httpx.Client | None = None,
    state_path: Path | None = None,
) -> Path:
    """Fetch a single URL, render to its source's format, write to disk. Dedup-aware.

    The source is picked by URL pattern (`Source.matches_url`); URLSource is
    the fallback. So `arxiv.org/abs/...` routes to ArXivSource (PDF),
    everything else routes to URLSource (EPUB via trafilatura).

    If `url` has already been ingested, returns the existing path without
    re-fetching. Re-runs are idempotent.
    """
    target = (output_dir or default_output_dir()).expanduser()
    key = dedup_key(url)

    source_cls = _pick_source_for_url(url)

    with connect(state_path) as conn:
        existing = is_seen(conn, key)
        if existing is not None:
            return Path(existing)

        with source_cls(client=client) as source:
            refs = list(source.discover(url))
            if len(refs) != 1:
                raise RuntimeError(
                    f"{source_cls.__name__}.discover yielded {len(refs)} items, expected exactly 1"
                )
            article = source.fetch(refs[0])
            content = source.render(article)

        sink = FilesystemSink(target)
        path = sink.write(article, content, source_cls.extension)

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
        return path


def _pick_source_for_url(url: str) -> type[Source]:
    """Return the source class that claims `url`. URLSource is the fallback."""
    for name, cls in REGISTRY.items():
        if name == URLSource.name:
            continue  # fallback - checked last
        if cls.matches_url(url):
            return cls
    return URLSource


def sync(
    config: Config | None = None,
    state_path: Path | None = None,
    client: httpx.Client | None = None,
) -> SyncTotal:
    """Run all subscriptions; for each, write EPUBs for any not-yet-seen items.

    Errors at feed level (network, parse) and item level (fetch, extract) are
    skip+log+continue per the spec; they end up in `SyncReport.error_messages`.
    """
    cfg = config or load_config()
    reports: list[SyncReport] = []
    with connect(state_path) as conn:
        for sub in cfg.subscriptions:
            reports.append(_sync_subscription(sub, cfg, conn, client))
    return SyncTotal(reports=tuple(reports))


def _sync_subscription(
    sub: Subscription,
    cfg: Config,
    conn: sqlite3.Connection,
    client: httpx.Client | None,
) -> SyncReport:
    try:
        source_cls = get_source(sub.source)
    except ValueError as exc:
        update_subscription_state(conn, sub.name, "error", str(exc))
        return SyncReport(sub.name, 0, 0, 1, (str(exc),))

    try:
        with source_cls.from_config(cfg, client=client) as source:
            return _sync_with_source(sub, cfg, conn, source)
    except (FetchError, ExtractionError) as exc:
        update_subscription_state(conn, sub.name, "error", str(exc))
        return SyncReport(sub.name, 0, 0, 1, (str(exc),))


def _sync_with_source(
    sub: Subscription,
    cfg: Config,
    conn: sqlite3.Connection,
    source: Source,
) -> SyncReport:
    refs = list(source.discover(sub.url))

    output_dir = cfg.output_dir_for(sub)
    sink = FilesystemSink(output_dir)

    new_items = 0
    skipped = 0
    errors = 0
    error_msgs: list[str] = []

    for ref in refs:
        key = dedup_key(ref.url)
        if is_seen(conn, key) is not None:
            skipped += 1
            continue
        try:
            article = source.fetch(ref)
            article = replace(article, subscription_name=sub.name)
            content = source.render(article)
            path = sink.write(article, content, type(source).extension)
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
            errors += 1
            error_msgs.append(f"{ref.url}: {exc}")

    status = "ok" if errors == 0 else "error"
    error_summary = "; ".join(error_msgs) if error_msgs else None
    update_subscription_state(conn, sub.name, status, error_summary)
    return SyncReport(sub.name, new_items, skipped, errors, tuple(error_msgs))


def _iso_or_none(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else None
