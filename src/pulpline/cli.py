"""pulpline CLI entry point."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
import lxml.html
import typer

from pulpline import __version__, pipeline
from pulpline.auth import AuthError, load_cookies
from pulpline.config import (
    Config,
    ConfigError,
    Subscription,
    add_subscription,
    load_config,
    remove_subscription,
    save_config,
)
from pulpline.importers import parse_selection
from pulpline.importers.opml import OpmlError, OpmlFeed, parse_opml
from pulpline.importers.substack import (
    SubstackPublication,
    list_user_subscriptions,
)
from pulpline.models import ExtractionError, FetchError
from pulpline.sources import REGISTRY as _SOURCE_REGISTRY
from pulpline.sources import pick_source_for_url
from pulpline.sources.rss import RSSSource
from pulpline.sources.url import URLSource
from pulpline.state import connect, get_subscription_state, list_oneshots
from pulpline.util.http import build_client

app = typer.Typer(
    help="Local-first content pipeline for e-readers.",
    no_args_is_help=True,
    add_completion=False,
)

import_app = typer.Typer(help="Bulk-import subscriptions from external services.")
app.add_typer(import_app, name="import")

mangadex_app = typer.Typer(
    help="MangaDex-specific subscription commands (chapter window, language)."
)
app.add_typer(mangadex_app, name="mangadex")

search_app = typer.Typer(help="Interactive search across libraries (Anna's Archive, ...).")
app.add_typer(search_app, name="search")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pulpline {__version__}")
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
    """Local-first content pipeline for e-readers."""
    from pulpline.util.logging import setup_logging

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
        help="Force one-shot fetch even for feeds; do not persist to config.",
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
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        help="Per-subscription output directory override.",
    ),
) -> None:
    """Add one or more URLs.

    Each URL is auto-classified independently: feeds are subscribed, single
    articles are one-shot. Use `--once` or `--feed` to override detection for
    every URL in the call. Errors on individual URLs do not abort the batch.

    For source-specific options (MangaDex language, chapter window, etc.),
    use `pulp mangadex add` (or the corresponding source subcommand).
    """
    if once and feed:
        typer.echo("--once and --feed are mutually exclusive.", err=True)
        raise typer.Exit(code=2)
    if name is not None and len(urls) > 1:
        typer.echo("--name only applies when adding a single URL.", err=True)
        raise typer.Exit(code=2)

    successes = 0
    failures = 0
    for url in urls:
        if once:
            ok = _add_once(url)
        elif feed:
            ok = _subscribe(url, name=name, output_dir=output_dir)
        else:
            ok = _auto_dispatch(url, name=name, output_dir=output_dir)
        successes += int(ok)
        failures += int(not ok)

    if len(urls) > 1:
        typer.echo(f"\nbatch: {successes} ok, {failures} failed")

    if failures:
        raise typer.Exit(code=1)


def _auto_dispatch(
    url: str,
    *,
    name: str | None,
    output_dir: str | None,
    options: dict[str, str | int] | None = None,
) -> bool:
    """Pick subscribe vs one-shot for a URL based on its claiming source.

    For URLs claimed by a non-fallback source (arXiv API URL, MangaDex title
    page), the source decides via `is_subscribable`. For generic URLs that
    fall back to URLSource, we keep the existing RSS-feed autodiscovery
    path so blog front-pages still subscribe.
    """
    source_cls = pick_source_for_url(url)
    if source_cls is not URLSource:
        if source_cls.is_subscribable(url):
            return _subscribe(url, name=name, output_dir=output_dir, options=options)
        return _add_once(url)

    # Generic URL: feed-or-article via feedparser + RSS autodiscovery.
    resolved = _resolve_feed_url(url)
    if resolved is not None:
        if resolved != url:
            typer.echo(f"discovered feed: {resolved}")
        return _subscribe(resolved, name=name, output_dir=output_dir, options=options)
    return _add_once(url)


def _resolve_feed_url(url: str) -> str | None:
    """Resolve a URL to the feed pulpline should subscribe to.

    Returns:
      - the input URL if it parses as a feed itself
      - an autodiscovered alternate-feed URL if the input is a front-page
        whose HTML advertises one via `<link rel="alternate"
        type="application/rss+xml">` (the W3C autodiscovery standard)
      - None if neither - caller falls through to one-shot.

    Autodiscovery is restricted to front-pages (empty/root path, no query)
    because article pages typically advertise their publication's feed too,
    and we don't want `pulp add <article-url>` to accidentally subscribe.
    """
    try:
        with build_client() as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPError, ValueError:
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
    except httpx.HTTPError, ValueError:
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


def _add_once(url: str) -> bool:
    """One-shot ingest a single URL. True on success; False (after logging) on failure."""
    try:
        path = pipeline.add_once(url)
    except FetchError as exc:
        typer.echo(f"fetch failed for {url}: {exc}", err=True)
        return False
    except ExtractionError as exc:
        typer.echo(f"extraction failed for {url}: {exc}", err=True)
        return False
    typer.echo(f"wrote {path}")
    return True


def _subscribe(
    url: str,
    name: str | None,
    output_dir: str | None,
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
                    "  if this is a single article, try `pulp add <url> --once`.",
                    err=True,
                )
                return False
        if entry_count == 0:
            typer.echo(
                f"feed at {url} has no entries; refusing to subscribe.\n"
                "  if this is a single article, try `pulp add <url> --once`.",
                err=True,
            )
            return False
        sub_name = name or _slug_from_title(feed_title or url)
        # The original URLSource auto-detect resolved this, so the canonical
        # subscription source name is RSS regardless of what pick_source_for_url returned.
        source_name = RSSSource.name
    else:
        # Source-specific URLs (arxiv, mangadex, etc.): we trust the URL
        # shape and let the source class derive a sensible default name.
        # Real validation happens on first `pulp sync`.
        sub_name = name or source_cls.default_subscription_name(url)

    sub = Subscription(
        name=sub_name,
        source=source_name,
        url=url,
        output_dir=output_dir,
        options=dict(options or {}),
    )

    try:
        new_config = add_subscription(config, sub)
    except ConfigError as exc:
        typer.echo(
            f"{exc}.\n  use `pulp add <url> --name <other>` to pick a different name.",
            err=True,
        )
        return False

    save_config(new_config)
    typer.echo(f"subscribed to {sub_name!r} ({source_name})")
    return True


@app.command()
def sync() -> None:
    """Re-run all subscriptions, fetch new items only."""
    config = load_config()
    if not config.subscriptions:
        typer.echo("no subscriptions configured. add one with `pulp add <feed-url>`.")
        return

    typer.echo(f"syncing {len(config.subscriptions)} subscription(s)...")
    with _CliProgress() as reporter:
        total = pipeline.sync(config=config, progress=reporter)

    for report in total.reports:
        if report.errors == 0:
            typer.echo(f"  [{report.name}] {report.new_items} new, {report.skipped} skipped")
        else:
            typer.echo(
                f"  [{report.name}] {report.new_items} new, "
                f"{report.skipped} skipped, {report.errors} error(s)"
            )
            for msg in report.error_messages:
                typer.echo(f"    {msg}")

    typer.echo(
        f"total: {total.total_new} new, {total.total_skipped} skipped, "
        f"{total.total_errors} error(s)"
    )

    if total.total_errors:
        raise typer.Exit(code=1)


class _CliProgress:
    """rich-Progress-backed `ProgressReporter` for `pulp sync`.

    Each subscription gets a task with a progress bar; the current item's
    title shows next to the bar while it's being fetched. Rich auto-detects
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
        20, "--limit", help="How many recent one-shot ingestions to show.", min=0
    ),
) -> None:
    """List subscriptions and recent one-shot ingestions."""
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
        typer.echo("nothing yet. add something with `pulp add <url>`.")
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
            typer.echo(f"              {item.canonical_url}")


@app.command()
def tui() -> None:
    """Launch the interactive Textual TUI (Library + Stats views)."""
    from pulpline.tui.app import run

    run()


@app.command()
def migrate(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview moves without touching disk."),
) -> None:
    """Reorganize existing items into per-subscription subfolders.

    Older pulpline versions wrote every item directly under `paths.output_dir`.
    This command walks the items table and moves each file to its new
    subscription-named subfolder (or `oneshots/` for one-shots). Run once
    after upgrading; idempotent and safe to re-run.
    """
    report = pipeline.migrate(dry_run=dry_run)
    label = "would move" if dry_run else "moved"
    typer.echo(f"{label}: {report.moved} file(s)")
    if report.skipped_already_correct:
        typer.echo(f"  already in place: {report.skipped_already_correct}")
    if report.skipped_collision:
        typer.echo(f"  skipped (target already exists): {report.skipped_collision}")
    if report.missing_on_disk:
        typer.echo(f"  files missing on disk: {report.missing_on_disk}")
    if report.orphaned:
        typer.echo(f"  orphaned (subscription removed): {report.orphaned}")


_MANGADEX_DEFAULT_MAX_CHAPTERS = 25


@mangadex_app.command("add")
def mangadex_add(
    url: str = typer.Argument(..., help="MangaDex URL: title page or chapter."),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Subscription name (default: derived from URL slug)."
    ),
    output_dir: str | None = typer.Option(
        None, "--output-dir", help="Per-subscription output directory override."
    ),
    language: str | None = typer.Option(
        None, "--language", "-l", help="Chapter translation language code (e.g. 'en', 'ru', 'ja')."
    ),
    max_chapters: int | None = typer.Option(
        None, "--max-chapters", help="Cap chapters tracked. 0 = unlimited (paginate to exhaustion)."
    ),
    from_start: bool = typer.Option(
        False,
        "--from-start",
        help="Read from chapter 1 forward (sets order=asc; defaults max-chapters=10).",
    ),
) -> None:
    """Subscribe to a MangaDex title with the read-from-start / language knobs.

    `pulp add <url>` works too and routes by URL, but uses defaults for these
    options. This subcommand exists when you want to set them at subscribe time.
    """
    options: dict[str, str | int] = {}
    if language:
        options["language"] = language
    if from_start:
        options["order"] = "asc"
        if max_chapters is None:
            max_chapters = 10
    if max_chapters is not None:
        options["max_chapters"] = max_chapters

    ok = _subscribe(url, name=name, output_dir=output_dir, options=options)
    if not ok:
        raise typer.Exit(code=1)


@mangadex_app.command("extend")
def mangadex_extend(
    name: str = typer.Argument(..., help="Subscription name."),
    by: int | None = typer.Option(None, "--by", help="Increment max_chapters by this many."),
    to: int | None = typer.Option(None, "--to", help="Set max_chapters to this absolute value."),
    all_: bool = typer.Option(
        False, "--all", help="Remove the cap entirely (paginate to exhaustion)."
    ),
) -> None:
    """Bump a MangaDex subscription's chapter window to fetch more on the next sync.

    Useful for read-from-start workflows: subscribe with `pulp mangadex add ...
    --from-start --max-chapters 10`, read those, then `pulp mangadex extend
    <name> --by 10` to grab the next batch.
    """
    flags_set = sum(x is not None for x in (by, to)) + int(all_)
    if flags_set != 1:
        typer.echo("provide exactly one of --by, --to, --all.", err=True)
        raise typer.Exit(code=2)

    config = load_config()
    sub = config.find(name)
    if sub is None:
        typer.echo(f"no subscription named {name!r}.", err=True)
        raise typer.Exit(code=1)

    current_raw = sub.option("max_chapters")
    current = current_raw if isinstance(current_raw, int) else _MANGADEX_DEFAULT_MAX_CHAPTERS
    if all_:
        new_value = 0
    elif to is not None:
        new_value = to
    else:
        assert by is not None
        new_value = current + by

    if new_value < 0:
        typer.echo("max_chapters cannot be negative.", err=True)
        raise typer.Exit(code=2)

    new_options = dict(sub.options)
    new_options["max_chapters"] = new_value
    new_sub = replace(sub, options=new_options)
    new_subs = tuple(new_sub if s.name == name else s for s in config.subscriptions)
    save_config(replace(config, subscriptions=new_subs))

    label = "unlimited" if new_value == 0 else str(new_value)
    typer.echo(f"{name}: max_chapters {current} -> {label}")


@search_app.command("anna")
def search_anna(
    query: str = typer.Argument(..., help="Search terms (title, author, ISBN, ...)."),
    content: str | None = typer.Option(
        None,
        "--content",
        "-c",
        help="Result type: book (default), paper, comic, magazine.",
    ),
    extension: str | None = typer.Option(
        None, "--ext", help="Filter by file extension (epub, pdf, mobi, ...)."
    ),
    language: str | None = typer.Option(
        None, "--lang", "-l", help="Filter by language code (en, ru, ja, ...)."
    ),
    limit: int = typer.Option(20, "--limit", help="Max results to show.", min=1, max=100),
) -> None:
    """Search Anna's Archive, pick a result, download it via the donation API.

    Search uses HTML scraping with a browser User-Agent (Anna does not ship
    a search API). Download uses the legitimate `fast_download.json`
    endpoint and requires `[auth.annas].api_key` (set after donating).
    """
    from pulpline.searchers.annas import AnnaSearcher

    config = load_config()
    with AnnaSearcher() as searcher:
        try:
            results = list(
                searcher.search(
                    query,
                    content=content,
                    extension=extension,
                    language=language,
                    limit=limit,
                )
            )
        except FetchError as exc:
            typer.echo(f"search failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    if not results:
        typer.echo("no results.")
        return

    from pulpline.searchers.picker import pick_results

    picked_indices = pick_results(results, query=query)
    if not picked_indices:
        typer.echo("nothing picked; nothing downloaded.")
        return

    from pulpline.sources.annas import AnnaSource

    failures = 0
    for idx in picked_indices:
        pick = results[idx]
        typer.echo(f"downloading: {pick.title}")
        try:
            path = pipeline.add_once(pick.target_url, config=config)
        except (FetchError, ExtractionError) as exc:
            typer.echo(f"  failed: {exc}", err=True)
            failures += 1
            continue
        typer.echo(f"  wrote {path}")
        quota = AnnaSource.LAST_QUOTA_INFO
        if quota is not None:
            typer.echo(
                f"  quota: {quota.downloads_left}/{quota.downloads_per_day} "
                f"fast downloads remaining today"
            )

    if failures:
        raise typer.Exit(code=1)


@app.command()
def remove(
    name: str = typer.Argument(..., help="Subscription name to remove."),
) -> None:
    """Remove a subscription from config (does not delete already-written files)."""
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
    username: str = typer.Argument(..., help="Your Substack handle (e.g. 'egorkonovalov')."),
    cookies: Path = typer.Option(
        ...,
        "--cookies",
        help="Path to a cookies.json from your logged-in browser session.",
    ),
) -> None:
    """Import all of your Substack subscriptions in one shot.

    Reads your session cookies, fetches the publications you follow, presents
    a numeric selection prompt, and writes the chosen ones into config.toml
    as `source = "substack"` subscriptions. Future `pulp sync` runs will use
    those cookies to access paid content.
    """
    try:
        cookie_dict = load_cookies(cookies)
    except AuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    with build_client() as client:
        try:
            pubs = list_user_subscriptions(username, cookie_dict, client)
        except httpx.HTTPError as exc:
            typer.echo(f"failed to fetch subscriptions for {username!r}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    if not pubs:
        typer.echo(
            f"no subscriptions found for {username!r}. is the handle correct, "
            "and were the cookies exported from a logged-in session?",
            err=True,
        )
        raise typer.Exit(code=1)

    chosen = _prompt_select(pubs)
    if not chosen:
        typer.echo("nothing selected; nothing imported.")
        return

    config = load_config()
    new_auth = dict(config.auth)
    new_auth["substack"] = {"cookies_path": str(cookies)}
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


# Annotation reference so `Config` is not flagged unused after later refactors.
_ = Config


def main() -> None:
    """Console-script entry point for both `pulpline` and `pulp`."""
    app()


if __name__ == "__main__":
    main()
