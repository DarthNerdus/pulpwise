"""Pipeline orchestration: compose sources, destination sinks, dedup, and state.

Per-item flow: dedup against the local ledger, build provider input, push it,
record the result, then ack the source. Readwise may fetch or receive content;
Shiori receives the discovered URL without a content fetch. The ordering is
the crash-safety contract: remote push strictly precedes ledger write, which
strictly precedes source ack.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Protocol

import httpx

from pulpwise.config import Config, ConfigError, Subscription, load_config
from pulpwise.models import (
    ExtractionError,
    FetchError,
    ItemRef,
    ItemSkipped,
    Paywalled,
    RateLimited,
    ReaderSubmission,
)
from pulpwise.sinks.readwise import (
    SAVE_LOCATIONS,
    ReadwiseAuthError,
    ReadwiseSink,
    canonical_location,
)
from pulpwise.sinks.shiori import ShioriAuthError, ShioriSink
from pulpwise.sources import get_source, pick_source_for_url
from pulpwise.sources.base import Source
from pulpwise.state import (
    ItemRecord,
    connect,
    is_seen,
    record_item,
    update_subscription_state,
    was_ingested,
)
from pulpwise.util.dedup import dedup_key
from pulpwise.util.logging import get_logger

_log = get_logger("pipeline")

#: Where saves land in Reader when nothing says otherwise. Pulp Wise acts as
#: a feed reader in front of Reader, so pushed items default to the Feed
#: section (like Reader's native RSS) rather than flooding the inbox. Route a
#: subscription elsewhere with `options.location`, or a one-shot with
#: `pulpwise add --location`. (Reader silently falls back to the account
#: default if the user has disabled the targeted location in settings.)
DEFAULT_LOCATION = "feed"
SHIORI_DESTINATION = "shiori"

DestinationSink = ReadwiseSink | ShioriSink


def _ack_item(source: Source, ref: ItemRef) -> None:
    """Tell the source an item is fully accounted for (recorded or skipped).

    Duck-typed like `discover_backwards`: sources that care (email's
    mark-read/move policy) implement `ack(ref)`; everyone else doesn't. Runs
    only *after* the ledger owns the item, so a source can safely do things
    that take the item out of future discovery (move the message). Failures
    are logged and swallowed - the item is already safe in the ledger.
    """
    ack = getattr(source, "ack", None)
    if not callable(ack):
        return
    try:
        ack(ref)
    except Exception as exc:  # ack is best-effort by contract
        _log.warning("ack failed url=%s err=%s", ref.url, exc)


def _build_submission(source: Source, ref: ItemRef) -> ReaderSubmission:
    """Turn a discovered ref into what gets pushed to Readwise."""
    if not source.fetch_needed:
        return source.submission_for_ref(ref)
    article = source.fetch(ref)
    return source.submission_for_article(article)


def _submission_for_destination(source: Source, ref: ItemRef, destination: str) -> ReaderSubmission:
    """Build provider input without fetching content for URL-only Shiori."""
    if destination == SHIORI_DESTINATION:
        return ReaderSubmission(url=ref.url, pub_date=ref.pub_date)
    return _build_submission(source, ref)


def _destination_key(url: str, destination: str) -> str:
    key = dedup_key(url)
    return key if destination == "readwise" else f"{destination}:{key}"


def _subscription_keys(url: str) -> tuple[str, str]:
    base = dedup_key(url)
    return base, f"{SHIORI_DESTINATION}:{base}"


def _was_subscription_ingested(conn: sqlite3.Connection, url: str) -> bool:
    """Subscription history is global even when its future destination changes."""
    return any(was_ingested(conn, key) for key in _subscription_keys(url))


def _already_ingested_as(
    conn: sqlite3.Connection,
    submission: ReaderSubmission,
    ref_key: str,
) -> bool:
    """True when the submission's Reader-identity URL was already ingested
    under a *different* discovery URL.

    The ledger dedups on the discovery URL (ref.url) because that check must
    run before any fetch; but the document's identity in Reader is
    submission.url, which can differ (arXiv abs vs pdf, email permalink vs
    mid:, one-shot vs feed forms). Without this second check, a tombstoned
    document could be resurrected - or double-rowed - via the other form.
    """
    submission_keys = _subscription_keys(submission.url)
    return ref_key not in submission_keys and any(
        was_ingested(conn, key) for key in submission_keys
    )


@dataclass(frozen=True, slots=True)
class PushOptions:
    destination: str
    location: str | None = None
    tags: tuple[str, ...] = ()


def _push_options(sub: Subscription) -> PushOptions:
    """Parse a subscription's destination and provider-specific routing."""
    location_raw = sub.option("location")
    if location_raw == SHIORI_DESTINATION:
        return PushOptions(destination=SHIORI_DESTINATION)

    location = DEFAULT_LOCATION
    if location_raw is not None:
        if isinstance(location_raw, str):
            location_raw = canonical_location(location_raw)
        if not isinstance(location_raw, str) or location_raw not in SAVE_LOCATIONS:
            valid = ", ".join(sorted((*SAVE_LOCATIONS, SHIORI_DESTINATION)))
            raise ConfigError(
                f"subscription {sub.name!r}: options.location must be one of "
                f"{valid} (got {location_raw!r})"
            )
        location = location_raw

    tags_raw = sub.option("tags")
    if tags_raw is None:
        tags: tuple[str, ...] = ()
    elif isinstance(tags_raw, str):
        tags = tuple(tag.strip() for tag in tags_raw.split(",") if tag.strip())
    else:
        raise ConfigError(
            f"subscription {sub.name!r}: options.tags must be a comma-separated string"
        )
    return PushOptions(destination="readwise", location=location, tags=tags)


class _SinkPool:
    """Lazily build one sink per destination for the duration of a run."""

    def __init__(self, cfg: Config, override: DestinationSink | None = None) -> None:
        self._cfg = cfg
        self._override = override
        self._owned: dict[str, DestinationSink] = {}

    def prepare(self, subscriptions: tuple[Subscription, ...]) -> None:
        destinations: set[str] = set()
        for sub in subscriptions:
            if sub.disabled:
                continue
            try:
                destinations.add(_push_options(sub).destination)
            except ConfigError:
                continue
        for destination in destinations:
            self.get(destination)

    def get(self, destination: str) -> DestinationSink:
        if self._override is not None:
            return self._override
        existing = self._owned.get(destination)
        if existing is not None:
            return existing
        if destination == "readwise":
            created: DestinationSink = ReadwiseSink.from_config(self._cfg)
        elif destination == SHIORI_DESTINATION:
            created = ShioriSink.from_config(self._cfg)
        else:
            raise ValueError(f"unknown destination {destination!r}")
        self._owned[destination] = created
        return created

    def close(self) -> None:
        for sink in self._owned.values():
            sink.close()


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
    rate_limited_host: str | None = None


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


@dataclass(frozen=True, slots=True)
class AddResult:
    """Outcome of a one-shot add at its selected destination."""

    reader_url: str
    deduped: bool  # True = the destination ledger already had it; nothing was pushed
    already_in_readwise: bool = False  # legacy name: destination already had the URL
    destination: str = "readwise"


def add_once(
    url: str,
    client: httpx.Client | None = None,
    state_path: Path | None = None,
    config: Config | None = None,
    sink: DestinationSink | None = None,
    *,
    location: str | None = None,
    tags: tuple[str, ...] = (),
) -> AddResult:
    """Push a single URL to Readwise Reader or Shiori. Dedup-aware."""
    cfg = config if config is not None else load_config()
    destination = SHIORI_DESTINATION if location == SHIORI_DESTINATION else "readwise"
    sink_location = None if destination == SHIORI_DESTINATION else location or DEFAULT_LOCATION
    key = _destination_key(url, destination)
    source_cls = pick_source_for_url(url)

    _log.info("add_once start url=%s source=%s destination=%s", url, source_cls.name, destination)

    with connect(state_path) as conn:
        existing = is_seen(conn, key)
        if existing is not None:
            _log.info("add_once dedup-hit url=%s destination=%s", url, destination)
            return AddResult(reader_url=existing, deduped=True, destination=destination)

        with source_cls.from_config(cfg, client=client) as source:
            refs = list(source.discover(url))
            if len(refs) != 1:
                raise RuntimeError(
                    f"{source_cls.__name__}.discover yielded {len(refs)} items, expected exactly 1"
                )
            submission = _submission_for_destination(source, refs[0], destination)

        submission_key = _destination_key(submission.url, destination)
        if submission_key != key:
            existing = is_seen(conn, submission_key)
            if existing is not None:
                _log.info("add_once dedup-hit url=%s destination=%s", url, destination)
                return AddResult(reader_url=existing, deduped=True, destination=destination)

        owns_sink = sink is None
        if sink is not None:
            active_sink = sink
        elif destination == SHIORI_DESTINATION:
            active_sink = ShioriSink.from_config(cfg)
        else:
            active_sink = ReadwiseSink.from_config(cfg, client=client)
        try:
            result = active_sink.push(submission, location=sink_location, tags=tags)
        finally:
            if owns_sink:
                active_sink.close()

        record_item(
            conn,
            ItemRecord(
                subscription_name=None,
                source_url=url,
                dedup_key=submission_key,
                canonical_url=submission.url,
                title=submission.title or refs[0].title,
                pub_date=_iso_or_none(submission.pub_date),
                readwise_id=result.document_id,
                readwise_url=result.reader_url,
                submission_kind=result.kind,
                destination=destination,
            ),
        )
        _log.info("add_once pushed url=%s destination=%s", url, destination)
        return AddResult(
            reader_url=result.reader_url,
            deduped=False,
            already_in_readwise=result.already_existed,
            destination=destination,
        )


def sync(
    config: Config | None = None,
    state_path: Path | None = None,
    client: httpx.Client | None = None,
    progress: ProgressReporter | None = None,
    sink: DestinationSink | None = None,
) -> SyncTotal:
    """Run enabled subscriptions and push unseen items to their destinations.

    Disabled subscriptions are skipped entirely - no discovery, no report.

    Errors at feed level (network, parse) and item level (fetch, push) are
    skip+log+continue per the spec; they end up in `SyncReport.error_messages`.
    Token problems raise instead - every subscription would fail identically,
    so fail once, loudly: a missing token before any work at all, a rejected
    token (401) on its first use.

    `progress` is an optional reporter that receives subscription/item-level
    events as they happen. Pass None for silent operation (tests, scripts).
    One sink instance per used provider spans the run so pacing and
    rate-limit cooldowns carry across subscriptions.
    """
    cfg = config or load_config()
    reports: list[SyncReport] = []
    blocked_destinations: set[str] = set()
    sinks = _SinkPool(cfg, override=sink)
    try:
        sinks.prepare(cfg.subscriptions)
        with connect(state_path) as conn:
            for sub in cfg.subscriptions:
                if sub.disabled:
                    _log.info("sync %s skipped (disabled)", sub.name)
                    continue
                try:
                    destination = _push_options(sub).destination
                except ConfigError:
                    destination = None
                if destination in blocked_destinations:
                    _log.info("sync %s deferred (%s rate limit)", sub.name, destination)
                    continue
                report = _sync_subscription(sub, cfg, conn, client, sinks, progress)
                reports.append(report)
                if destination is not None and report.rate_limited_host == destination:
                    blocked_destinations.add(destination)
    finally:
        sinks.close()
    return SyncTotal(reports=tuple(reports))


def _sync_subscription(
    sub: Subscription,
    cfg: Config,
    conn: sqlite3.Connection,
    client: httpx.Client | None,
    sinks: _SinkPool,
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
        push_options = _push_options(sub)
        sink = sinks.get(push_options.destination)
        with source_cls.from_config(cfg, client=client, subscription=sub) as source:
            report = _sync_with_source(sub, cfg, conn, source, sink, progress)
    except (ReadwiseAuthError, ShioriAuthError) as exc:
        # A rejected token dooms every subscription identically - record the
        # failure and propagate so the run fails once, loudly (the sync()
        # docstring's contract), instead of logging one 401 per item.
        _log.error("sync %s aborted: %s", sub.name, exc)
        update_subscription_state(conn, sub.name, "error", str(exc))
        raise
    except (FetchError, ExtractionError, ConfigError) as exc:
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
    sink: DestinationSink,
    progress: ProgressReporter | None,
) -> SyncReport:
    push_options = _push_options(sub)
    refs = list(source.discover(sub.url))
    if progress is not None:
        progress.subscription_discovered(sub.name, len(refs))

    new_items = 0
    skipped = 0
    errors = 0
    error_msgs: list[str] = []
    paywalled_items: list[PaywalledItem] = []
    rate_limited_host: str | None = None

    for index, ref in enumerate(refs):
        key = _destination_key(ref.url, push_options.destination)
        if _was_subscription_ingested(conn, ref.url):
            # Includes soft-deleted items - don't re-push what the user deleted.
            skipped += 1
            # Ack skips too: this lets a source heal external state that a
            # past crash left behind (item recorded but message never
            # marked/moved).
            _ack_item(source, ref)
            if progress is not None:
                progress.item_finished(sub.name, skipped=True)
            continue
        if progress is not None:
            progress.item_started(sub.name, ref.title or ref.url)
        try:
            submission = _submission_for_destination(source, ref, push_options.destination)
            if _already_ingested_as(conn, submission, key):
                # The destination identity URL (arXiv pdf vs abs, email permalink
                # vs mid:) was already ingested under a different discovery
                # URL - possibly deleted since. Skip; never resurrect.
                skipped += 1
                _ack_item(source, ref)
                if progress is not None:
                    progress.item_finished(sub.name, skipped=True)
                continue
            result = sink.push(
                submission,
                location=push_options.location,
                tags=push_options.tags,
            )
            record_item(
                conn,
                ItemRecord(
                    subscription_name=sub.name,
                    source_url=sub.url,
                    dedup_key=key,
                    canonical_url=submission.url,
                    title=submission.title or ref.title,
                    pub_date=_iso_or_none(submission.pub_date),
                    readwise_id=result.document_id,
                    readwise_url=result.reader_url,
                    submission_kind=result.kind,
                    destination=push_options.destination,
                ),
            )
            new_items += 1
            _ack_item(source, ref)
        except ItemSkipped as exc:
            _log.info("sync %s skipped url=%s: %s", sub.name, ref.url, exc)
            skipped += 1
            # Ack so mark-read policies still apply to skipped mail.
            _ack_item(source, ref)
        except ReadwiseAuthError, ShioriAuthError:
            # Not an item problem - the token is bad and every remaining
            # push is doomed identically. Propagate (fail once, loudly).
            raise
        except RateLimited as exc:
            # Must precede the FetchError clause (it's a subclass). Raised by
            # either side of the pipe - a source's fetch or the Readwise
            # push. Both mean the same thing: every remaining item in this
            # subscription is doomed too, so stop instead of burning a
            # request cycle per item. Nothing is lost: unpushed refs aren't
            # in the ledger, so the next sync picks them up. (The Readwise
            # sink also arms a fail-fast breaker, so later subscriptions
            # error instantly instead of stacking Retry-After sleeps.)
            remaining = len(refs) - index
            _log.warning("sync %s rate-limited url=%s err=%s", sub.name, ref.url, exc)
            errors += 1
            rate_limited_host = exc.host
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
        sub.name,
        new_items,
        skipped,
        errors,
        tuple(error_msgs),
        tuple(paywalled_items),
        rate_limited_host,
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
    sink: DestinationSink | None = None,
    *,
    max_new: int | None = 50,
    since_iso: str | None = None,
) -> BackfillReport:
    """Walk a subscription's archive backwards, pushing older posts to its destination.

    Unlike `sync`, which only looks at the newest page, this paginates as
    deep as the source supports. Stop conditions:
      - `max_new`: stop after N successful pushes (None = unlimited)
      - `since_iso`: stop when a post's pub_date is older than this date
      - Source's pagination exhausted (API returns empty page)
      - A provider (or Readwise itself) rate-limits us past the retries:
        the walk ends early with `stopped_reason='rate_limited'`; re-running
        backfill later resumes where it left off via the dedup ledger

    Already-ingested items (including soft-deleted ones) are skipped via
    the dedup ledger but don't stop the walk - we keep going past them
    to catch sparse holes in the existing data.

    Raises `BackfillUnsupported` if the source doesn't implement
    `discover_backwards`. Currently the Substack sources do: publications
    walk the archive by offset, the saves list walks the reader feed by
    save time. Email walks the whole mailbox newest-first.
    """
    cfg = config or load_config()
    try:
        source_cls = get_source(sub.source)
    except ValueError as exc:
        raise BackfillUnsupported(str(exc)) from exc

    sinks = _SinkPool(cfg, override=sink)
    push_options = _push_options(sub)
    active_sink = sinks.get(push_options.destination)
    try:
        with (
            connect(state_path) as conn,
            source_cls.from_config(cfg, client=client, subscription=sub) as source,
        ):
            if not hasattr(source, "discover_backwards"):
                raise BackfillUnsupported(
                    f"source {sub.source!r} doesn't support backfill yet "
                    "(Substack and email sources do)"
                )
            return _backfill_with_source(
                sub,
                conn,
                source,
                active_sink,
                max_new=max_new,
                since_iso=since_iso,
            )
    finally:
        sinks.close()


def _backfill_with_source(
    sub: Subscription,
    conn: sqlite3.Connection,
    source: Source,
    sink: DestinationSink,
    *,
    max_new: int | None,
    since_iso: str | None,
) -> BackfillReport:
    push_options = _push_options(sub)

    new_items = 0
    skipped = 0
    errors = 0
    pages_walked = 0
    error_msgs: list[str] = []
    stopped_reason = "exhausted"

    # discover_backwards yields one ref at a time but fetches pages of N
    # under the hood; we count pages by tracking when we cross page
    # boundaries via the ref-counter. Sources declare their page size
    # (substack archive 25, saved feed 20); 25 is the fallback.
    seen_refs = 0
    page_size = int(getattr(source, "backfill_page_size", 25))

    refs_iter = source.discover_backwards(sub.url)  # type: ignore[attr-defined]
    # RateLimited is caught around the whole walk (not per item) because it
    # can surface from three places: an item fetch, the Readwise push, or
    # the pagination request hiding inside the refs_iter generator. Either
    # way the response is the same - stop walking; a later backfill resumes
    # via the dedup ledger.
    try:
        for ref in refs_iter:
            seen_refs += 1
            if seen_refs % page_size == 1:
                pages_walked += 1

            # Date floor: refs come newest-first, so once we see a pub_date
            # before the floor we know everything after is older too.
            # Normalize to UTC before the string comparison - a non-UTC
            # offset would make lexicographic ordering non-chronological.
            if since_iso and ref.pub_date is not None:
                pub = ref.pub_date
                pub = pub if pub.tzinfo is not None else pub.replace(tzinfo=UTC)
                if pub.astimezone(UTC).isoformat() < since_iso:
                    stopped_reason = "since"
                    break

            key = _destination_key(ref.url, push_options.destination)
            if _was_subscription_ingested(conn, ref.url):
                skipped += 1
                _ack_item(source, ref)
                continue

            try:
                submission = _submission_for_destination(source, ref, push_options.destination)
                if _already_ingested_as(conn, submission, key):
                    skipped += 1
                    _ack_item(source, ref)
                    continue
                result = sink.push(
                    submission,
                    location=push_options.location,
                    tags=push_options.tags,
                )
                record_item(
                    conn,
                    ItemRecord(
                        subscription_name=sub.name,
                        source_url=sub.url,
                        dedup_key=key,
                        canonical_url=submission.url,
                        title=submission.title or ref.title,
                        pub_date=_iso_or_none(submission.pub_date),
                        readwise_id=result.document_id,
                        readwise_url=result.reader_url,
                        submission_kind=result.kind,
                        destination=push_options.destination,
                    ),
                )
                new_items += 1
                _ack_item(source, ref)
            except ItemSkipped as exc:
                _log.info("backfill %s skipped url=%s: %s", sub.name, ref.url, exc)
                skipped += 1
                _ack_item(source, ref)
            except Paywalled as exc:
                _log.info("backfill %s paywalled url=%s host=%s", sub.name, ref.url, exc.host)
            except ReadwiseAuthError, ShioriAuthError:
                raise  # bad token dooms the whole walk; fail once, loudly
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


def _iso_or_none(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else None
