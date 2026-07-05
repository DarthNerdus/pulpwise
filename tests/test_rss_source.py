"""Tests for RSSSource - feed discovery, category filtering, no-fetch contract."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from pulpwise.models import ExtractionError, FetchError, ItemRef
from pulpwise.sources.rss import RSSSource
from tests.conftest import FIXTURES

ClientFactory = Callable[[dict[str, str]], httpx.Client]


def _read_feed_fixture() -> str:
    return (FIXTURES / "sample_feed.xml").read_text()


def test_discover_yields_one_ref_per_entry(mock_client_factory: ClientFactory) -> None:
    feed_url = "https://example.com/feed"
    client = mock_client_factory({feed_url: _read_feed_fixture()})

    with RSSSource(client=client) as source:
        refs = list(source.discover(feed_url))

    assert len(refs) == 2
    by_url = {r.url: r for r in refs}
    assert set(by_url) == {"https://example.com/full", "https://example.com/teaser"}

    full = by_url["https://example.com/full"]
    assert full.title == "Full Content Article"
    assert full.guid == "tag:example.com,2026:full"
    assert full.pub_date is not None
    assert (full.pub_date.year, full.pub_date.month, full.pub_date.day) == (2026, 4, 28)


def test_rss_is_a_no_fetch_source() -> None:
    assert RSSSource.fetch_needed is False


def test_fetch_raises_not_implemented() -> None:
    with (
        RSSSource(client=httpx.Client()) as source,
        pytest.raises(NotImplementedError, match="bare-URL saves"),
    ):
        source.fetch(ItemRef(url="https://example.com/full"))


def test_submission_for_ref_is_a_bare_url_save(mock_client_factory: ClientFactory) -> None:
    feed_url = "https://example.com/feed"
    client = mock_client_factory({feed_url: _read_feed_fixture()})

    with RSSSource(client=client) as source:
        refs = list(source.discover(feed_url))
        submission = source.submission_for_ref(refs[0])

    assert submission.url == refs[0].url
    assert submission.html is None
    assert submission.kind == "url"
    assert submission.title == refs[0].title


def test_discover_http_failure_raises_fetch_error(mock_client_factory: ClientFactory) -> None:
    client = mock_client_factory({})  # 404 everywhere
    with RSSSource(client=client) as source, pytest.raises(FetchError):
        list(source.discover("https://example.com/feed"))


def test_discover_unparseable_body_raises_extraction_error(
    mock_client_factory: ClientFactory,
) -> None:
    feed_url = "https://example.com/feed"
    client = mock_client_factory({feed_url: "<<< not a feed at all >>>"})
    with RSSSource(client=client) as source, pytest.raises(ExtractionError):
        list(source.discover(feed_url))


# --- category filtering -------------------------------------------------------

_CATEGORIZED_FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Curated Links</title>
    <link>https://links.example.com/</link>
    <description>Test feed with categories</description>
    <item>
      <title>An Essay</title>
      <link>https://example.com/essay</link>
      <category>Recommended Reading</category>
    </item>
    <item>
      <title>A Talk</title>
      <link>https://example.com/talk</link>
      <category>Recommended Viewing</category>
    </item>
    <item>
      <title>A Paper</title>
      <link>https://example.com/paper</link>
      <category>Recommended Reading</category>
      <category>Research</category>
    </item>
    <item>
      <title>Untagged Note</title>
      <link>https://example.com/note</link>
    </item>
  </channel>
</rss>
"""

_CATEGORIZED_FEED_URL = "https://links.example.com/rss"


def _discover_categorized(
    mock_client_factory: ClientFactory,
    categories: frozenset[str] | None = None,
    exclude_categories: frozenset[str] | None = None,
) -> set[str]:
    client = mock_client_factory({_CATEGORIZED_FEED_URL: _CATEGORIZED_FEED})
    with RSSSource(
        client=client, categories=categories, exclude_categories=exclude_categories
    ) as source:
        return {ref.url for ref in source.discover(_CATEGORIZED_FEED_URL)}


def test_no_filter_discovers_every_entry(mock_client_factory: ClientFactory) -> None:
    urls = _discover_categorized(mock_client_factory)
    assert urls == {
        "https://example.com/essay",
        "https://example.com/talk",
        "https://example.com/paper",
        "https://example.com/note",
    }


def test_categories_selects_only_matching_entries(mock_client_factory: ClientFactory) -> None:
    urls = _discover_categorized(mock_client_factory, categories=frozenset({"recommended reading"}))
    # Uncategorized entries don't pass an include-list: it means "only these".
    assert urls == {"https://example.com/essay", "https://example.com/paper"}


def test_category_matching_is_case_insensitive(mock_client_factory: ClientFactory) -> None:
    # Constructor contract: filter sets are lowercased; feed-side casing
    # ("Recommended Viewing") must not matter.
    urls = _discover_categorized(mock_client_factory, categories=frozenset({"recommended viewing"}))
    assert urls == {"https://example.com/talk"}


def test_exclude_categories_drops_matching_keeps_untagged(
    mock_client_factory: ClientFactory,
) -> None:
    urls = _discover_categorized(
        mock_client_factory, exclude_categories=frozenset({"recommended viewing"})
    )
    assert urls == {
        "https://example.com/essay",
        "https://example.com/paper",
        "https://example.com/note",
    }


def test_exclude_wins_over_include(mock_client_factory: ClientFactory) -> None:
    # The paper is both "recommended reading" (included) and "research"
    # (excluded); exclusion takes precedence.
    urls = _discover_categorized(
        mock_client_factory,
        categories=frozenset({"recommended reading"}),
        exclude_categories=frozenset({"research"}),
    )
    assert urls == {"https://example.com/essay"}


def test_from_config_parses_category_options() -> None:
    from pulpwise.config import Config, Subscription

    sub = Subscription(
        name="links",
        source="rss",
        url=_CATEGORIZED_FEED_URL,
        options={"categories": "Recommended Reading", "exclude_categories": " Research , Ads "},
    )
    source = RSSSource.from_config(Config(), subscription=sub)
    assert source._categories == frozenset({"recommended reading"})
    assert source._exclude_categories == frozenset({"research", "ads"})
    source.close()


def test_from_config_without_options_applies_no_filter() -> None:
    from pulpwise.config import Config, Subscription

    sub = Subscription(name="links", source="rss", url=_CATEGORIZED_FEED_URL)
    source = RSSSource.from_config(Config(), subscription=sub)
    assert source._categories is None
    assert source._exclude_categories is None
    source.close()


def test_blank_category_option_means_no_filter() -> None:
    from pulpwise.config import Config, Subscription

    # `categories = " , "` parsing to an empty set would silently drop every
    # entry; blanks must collapse to "no constraint" instead.
    sub = Subscription(
        name="links", source="rss", url=_CATEGORIZED_FEED_URL, options={"categories": " , "}
    )
    source = RSSSource.from_config(Config(), subscription=sub)
    assert source._categories is None
    source.close()
