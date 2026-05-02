"""pulpline CLI entry point."""

from __future__ import annotations

import re

import feedparser
import httpx
import typer

from pulpline import __version__, pipeline
from pulpline.config import (
    ConfigError,
    Subscription,
    add_subscription,
    load_config,
    remove_subscription,
    save_config,
)
from pulpline.models import ExtractionError, FetchError
from pulpline.state import connect, get_subscription_state
from pulpline.util.http import build_client

app = typer.Typer(
    help="Local-first content pipeline for e-readers.",
    no_args_is_help=True,
    add_completion=False,
)


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
) -> None:
    """Local-first content pipeline for e-readers."""


@app.command()
def add(
    url: str = typer.Argument(..., help="Feed URL (subscribe) or article URL (one-shot)."),
    once: bool = typer.Option(
        False,
        "--once",
        help="Force one-shot fetch even for feeds; do not persist to config.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Subscription name (default: derived from feed title).",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        help="Per-subscription output directory override.",
    ),
) -> None:
    """Add a subscription, or fetch a single article one-shot."""
    if once:
        _add_once(url)
        return

    _subscribe(url, name=name, output_dir=output_dir)


def _add_once(url: str) -> None:
    try:
        path = pipeline.add_once(url)
    except FetchError as exc:
        typer.echo(f"fetch failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ExtractionError as exc:
        typer.echo(f"extraction failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"wrote {path}")


def _subscribe(url: str, name: str | None, output_dir: str | None) -> None:
    config = load_config()

    with build_client() as client:
        try:
            feed_title, entry_count = _validate_feed(url, client)
        except Exception as exc:
            typer.echo(
                f"could not parse {url} as a feed: {exc}.\n"
                "  if this is a single article, try `pulp add <url> --once`.",
                err=True,
            )
            raise typer.Exit(code=1) from exc

    if entry_count == 0:
        typer.echo(
            f"feed at {url} has no entries; refusing to subscribe.\n"
            "  if this is a single article, try `pulp add <url> --once`.",
            err=True,
        )
        raise typer.Exit(code=1)

    sub_name = name or _slug_from_title(feed_title or url)
    sub = Subscription(name=sub_name, source="rss", url=url, output_dir=output_dir)

    try:
        new_config = add_subscription(config, sub)
    except ConfigError as exc:
        typer.echo(
            f"{exc}.\n  use `pulp add <url> --name <other>` to pick a different name.",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    save_config(new_config)
    typer.echo(f"subscribed to {sub_name!r} ({entry_count} items in feed)")


@app.command()
def sync() -> None:
    """Re-run all subscriptions, fetch new items only."""
    config = load_config()
    if not config.subscriptions:
        typer.echo("no subscriptions configured. add one with `pulp add <feed-url>`.")
        return

    typer.echo(f"syncing {len(config.subscriptions)} subscription(s)...")
    total = pipeline.sync(config=config)

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


@app.command("list")
def list_() -> None:
    """List configured subscriptions with their last sync status."""
    config = load_config()
    if not config.subscriptions:
        typer.echo("no subscriptions configured.")
        return

    rows = []
    with connect() as conn:
        for sub in config.subscriptions:
            state = get_subscription_state(conn, sub.name)
            if state is None or state.last_synced_at is None:
                last = "never"
            else:
                last = state.last_synced_at
            status = state.last_status if state else None
            rows.append((sub.name, sub.source, sub.url, last, status))

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    src_w = max(len("SOURCE"), max(len(r[1]) for r in rows))

    typer.echo(f"{'NAME':<{name_w}}  {'SOURCE':<{src_w}}  URL")
    for name, source, url, last, status in rows:
        suffix = f"  ({status})" if status else ""
        typer.echo(f"{name:<{name_w}}  {source:<{src_w}}  {url}")
        typer.echo(f"{'':<{name_w}}  {'':<{src_w}}  last sync: {last}{suffix}")


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


def main() -> None:
    """Console-script entry point for both `pulpline` and `pulp`."""
    app()


if __name__ == "__main__":
    main()
