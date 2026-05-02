"""pulpline CLI entry point."""

from __future__ import annotations

import typer

from pulpline import __version__, pipeline
from pulpline.models import ExtractionError, FetchError

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
) -> None:
    """Add a subscription, or fetch a single article one-shot."""
    if not once:
        typer.echo(
            "subscriptions are not yet implemented; use `pulp add <url> --once` for now. "
            "See SPEC.md Phase 2.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        path = pipeline.add_once(url)
    except FetchError as exc:
        typer.echo(f"fetch failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ExtractionError as exc:
        typer.echo(f"extraction failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"wrote {path}")


@app.command()
def sync() -> None:
    """Re-run all subscriptions, fetch new items only."""
    typer.echo("sync: not yet implemented. See SPEC.md Phase 2.", err=True)
    raise typer.Exit(code=1)


@app.command("list")
def list_() -> None:
    """List configured subscriptions."""
    typer.echo("list: not yet implemented. See SPEC.md Phase 2.", err=True)
    raise typer.Exit(code=1)


@app.command()
def remove(
    name: str = typer.Argument(..., help="Subscription name to remove."),
) -> None:
    """Remove a subscription from config (does not delete already-written files)."""
    typer.echo(
        f"remove: not yet implemented (name={name!r}). See SPEC.md Phase 2.",
        err=True,
    )
    raise typer.Exit(code=1)


def main() -> None:
    """Console-script entry point for both `pulpline` and `pulp`."""
    app()


if __name__ == "__main__":
    main()
