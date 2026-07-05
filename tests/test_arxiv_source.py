"""Tests for ArXivSource - mocked Atom API, PDF submission mapping."""

from __future__ import annotations

import httpx
import pytest

from pulpwise.models import ExtractionError, FetchError, ItemRef
from pulpwise.sources.arxiv import (
    ArXivSource,
    _paper_id_from_url,
    _pdf_url_from_abs,
)
from tests.conftest import FIXTURES


def _atom() -> bytes:
    return (FIXTURES / "sample_arxiv.xml").read_bytes()


def _client(routes: dict[str, bytes | str]) -> httpx.Client:
    """MockTransport client that returns bytes/strings keyed by URL (without query)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        full = str(request.url)
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


def test_is_subscribable_only_for_api_query_urls() -> None:
    assert ArXivSource.is_subscribable("http://export.arxiv.org/api/query?search_query=cat:cs.AI")
    assert not ArXivSource.is_subscribable("https://arxiv.org/abs/2401.12345")
    assert not ArXivSource.is_subscribable("https://arxiv.org/pdf/2401.12345")


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


def test_arxiv_is_a_no_fetch_source() -> None:
    assert ArXivSource.fetch_needed is False


def test_fetch_raises_not_implemented() -> None:
    with (
        ArXivSource(client=httpx.Client()) as source,
        pytest.raises(NotImplementedError, match="bare-URL saves"),
    ):
        source.fetch(ItemRef(url="http://arxiv.org/abs/2401.12345v1"))


def test_submission_for_ref_maps_abs_to_pdf_with_category() -> None:
    """The abs page is just the abstract; Reader gets the PDF URL plus an
    explicit category hint (arXiv PDF URLs carry no .pdf suffix)."""
    from datetime import UTC, datetime

    ref = ItemRef(
        url="http://arxiv.org/abs/2401.12345v1",
        title="Attention is All You Really Need",
        pub_date=datetime(2024, 1, 15, tzinfo=UTC),
    )
    with ArXivSource(client=httpx.Client()) as source:
        submission = source.submission_for_ref(ref)

    assert submission.url == "http://arxiv.org/pdf/2401.12345v1"
    assert submission.category == "pdf"
    assert submission.html is None
    assert submission.kind == "url"
    assert submission.title == ref.title
    assert submission.pub_date == ref.pub_date


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
    by_url = {r.url: r for r in refs}
    first = by_url["http://arxiv.org/abs/2401.12345v1"]
    assert first.title == "Attention is All You Really Need"
    assert first.pub_date is not None
    assert first.pub_date.year == 2024


def test_discover_one_shot_paper_url_fetches_metadata() -> None:
    """`pulp add https://arxiv.org/abs/<id>` does one metadata round-trip via
    the id_list API so the ledger + Reader get a clean title and date."""
    paper_url = "https://arxiv.org/abs/2401.12345"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/api/query":
            return httpx.Response(200, content=_atom())
        return httpx.Response(404, text=f"unmocked: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with ArXivSource(client=client) as source:
        refs = list(source.discover(paper_url))

    assert requested == ["http://export.arxiv.org/api/query?id_list=2401.12345"]
    assert len(refs) == 1
    assert refs[0].url == "http://arxiv.org/abs/2401.12345v1"  # entry link, versioned
    assert refs[0].title == "Attention is All You Really Need"
    assert refs[0].pub_date is not None


def test_discover_unsupported_url_raises() -> None:
    with ArXivSource(client=httpx.Client()) as source, pytest.raises(ExtractionError):
        list(source.discover("https://arxiv.org/about"))


def test_discover_query_failure_raises_fetch_error() -> None:
    client = _client({})  # 404
    with ArXivSource(client=client) as source, pytest.raises(FetchError):
        list(source.discover("http://export.arxiv.org/api/query?x=y"))


def test_default_subscription_name_from_query() -> None:
    url = "http://export.arxiv.org/api/query?search_query=cat:cs.AI"
    assert ArXivSource.default_subscription_name(url) == "arxiv-cat-cs-ai"
    assert ArXivSource.default_subscription_name("http://export.arxiv.org/api/query") == "arxiv"
