"""Integration tests for pipeline.sync (real RSSSource, real EpubRenderer, mocked HTTP)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from pulpline import pipeline
from pulpline.config import Config, Subscription
from pulpline.state import connect, is_seen

ClientFactory = Callable[[dict[str, str]], httpx.Client]


def _feed() -> str:
    return (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_text()


def _config(feed_url: str, output_dir: Path) -> Config:
    return Config(
        subscriptions=(
            Subscription(name="test", source="rss", url=feed_url, output_dir=str(output_dir)),
        )
    )


def test_sync_writes_epubs_for_new_items(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    feed_url = "https://example.com/feed"
    out = tmp_path / "out"
    client = mock_client_factory(
        {
            feed_url: _feed(),
            "https://example.com/teaser": sample_html,  # fallback fetch for summary-only
        }
    )

    total = pipeline.sync(config=_config(feed_url, out), client=client)

    assert len(total.reports) == 1
    report = total.reports[0]
    assert report.name == "test"
    assert report.new_items == 2
    assert report.skipped == 0
    assert report.errors == 0

    written = sorted(p.name for p in out.iterdir())
    assert len(written) == 2
    assert all(name.endswith(".epub") for name in written)


def test_sync_dedups_on_second_run(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    feed_url = "https://example.com/feed"
    out = tmp_path / "out"
    client = mock_client_factory(
        {
            feed_url: _feed(),
            "https://example.com/teaser": sample_html,
        }
    )

    pipeline.sync(config=_config(feed_url, out), client=client)
    # Second run: all items now seen, none should be re-fetched / re-written.
    second = pipeline.sync(config=_config(feed_url, out), client=client)

    assert second.total_new == 0
    assert second.total_skipped == 2
    assert second.total_errors == 0


def test_sync_records_items_in_dedup_ledger(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    feed_url = "https://example.com/feed"
    client = mock_client_factory(
        {
            feed_url: _feed(),
            "https://example.com/teaser": sample_html,
        }
    )

    pipeline.sync(config=_config(feed_url, tmp_path / "out"), client=client)

    from pulpline.util.dedup import dedup_key

    with connect() as conn:
        assert is_seen(conn, dedup_key("https://example.com/full")) is not None
        assert is_seen(conn, dedup_key("https://example.com/teaser")) is not None


def test_sync_feed_level_error_does_not_abort_run(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    """A failing feed should be reported as error and skip-and-continue to the next."""
    bad_feed = "https://broken.example.com/feed"
    good_feed = "https://example.com/feed"
    client = mock_client_factory(
        {
            good_feed: _feed(),
            "https://example.com/teaser": sample_html,
        }
    )

    out = tmp_path / "out"
    config = Config(
        subscriptions=(
            Subscription(name="bad", source="rss", url=bad_feed),
            Subscription(name="good", source="rss", url=good_feed, output_dir=str(out)),
        )
    )
    total = pipeline.sync(config=config, client=client)

    by_name = {r.name: r for r in total.reports}
    assert by_name["bad"].errors == 1
    assert by_name["good"].new_items == 2
    assert by_name["good"].errors == 0


def test_add_once_is_idempotent_via_dedup(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    url = "https://example.com/article"
    client = mock_client_factory({url: sample_html})

    first = pipeline.add_once(url, output_dir=tmp_path, client=client)
    second = pipeline.add_once(url, output_dir=tmp_path, client=client)

    assert first == second
    # The article was written exactly once.
    assert len(list(tmp_path.glob("*.epub"))) == 1
