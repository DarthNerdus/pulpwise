"""pulpwise CLI entry point."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
import lxml.html
import typer

from pulpwise import __version__, pipeline
from pulpwise.auth import AuthError, load_cookies
from pulpwise.config import (
    Config,
    ConfigError,
    Subscription,
    add_subscription,
    load_config,
    remove_subscription,
    save_config,
)
from pulpwise.importers import parse_selection
from pulpwise.importers.opml import OpmlError, OpmlFeed, parse_opml
from pulpwise.importers.substack import (
    SubstackPublication,
    list_user_subscriptions,
)
from pulpwise.models import ExtractionError, FetchError, RateLimited
from pulpwise.sinks.readwise import SAVE_LOCATIONS, ReadwiseAuthError, ReadwiseSink
from pulpwise.sources import REGISTRY as _SOURCE_REGISTRY
from pulpwise.sources import pick_source_for_url
from pulpwise.sources.rss import RSSSource
from pulpwise.sources.url import URLSource
from pulpwise.state import connect, get_subscription_state, list_oneshots
from pulpwise.util.http import build_client

app = typer.Typer(
    help="Pipe feeds, newsletters, and one-shot URLs into Readwise Reader.",
    no_args_is_help=True,
    add_completion=False,
)

import_app = typer.Typer(help="Bulk-import subscriptions from external services.")
app.add_typer(import_app, name="import")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pulpwise {__version__}")
        raise typer.Exit


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Mirror logs to stderr at DEBUG level (logs always go to file).",
    ),
) -> None:
    """Pipe feeds, newsletters, and one-shot URLs into Readwise Reader."""
    from pulpwise.util.logging import setup_logging

    setup_logging(verbose=verbose)


@app.command()
def add(
    urls: list[str] = typer.Argument(
        ...,
        help="One or more URLs. Each is auto-classified independently.",
        metavar="URL [URL ...]",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Force one-shot push even for feeds; do not persist to config.",
    ),
    feed: bool = typer.Option(
        False,
        "--feed",
        help="Force subscribe path; skip feed-vs-article auto-detection.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Subscription name (only valid with a single URL).",
    ),
    location: str | None = typer.Option(
        None,
        "--location",
        "-l",
        help="Where saves land in Reader: new, later, archive, or feed. "
        "Default: feed. On subscribes this persists as options.location.",
    ),
) -> None:
    """Add one or more URLs.

    Each URL is auto-classified independently: feeds are subscribed, single
    articles are pushed to Readwise Reader one-shot. Use `--once` or `--feed`
    to override detection for every URL in the call. Errors on individual
    URLs do not abort the batch.

    Saves land in Reader's Feed section unless `--location` (here) or the
    subscription's `options.location` (in config.toml) says otherwise.
    """
    if once and feed:
        typer.echo("--once and --feed are mutually exclusive.", err=True)
        raise typer.Exit(code=2)
    if name is not None and len(urls) > 1:
        typer.echo("--name only applies when adding a single URL.", err=True)
        raise typer.Exit(code=2)
    if location is not None and location not in SAVE_LOCATIONS:
        valid = ", ".join(sorted(SAVE_LOCATIONS))
        typer.echo(f"--location must be one of: {valid}", err=True)
        raise typer.Exit(code=2)
    # Persisted on subscribes so every future sync routes the same way.
    options: dict[str, str | int] | None = None
    if location is not None:
        options = {"location": location}

    successes = 0
    failures = 0
    batch_sink = _BatchSink(load_config())
    try:
        for url in urls:
            if once:
                ok = _add_once(url, batch_sink, location=location)
            elif feed:
                ok = _subscribe(url, name=name, options=options)
            else:
                ok = _auto_dispatch(
                    url, name=name, sink=batch_sink, location=location, options=options
                )
            successes += int(ok)
            failures += int(not ok)
    finally:
        batch_sink.close()

    if len(urls) > 1:
        typer.echo(f"\nbatch: {successes} ok, {failures} failed")

    if failures:
        raise typer.Exit(code=1)


class _BatchSink:
    """One lazily-created ReadwiseSink shared by every one-shot in an `add` batch.

    A batch of N one-shot URLs must share a single sink so the 45/min save
    pacing and the 429 fail-fast breaker span the whole batch (a fresh sink
    per URL resets both). Creation is lazy so feed-subscribes never require
    a Readwise token. A failed creation (missing/rejected token) is
    remembered: every later one-shot in the batch fails fast with the same
    hint instead of re-attempting creation per URL, while feed-subscribes
    in the same batch still proceed.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._sink: ReadwiseSink | None = None
        self._auth_error: ReadwiseAuthError | None = None

    def get(self) -> ReadwiseSink:
        """Return the shared sink, creating it on first use.

        Raises `ReadwiseAuthError` - the remembered one after a failed
        creation - when no usable token is configured.
        """
        if self._auth_error is not None:
            raise self._auth_error
        if self._sink is None:
            try:
                self._sink = ReadwiseSink.from_config(self._cfg)
            except ReadwiseAuthError as exc:
                self._auth_error = exc
                raise
        return self._sink

    def close(self) -> None:
        if self._sink is not None:
            self._sink.close()


def _auto_dispatch(
    url: str,
    *,
    name: str | None,
    sink: _BatchSink,
    location: str | None = None,
    options: dict[str, str | int] | None = None,
) -> bool:
    """Pick subscribe vs one-shot for a URL based on its claiming source.

    For URLs claimed by a non-fallback source (arXiv API URL, etc.), the
    source decides via `is_subscribable`. For generic URLs that fall back
    to URLSource, we keep the existing RSS-feed autodiscovery path so blog
    front-pages still subscribe.
    """
    source_cls = pick_source_for_url(url)
    if source_cls is not URLSource:
        if source_cls.is_subscribable(url):
            return _subscribe(url, name=name, options=options)
        return _add_once(url, sink, location=location)

    # Generic URL: feed-or-article via feedparser + RSS autodiscovery.
    resolved = _resolve_feed_url(url)
    if resolved is not None:
        if resolved != url:
            typer.echo(f"discovered feed: {resolved}")
        return _subscribe(resolved, name=name, options=options)
    return _add_once(url, sink, location=location)


def _resolve_feed_url(url: str) -> str | None:
    """Resolve a URL to the feed pulpwise should subscribe to.

    Returns:
      - the input URL if it parses as a feed itself
      - an autodiscovered alternate-feed URL if the input is a front-page
        whose HTML advertises one via `<link rel="alternate"
        type="application/rss+xml">` (the W3C autodiscovery standard)
      - None if neither - caller falls through to one-shot.

    Autodiscovery is restricted to front-pages (empty/root path, no query)
    because article pages typically advertise their publication's feed too,
    and we don't want `pulpwise add <article-url>` to accidentally subscribe.
    """
    try:
        with build_client() as client:
            response = client.get(url)
            response.raise_for_status()
    # FetchError covers the retrying transport's RateLimited, which is
    # deliberately NOT an httpx error; without it a single 429-ing host
    # would abort the whole `add` batch with a traceback.
    except httpx.HTTPError, FetchError, ValueError:
        return None

    if feedparser.parse(response.content).entries:
        return url

    if not _is_front_page(url):
        return None

    alt = _alternate_feed_url(response.text, url)
    if alt is None:
        return None

    try:
        with build_client() as client:
            alt_response = client.get(alt)
            alt_response.raise_for_status()
    except httpx.HTTPError, FetchError, ValueError:
        return None

    if feedparser.parse(alt_response.content).entries:
        return alt
    return None


def _is_front_page(url: str) -> bool:
    """True if URL has empty/root path and no query - looks like a publication front-page."""
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


def _alternate_feed_url(html: str, base_url: str) -> str | None:
    """Find the first <link rel=alternate type=application/{rss,atom}+xml> href in HTML head."""
    try:
        tree = lxml.html.fromstring(html)
    except ValueError, lxml.etree.ParserError:
        return None

    for link in tree.iter("link"):
        rel = (link.get("rel") or "").lower()
        type_ = (link.get("type") or "").lower()
        href = link.get("href")
        if (
            "alternate" in rel
            and type_ in {"application/rss+xml", "application/atom+xml"}
            and isinstance(href, str)
            and href
        ):
            return urljoin(base_url, href)
    return None


def _add_once(url: str, sink: _BatchSink, location: str | None = None) -> bool:
    """Push a single URL to Readwise Reader. True on success; False (after logging) on failure."""
    try:
        result = pipeline.add_once(url, sink=sink.get(), location=location)
    except ReadwiseAuthError as exc:
        # Subclass of FetchError; must be caught first. Raised by shared-sink
        # creation (no token configured) or mid-push (rejected 401 token);
        # the message carries the setup hint (where to get a token, where to
        # put it).
        typer.echo(f"{url}: {exc}", err=True)
        return False
    except FetchError as exc:
        typer.echo(f"fetch failed for {url}: {exc}", err=True)
        return False
    except ExtractionError as exc:
        typer.echo(f"extraction failed for {url}: {exc}", err=True)
        return False
    except RuntimeError as exc:
        # pipeline.add_once raises RuntimeError when discovery yields more
        # (or fewer) than one item - e.g. `--once` on an arXiv API query URL.
        typer.echo(
            f"cannot push {url} as a one-shot: {exc}.\n"
            "  it looks like a feed/query URL - try `pulpwise add <url>` "
            "(without --once) to subscribe instead.",
            err=True,
        )
        return False
    if result.deduped:
        typer.echo(f"already pushed → {result.reader_url}")
        return True
    typer.echo(f"→ Readwise: {result.reader_url}")
    if result.already_in_readwise:
        typer.echo("  (Readwise already had this URL)")
    return True


def _subscribe(
    url: str,
    name: str | None,
    options: dict[str, str | int] | None = None,
) -> bool:
    """Subscribe to a URL. Returns True on success, False (with logged error) on failure."""
    config = load_config()
    source_cls = pick_source_for_url(url)
    source_name = source_cls.name

    if source_cls is RSSSource or source_cls is URLSource:
        # RSS path: feedparser-validate so we catch typos and 404s, and grab
        # the feed's <title> for the default subscription name.
        with build_client() as client:
            try:
                feed_title, entry_count = _validate_feed(url, client)
            except Exception as exc:
                typer.echo(
                    f"could not parse {url} as a feed: {exc}.\n"
                    "  if this is a single article, try `pulpwise add <url> --once`.",
                    err=True,
                )
                return False
        if entry_count == 0:
            typer.echo(
                f"feed at {url} has no entries; refusing to subscribe.\n"
                "  if this is a single article, try `pulpwise add <url> --once`.",
                err=True,
            )
            return False
        sub_name = name or _slug_from_title(feed_title or url)
        # The original URLSource auto-detect resolved this, so the canonical
        # subscription source name is RSS regardless of what pick_source_for_url returned.
        source_name = RSSSource.name
    else:
        # Source-specific URLs (arxiv, etc.): we trust the URL shape and let
        # the source class derive a sensible default name. Real validation
        # happens on first `pulpwise sync`.
        sub_name = name or source_cls.default_subscription_name(url)

    sub = Subscription(
        name=sub_name,
        source=source_name,
        url=url,
        options=dict(options or {}),
    )

    try:
        new_config = add_subscription(config, sub)
    except ConfigError as exc:
        typer.echo(
            f"{exc}.\n  use `pulpwise add <url> --name <other>` to pick a different name.",
            err=True,
        )
        return False

    save_config(new_config)
    typer.echo(f"subscribed to {sub_name!r} ({source_name})")
    return True


@app.command()
def sync() -> None:
    """Re-run all subscriptions, push new items to Readwise Reader."""
    config = load_config()
    if not config.subscriptions:
        typer.echo("no subscriptions configured. add one with `pulpwise add <feed-url>`.")
        return

    typer.echo(f"syncing {len(config.subscriptions)} subscription(s)...")
    try:
        with _CliProgress() as reporter:
            total = pipeline.sync(config=config, progress=reporter)
    except ReadwiseAuthError as exc:
        # Raised before any discovery work when no token is configured or
        # Readwise rejects it; the message carries the setup hint.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _print_sync_summary(total)

    if total.total_errors:
        raise typer.Exit(code=1)


def _print_sync_summary(total: pipeline.SyncTotal) -> None:
    """Compact, grouped post-sync summary.

    Skips silent rows (nothing new + no errors), groups paywalled items
    by host (so the "fix your cookies for X" hint is stated once per
    host, not once per item), and groups generic errors by subscription.
    """
    from rich.console import Console

    console = Console()
    n_subs = len(total.reports)

    # Per-sub one-liners, only for rows that produced something.
    for r in total.reports:
        bits: list[str] = []
        if r.new_items:
            bits.append(f"[green]+{r.new_items} new[/]")
        if r.errors:
            bits.append(f"[red]{r.errors} error(s)[/]")
        if r.paywalled:
            bits.append(f"[yellow]{len(r.paywalled)} paywalled[/]")
        if not bits:
            continue
        console.print(f"  {' · '.join(bits)} on [b]{r.name}[/]")

    # Totals line.
    pieces: list[str] = []
    if total.total_new:
        pieces.append(f"[green]+{total.total_new} new[/]")
    else:
        pieces.append("[dim]+0 new[/]")
    if total.total_paywalled:
        pieces.append(f"[yellow]{total.total_paywalled} paywalled[/]")
    if total.total_errors:
        pieces.append(f"[red]{total.total_errors} error(s)[/]")
    sub_word = "sub" if n_subs == 1 else "subs"
    pieces.append(f"[dim]across {n_subs} {sub_word}[/]")
    console.print(" · ".join(pieces))

    # Paywalled detail intentionally elided. The per-sub one-liner
    # ("4 paywalled on astral-codex-ten") names what + where; the user
    # already knows the fix is to export that host's cookies. Logs at
    # INFO level still record each paywalled URL for postmortems.

    # Generic errors grouped by subscription (already-narrow lists, so
    # fine to repeat the full message verbatim).
    for r in total.reports:
        if not r.error_messages:
            continue
        console.print(f"\n[red]✗[/] [b]{r.name}[/] - {len(r.error_messages)} error(s)")
        for msg in r.error_messages:
            console.print(f"     [dim]•[/] {msg}")


class _CliProgress:
    """rich-Progress-backed `ProgressReporter` for `pulpwise sync`.

    Each subscription gets a task with a progress bar; the current item's
    title shows next to the bar while it's being pushed. Rich auto-detects
    non-TTY stdout (cron logs) and falls back to compact text output.
    """

    def __init__(self) -> None:
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskID,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("[dim]{task.fields[item]}"),
            TimeElapsedColumn(),
            transient=False,
        )
        self._tasks: dict[str, TaskID] = {}

    def __enter__(self) -> _CliProgress:
        self._progress.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._progress.stop()

    def subscription_discovered(self, name: str, item_total: int) -> None:
        task_id = self._progress.add_task(name, total=max(item_total, 1), item="discovering...")
        self._tasks[name] = task_id

    def item_started(self, name: str, title: str) -> None:
        if name not in self._tasks:
            return
        short = title if len(title) <= 60 else title[:57] + "..."
        self._progress.update(self._tasks[name], item=short)

    def item_finished(self, name: str, *, skipped: bool = False) -> None:
        if name not in self._tasks:
            return
        self._progress.advance(self._tasks[name])
        if skipped:
            self._progress.update(self._tasks[name], item="(already have)")

    def subscription_finished(self, name: str, report: pipeline.SyncReport) -> None:
        if name not in self._tasks:
            return
        if report.errors:
            self._progress.update(self._tasks[name], item=f"[red]{report.errors} error(s)[/red]")
        else:
            self._progress.update(self._tasks[name], item="done")


@app.command("list")
def list_(
    limit: int = typer.Option(
        20, "--limit", help="How many recent one-shot pushes to show.", min=0
    ),
) -> None:
    """List subscriptions and recent one-shot pushes."""
    config = load_config()

    with connect() as conn:
        oneshots = list_oneshots(conn, limit=limit)

        sub_rows = []
        for sub in config.subscriptions:
            state = get_subscription_state(conn, sub.name)
            if state is None or state.last_synced_at is None:
                last = "never"
            else:
                last = state.last_synced_at
            status = state.last_status if state else None
            sub_rows.append((sub.name, sub.source, sub.url, last, status))

    if not sub_rows and not oneshots:
        typer.echo("nothing yet. add something with `pulpwise add <url>`.")
        return

    if sub_rows:
        name_w = max(len("NAME"), max(len(r[0]) for r in sub_rows))
        src_w = max(len("SOURCE"), max(len(r[1]) for r in sub_rows))
        typer.echo("SUBSCRIPTIONS")
        typer.echo(f"  {'NAME':<{name_w}}  {'SOURCE':<{src_w}}  URL")
        for name, source, url, last, status in sub_rows:
            suffix = f"  ({status})" if status else ""
            typer.echo(f"  {name:<{name_w}}  {source:<{src_w}}  {url}")
            typer.echo(f"  {'':<{name_w}}  {'':<{src_w}}  last sync: {last}{suffix}")

    if oneshots:
        if sub_rows:
            typer.echo("")
        suffix = f" (most recent {len(oneshots)})" if len(oneshots) >= limit else ""
        typer.echo(f"ONE-SHOTS{suffix}")
        for item in oneshots:
            title = item.title or "(untitled)"
            typer.echo(f"  {item.ingested_at[:10]}  {title}")
            typer.echo(f"              {item.readwise_url or item.canonical_url}")


@app.command()
def tui() -> None:
    """Launch the interactive Textual TUI (Library / Subscriptions / Sync / Stats)."""
    from pulpwise.tui.app import run

    run()


@app.command()
def backfill(
    name: str = typer.Argument(..., help="Subscription name to backfill."),
    posts: int = typer.Option(
        50,
        "--posts",
        "-n",
        help="Stop after this many newly-pushed posts. Pass 0 for unlimited "
        "(walks until the publication's archive is exhausted).",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Stop when a post's pub_date is older than YYYY-MM-DD. "
        "Combine with --posts as belt-and-suspenders bounds.",
    ),
) -> None:
    """Pull older posts of a subscription by paginating its archive.

    Regular `pulpwise sync` only looks at the newest page (~25 posts) per
    subscription. `backfill` keeps paginating backwards via the source's
    archive endpoint until one of the stop conditions fires.

    Already-pushed items are skipped (they don't count against --posts)
    but the walk continues past them, so partially-populated subscriptions
    get their gaps filled in too.

    Substack publications and email mailboxes support backfill (an email
    backfill walks the whole IMAP folder newest-first, past the regular
    `since_days` window). RSS feeds can't be paginated (the feed only
    serves what it serves); arXiv backfill is done by editing the query
    URL itself.
    """
    config = load_config()
    sub = config.find(name)
    if sub is None:
        typer.echo(f"no subscription named {name!r}.", err=True)
        raise typer.Exit(code=2)

    since_iso: str | None = None
    if since is not None:
        try:
            # Accept YYYY-MM-DD; expand to a full ISO timestamp so string
            # comparison with pub_date.isoformat() does the right thing.
            since_iso = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC).isoformat()
        except ValueError as exc:
            typer.echo(f"invalid --since date: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    max_new: int | None = posts if posts > 0 else None

    try:
        report = pipeline.backfill(sub, config=config, max_new=max_new, since_iso=since_iso)
    except pipeline.BackfillUnsupported as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ReadwiseAuthError as exc:
        # Subclass of FetchError; must be caught first.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except (FetchError, ExtractionError, ConfigError) as exc:
        # Pagination fetch failures and bad subscription options (junk
        # `location`/`tags`) should fail cleanly, not traceback.
        typer.echo(f"backfill {name!r} failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    base_count = report.skipped_already_ingested + report.new_items
    if report.new_items == 0 and report.stopped_reason == "exhausted":
        typer.echo(f"nothing more to fetch - full archive ({base_count} posts) already pushed")
        return
    bits = [f"backfilled {report.new_items} post(s)"]
    if report.skipped_already_ingested:
        bits.append(f"{report.skipped_already_ingested} already in ledger")
    if report.errors:
        bits.append(f"{report.errors} error(s)")
    bits.append(f"pages walked: {report.pages_walked}")
    if report.stopped_reason == "max_new":
        bits.append("stopped at --posts limit (archive has more; raise --posts to get them)")
    elif report.stopped_reason == "exhausted":
        bits.append(f"full archive now pushed ({base_count} posts)")
    elif report.stopped_reason == "since":
        bits.append("stopped at --since date")
    elif report.stopped_reason == "rate_limited":
        bits.append("stopped early: rate limited (wait a bit, then re-run to continue)")
    typer.echo("; ".join(bits))


@app.command()
def remove(
    name: str = typer.Argument(..., help="Subscription name to remove."),
) -> None:
    """Remove a subscription from config (does not touch documents already in Readwise)."""
    config = load_config()
    try:
        new_config = remove_subscription(config, name)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    save_config(new_config)
    typer.echo(f"removed subscription {name!r}")


def _source_name_for_url(url: str) -> str:
    """Pick a source name for a subscription URL by host.

    Sources that override `matches_url` claim their domain (e.g. arXiv);
    URLSource and RSSSource don't override since they're the generic
    fallbacks. Anything that no source claims goes to RSS.
    """
    for src_name, cls in _SOURCE_REGISTRY.items():
        if src_name in (URLSource.name, RSSSource.name):
            continue
        if cls.matches_url(url):
            return src_name
    return RSSSource.name


def _validate_feed(url: str, client: httpx.Client) -> tuple[str | None, int]:
    """Fetch + parse `url` as a feed; return (feed_title, entry_count).

    Raises on HTTP failure or unparseable content.
    """
    response = client.get(url)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise ValueError(str(feed.bozo_exception))
    title = feed.feed.get("title")
    return title, len(feed.entries)


_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug_from_title(text: str) -> str:
    """Lowercased alphanumeric+hyphen slug suitable as a subscription name."""
    s = _SLUG_NON_ALNUM.sub("-", text.lower()).strip("-")
    return s or "feed"


@import_app.command("opml")
def import_opml(
    file: Path = typer.Argument(
        ..., help="Path to an OPML export from your feed reader / Substack / etc."
    ),
) -> None:
    """Import feeds from an OPML file.

    Works with exports from Reeder, NetNewsWire, Inoreader, Feedly, Substack,
    and any other reader that emits OPML 2.0. Each feed becomes an `rss`
    subscription; existing names are skipped.
    """
    try:
        feeds = parse_opml(file)
    except OpmlError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    selected = _prompt_select_opml(feeds)
    if not selected:
        typer.echo("nothing selected; nothing imported.")
        return

    config = load_config()
    added = 0
    skipped = 0
    for feed in selected:
        sub = Subscription(
            name=_slug_from_title(feed.title),
            source="rss",
            url=feed.feed_url,
        )
        try:
            config = add_subscription(config, sub)
            added += 1
        except ConfigError:
            skipped += 1

    save_config(config)
    msg = f"imported {added} subscription(s)"
    if skipped:
        msg += f"; {skipped} skipped (name already exists)"
    typer.echo(msg)


def _prompt_select_opml(feeds: list[OpmlFeed]) -> list[OpmlFeed]:
    typer.echo(f"\nfound {len(feeds)} feed(s):")
    for i, feed in enumerate(feeds, start=1):
        folder = f"  [{feed.folder}]" if feed.folder else ""
        typer.echo(f"  [{i:>2}] {feed.title}{folder}\n       {feed.feed_url}")

    raw = typer.prompt(
        "\nwhich to import? (e.g. '1,3,5-7', 'all', or 'none')",
        default="all",
        show_default=True,
    )
    try:
        indices = parse_selection(raw, len(feeds))
    except ValueError as exc:
        typer.echo(f"invalid selection: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    return [feeds[i - 1] for i in sorted(indices)]


@import_app.command("substack")
def import_substack(
    username: str | None = typer.Argument(
        None,
        help="Your Substack handle. Defaults to [auth.substack].username if set.",
    ),
    cookies: Path | None = typer.Option(
        None,
        "--cookies",
        help="Path to cookies.json. Defaults to [auth.substack].cookies_path if set.",
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        "-y",
        help="Skip the picker; auto-add every Substack you follow that isn't "
        "already a pulpwise subscription. For cron-driven 'pick up new follows' runs.",
    ),
) -> None:
    """Import your Substack subscriptions.

    Reads your session cookies, fetches the publications you follow, and writes
    them into config.toml as `source = "substack"` subscriptions. Interactive
    by default; pass `--auto` to skip the picker and add every new follow.

    First run: pass `<username> --cookies <path>` (everything gets persisted to
    [auth.substack]). Later runs (or cron): just `pulpwise import substack --auto`.
    """
    config = load_config()
    auth = config.auth_for("substack")

    cfg_username = auth.get("username")
    resolved_username = username or (cfg_username if isinstance(cfg_username, str) else None)
    if not resolved_username:
        typer.echo(
            "no username given and [auth.substack].username not set in config.",
            err=True,
        )
        raise typer.Exit(code=2)

    cfg_cookies_path = auth.get("cookies_path")
    cookies_path = cookies or (
        Path(cfg_cookies_path).expanduser() if isinstance(cfg_cookies_path, str) else None
    )
    if cookies_path is None:
        typer.echo(
            "no cookies path given and [auth.substack].cookies_path not set in config.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        cookie_dict = load_cookies(cookies_path)
    except AuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    with build_client(scope="substack") as client:
        try:
            pubs = list_user_subscriptions(resolved_username, cookie_dict, client)
        except (httpx.HTTPError, RateLimited) as exc:
            typer.echo(
                f"failed to fetch subscriptions for {resolved_username!r}: {exc}",
                err=True,
            )
            raise typer.Exit(code=1) from exc

    if not pubs:
        typer.echo(
            f"no subscriptions found for {resolved_username!r}. is the handle correct, "
            "and were the cookies exported from a logged-in session?",
            err=True,
        )
        raise typer.Exit(code=1)

    # Drop pubs already present (matched by URL or hostname so we don't duplicate
    # whatever the user added manually as `rss` or otherwise).
    existing_hosts = {urlsplit(s.url).hostname for s in config.subscriptions}
    new_pubs = [p for p in pubs if urlsplit(p.url).hostname not in existing_hosts]

    if auto:
        chosen = new_pubs
        if not chosen:
            typer.echo(f"all {len(pubs)} substacks already in config; nothing to add.")
            _persist_substack_auth(config, resolved_username, cookies_path)
            return
    else:
        if not new_pubs:
            typer.echo(f"all {len(pubs)} substacks already in config; nothing to add.")
            _persist_substack_auth(config, resolved_username, cookies_path)
            return
        chosen = _prompt_select(new_pubs)
        if not chosen:
            typer.echo("nothing selected; nothing imported.")
            _persist_substack_auth(config, resolved_username, cookies_path)
            return

    # Merge into the existing [auth.substack] table - replacing it wholesale
    # would silently delete keys other consumers own (extra_cookies_paths).
    new_auth = dict(config.auth)
    new_auth["substack"] = {
        **config.auth_for("substack"),
        "cookies_path": str(cookies_path),
        "username": resolved_username,
    }
    new_config = replace(config, auth=new_auth)

    added = 0
    skipped = 0
    for pub in chosen:
        sub_name = _slug_from_title(pub.name)
        sub = Subscription(name=sub_name, source="substack", url=pub.url)
        try:
            new_config = add_subscription(new_config, sub)
            added += 1
        except ConfigError:
            skipped += 1  # name collision with existing sub

    save_config(new_config)
    msg = f"imported {added} subscription(s)"
    if skipped:
        msg += f"; {skipped} skipped (name already exists)"
    typer.echo(msg)


def _persist_substack_auth(config: Config, username: str, cookies_path: Path) -> None:
    """Update [auth.substack] in config so future runs can be zero-arg."""
    auth = config.auth_for("substack")
    if auth.get("username") == username and auth.get("cookies_path") == str(cookies_path):
        return
    # Merge, don't replace: preserve keys other consumers own
    # (extra_cookies_paths).
    new_auth = dict(config.auth)
    new_auth["substack"] = {
        **auth,
        "cookies_path": str(cookies_path),
        "username": username,
    }
    save_config(replace(config, auth=new_auth))


def _prompt_select(pubs: list[SubstackPublication]) -> list[SubstackPublication]:
    typer.echo(f"\nfound {len(pubs)} subscription(s):")
    for i, pub in enumerate(pubs, start=1):
        flag = " [paid]" if pub.paid else ""
        typer.echo(f"  [{i:>2}] {pub.name}{flag}  -  {pub.url}")

    raw = typer.prompt(
        "\nwhich to import? (e.g. '1,3,5-7', 'all', or 'none')",
        default="all",
        show_default=True,
    )
    try:
        indices = parse_selection(raw, len(pubs))
    except ValueError as exc:
        typer.echo(f"invalid selection: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    return [pubs[i - 1] for i in sorted(indices)]


def main() -> None:
    """Console-script entry point for both `pulpwise` and `pw`."""
    app()


if __name__ == "__main__":
    main()
