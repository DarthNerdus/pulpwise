"""HTTP fetch + trafilatura extraction with lxml fallbacks.

Shared by URLSource (one-shot) and RSSSource (when the feed is summary-only and
we have to fetch the article body ourselves). Keeping the extraction in one
place means both sources behave identically for the same article URL - the
canonical URL, language detection, and metadata fallback rules cannot diverge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
import lxml.html
import trafilatura

from pulpline.models import ExtractionError, FetchError, RawArticle


@dataclass(frozen=True, slots=True)
class _Fallbacks:
    title: str | None
    author: str | None
    language: str | None


def fetch_article(url: str, client: httpx.Client, source_url: str = "direct") -> RawArticle:
    """Fetch `url` via `client`, extract via trafilatura, fall back to lxml for missed metadata.

    `source_url` is what gets written to the EPUB's `dc:source` field - "direct"
    for one-shots, the feed URL for RSS items.
    """
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FetchError(f"failed to fetch {url}: {exc}") from exc

    html = response.text
    metadata = trafilatura.bare_extraction(
        html,
        with_metadata=True,
        include_images=True,
        include_links=True,
        url=url,
    )
    if metadata is None:
        raise ExtractionError(f"trafilatura could not extract content from {url}")

    body_html = trafilatura.extract(
        html,
        output_format="html",
        include_images=True,
        include_links=True,
        url=url,
    )
    if not body_html:
        raise ExtractionError(f"trafilatura returned empty body for {url}")

    fallbacks = _parse_fallbacks(html)

    return RawArticle(
        title=_attr(metadata, "title") or fallbacks.title or "Untitled",
        body_html=body_html,
        canonical_url=_attr(metadata, "url") or url,
        source_url=source_url,
        author=_attr(metadata, "author") or fallbacks.author,
        publisher=_attr(metadata, "sitename") or _attr(metadata, "hostname"),
        pub_date=_parse_date(_attr(metadata, "date")),
        language=_attr(metadata, "language") or fallbacks.language or "en",
    )


def _attr(obj: object, name: str) -> str | None:
    value = getattr(obj, name, None)
    if value is None or value == "":
        return None
    return str(value)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _parse_fallbacks(html: str) -> _Fallbacks:
    """Cheap second-pass lxml extraction for pages where trafilatura misses metadata.

    Some HTML5-minimal pages (no `<head>` block, e.g. danluu.com) confuse
    trafilatura's metadata extractor but parse fine with plain lxml.
    """
    try:
        tree = lxml.html.fromstring(html)
    except ValueError, lxml.etree.ParserError:
        return _Fallbacks(None, None, None)

    title_el = tree.find(".//title")
    title = title_el.text.strip() if title_el is not None and title_el.text else None

    author = None
    for xpath in (
        './/meta[@name="author"]',
        './/meta[@property="article:author"]',
        './/meta[@name="twitter:creator"]',
    ):
        el = tree.find(xpath)
        if el is not None:
            content = el.get("content")
            if content and content.strip():
                author = content.strip()
                break

    lang = tree.get("lang") or None
    if lang:
        lang = lang.split("-", 1)[0]

    return _Fallbacks(title=title or None, author=author, language=lang)
