"""Tests for ArXivSource - mocked Atom API + PDF download."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pulpline.models import ExtractionError, FetchError, ItemRef
from pulpline.sources.arxiv import (
    ArXivSource,
    _paper_id_from_url,
    _pdf_url_from_abs,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _atom() -> bytes:
    return (FIXTURES / "sample_arxiv.xml").read_bytes()


def _client(routes: dict[str, bytes | str]) -> httpx.Client:
    """MockTransport client that returns bytes/strings keyed by URL (without query)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        full = str(request.url)
        # Match either base or full URL
        for key, payload in routes.items():
            if key in (url, full):
                if isinstance(payload, bytes):
                    return httpx.Response(200, content=payload)
                return httpx.Response(200, content=payload.encode("utf-8"))
        return httpx.Response(404, text=f"unmocked: {full}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_matches_url_claims_arxiv_hosts() -> None:
    assert ArXivSource.matches_url("https://arxiv.org/abs/2401.12345")
    assert ArXivSource.matches_url("http://export.arxiv.org/api/query?id_list=x")
    assert ArXivSource.matches_url("https://www.arxiv.org/abs/x")
    assert not ArXivSource.matches_url("https://example.com/")


def test_paper_id_from_url() -> None:
    assert _paper_id_from_url("https://arxiv.org/abs/2401.12345") == "2401.12345"
    assert _paper_id_from_url("https://arxiv.org/abs/2401.12345v2") == "2401.12345v2"
    assert _paper_id_from_url("https://arxiv.org/pdf/2401.12345.pdf") == "2401.12345"
    assert _paper_id_from_url("https://arxiv.org/pdf/2401.12345v3") == "2401.12345v3"
    assert _paper_id_from_url("https://example.com/article") is None


def test_pdf_url_from_abs() -> None:
    assert (
        _pdf_url_from_abs("http://arxiv.org/abs/2401.12345v1")
        == "http://arxiv.org/pdf/2401.12345v1"
    )


def test_discover_query_yields_itemrefs() -> None:
    api = "http://export.arxiv.org/api/query?search_query=cat:cs.AI&max_results=2"
    client = _client({"http://export.arxiv.org/api/query": _atom()})

    with ArXivSource(client=client) as source:
        refs = list(source.discover(api))

    assert len(refs) == 2
    assert {r.url for r in refs} == {
        "http://arxiv.org/abs/2401.12345v1",
        "http://arxiv.org/abs/2402.67890v2",
    }


def test_fetch_builds_article_with_abstract_and_authors() -> None:
    api = "http://export.arxiv.org/api/query?search_query=cat:cs.AI"
    client = _client({"http://export.arxiv.org/api/query": _atom()})

    with ArXivSource(client=client) as source:
        list(source.discover(api))
        article = source.fetch(ItemRef(url="http://arxiv.org/abs/2401.12345v1"))

    assert article.title == "Attention is All You Really Need"
    assert article.publisher == "arXiv"
    assert article.author == "Jane Researcher, John Coauthor"
    assert "Transformer-Plus" in article.body_html
    assert article.pub_date is not None
    assert article.pub_date.year == 2024


def test_render_downloads_pdf() -> None:
    fake_pdf = b"%PDF-1.7\n..."
    pdf_url = "http://arxiv.org/pdf/2401.12345v1"
    api = "http://export.arxiv.org/api/query"
    client = _client({api: _atom(), pdf_url: fake_pdf})

    with ArXivSource(client=client) as source:
        list(source.discover(f"{api}?search_query=cat:cs.AI"))
        article = source.fetch(ItemRef(url="http://arxiv.org/abs/2401.12345v1"))
        content = source.render(article)

    assert content == fake_pdf
    assert content.startswith(b"%PDF")


def test_render_rejects_non_pdf_response() -> None:
    not_pdf = b"<html>error</html>"
    pdf_url = "http://arxiv.org/pdf/2401.12345v1"
    api = "http://export.arxiv.org/api/query"
    client = _client({api: _atom(), pdf_url: not_pdf})

    with ArXivSource(client=client) as source:
        list(source.discover(f"{api}?search_query=cat:cs.AI"))
        article = source.fetch(ItemRef(url="http://arxiv.org/abs/2401.12345v1"))
        with pytest.raises(ExtractionError, match="did not return a PDF"):
            source.render(article)


def test_discover_one_shot_paper_url_fetches_metadata() -> None:
    """`pulp add https://arxiv.org/abs/<id>` pre-fetches metadata via id_list API."""
    paper_url = "https://arxiv.org/abs/2401.12345"
    api = "http://export.arxiv.org/api/query"
    client = _client({api: _atom()})

    with ArXivSource(client=client) as source:
        refs = list(source.discover(paper_url))

    assert len(refs) == 1
    assert refs[0].title == "Attention is All You Really Need"


def test_discover_unsupported_url_raises() -> None:
    with ArXivSource(client=httpx.Client()) as source, pytest.raises(ExtractionError):
        list(source.discover("https://arxiv.org/about"))


def test_discover_query_failure_raises_fetch_error() -> None:
    client = _client({})  # 404
    with ArXivSource(client=client) as source, pytest.raises(FetchError):
        list(source.discover("http://export.arxiv.org/api/query?x=y"))
