"""Tests for MangaDexSource - mocked /manga, /feed, /at-home, image fetches."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from pulpline.models import ExtractionError, FetchError, ItemRef
from pulpline.sources.mangadex import (
    MangaDexSource,
    _build_cbz,
)

MANGA_ID = "11111111-2222-3333-4444-555555555555"
CHAPTER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHAPTER_ID_2 = "ffffffff-aaaa-bbbb-cccc-dddddddddddd"


def _manga_payload() -> dict[str, Any]:
    return {
        "result": "ok",
        "data": {
            "id": MANGA_ID,
            "attributes": {"title": {"en": "Berserk"}},
        },
    }


def _feed_payload() -> dict[str, Any]:
    return {
        "result": "ok",
        "data": [
            {
                "id": CHAPTER_ID,
                "attributes": {
                    "chapter": "357",
                    "title": "Final Fight",
                    "translatedLanguage": "en",
                    "publishAt": "2026-04-30T12:00:00+00:00",
                },
            },
            {
                "id": CHAPTER_ID_2,
                "attributes": {
                    "chapter": "356",
                    "title": "",
                    "translatedLanguage": "en",
                    "publishAt": "2026-04-15T12:00:00+00:00",
                },
            },
        ],
    }


def _at_home_payload() -> dict[str, Any]:
    return {
        "result": "ok",
        "baseUrl": "https://uploads.mangadex.org",
        "chapter": {
            "hash": "abc123hash",
            "data": ["1.png", "2.png", "3.png"],
            "dataSaver": ["1.jpg", "2.jpg", "3.jpg"],
        },
    }


def _routed_client(routes: Mapping[str, object]) -> httpx.Client:
    """MockTransport: returns JSON dicts as JSON, bytes as raw bytes."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        # Match either with-query or stripped; try query-stripped first.
        for key, payload in routes.items():
            if key == url or key == str(request.url):
                if isinstance(payload, bytes):
                    return httpx.Response(200, content=payload)
                if isinstance(payload, dict):
                    return httpx.Response(200, json=payload)
        return httpx.Response(404, text=f"unmocked: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_matches_url_claims_mangadex_hosts() -> None:
    assert MangaDexSource.matches_url(f"https://mangadex.org/title/{MANGA_ID}/berserk")
    assert MangaDexSource.matches_url(f"https://mangadex.org/chapter/{CHAPTER_ID}")
    assert MangaDexSource.matches_url("https://api.mangadex.org/manga")
    assert not MangaDexSource.matches_url("https://example.com/title/x")


def test_is_subscribable_distinguishes_title_vs_chapter() -> None:
    """Title pages subscribe; chapter pages one-shot."""
    assert MangaDexSource.is_subscribable(f"https://mangadex.org/title/{MANGA_ID}/berserk")
    assert MangaDexSource.is_subscribable(f"https://mangadex.org/title/{MANGA_ID}")
    assert not MangaDexSource.is_subscribable(f"https://mangadex.org/chapter/{CHAPTER_ID}")
    assert not MangaDexSource.is_subscribable("https://mangadex.org/about")


def test_discover_title_url_yields_chapters_from_feed() -> None:
    routes = {
        f"https://api.mangadex.org/manga/{MANGA_ID}": _manga_payload(),
        f"https://api.mangadex.org/manga/{MANGA_ID}/feed": _feed_payload(),
    }
    client = _routed_client(routes)

    with MangaDexSource(client=client) as source:
        refs = list(source.discover(f"https://mangadex.org/title/{MANGA_ID}/berserk"))

    assert len(refs) == 2
    titles = {r.title for r in refs}
    assert "Berserk - Ch. 357 - Final Fight" in titles
    assert "Berserk - Ch. 356" in titles


def test_discover_chapter_url_yields_single_ref() -> None:
    chapter_payload = {
        "result": "ok",
        "data": {
            "id": CHAPTER_ID,
            "attributes": {
                "chapter": "357",
                "title": "Final Fight",
                "translatedLanguage": "en",
                "publishAt": "2026-04-30T12:00:00Z",
            },
            "relationships": [{"id": MANGA_ID, "type": "manga"}],
        },
    }
    routes = {
        f"https://api.mangadex.org/chapter/{CHAPTER_ID}": chapter_payload,
        f"https://api.mangadex.org/manga/{MANGA_ID}": _manga_payload(),
    }
    client = _routed_client(routes)

    with MangaDexSource(client=client) as source:
        refs = list(source.discover(f"https://mangadex.org/chapter/{CHAPTER_ID}"))

    assert len(refs) == 1
    assert refs[0].url == f"https://mangadex.org/chapter/{CHAPTER_ID}"
    assert refs[0].title == "Berserk - Ch. 357 - Final Fight"


def test_discover_unsupported_url_raises() -> None:
    with (
        MangaDexSource(client=httpx.Client()) as source,
        pytest.raises(ExtractionError, match="unsupported"),
    ):
        list(source.discover("https://mangadex.org/about"))


def test_fetch_returns_rawarticle() -> None:
    routes = {
        f"https://api.mangadex.org/manga/{MANGA_ID}": _manga_payload(),
        f"https://api.mangadex.org/manga/{MANGA_ID}/feed": _feed_payload(),
    }
    client = _routed_client(routes)

    with MangaDexSource(client=client) as source:
        list(source.discover(f"https://mangadex.org/title/{MANGA_ID}/berserk"))
        article = source.fetch(ItemRef(url=f"https://mangadex.org/chapter/{CHAPTER_ID}"))

    assert article.title.startswith("Berserk - Ch. 357")
    assert article.publisher == "MangaDex"
    assert article.body_html == ""  # CBZ has no body html
    assert article.pub_date is not None
    assert article.pub_date.year == 2026


def test_render_builds_cbz_from_at_home_pages() -> None:
    fake_image = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    routes: dict[str, object] = {
        f"https://api.mangadex.org/manga/{MANGA_ID}": _manga_payload(),
        f"https://api.mangadex.org/manga/{MANGA_ID}/feed": _feed_payload(),
        f"https://api.mangadex.org/at-home/server/{CHAPTER_ID}": _at_home_payload(),
        "https://uploads.mangadex.org/data/abc123hash/1.png": fake_image,
        "https://uploads.mangadex.org/data/abc123hash/2.png": fake_image,
        "https://uploads.mangadex.org/data/abc123hash/3.png": fake_image,
    }
    client = _routed_client(routes)

    with MangaDexSource(client=client) as source:
        list(source.discover(f"https://mangadex.org/title/{MANGA_ID}/berserk"))
        article = source.fetch(ItemRef(url=f"https://mangadex.org/chapter/{CHAPTER_ID}"))
        cbz = source.render(article)

    # Verify it's a valid zip with three pages, named for lex-sortable order.
    with zipfile.ZipFile(io.BytesIO(cbz)) as zf:
        names = zf.namelist()
    assert names == ["001.png", "002.png", "003.png"]


def test_render_raises_when_at_home_missing_pages() -> None:
    routes = {
        f"https://api.mangadex.org/manga/{MANGA_ID}": _manga_payload(),
        f"https://api.mangadex.org/manga/{MANGA_ID}/feed": _feed_payload(),
        f"https://api.mangadex.org/at-home/server/{CHAPTER_ID}": {
            "result": "ok",
            "baseUrl": "https://uploads.mangadex.org",
            "chapter": {"hash": "h", "data": []},
        },
    }
    client = _routed_client(routes)

    with MangaDexSource(client=client) as source:
        list(source.discover(f"https://mangadex.org/title/{MANGA_ID}/berserk"))
        article = source.fetch(ItemRef(url=f"https://mangadex.org/chapter/{CHAPTER_ID}"))
        with pytest.raises(ExtractionError, match="missing pages"):
            source.render(article)


def test_render_raises_on_image_fetch_failure() -> None:
    routes: dict[str, object] = {
        f"https://api.mangadex.org/manga/{MANGA_ID}": _manga_payload(),
        f"https://api.mangadex.org/manga/{MANGA_ID}/feed": _feed_payload(),
        f"https://api.mangadex.org/at-home/server/{CHAPTER_ID}": _at_home_payload(),
        # No image routes -> 404
    }
    client = _routed_client(routes)

    with MangaDexSource(client=client) as source:
        list(source.discover(f"https://mangadex.org/title/{MANGA_ID}/berserk"))
        article = source.fetch(ItemRef(url=f"https://mangadex.org/chapter/{CHAPTER_ID}"))
        with pytest.raises(FetchError):
            source.render(article)


def test_from_config_uses_subscription_language() -> None:
    from pulpline.config import Config, Subscription

    cfg = Config()
    sub = Subscription(name="berserk", source="mangadex", url="x", options={"language": "ru"})
    source = MangaDexSource.from_config(cfg, subscription=sub)
    assert source._language == "ru"


def test_from_config_falls_back_to_english_when_no_language() -> None:
    from pulpline.config import Config

    cfg = Config()
    source = MangaDexSource.from_config(cfg)
    assert source._language == "en"


def test_discover_passes_language_to_feed_query() -> None:
    """Subscribing with language='ja' filters /feed by translatedLanguage[]=ja."""
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        # Capture the translatedLanguage[] query parameter value.
        for k, v in request.url.params.multi_items():
            if k == "translatedLanguage[]":
                seen_params["lang"] = v
        if url == f"https://api.mangadex.org/manga/{MANGA_ID}":
            return httpx.Response(200, json=_manga_payload())
        if url == f"https://api.mangadex.org/manga/{MANGA_ID}/feed":
            return httpx.Response(200, json={"result": "ok", "data": []})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with MangaDexSource(client=client, language="ja") as source:
        list(source.discover(f"https://mangadex.org/title/{MANGA_ID}/whatever"))

    assert seen_params.get("lang") == "ja"


def test_build_cbz_directly() -> None:
    pages = [("a.png", b"AAA"), ("b.jpg", b"BBB")]
    cbz = _build_cbz(pages)
    with zipfile.ZipFile(io.BytesIO(cbz)) as zf:
        assert zf.namelist() == ["001.png", "002.jpg"]
        assert zf.read("001.png") == b"AAA"
        assert zf.read("002.jpg") == b"BBB"
