"""arXiv source: subscribe to the API search feed, one-shot a single paper.

arXiv's `export.arxiv.org/api/query?...` endpoint returns Atom-formatted
results, which feedparser handles natively. Each entry's `<link
type="application/pdf">` is the actual paper PDF; we download that as the
deliverable instead of running the abstract page through trafilatura. Math
papers have figures and equations that PDF preserves and any text-extraction
pipeline destroys.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import feedparser
import httpx

from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.sources.base import Source

_ARXIV_API = "http://export.arxiv.org/api/query"
_ABS_PATH = re.compile(r"/abs/(?P<id>[^/]+)")


class ArXivSource(Source):
    name: ClassVar[str] = "arxiv"
    extension: ClassVar[str] = "pdf"

    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(client=client)
        self._entries: dict[str, dict[str, Any]] = {}
        self._feed_url: str = ""

    @classmethod
    def matches_url(cls, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}

    @classmethod
    def is_subscribable(cls, url: str) -> bool:
        """API query URLs subscribe; /abs/ and /pdf/ paths one-shot."""
        return "/api/query" in urlsplit(url).path

    @classmethod
    def default_subscription_name(cls, url: str) -> str:
        from urllib.parse import parse_qs

        qs = parse_qs(urlsplit(url).query)
        search_query = qs.get("search_query", [""])[0]
        if search_query:
            clean = re.sub(r"[^a-z0-9]+", "-", search_query.lower()).strip("-")
            return f"arxiv-{clean}" if clean else "arxiv"
        return "arxiv"

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        self._feed_url = target_url

        if "/api/query" in target_url:
            yield from self._discover_query(target_url)
            return

        # Single paper URL: arxiv.org/abs/<id> or /pdf/<id>
        paper_id = _paper_id_from_url(target_url)
        if not paper_id:
            raise ExtractionError(f"unsupported arXiv URL: {target_url}")
        # Pre-fetch metadata so fetch() doesn't have to round-trip again.
        entry = self._fetch_single_metadata(paper_id)
        link = entry.get("link") or f"https://arxiv.org/abs/{paper_id}"
        self._entries[link] = entry
        yield ItemRef(
            url=link,
            title=_clean_title(entry.get("title")),
            pub_date=_struct_to_datetime(entry.get("published_parsed")),
            guid=entry.get("id"),
        )

    def fetch(self, ref: ItemRef) -> RawArticle:
        entry = self._entries.get(ref.url)
        if entry is None:
            paper_id = _paper_id_from_url(ref.url)
            if not paper_id:
                raise ExtractionError(f"cannot fetch {ref.url}: not an arXiv abs URL")
            entry = self._fetch_single_metadata(paper_id)

        title = _clean_title(entry.get("title")) or "Untitled"
        abstract = _clean_summary(entry.get("summary")) or ""
        authors = _authors_from_entry(entry)

        # body_html is mostly informational here - the PDF is what the renderer
        # ships. We populate it for the items-table record + any future use.
        author_block = f"<p><em>{authors}</em></p>" if authors else ""
        body_html = (
            f"<h1>{_html_escape(title)}</h1>"
            f"{author_block}"
            f"<h2>Abstract</h2>"
            f"<p>{_html_escape(abstract)}</p>"
        )

        return RawArticle(
            title=title,
            body_html=body_html,
            canonical_url=entry.get("link") or ref.url,
            source_url=self._feed_url or "arxiv",
            author=authors,
            publisher="arXiv",
            pub_date=_struct_to_datetime(entry.get("published_parsed")),
            language="en",
        )

    def render(self, article: RawArticle) -> bytes:
        pdf_url = _pdf_url_from_abs(article.canonical_url)
        try:
            response = self.client.get(pdf_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to download PDF {pdf_url}: {exc}") from exc

        if not response.content.startswith(b"%PDF"):
            raise ExtractionError(f"{pdf_url} did not return a PDF")
        return response.content

    def _discover_query(self, target_url: str) -> Iterable[ItemRef]:
        try:
            response = self.client.get(target_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch arXiv feed {target_url}: {exc}") from exc

        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise ExtractionError(
                f"arXiv feed parse failed for {target_url}: {feed.bozo_exception}"
            )

        for entry in feed.entries:
            link = entry.get("link")
            if not isinstance(link, str) or not link:
                continue
            self._entries[link] = entry
            yield ItemRef(
                url=link,
                title=_clean_title(entry.get("title")),
                pub_date=_struct_to_datetime(entry.get("published_parsed")),
                guid=entry.get("id"),
            )

    def _fetch_single_metadata(self, paper_id: str) -> dict[str, Any]:
        url = f"{_ARXIV_API}?id_list={paper_id}"
        try:
            response = self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch arXiv metadata for {paper_id}: {exc}") from exc

        feed = feedparser.parse(response.content)
        if not feed.entries:
            raise ExtractionError(f"no metadata for arXiv paper {paper_id}")
        first: dict[str, Any] = dict(feed.entries[0])
        return first


def _paper_id_from_url(url: str) -> str | None:
    """Extract an arXiv paper id (e.g. '2401.12345' or '2401.12345v2') from a URL."""
    parts = urlsplit(url)
    match = _ABS_PATH.search(parts.path)
    if match:
        return match.group("id")
    # /pdf/<id> or /pdf/<id>.pdf
    if "/pdf/" in parts.path:
        candidate = parts.path.rsplit("/", 1)[-1]
        return candidate.removesuffix(".pdf") or None
    return None


def _pdf_url_from_abs(abs_url: str) -> str:
    """Map an /abs/<id> URL to its /pdf/<id> equivalent."""
    if "/abs/" in abs_url:
        return abs_url.replace("/abs/", "/pdf/", 1)
    return abs_url


def _clean_title(value: object) -> str:
    """arXiv titles often have line breaks for column layout - flatten them."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _clean_summary(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _authors_from_entry(entry: dict[str, Any]) -> str | None:
    authors = entry.get("authors") or []
    names: list[str] = []
    for a in authors:
        if isinstance(a, dict):
            name = a.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return ", ".join(names) if names else None


def _struct_to_datetime(value: time.struct_time | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except TypeError, ValueError:
        return None


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
