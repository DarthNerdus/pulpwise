"""Tests for URLSource (httpx + trafilatura)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from pulpline.models import ExtractionError, FetchError, ItemRef
from pulpline.sources.url import URLSource

ClientFactory = Callable[[dict[str, str]], httpx.Client]


def test_discover_yields_single_ref() -> None:
    with URLSource(client=httpx.Client()) as source:
        refs = list(source.discover("https://example.com/article"))
    assert refs == [ItemRef(url="https://example.com/article")]


def test_fetch_extracts_article_metadata(
    mock_client_factory: ClientFactory, sample_html: str
) -> None:
    url = "https://example.com/article"
    client = mock_client_factory({url: sample_html})
    with URLSource(client=client) as source:
        article = source.fetch(ItemRef(url=url))

    assert article.title == "The End of the Beginning"
    assert article.author == "Ben Thompson"
    assert article.publisher in {"Stratechery", "example.com"}
    assert article.canonical_url
    assert article.source_url == "direct"
    assert "<p>" in article.body_html.lower() or "<p " in article.body_html.lower()
    assert article.pub_date is not None
    assert article.pub_date.year == 2026


def test_fetch_raises_fetch_error_on_http_failure(mock_client_factory: ClientFactory) -> None:
    url = "https://example.com/missing"
    client = mock_client_factory({})
    with URLSource(client=client) as source, pytest.raises(FetchError):
        source.fetch(ItemRef(url=url))


def test_fetch_raises_extraction_error_on_empty_page(
    mock_client_factory: ClientFactory,
) -> None:
    url = "https://example.com/empty"
    empty_html = "<html><head><title>x</title></head><body></body></html>"
    client = mock_client_factory({url: empty_html})
    with URLSource(client=client) as source, pytest.raises(ExtractionError):
        source.fetch(ItemRef(url=url))
