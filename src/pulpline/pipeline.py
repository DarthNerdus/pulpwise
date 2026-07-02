"""Pipeline orchestration: compose sources, renderers, sinks, dedup, state."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import httpx

from pulpline.config import Config, Subscription, default_output_dir, load_config
from pulpline.models import ExtractionError, FetchError, Paywalled, RateLimited
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
class PaywalledItem:
    """One paywalled URL grouped under its host so the CLI can render
    a single 'fix your cookies for X' hint per host."""

    url: str
    host: str


@dataclass(frozen=True, slots=True)
class SyncReport:
    name: str
    new_items: int
    skipped: int
    errors: int
    error_messages: tuple[str, ...] = ()
    paywalled: tuple[PaywalledItem, ...] = ()


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

    @property
    def total_paywalled(self) -> int:
        return sum(len(r.paywalled) for r in self.reports)


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
    paywalled_items: list[PaywalledItem] = []

    for index, ref in enumerate(refs):
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
        except RateLimited as exc:
            # Must precede the FetchError clause (it's a subclass). By the
            # time this raises, the transport already waited out one full
            # cooldown - the limiter is genuinely angry, so every remaining
            # fetch to the same bucket is doomed too. Stop the subscription
            # instead of burning a request cycle per item. Nothing is lost:
            # unfetched refs aren't in the ledger, so the next sync picks
            # them up.
            remaining = len(refs) - index
            _log.warning("sync %s rate-limited url=%s err=%s", sub.name, ref.url, exc)
            errors += 1
            error_msgs.append(f"{exc}; {remaining} item(s) deferred to the next sync run")
            if progress is not None:
                progress.item_finished(sub.name)
            break
        except Paywalled as exc:
            _log.info("sync %s paywalled url=%s host=%s", sub.name, ref.url, exc.host)
            paywalled_items.append(PaywalledItem(url=ref.url, host=exc.host))
        except (FetchError, ExtractionError) as exc:
            _log.warning("sync %s item failed url=%s err=%s", sub.name, ref.url, exc)
            errors += 1
            error_msgs.append(f"{ref.url}: {exc}")
        if progress is not None:
            progress.item_finished(sub.name)

    # Paywalled items are a setup issue (cookies not exporting), not a
    # subscription-level error worth marking the row red. Treat them
    # as an "ok" sync with a separate visible bucket in the CLI.
    status = "ok" if errors == 0 else "error"
    error_summary = "; ".join(error_msgs) if error_msgs else None
    update_subscription_state(
        conn,
        sub.name,
        status,
        error_summary,
        total_items=source.last_known_total,
    )
    return SyncReport(
        sub.name, new_items, skipped, errors, tuple(error_msgs), tuple(paywalled_items)
    )


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """Result of a `backfill` run. Mirrors SyncReport but counts pages walked."""

    name: str
    new_items: int
    skipped_already_ingested: int
    errors: int
    pages_walked: int
    stopped_reason: str  # 'max_new', 'since', 'exhausted', 'rate_limited', 'unsupported'
    error_messages: tuple[str, ...] = ()


class BackfillUnsupported(Exception):  # noqa: N818
    """Raised when the requested subscription's source can't paginate backwards.

    Kept Suffix-free for readability at call sites - this is a control-flow
    signal callers consume with try/except, not a recoverable error class.
    """


def backfill(
    sub: Subscription,
    config: Config | None = None,
    state_path: Path | None = None,
    client: httpx.Client | None = None,
    *,
    max_new: int | None = 50,
    since_iso: str | None = None,
) -> BackfillReport:
    """Walk a subscription's archive backwards, ingesting older posts.

    Unlike `sync`, which only looks at the newest page, this paginates as
    deep as the source supports. Stop conditions:
      - `max_new`: stop after N successful ingestions (None = unlimited)
      - `since_iso`: stop when a post's pub_date is older than this date
      - Source's pagination exhausted (API returns empty page)
      - Host rate-limits us past the transport's retries (persistent 429):
        the walk ends early with `stopped_reason='rate_limited'`; re-running
        backfill later resumes where it left off via the dedup ledger

    Already-ingested items (including soft-deleted ones) are skipped via
    the dedup ledger but don't stop the walk - we keep going past them
    to catch sparse holes in the existing data.

    Raises `BackfillUnsupported` if the source doesn't implement
    `discover_backwards`. Currently the Substack sources do: publications
    walk the archive by offset, the saves list walks the reader feed by
    save time.
    """
    cfg = config or load_config()
    try:
        source_cls = get_source(sub.source)
    except ValueError as exc:
        raise BackfillUnsupported(str(exc)) from exc

    with (
        connect(state_path) as conn,
        source_cls.from_config(cfg, client=client, subscription=sub) as source,
    ):
        if not hasattr(source, "discover_backwards"):
            raise BackfillUnsupported(
                f"source {sub.source!r} doesn't support backfill yet "
                "(only Substack publications do for now)"
            )
        return _backfill_with_source(sub, cfg, conn, source, max_new=max_new, since_iso=since_iso)


def _backfill_with_source(
    sub: Subscription,
    cfg: Config,
    conn: sqlite3.Connection,
    source: Source,
    *,
    max_new: int | None,
    since_iso: str | None,
) -> BackfillReport:
    output_dir = cfg.output_dir_for(sub)
    sink = FilesystemSink(output_dir)

    new_items = 0
    skipped = 0
    errors = 0
    pages_walked = 0
    error_msgs: list[str] = []
    stopped_reason = "exhausted"

    # discover_backwards yields one ref at a time but fetches pages of N
    # under the hood; we count pages by tracking when we cross page
    # boundaries via the ref-counter.
    seen_refs = 0
    page_size = 25  # mirrors _DISCOVER_LIMIT in substack source

    refs_iter = source.discover_backwards(sub.url)  # type: ignore[attr-defined]
    # RateLimited is caught around the whole walk (not per item) because it
    # can surface from two places: an item fetch, or the pagination request
    # hiding inside the refs_iter generator. Either way the response is the
    # same - stop walking; a later backfill resumes via the dedup ledger.
    try:
        for ref in refs_iter:
            seen_refs += 1
            if seen_refs % page_size == 1:
                pages_walked += 1

            # Date floor: refs come newest-first, so once we see a pub_date
            # before the floor we know everything after is older too.
            if since_iso and ref.pub_date is not None:
                ref_iso = ref.pub_date.isoformat()
                if ref_iso < since_iso:
                    stopped_reason = "since"
                    break

            key = dedup_key(ref.url)
            if was_ingested(conn, key):
                skipped += 1
                continue

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
            except Paywalled as exc:
                _log.info("backfill %s paywalled url=%s host=%s", sub.name, ref.url, exc.host)
            except RateLimited:
                raise  # must precede FetchError (subclass); handled by the outer except
            except (FetchError, ExtractionError) as exc:
                _log.warning("backfill %s item failed url=%s err=%s", sub.name, ref.url, exc)
                errors += 1
                error_msgs.append(f"{ref.url}: {exc}")

            if max_new is not None and new_items >= max_new:
                stopped_reason = "max_new"
                break
    except RateLimited as exc:
        _log.warning("backfill %s rate-limited: %s", sub.name, exc)
        errors += 1
        error_msgs.append(str(exc))
        stopped_reason = "rate_limited"

    _log.info(
        "backfill %s done: new=%d skipped=%d errors=%d pages=%d stopped=%s",
        sub.name,
        new_items,
        skipped,
        errors,
        pages_walked,
        stopped_reason,
    )
    return BackfillReport(
        name=sub.name,
        new_items=new_items,
        skipped_already_ingested=skipped,
        errors=errors,
        pages_walked=pages_walked,
        stopped_reason=stopped_reason,
        error_messages=tuple(error_msgs),
    )


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
