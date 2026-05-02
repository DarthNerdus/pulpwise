"""EPUB renderer (ebooklib). Single-chapter EPUB 3 with Dublin Core OPF metadata."""

from __future__ import annotations

import html
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from ebooklib import epub

from pulpline.models import RawArticle
from pulpline.renderers.base import Renderer


class EpubRenderer(Renderer):
    extension: ClassVar[str] = "epub"

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

        chapter = epub.EpubHtml(
            title=article.title,
            file_name="article.xhtml",
            lang=article.language,
        )
        chapter.content = _wrap_html(article)
        book.add_item(chapter)

        book.toc = (chapter,)
        book.spine = ["nav", chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        return _serialize(book)


def _serialize(book: epub.EpubBook) -> bytes:
    """ebooklib only writes to disk paths, so round-trip via a tempdir."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.epub"
        epub.write_epub(str(out), book)
        return out.read_bytes()


def _wrap_html(article: RawArticle) -> str:
    title = html.escape(article.title)
    byline_html = ""
    if article.author:
        byline_html = f'<p class="byline">By {html.escape(article.author)}</p>'
    elif article.publisher:
        byline_html = f'<p class="byline">{html.escape(article.publisher)}</p>'

    canonical_attr = html.escape(article.canonical_url, quote=True)
    canonical_text = html.escape(article.canonical_url)
    lang_attr = html.escape(article.language, quote=True)

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
        f"{article.body_html}\n"
        "<hr/>\n"
        f'<p class="source"><a href="{canonical_attr}">{canonical_text}</a></p>\n'
        "</body>\n"
        "</html>\n"
    )


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")
