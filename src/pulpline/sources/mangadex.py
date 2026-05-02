"""MangaDex source: subscribe to a manga's chapter feed, render as CBZ.

MangaDex distributes manga chapters as ordered images served from a volunteer
CDN ("at-home" servers). The API flow per chapter is:

  1. /at-home/server/{chapter_id}  ->  baseUrl + page filenames
  2. {baseUrl}/data/{hash}/{filename}  ->  image bytes (one per page)

We zip the images (ZIP_STORED - they're already JPEG/PNG) into a CBZ which
Boox / KOReader / Calibre handle natively. No comicbook.xml metadata is
required for read-only consumption.

URL routing:
  - https://mangadex.org/title/{uuid}/...  -> subscribe to that manga
  - https://mangadex.org/chapter/{uuid}    -> one-shot a single chapter
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.sources.base import Source

_API_BASE = "https://api.mangadex.org"
_DEFAULT_LANGUAGE = "en"
_DISCOVER_LIMIT = 25
_PATH_TITLE = re.compile(r"^/title/([0-9a-f-]+)")
_PATH_CHAPTER = re.compile(r"^/chapter/([0-9a-f-]+)")


class MangaDexSource(Source):
    name: ClassVar[str] = "mangadex"
    extension: ClassVar[str] = "cbz"

    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(client=client)
        self._chapters: dict[str, dict[str, Any]] = {}
        self._feed_url: str = ""

    @classmethod
    def matches_url(cls, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host in {"mangadex.org", "www.mangadex.org", "api.mangadex.org"}

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        self._feed_url = target_url
        parts = urlsplit(target_url)

        title_match = _PATH_TITLE.match(parts.path)
        if title_match:
            yield from self._discover_manga(title_match.group(1))
            return

        chapter_match = _PATH_CHAPTER.match(parts.path)
        if chapter_match:
            chapter_id = chapter_match.group(1)
            cache = self._fetch_chapter_meta(chapter_id)
            self._chapters[chapter_id] = cache
            yield ItemRef(
                url=f"https://mangadex.org/chapter/{chapter_id}",
                title=_chapter_label(cache),
                pub_date=_parse_iso(cache["chapter_attrs"].get("publishAt")),
                guid=chapter_id,
            )
            return

        raise ExtractionError(f"unsupported MangaDex URL: {target_url}")

    def fetch(self, ref: ItemRef) -> RawArticle:
        chapter_id = ref.url.rsplit("/", 1)[-1]
        cache = self._chapters.get(chapter_id)
        if cache is None:
            cache = self._fetch_chapter_meta(chapter_id)
            self._chapters[chapter_id] = cache

        attrs = cache["chapter_attrs"]
        return RawArticle(
            title=_chapter_label(cache),
            body_html="",  # CBZ is the renderable; body_html is informational only
            canonical_url=ref.url,
            source_url=self._feed_url or "mangadex",
            author=cache.get("manga_authors"),
            publisher="MangaDex",
            pub_date=_parse_iso(attrs.get("publishAt")),
            language=str(attrs.get("translatedLanguage") or _DEFAULT_LANGUAGE),
        )

    def render(self, article: RawArticle) -> bytes:
        chapter_id = article.canonical_url.rsplit("/", 1)[-1]

        try:
            response = self.client.get(f"{_API_BASE}/at-home/server/{chapter_id}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to get at-home info for {chapter_id}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExtractionError(f"at-home for {chapter_id} not JSON") from exc

        base_url = payload.get("baseUrl")
        chapter_data = payload.get("chapter") or {}
        chapter_hash = chapter_data.get("hash")
        page_filenames = chapter_data.get("data") or []
        if not (
            isinstance(base_url, str)
            and isinstance(chapter_hash, str)
            and isinstance(page_filenames, list)
            and page_filenames
        ):
            raise ExtractionError(f"at-home payload for {chapter_id} missing pages")

        pages: list[tuple[str, bytes]] = []
        for filename in page_filenames:
            if not isinstance(filename, str):
                continue
            url = f"{base_url}/data/{chapter_hash}/{filename}"
            try:
                img = self.client.get(url)
                img.raise_for_status()
            except httpx.HTTPError as exc:
                raise FetchError(f"failed to fetch page {url}: {exc}") from exc
            pages.append((filename, img.content))

        if not pages:
            raise ExtractionError(f"chapter {chapter_id} has no pages")

        return _build_cbz(pages)

    def _discover_manga(self, manga_id: str) -> Iterable[ItemRef]:
        manga_meta = self._fetch_manga_meta(manga_id)
        manga_title = manga_meta["title"]
        manga_authors = manga_meta.get("authors")

        try:
            response = self.client.get(
                f"{_API_BASE}/manga/{manga_id}/feed",
                params={
                    "translatedLanguage[]": _DEFAULT_LANGUAGE,
                    "order[chapter]": "desc",
                    "limit": str(_DISCOVER_LIMIT),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch feed for {manga_id}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExtractionError(f"feed for {manga_id} not JSON") from exc

        chapters = payload.get("data") or []
        if not isinstance(chapters, list):
            raise ExtractionError(f"feed for {manga_id} returned non-list")

        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            cid = chapter.get("id")
            attrs = chapter.get("attributes") or {}
            if not isinstance(cid, str) or not isinstance(attrs, dict):
                continue
            cache: dict[str, Any] = {
                "manga_title": manga_title,
                "manga_authors": manga_authors,
                "chapter_attrs": attrs,
            }
            self._chapters[cid] = cache
            yield ItemRef(
                url=f"https://mangadex.org/chapter/{cid}",
                title=_chapter_label(cache),
                pub_date=_parse_iso(attrs.get("publishAt")),
                guid=cid,
            )

    def _fetch_manga_meta(self, manga_id: str) -> dict[str, Any]:
        try:
            response = self.client.get(f"{_API_BASE}/manga/{manga_id}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch manga {manga_id}: {exc}") from exc
        try:
            data = response.json().get("data") or {}
        except ValueError as exc:
            raise ExtractionError(f"manga {manga_id} not JSON") from exc

        attrs = data.get("attributes") or {}
        title_map = attrs.get("title") or {}
        title = (
            title_map.get(_DEFAULT_LANGUAGE)
            or next(iter(title_map.values()), None)
            or "Untitled Manga"
        )
        return {"title": str(title)}

    def _fetch_chapter_meta(self, chapter_id: str) -> dict[str, Any]:
        try:
            response = self.client.get(f"{_API_BASE}/chapter/{chapter_id}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch chapter {chapter_id}: {exc}") from exc
        try:
            data = response.json().get("data") or {}
        except ValueError as exc:
            raise ExtractionError(f"chapter {chapter_id} not JSON") from exc
        attrs = data.get("attributes") or {}

        # We didn't come through the feed path, so we don't know the manga
        # title from the chapter response alone. Look it up via relationships.
        manga_title = "Untitled Manga"
        for rel in data.get("relationships") or []:
            if isinstance(rel, dict) and rel.get("type") == "manga":
                manga_id = rel.get("id")
                if isinstance(manga_id, str):
                    try:
                        manga_meta = self._fetch_manga_meta(manga_id)
                        manga_title = manga_meta.get("title", manga_title)
                    except FetchError, ExtractionError:
                        pass
                break

        return {"manga_title": manga_title, "chapter_attrs": attrs}


def _chapter_label(cache: dict[str, Any]) -> str:
    """`{Manga} - Ch. {N} - {chapter title}` (with graceful fallbacks)."""
    manga = cache.get("manga_title") or "Untitled"
    attrs = cache.get("chapter_attrs") or {}
    chapter_num = attrs.get("chapter")
    chapter_title = attrs.get("title")
    parts = [str(manga)]
    if chapter_num:
        parts.append(f"Ch. {chapter_num}")
    if chapter_title:
        parts.append(str(chapter_title))
    return " - ".join(parts)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _build_cbz(pages: list[tuple[str, bytes]]) -> bytes:
    """Build a CBZ from (orig_filename, image_bytes) pairs.

    Pages are renamed `001.<ext>`, `002.<ext>`, ... so lexical sort matches
    page order. ZIP_STORED because images are already compressed.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i, (orig_name, data) in enumerate(pages, start=1):
            ext = orig_name.rsplit(".", 1)[-1] if "." in orig_name else "png"
            zf.writestr(f"{i:03d}.{ext}", data)
    return buf.getvalue()
