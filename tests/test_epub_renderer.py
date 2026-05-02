"""Tests for EpubRenderer - render then read back to verify Dublin Core metadata."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ebooklib import epub

from pulpline.models import RawArticle
from pulpline.renderers.epub import EpubRenderer


def _render_and_parse(article: RawArticle, tmp_path: Path) -> epub.EpubBook:
    bytes_ = EpubRenderer().render(article)
    out = tmp_path / "out.epub"
    out.write_bytes(bytes_)
    return epub.read_epub(str(out))


def _dc_first(book: epub.EpubBook, name: str) -> str | None:
    items = book.get_metadata("DC", name)
    if not items:
        return None
    value = items[0][0]
    return None if value is None else str(value)


def test_render_round_trip_preserves_core_metadata(tmp_path: Path) -> None:
    article = RawArticle(
        title="The End of the Beginning",
        body_html="<p>Some body content for the chapter.</p>",
        canonical_url="https://stratechery.com/2026/the-end-of-the-beginning/",
        source_url="direct",
        author="Ben Thompson",
        publisher="Stratechery",
        pub_date=datetime(2026, 4, 28),
        language="en",
    )

    book = _render_and_parse(article, tmp_path)

    assert _dc_first(book, "title") == article.title
    assert _dc_first(book, "publisher") == "Stratechery"
    assert _dc_first(book, "source") == "direct"
    assert _dc_first(book, "date") == "2026-04-28"
    creators = book.get_metadata("DC", "creator")
    assert any("Ben Thompson" in c[0] for c in creators)


def test_render_uses_subscription_tag_when_set(tmp_path: Path) -> None:
    article = RawArticle(
        title="Title",
        body_html="<p>Body content.</p>",
        canonical_url="https://example.com/x",
        source_url="https://example.com/feed",
        subscription_name="example",
    )
    book = _render_and_parse(article, tmp_path)
    assert _dc_first(book, "subject") == "pulpline:example"


def test_render_uses_once_tag_when_no_subscription(tmp_path: Path) -> None:
    article = RawArticle(
        title="Title",
        body_html="<p>Body content.</p>",
        canonical_url="https://example.com/x",
        source_url="direct",
    )
    book = _render_and_parse(article, tmp_path)
    assert _dc_first(book, "subject") == "pulpline:once"


def test_render_escapes_html_in_title(tmp_path: Path) -> None:
    article = RawArticle(
        title='Article with <script>alert("xss")</script> in title',
        body_html="<p>Body.</p>",
        canonical_url="https://example.com/x",
        source_url="direct",
    )
    bytes_ = EpubRenderer().render(article)
    # Verify the raw script tag does not appear unescaped in any chapter content.
    assert b"<script>alert" not in bytes_
