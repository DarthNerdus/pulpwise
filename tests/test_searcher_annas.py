"""Tests for AnnaSearcher - parser fixture + mirror fail-over with mocked httpx."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from pulpline.models import FetchError
from pulpline.searchers.annas import (
    DEFAULT_MIRRORS,
    AnnaSearcher,
    _content_param,
    _looks_like_block,
    _parse_results,
    _split_meta,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture() -> str:
    return (FIXTURES / "annas_search_books.html").read_text(encoding="utf-8")


def test_parse_results_yields_one_per_unique_md5() -> None:
    """The duplicate title-text link with the same href must not yield a 2nd row."""
    results = list(_parse_results(_load_fixture(), "https://annas-archive.li"))
    md5s = [r.target_url.rsplit("/", 1)[-1] for r in results]
    assert md5s == [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "cccccccccccccccccccccccccccccccc",
    ]


def test_parse_results_extracts_title_and_authors() -> None:
    results = list(_parse_results(_load_fixture(), "https://annas-archive.li"))
    first = results[0]
    assert first.title == "Designing Data-Intensive Applications"
    assert first.authors == "Martin Kleppmann"
    assert first.publisher == "O'Reilly"


def test_parse_results_extracts_metadata_pipe_string() -> None:
    results = list(_parse_results(_load_fixture(), "https://annas-archive.li"))
    epub = results[0]
    assert epub.language == "en"
    assert epub.extension == "epub"
    assert epub.size == "4.2MB"
    assert epub.year == "2017"

    pdf = results[1]
    assert pdf.extension == "pdf"
    assert pdf.size == "12.3 MB"

    ru = results[2]
    assert ru.language == "ru"
    assert ru.title == "Высоконагруженные приложения"


def test_split_meta_handles_multilingual_lines() -> None:
    lang, ext, size, year = _split_meta("✅ English [en] · Hindi [hi] · EPUB · 0.7MB · 2024")
    assert lang == "en"
    assert ext == "epub"
    assert size == "0.7MB"
    assert year == "2024"


def test_content_param_aliases() -> None:
    assert _content_param("book") == "book_any"
    assert _content_param("books") == "book_any"
    assert _content_param("paper") == "journal"
    assert _content_param("article") == "journal"
    assert _content_param("comic") == "comic"
    assert _content_param(None) is None


def test_looks_like_block_detects_ddos_guard_interstitial() -> None:
    assert _looks_like_block("<html>DDoS-Guard checking your browser...</html>")
    assert _looks_like_block("<html>cloudflare please wait</html>")
    assert not _looks_like_block('<html><a href="/md5/abc">book</a> ddos-guard mention</html>')


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_returns_results_from_first_working_mirror() -> None:
    fixture_html = _load_fixture()
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.host)
        return httpx.Response(200, text=fixture_html)

    with AnnaSearcher(client=_make_client(handler)) as searcher:
        results = list(searcher.search("ddia", limit=10))

    assert len(results) == 3
    # Hits the first mirror only since it succeeds.
    assert visited[0] == f"annas-archive.{DEFAULT_MIRRORS[0]}"


def test_search_falls_over_to_next_mirror_on_http_error() -> None:
    fixture_html = _load_fixture()
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.host)
        # First mirror returns 503; second succeeds.
        if request.url.host == f"annas-archive.{DEFAULT_MIRRORS[0]}":
            return httpx.Response(503, text="<html>down</html>")
        return httpx.Response(200, text=fixture_html)

    with AnnaSearcher(client=_make_client(handler)) as searcher:
        results = list(searcher.search("ddia"))

    assert len(results) == 3
    assert len(visited) == 2


def test_search_falls_over_on_block_page() -> None:
    """A 200-OK that's actually a DDoS-Guard interstitial should fail over."""
    fixture_html = _load_fixture()
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.host)
        if request.url.host == f"annas-archive.{DEFAULT_MIRRORS[0]}":
            return httpx.Response(200, text="<html>DDoS-Guard checking your browser</html>")
        return httpx.Response(200, text=fixture_html)

    with AnnaSearcher(client=_make_client(handler)) as searcher:
        results = list(searcher.search("ddia"))

    assert len(results) == 3
    assert visited[0] == f"annas-archive.{DEFAULT_MIRRORS[0]}"


def test_search_raises_when_all_mirrors_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with AnnaSearcher(client=_make_client(handler)) as searcher, pytest.raises(FetchError):
        list(searcher.search("ddia"))


def test_search_passes_filters_in_query_string() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, text=_load_fixture())

    with AnnaSearcher(client=_make_client(handler)) as searcher:
        list(searcher.search("ddia", content="book", extension="epub", language="en"))

    url = seen_urls[0]
    assert "q=ddia" in url
    assert "content=book_any" in url
    assert "ext=epub" in url
    assert "lang=en" in url
