"""RSS / Atom feed source.

Two-phase shape pays off here: `discover` parses the feed once (one HTTP call,
yields N ItemRefs), the orchestrator dedups against the items table, and
`fetch` is only invoked for unseen items. When the feed itself ships full
article bodies (`<content:encoded>` for RSS, Atom `content[type=html]`) we use
them directly; otherwise we fetch the article URL and run trafilatura.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import feedparser
import httpx

from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.sources.base import Source
from pulpline.util.extract import fetch_article

# Feed-supplied bodies shorter than this fall through to a fresh trafilatura
# fetch on the article URL. Below ~600 chars feeds almost always carry just a
# teaser, not the article.
_MIN_FEED_BODY_LEN = 600


class RSSSource(Source):
    name: ClassVar[str] = "rss"

    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(client=client)
        self._entries: dict[str, dict[str, Any]] = {}
        self._feed_url: str = ""
        self._feed_title: str | None = None

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        self._feed_url = target_url
        try:
            response = self.client.get(target_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch feed {target_url}: {exc}") from exc

        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise ExtractionError(f"feed parse failed for {target_url}: {feed.bozo_exception}")

        self._feed_title = feed.feed.get("title")

        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            self._entries[link] = entry
            yield ItemRef(
                url=link,
                title=entry.get("title"),
                pub_date=_struct_to_datetime(entry.get("published_parsed")),
                guid=entry.get("id") or entry.get("guid"),
            )

    def fetch(self, ref: ItemRef) -> RawArticle:
        entry = self._entries.get(ref.url)
        if entry is not None:
            body_html = _body_from_entry(entry)
            if body_html:
                return _article_from_entry(
                    entry,
                    body_html,
                    feed_url=self._feed_url,
                    feed_title=self._feed_title,
                )

        # Fall back to fetching + extracting the article page. Feed URL still
        # wins as `dc:source` so the EPUB records which feed delivered it.
        return fetch_article(ref.url, self.client, source_url=self._feed_url or "direct")


def _body_from_entry(entry: dict[str, Any]) -> str | None:
    """Return feed-supplied HTML body if substantial enough, else None."""
    for item in entry.get("content", []) or []:
        value = item.get("value")
        if value and len(value) >= _MIN_FEED_BODY_LEN:
            return str(value)

    summary = entry.get("summary")
    detail = entry.get("summary_detail") or {}
    if summary and "html" in detail.get("type", "") and len(summary) >= _MIN_FEED_BODY_LEN:
        return str(summary)

    return None


def _article_from_entry(
    entry: dict[str, Any],
    body_html: str,
    feed_url: str,
    feed_title: str | None,
) -> RawArticle:
    canonical_url = entry.get("link") or ""
    publisher = feed_title or _hostname(feed_url)

    return RawArticle(
        title=entry.get("title") or "Untitled",
        body_html=body_html,
        canonical_url=canonical_url,
        source_url=feed_url,
        author=_author_from_entry(entry),
        publisher=publisher,
        pub_date=_struct_to_datetime(entry.get("published_parsed")),
        language=entry.get("language") or "en",
    )


def _author_from_entry(entry: dict[str, Any]) -> str | None:
    raw = entry.get("author") or entry.get("dc_creator")
    if raw:
        return str(raw).strip() or None
    authors = entry.get("authors") or []
    for a in authors:
        name = a.get("name") if isinstance(a, dict) else None
        if name:
            return str(name).strip() or None
    return None


def _struct_to_datetime(value: time.struct_time | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except TypeError, ValueError:
        return None


def _hostname(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url
