"""EPUB renderer (ebooklib). Single-chapter EPUB 3 with Dublin Core OPF metadata."""

from __future__ import annotations

import html
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin

import httpx
import lxml.etree
import lxml.html
from ebooklib import epub

from pulpline.models import RawArticle
from pulpline.renderers.base import Renderer
from pulpline.util.http import build_client

_log = logging.getLogger("pulpline.renderers.epub")

_IMAGE_DIR = "images"
_IMAGE_TIMEOUT = 30.0
# Map content-type prefixes to file extensions. Substack mostly serves
# webp/png/jpg/gif; SVG is rare but worth handling. Anything else gets
# left as `bin` and the EPUB validator will probably complain - the
# fail-soft path skips embedding if we can't classify the bytes.
_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


class EpubRenderer(Renderer):
    extension: ClassVar[str] = "epub"

    def __init__(self, image_client: httpx.Client | None = None) -> None:
        self._image_client = image_client

    def render(self, article: RawArticle) -> bytes:
        book = epub.EpubBook()
        book.set_identifier(article.canonical_url)
        book.set_title(article.title)
        book.set_language(article.language)

        if article.author:
            book.add_author(article.author)
        if article.publisher:
            book.add_metadata("DC", "publisher", article.publisher)
        if article.pub_date is not None:
            book.add_metadata("DC", "date", article.pub_date.date().isoformat())
        book.add_metadata("DC", "source", article.source_url)

        tag = (
            f"pulpline:{article.subscription_name}"
            if article.subscription_name
            else "pulpline:once"
        )
        book.add_metadata("DC", "subject", tag)

        book.add_metadata(
            None,
            "meta",
            "",
            {"name": "calibre:timestamp", "content": _now_iso()},
        )

        # Download every <img src=...> in the body, embed as an EPUB resource,
        # rewrite to a relative path. Falls back to leaving the original src
        # if any image fetch fails - the EPUB still opens, the network-bound
        # image just doesn't display offline.
        body_html = _embed_images(
            article.body_html,
            book,
            base_url=article.canonical_url,
            client=self._image_client,
        )

        chapter = epub.EpubHtml(
            title=article.title,
            file_name="article.xhtml",
            lang=article.language,
        )
        chapter.content = _wrap_html(article, body_html=body_html)
        book.add_item(chapter)

        book.toc = (chapter,)
        book.spine = ["nav", chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        return _serialize(book)


def _embed_images(
    body_html: str,
    book: epub.EpubBook,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Replace remote `<img src>` URLs with bytes embedded in `book`.

    Returns the rewritten HTML. Failures are silent (per-image): we keep
    the original `src` so an online reader still loads the image, while
    the rest of the article renders.
    """
    if not body_html:
        return body_html
    try:
        # Wrap in a root so lxml doesn't synthesize <html><body> for us
        # (which would corrupt `<img>`-only fragments).
        tree = lxml.html.fragment_fromstring(body_html, create_parent="div")
    except lxml.etree.ParserError, ValueError:
        return body_html

    img_tags = tree.xpath(".//img[@src]")
    if not img_tags:
        return body_html

    own_client = client is None
    c = client or build_client(timeout=_IMAGE_TIMEOUT)
    seen: dict[str, str] = {}  # url -> local path (deduped)
    counter = 0
    try:
        for img in img_tags:
            src = (img.get("src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            if base_url and not src.startswith(("http://", "https://", "//")):
                src = urljoin(base_url, src)
            elif src.startswith("//"):
                src = "https:" + src

            if src in seen:
                img.set("src", seen[src])
                _strip_responsive_attrs(img)
                continue

            try:
                response = c.get(src)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                _log.debug("epub image fetch failed url=%s err=%s", src, exc)
                continue

            content_type = (
                (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            )
            ext = _CONTENT_TYPE_TO_EXT.get(content_type)
            if ext is None:
                _log.debug(
                    "epub skipping image with unknown content-type=%r url=%s",
                    content_type,
                    src,
                )
                continue

            counter += 1
            local_path = f"{_IMAGE_DIR}/img{counter:03d}{ext}"
            book.add_item(
                epub.EpubImage(
                    uid=f"img{counter:03d}",
                    file_name=local_path,
                    media_type=content_type,
                    content=response.content,
                )
            )
            seen[src] = local_path
            img.set("src", local_path)
            _strip_responsive_attrs(img)
    finally:
        if own_client:
            c.close()

    return _inner_html(tree)


def _inner_html(wrapper: lxml.html.HtmlElement) -> str:
    """Serialize children of `wrapper` without the wrapper element itself."""
    parts: list[str] = []
    if wrapper.text:
        parts.append(wrapper.text)
    for child in wrapper:
        parts.append(lxml.html.tostring(child, encoding="unicode"))
    return "".join(parts)


def _strip_responsive_attrs(img: lxml.html.HtmlElement) -> None:
    """Drop srcset / sizes / data-* so the local <img src=...> wins.

    Substack serves multiple resolutions via srcset; if we keep it, the
    reader picks a remote URL on devices with network and ignores our
    embedded image.
    """
    for attr in ("srcset", "data-srcset", "data-src", "loading", "sizes"):
        if attr in img.attrib:
            del img.attrib[attr]


def _serialize(book: epub.EpubBook) -> bytes:
    """ebooklib only writes to disk paths, so round-trip via a tempdir."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.epub"
        epub.write_epub(str(out), book)
        return out.read_bytes()


def _wrap_html(article: RawArticle, body_html: str | None = None) -> str:
    title = html.escape(article.title)
    byline_html = ""
    if article.author:
        byline_html = f'<p class="byline">By {html.escape(article.author)}</p>'
    elif article.publisher:
        byline_html = f'<p class="byline">{html.escape(article.publisher)}</p>'

    canonical_attr = html.escape(article.canonical_url, quote=True)
    canonical_text = html.escape(article.canonical_url)
    lang_attr = html.escape(article.language, quote=True)
    body = body_html if body_html is not None else article.body_html

    # Note: no `<?xml ...?>` declaration; ebooklib's chapter parser silently
    # produces empty output when one is present. DOCTYPE alone is fine.
    return (
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang_attr}">\n'
        "<head>\n"
        f"<title>{title}</title>\n"
        '<meta charset="utf-8"/>\n'
        "</head>\n"
        "<body>\n"
        f"<h1>{title}</h1>\n"
        f"{byline_html}\n"
        f"{body}\n"
        "<hr/>\n"
        f'<p class="source"><a href="{canonical_attr}">{canonical_text}</a></p>\n'
        "</body>\n"
        "</html>\n"
    )


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")
