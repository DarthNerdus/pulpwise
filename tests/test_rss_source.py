"""Tests for RSSSource - feed parsing, content fallback, two-phase shape."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from pulpline.models import ItemRef
from pulpline.sources.rss import RSSSource

ClientFactory = Callable[[dict[str, str]], httpx.Client]


def _read_feed_fixture() -> str:
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_text()


def test_discover_yields_one_ref_per_entry(mock_client_factory: ClientFactory) -> None:
    feed_url = "https://example.com/feed"
    client = mock_client_factory({feed_url: _read_feed_fixture()})

    with RSSSource(client=client) as source:
        refs = list(source.discover(feed_url))

    assert len(refs) == 2
    assert {r.url for r in refs} == {
        "https://example.com/full",
        "https://example.com/teaser",
    }
    titles = {r.title for r in refs}
    assert "Full Content Article" in titles


def test_fetch_uses_feed_supplied_content_when_substantial(
    mock_client_factory: ClientFactory,
) -> None:
    feed_url = "https://example.com/feed"
    client = mock_client_factory({feed_url: _read_feed_fixture()})

    with RSSSource(client=client) as source:
        list(source.discover(feed_url))
        article = source.fetch(ItemRef(url="https://example.com/full"))

    assert article.title == "Full Content Article"
    assert "first paragraph" in article.body_html
    assert article.source_url == feed_url
    assert article.publisher == "Stratechery by Ben Thompson"


def test_fetch_falls_back_to_url_when_feed_summary_only(
    mock_client_factory: ClientFactory, sample_html: str
) -> None:
    feed_url = "https://example.com/feed"
    article_url = "https://example.com/teaser"
    client = mock_client_factory({feed_url: _read_feed_fixture(), article_url: sample_html})

    with RSSSource(client=client) as source:
        list(source.discover(feed_url))
        article = source.fetch(ItemRef(url=article_url))

    # Body came from trafilatura on the article URL, not from the feed teaser.
    assert "the first substantive paragraph" in article.body_html.lower()
    # source_url is still the feed URL so dc:source records the feed.
    assert article.source_url == feed_url
