"""Tests for the pipeline's post-record ack hook (duck-typed source.ack)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

import pytest

from pulpwise import pipeline
from pulpwise.config import Config, Subscription
from pulpwise.models import FetchError, ItemRef, RawArticle, ReaderSubmission
from pulpwise.sinks.readwise import ReadwiseSink
from pulpwise.sources.base import Source
from pulpwise.state import connect

MAILBOX = "https://mail.example/box"

SUB = Subscription(name="fake", source="fake-acking", url=MAILBOX)


class AckingSource(Source):
    """Fake fetching source that records event ordering across fetch/ack."""

    name: ClassVar[str] = "fake-acking"
    fetch_needed: ClassVar[bool] = True

    def __init__(
        self,
        urls: list[str],
        fail_urls: set[str] | None = None,
        ack_raises: bool = False,
    ) -> None:
        super().__init__(client=None)
        self.urls = urls
        self.fail_urls = fail_urls or set()
        self.ack_raises = ack_raises
        self.events: list[tuple[str, str]] = []

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return [ItemRef(url=u, title=u) for u in self.urls]

    def discover_backwards(self, target_url: str) -> Iterable[ItemRef]:
        return self.discover(target_url)

    def fetch(self, ref: ItemRef) -> RawArticle:
        if ref.url in self.fail_urls:
            raise FetchError(f"boom {ref.url}")
        self.events.append(("fetch", ref.url))
        return RawArticle(
            title=ref.url.rsplit("/", 1)[-1],
            body_html="<p>x</p>",
            canonical_url=ref.url,
            source_url=MAILBOX,
            content_gated=True,
        )

    def ack(self, ref: ItemRef) -> None:
        if self.ack_raises:
            raise RuntimeError("ack exploded")
        self.events.append(("ack", ref.url))


def _run(source: Source, sink: ReadwiseSink) -> pipeline.SyncReport:
    cfg = Config(subscriptions=(SUB,))
    with connect() as conn:
        return pipeline._sync_with_source(SUB, cfg, conn, source, sink, None)


def test_ack_runs_after_record_per_item(readwise_sink: ReadwiseSink) -> None:
    source = AckingSource(["https://mail.example/a", "https://mail.example/b"])
    report = _run(source, readwise_sink)

    assert report.new_items == 2
    assert source.events == [
        ("fetch", "https://mail.example/a"),
        ("ack", "https://mail.example/a"),
        ("fetch", "https://mail.example/b"),
        ("ack", "https://mail.example/b"),
    ]


def test_push_precedes_record_precedes_ack(
    readwise_sink: ReadwiseSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash-safety contract: remote push, then ledger write, then ack."""
    source = AckingSource(["https://mail.example/a"])
    events = source.events  # share one list so ordering is globally visible

    original_push = readwise_sink.push

    def spy_push(submission: ReaderSubmission, **kwargs: object) -> object:
        events.append(("push", submission.url))
        return original_push(submission, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(readwise_sink, "push", spy_push)

    from pulpwise.state import record_item as original_record

    def spy_record(conn: object, item: object) -> None:
        events.append(("record", item.canonical_url))  # type: ignore[attr-defined]
        original_record(conn, item)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline, "record_item", spy_record)

    report = _run(source, readwise_sink)

    assert report.new_items == 1
    assert events == [
        ("fetch", "https://mail.example/a"),
        ("push", "https://mail.example/a"),
        ("record", "https://mail.example/a"),
        ("ack", "https://mail.example/a"),
    ]


def test_ack_runs_on_ledger_skips(readwise_sink: ReadwiseSink) -> None:
    _run(AckingSource(["https://mail.example/a"]), readwise_sink)

    second = AckingSource(["https://mail.example/a"])
    report = _run(second, readwise_sink)

    assert report.skipped == 1
    assert second.events == [("ack", "https://mail.example/a")]  # no fetch, still acked


def test_ack_not_called_for_failed_items(readwise_sink: ReadwiseSink) -> None:
    source = AckingSource(
        ["https://mail.example/a", "https://mail.example/b"],
        fail_urls={"https://mail.example/b"},
    )
    report = _run(source, readwise_sink)

    assert report.new_items == 1
    assert report.errors == 1
    assert ("ack", "https://mail.example/b") not in source.events


def test_ack_failure_does_not_fail_the_item(readwise_sink: ReadwiseSink) -> None:
    source = AckingSource(["https://mail.example/a"], ack_raises=True)
    report = _run(source, readwise_sink)

    assert report.new_items == 1
    assert report.errors == 0


def test_backfill_acks_on_record_and_skip(readwise_sink: ReadwiseSink) -> None:
    first = AckingSource(["https://mail.example/a", "https://mail.example/b"])
    with connect() as conn:
        report = pipeline._backfill_with_source(
            SUB, conn, first, readwise_sink, max_new=None, since_iso=None
        )
    assert report.new_items == 2
    assert ("ack", "https://mail.example/a") in first.events
    assert ("ack", "https://mail.example/b") in first.events

    # Second walk: both items are ledger-skips but still acked (heal path).
    second = AckingSource(["https://mail.example/a", "https://mail.example/b"])
    with connect() as conn:
        report2 = pipeline._backfill_with_source(
            SUB, conn, second, readwise_sink, max_new=None, since_iso=None
        )
    assert report2.skipped_already_ingested == 2
    assert second.events == [
        ("ack", "https://mail.example/a"),
        ("ack", "https://mail.example/b"),
    ]


def test_sources_without_ack_are_untouched(readwise_sink: ReadwiseSink) -> None:
    class PlainSource(Source):
        name: ClassVar[str] = "fake-plain"
        fetch_needed: ClassVar[bool] = False

        def discover(self, target_url: str) -> Iterable[ItemRef]:
            return [ItemRef(url="https://mail.example/a", title="a")]

        def fetch(self, ref: ItemRef) -> RawArticle:
            raise NotImplementedError

    report = _run(PlainSource(client=None), readwise_sink)
    assert report.new_items == 1
