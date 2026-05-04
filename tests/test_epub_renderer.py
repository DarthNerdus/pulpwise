"""Tests for EpubRenderer - render then read back to verify Dublin Core metadata."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
from ebooklib import ITEM_IMAGE, epub

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


# Minimal valid PNG (1x1 transparent pixel) for image-embed tests.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000148f9be9c0000000049454e44"
    "ae426082"
)


def _image_client(routes: dict[str, tuple[bytes, str]]) -> httpx.Client:
    """Mock httpx that returns canned image bytes per URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in routes:
            content, ct = routes[url]
            return httpx.Response(200, content=content, headers={"content-type": ct})
        return httpx.Response(404, text="not found")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_render_embeds_remote_images_into_epub(tmp_path: Path) -> None:
    body = (
        "<p>Intro text.</p>"
        '<img src="https://substackcdn.com/image/abc.png" '
        'srcset="https://substackcdn.com/image/abc-2x.png 2x"/>'
        "<p>More text.</p>"
        '<img src="https://substackcdn.com/image/def.jpg"/>'
    )
    routes = {
        "https://substackcdn.com/image/abc.png": (_TINY_PNG, "image/png"),
        "https://substackcdn.com/image/def.jpg": (b"\xff\xd8\xff\xd9", "image/jpeg"),
    }
    article = RawArticle(
        title="Article With Images",
        body_html=body,
        canonical_url="https://example.com/post",
        source_url="direct",
    )

    renderer = EpubRenderer(image_client=_image_client(routes))
    out_bytes = renderer.render(article)
    out_path = tmp_path / "out.epub"
    out_path.write_bytes(out_bytes)
    book = epub.read_epub(str(out_path))

    images = [item for item in book.get_items_of_type(ITEM_IMAGE)]
    assert len(images) == 2

    # Body HTML inside the chapter should reference local image paths,
    # not the remote URLs, and srcset should be stripped.
    chapter = next(c for c in book.get_items() if c.get_name().endswith(".xhtml"))
    chapter_text = chapter.get_content().decode("utf-8")
    assert "images/img001.png" in chapter_text
    assert "images/img002.jpg" in chapter_text
    assert "substackcdn.com/image/abc.png" not in chapter_text
    assert "srcset" not in chapter_text


def test_render_dedupes_repeated_image_urls(tmp_path: Path) -> None:
    body = (
        '<img src="https://example.com/x.png"/>'
        '<img src="https://example.com/x.png"/>'
        '<img src="https://example.com/x.png"/>'
    )
    routes = {"https://example.com/x.png": (_TINY_PNG, "image/png")}
    article = RawArticle(
        title="Dedupe", body_html=body, canonical_url="https://example.com", source_url="direct"
    )
    renderer = EpubRenderer(image_client=_image_client(routes))
    out = tmp_path / "out.epub"
    out.write_bytes(renderer.render(article))
    book = epub.read_epub(str(out))
    images = list(book.get_items_of_type(ITEM_IMAGE))
    assert len(images) == 1


def test_render_falls_back_when_image_fetch_fails(tmp_path: Path) -> None:
    """A 404 on one image leaves the original src so an online reader can still load it."""
    body = '<img src="https://example.com/missing.png"/><p>text</p>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    article = RawArticle(
        title="t", body_html=body, canonical_url="https://example.com", source_url="direct"
    )
    out = tmp_path / "out.epub"
    out.write_bytes(EpubRenderer(image_client=client).render(article))
    book = epub.read_epub(str(out))
    images = list(book.get_items_of_type(ITEM_IMAGE))
    assert len(images) == 0
    chapter = next(c for c in book.get_items() if c.get_name().endswith(".xhtml"))
    assert b"https://example.com/missing.png" in chapter.get_content()


def test_render_skips_data_url_images(tmp_path: Path) -> None:
    """data: URLs are inlined already - don't try to HTTP-fetch them."""
    body = '<img src="data:image/png;base64,iVBORw0KGgo="/>'
    article = RawArticle(
        title="t", body_html=body, canonical_url="https://example.com", source_url="direct"
    )
    # No client given - if we tried to fetch, we'd hit real network
    # (or fail in tests where there's no network). data: should be skipped.
    out_bytes = EpubRenderer().render(article)
    out = tmp_path / "out.epub"
    out.write_bytes(out_bytes)
    book = epub.read_epub(str(out))
    assert len(list(book.get_items_of_type(ITEM_IMAGE))) == 0
