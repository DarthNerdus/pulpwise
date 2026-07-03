"""Tests for the pipeline's post-record ack hook (duck-typed source.ack)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from pulpline import pipeline
from pulpline.config import Config, Subscription
from pulpline.models import FetchError, ItemRef, RawArticle
from pulpline.sources.base import Source
from pulpline.state import connect


class AckingSource(Source):
    """Fake source that records event ordering across fetch/render/ack."""

    name: ClassVar[str] = "fake-acking"

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
            source_url=target_url_placeholder,
        )

    def render(self, article: RawArticle) -> bytes:
        return b"bytes"

    def ack(self, ref: ItemRef) -> None:
        if self.ack_raises:
            raise RuntimeError("ack exploded")
        self.events.append(("ack", ref.url))


target_url_placeholder = "fake://mailbox"


def _sub(out: Path) -> Subscription:
    return Subscription(
        name="fake", source="fake-acking", url=target_url_placeholder, output_dir=str(out)
    )


def _run(source: AckingSource, out: Path) -> pipeline.SyncReport:
    cfg = Config(subscriptions=(_sub(out),))
    with connect() as conn:
        return pipeline._sync_with_source(_sub(out), cfg, conn, source, progress=None)


def test_ack_runs_after_record_per_item(tmp_path: Path) -> None:
    source = AckingSource(["fake://a", "fake://b"])
    report = _run(source, tmp_path / "out")

    assert report.new_items == 2
    assert source.events == [
        ("fetch", "fake://a"),
        ("ack", "fake://a"),
        ("fetch", "fake://b"),
        ("ack", "fake://b"),
    ]


def test_ack_runs_on_ledger_skips(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(AckingSource(["fake://a"]), out)

    second = AckingSource(["fake://a"])
    report = _run(second, out)

    assert report.skipped == 1
    assert second.events == [("ack", "fake://a")]  # no fetch, still acked


def test_ack_not_called_for_failed_items(tmp_path: Path) -> None:
    source = AckingSource(["fake://a", "fake://b"], fail_urls={"fake://b"})
    report = _run(source, tmp_path / "out")

    assert report.new_items == 1
    assert report.errors == 1
    assert ("ack", "fake://b") not in source.events


def test_ack_failure_does_not_fail_the_item(tmp_path: Path) -> None:
    source = AckingSource(["fake://a"], ack_raises=True)
    report = _run(source, tmp_path / "out")

    assert report.new_items == 1
    assert report.errors == 0


def test_backfill_acks_on_record_and_skip(tmp_path: Path) -> None:
    out = tmp_path / "out"
    cfg = Config(subscriptions=(_sub(out),))

    first = AckingSource(["fake://a", "fake://b"])
    with connect() as conn:
        report = pipeline._backfill_with_source(
            _sub(out), cfg, conn, first, max_new=None, since_iso=None
        )
    assert report.new_items == 2
    assert ("ack", "fake://a") in first.events
    assert ("ack", "fake://b") in first.events

    # Second walk: both items are ledger-skips but still acked (heal path).
    second = AckingSource(["fake://a", "fake://b"])
    with connect() as conn:
        report2 = pipeline._backfill_with_source(
            _sub(out), cfg, conn, second, max_new=None, since_iso=None
        )
    assert report2.skipped_already_ingested == 2
    assert second.events == [("ack", "fake://a"), ("ack", "fake://b")]


def test_sources_without_ack_are_untouched(tmp_path: Path) -> None:
    class PlainSource(Source):
        name: ClassVar[str] = "fake-plain"

        def discover(self, target_url: str) -> Iterable[ItemRef]:
            return [ItemRef(url="fake://a", title="a")]

        def fetch(self, ref: ItemRef) -> RawArticle:
            return RawArticle(
                title="a",
                body_html="<p>x</p>",
                canonical_url=ref.url,
                source_url=target_url_placeholder,
            )

        def render(self, article: RawArticle) -> bytes:
            return b"bytes"

    cfg = Config(subscriptions=(_sub(tmp_path / "out"),))
    with connect() as conn:
        report = pipeline._sync_with_source(
            _sub(tmp_path / "out"), cfg, conn, PlainSource(client=None), progress=None
        )
    assert report.new_items == 1
