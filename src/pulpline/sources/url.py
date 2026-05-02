"""Generic URL source: fetch one HTTP page and extract its article via trafilatura."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

import httpx
import lxml.html
import trafilatura

from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.sources.base import Source


@dataclass(frozen=True, slots=True)
class _Fallbacks:
    title: str | None
    author: str | None
    language: str | None


def _parse_fallbacks(html: str) -> _Fallbacks:
    """Cheap second-pass extraction for pages where trafilatura missed metadata.

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
        lang = lang.split("-", 1)[0]  # "en-US" -> "en"

    return _Fallbacks(title=title or None, author=author, language=lang)


class URLSource(Source):
    """Single-URL fetcher. `discover` yields exactly one ItemRef pointing at the URL."""

    name: ClassVar[str] = "url"

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return (ItemRef(url=target_url),)

    def fetch(self, ref: ItemRef) -> RawArticle:
        try:
            response = self.client.get(ref.url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch {ref.url}: {exc}") from exc

        html = response.text
        metadata = trafilatura.bare_extraction(
            html,
            with_metadata=True,
            include_images=True,
            include_links=True,
            url=ref.url,
        )
        if metadata is None:
            raise ExtractionError(f"trafilatura could not extract content from {ref.url}")

        body_html = trafilatura.extract(
            html,
            output_format="html",
            include_images=True,
            include_links=True,
            url=ref.url,
        )
        if not body_html:
            raise ExtractionError(f"trafilatura returned empty body for {ref.url}")

        fallbacks = _parse_fallbacks(html)

        return RawArticle(
            title=_attr(metadata, "title") or fallbacks.title or "Untitled",
            body_html=body_html,
            canonical_url=_attr(metadata, "url") or ref.url,
            source_url="direct",
            author=_attr(metadata, "author") or fallbacks.author,
            publisher=_attr(metadata, "sitename") or _attr(metadata, "hostname"),
            pub_date=_parse_date(_attr(metadata, "date")),
            language=_attr(metadata, "language") or fallbacks.language or "en",
        )


def _attr(obj: object, name: str) -> str | None:
    """Read an optional attribute from trafilatura's Document, returning None for missing/empty."""
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
