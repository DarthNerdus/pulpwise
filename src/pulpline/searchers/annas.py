"""Anna's Archive search via HTML scrape with a browser UA.

Anna explicitly does not ship a search API even for donors (see their
`llms.txt`). The legitimate programmatic-search path they document is the
multi-TB `aa_derived_mirror_metadata` torrent, which is not laptop-scale.
For interactive single-flight CLI search we scrape the same HTML the web
UI serves, with a real browser User-Agent so DDoS-Guard doesn't 403 us.

Selectors are cribbed from the published `annas-mcp` Go server, which has
been running this approach in production. Anna may redesign at any time;
when it does, the parser fails loudly and is easy to re-fixture.

Download is handled separately by `sources.annas.AnnaSource`, which uses
the legitimate `fast_download.json` API with the user's donation key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar
from urllib.parse import quote_plus, urljoin

import httpx
import lxml.html

from pulpline.models import FetchError
from pulpline.searchers.base import Searcher, SearchResult
from pulpline.util.http import build_browser_client

DEFAULT_MIRRORS = ("gl", "pk", "gd")
"""Working Anna's Archive domains as of 2026-05. `.li` is excluded - it's
been hijacked and now serves a router.parklogic.com parked-domain page.
SLUM (uptime monitor) is the source of truth long-term; this list is the
ordered fallback."""

_CONTENT_BOOK = "book_any"
_CONTENT_PAPER = "journal"

_FORMAT_RX = re.compile(r"\b(EPUB|PDF|MOBI|AZW3|AZW|DJVU|CBZ|CBR|FB2|DOCX?|TXT)\b", re.IGNORECASE)
_SIZE_RX = re.compile(r"\d+\.?\d*\s*(?:MB|KB|GB|TB)", re.IGNORECASE)
_YEAR_RX = re.compile(r"\b(19|20)\d{2}\b")
_LANG_RX = re.compile(r"\[([a-z][a-z](?:-[a-z]+)?)\]", re.IGNORECASE)


class AnnaSearcher(Searcher):
    name: ClassVar[str] = "anna"

    def __init__(
        self,
        client: httpx.Client | None = None,
        mirrors: tuple[str, ...] = DEFAULT_MIRRORS,
    ) -> None:
        super().__init__(client=client)
        self._mirrors = mirrors

    def _build_client(self) -> httpx.Client:
        return build_browser_client()

    def search(
        self,
        query: str,
        *,
        content: str | None = None,
        extension: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> Iterable[SearchResult]:
        anna_content = _content_param(content)
        params = [("q", query)]
        if anna_content:
            params.append(("content", anna_content))
        if extension:
            params.append(("ext", extension.lower()))
        if language:
            params.append(("lang", language.lower()))
        query_string = "&".join(f"{k}={quote_plus(v)}" for k, v in params)

        last_error: Exception | None = None
        for tld in self._mirrors:
            base = f"https://annas-archive.{tld}"
            url = f"{base}/search?{query_string}"
            try:
                response = self.client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = exc
                continue

            if _looks_like_block(response.text):
                last_error = FetchError(
                    f"Anna's Archive at {tld} returned an anti-bot challenge "
                    "(DDoS-Guard / Cloudflare). Try a different --mirror or wait."
                )
                continue

            return list(_parse_results(response.text, base))[:limit]

        if last_error is not None:
            raise FetchError(f"all Anna's mirrors failed: {last_error}") from last_error
        raise FetchError("no Anna's mirrors configured")


def _content_param(content: str | None) -> str | None:
    """Translate pulpline's --content flag to Anna's `content=` query value."""
    if content is None:
        return None
    normalized = content.strip().lower()
    if normalized in {"book", "books"}:
        return _CONTENT_BOOK
    if normalized in {"paper", "papers", "article", "journal", "journals"}:
        return _CONTENT_PAPER
    # Pass through anything else (Anna also supports comic, magazine, ...).
    return normalized


def _looks_like_block(html: str) -> bool:
    """Heuristic for DDoS-Guard / Cloudflare interstitial pages.

    These pages are short, contain no `/md5/` links, and usually mention
    the protector by name. We use this as a soft signal to fall over to
    the next mirror rather than parse 0 results and confuse the user.
    """
    if "/md5/" in html:
        return False
    lower = html.lower()
    return any(
        marker in lower
        for marker in (
            "ddos-guard",
            "ddosguard",
            "cloudflare",
            "checking your browser",
            "parklogic",  # `.li` was hijacked into a parked-domain redirect
            "redirecting...",
        )
    )


def _parse_results(html: str, base: str) -> Iterable[SearchResult]:
    """Yield SearchResults from a search page's HTML."""
    tree = lxml.html.fromstring(html)
    # Anchor on the cover-image link (matches annas-mcp's selector); the
    # title-text link has the same href but a different class, so this
    # de-duplicates without us having to track seen md5s.
    cover_links = tree.xpath(
        "//a[starts-with(@href, '/md5/') and "
        "contains(@class, 'custom-a') and contains(@class, 'block')]"
    )

    for cover in cover_links:
        href = cover.get("href") or ""
        md5 = href.removeprefix("/md5/").strip("/")
        if not md5:
            continue

        info = cover.getparent()
        if info is None:
            continue

        # Anna nests a `div.max-w-full` under the parent that holds title,
        # authors, publisher, and the metadata pipe-string. The cover link
        # itself is a sibling.
        info_div = info.find(".//div[@class='max-w-full']")
        if info_div is None:
            # Fallback: any descendant containing both title link and meta line.
            for candidate in info.iter("div"):
                cls = candidate.get("class") or ""
                if "max-w-full" in cls:
                    info_div = candidate
                    break
        if info_div is None:
            continue

        title = _first_text(info_div.xpath(".//a[starts-with(@href, '/md5/')]"))
        if not title:
            continue

        authors = _icon_field(info_div, "mdi--user-edit")
        publisher = _icon_field(info_div, "mdi--company")
        meta_text = _first_text(info_div.xpath(".//div[contains(@class, 'text-gray-800')]"))
        language, extension, size, year = _split_meta(meta_text or "")

        yield SearchResult(
            target_url=urljoin(base + "/", href.lstrip("/")),
            title=title,
            authors=authors or None,
            publisher=publisher or None,
            year=year,
            language=language,
            extension=extension,
            size=size,
        )


def _first_text(nodes: list[lxml.html.HtmlElement]) -> str:
    if not nodes:
        return ""
    return " ".join(nodes[0].text_content().split()).strip()


def _icon_field(info_div: lxml.html.HtmlElement, icon_name: str) -> str:
    """Anna marks authors/publisher with mdi icon spans inside `/search` links.

    The text we want is the parent anchor's collapsed text content.
    """
    spans = info_div.xpath(
        f".//a[starts-with(@href, '/search')]//span[contains(@class, 'icon-[{icon_name}]')]"
    )
    if not spans:
        return ""
    anchor = spans[0].getparent()
    while anchor is not None and anchor.tag != "a":
        anchor = anchor.getparent()
    if anchor is None:
        return ""
    return " ".join(anchor.text_content().split()).strip()


def _split_meta(meta: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse Anna's pipe-delimited metadata line.

    Format examples:
      "✅ English [en] · EPUB · 0.7MB · 2015 · ..."
      "✅ English [en] · Hindi [hi] · EPUB · 12 MB · 2024"
    """
    if not meta:
        return None, None, None, None

    parts = [p.strip() for p in meta.split("·") if p.strip()]
    language: str | None = None
    extension: str | None = None
    size: str | None = None
    year: str | None = None

    if parts:
        lang_match = _LANG_RX.search(parts[0])
        if lang_match:
            language = lang_match.group(1).lower()

    for part in parts[1:]:
        if extension is None:
            ext_match = _FORMAT_RX.search(part)
            if ext_match:
                extension = ext_match.group(1).lower()
                continue
        if size is None:
            size_match = _SIZE_RX.search(part)
            if size_match:
                size = size_match.group(0)
                continue
        if year is None:
            year_match = _YEAR_RX.search(part)
            if year_match:
                year = year_match.group(0)

    return language, extension, size, year
