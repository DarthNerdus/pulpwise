"""Tests for pipeline.add_once - real sources + a real ReadwiseSink over MockTransport."""

from __future__ import annotations

from pathlib import Path

import httpx

from pulpwise import pipeline
from pulpwise.config import Config
from pulpwise.sinks.readwise import ReadwiseSink
from pulpwise.sinks.shiori import ShioriSink
from pulpwise.state import connect, is_seen
from pulpwise.util.dedup import dedup_key
from tests.conftest import FIXTURES, FakeReadwise, FakeShiori

URL = "https://example.com/article"


def test_add_once_pushes_url_and_records_ledger(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    result = pipeline.add_once(URL, config=Config(), sink=readwise_sink)

    assert result.deduped is False
    assert result.already_in_readwise is False
    assert result.reader_url == "https://read.readwise.io/read/doc-1"

    # URLSource is the fallback: a bare-URL save, no html.
    payload = fake_readwise.save_payloads[0]
    assert payload["url"] == URL
    assert "html" not in payload

    with connect() as conn:
        assert is_seen(conn, dedup_key(URL)) == "https://read.readwise.io/read/doc-1"
        row = conn.execute("SELECT * FROM items").fetchone()
        assert row["subscription_name"] is None
        assert row["readwise_id"] == "doc-1"
        assert row["submission_kind"] == "url"


def test_add_once_second_run_dedupes_without_pushing(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    first = pipeline.add_once(URL, config=Config(), sink=readwise_sink)
    second = pipeline.add_once(URL, config=Config(), sink=readwise_sink)

    assert second.deduped is True
    assert second.reader_url == first.reader_url
    assert len(fake_readwise.save_payloads) == 1  # nothing was re-pushed


def test_add_once_reports_already_in_readwise(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    """Reader already knows the URL (pushed from another device, say): the
    save answers 200 and the result reports it, but it is not a ledger dedup."""
    fake_readwise.documents[URL] = "doc-77"

    result = pipeline.add_once(URL, config=Config(), sink=readwise_sink)

    assert result.deduped is False
    assert result.already_in_readwise is True
    assert result.reader_url == fake_readwise.reader_url("doc-77")


def test_add_once_passes_location_and_tags(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    pipeline.add_once(URL, config=Config(), sink=readwise_sink, location="later", tags=("essays",))

    payload = fake_readwise.save_payloads[0]
    assert payload["location"] == "later"
    assert payload["tags"] == ["essays"]


def test_add_once_defaults_to_feed_location(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    pipeline.add_once(URL, config=Config(), sink=readwise_sink)

    assert fake_readwise.save_payloads[0]["location"] == "feed"


def test_add_once_shiori_saves_only_original_url_and_records_destination(
    fake_shiori: FakeShiori,
    shiori_sink: ShioriSink,
) -> None:
    result = pipeline.add_once(
        URL,
        config=Config(),
        sink=shiori_sink,
        location="shiori",
    )

    assert result.reader_url == URL
    assert result.already_in_readwise is False
    assert fake_shiori.save_payloads == [{"url": URL}]
    with connect() as conn:
        row = conn.execute(
            "SELECT canonical_url, destination, readwise_id, submission_kind FROM items"
        ).fetchone()
        assert row["canonical_url"] == URL
        assert row["destination"] == "shiori"
        assert row["readwise_id"] == "link-1"
        assert row["submission_kind"] == "url"


def test_add_once_can_save_same_url_to_both_destinations(
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
    fake_shiori: FakeShiori,
    shiori_sink: ShioriSink,
) -> None:
    pipeline.add_once(URL, config=Config(), sink=readwise_sink)
    pipeline.add_once(URL, config=Config(), sink=shiori_sink, location="shiori")

    assert len(fake_readwise.save_payloads) == 1
    assert len(fake_shiori.save_payloads) == 1
    with connect() as conn:
        rows = conn.execute("SELECT destination FROM items ORDER BY id").fetchall()
        assert [row["destination"] for row in rows] == ["readwise", "shiori"]


def test_add_once_routes_arxiv_urls_to_pdf_submission(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    """arxiv.org/abs/... is claimed by ArXivSource: one metadata round-trip,
    then a bare-URL save of the /pdf/ URL with the pdf category hint."""
    atom = (FIXTURES / "sample_arxiv.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org" and request.url.path == "/api/query":
            return httpx.Response(200, content=atom)
        return httpx.Response(404, text=f"unmocked: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = pipeline.add_once(
        "https://arxiv.org/abs/2401.12345", client=client, config=Config(), sink=readwise_sink
    )

    assert result.deduped is False
    payload = fake_readwise.save_payloads[0]
    assert payload["url"] == "http://arxiv.org/pdf/2401.12345v1"
    assert payload["category"] == "pdf"
    assert payload["title"] == "Attention is All You Really Need"
    assert "html" not in payload


def test_add_once_records_state_at_explicit_path(
    tmp_path: Path, fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    db = tmp_path / "custom-state.db"
    pipeline.add_once(URL, config=Config(), sink=readwise_sink, state_path=db)

    assert db.exists()
    with connect(db) as conn:
        assert is_seen(conn, dedup_key(URL)) is not None
