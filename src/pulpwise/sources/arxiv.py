"""arXiv source: subscribe to the API search feed, one-shot a single paper.

arXiv's `export.arxiv.org/api/query?...` endpoint returns Atom-formatted
results, which feedparser handles natively. Papers are submitted to Readwise
as bare-URL saves of the `/pdf/<id>` URL (public, no auth) with an explicit
`pdf` category hint - Reader ingests PDFs by URL, and the PDF preserves the
figures and equations that any text-extraction pipeline destroys.
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

from pulpwise.models import ExtractionError, FetchError, ItemRef, RawArticle, ReaderSubmission
from pulpwise.sources.base import Source

_ARXIV_API = "http://export.arxiv.org/api/query"
_ABS_PATH = re.compile(r"/abs/(?P<id>[^/]+)")


class ArXivSource(Source):
    name: ClassVar[str] = "arxiv"
    fetch_needed: ClassVar[bool] = False

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
        if "/api/query" in target_url:
            yield from self._discover_query(target_url)
            return

        # Single paper URL: arxiv.org/abs/<id> or /pdf/<id>
        paper_id = _paper_id_from_url(target_url)
        if not paper_id:
            raise ExtractionError(f"unsupported arXiv URL: {target_url}")
        # One metadata round-trip so the ledger + Reader get a clean title
        # and date instead of whatever the PDF parse guesses.
        entry = self._fetch_single_metadata(paper_id)
        link = entry.get("link") or f"https://arxiv.org/abs/{paper_id}"
        yield ItemRef(
            url=link,
            title=_clean_title(entry.get("title")),
            pub_date=_struct_to_datetime(entry.get("published_parsed")),
            guid=entry.get("id"),
        )

    def fetch(self, ref: ItemRef) -> RawArticle:
        raise NotImplementedError("arXiv papers are bare-URL saves; nothing to fetch")

    def submission_for_ref(self, ref: ItemRef) -> ReaderSubmission:
        """Submit the paper's PDF URL, not the abstract page.

        The abs page is just the abstract; the PDF is the paper. Reader
        fetches PDFs by URL - the explicit category spares its guesser,
        since arXiv PDF URLs carry no `.pdf` suffix.
        """
        return ReaderSubmission(
            url=_pdf_url_from_abs(ref.url),
            title=ref.title,
            pub_date=ref.pub_date,
            category="pdf",
        )

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


def _struct_to_datetime(value: time.struct_time | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except TypeError, ValueError:
        return None
