"""Tests for pipeline.sync / pipeline.backfill against a mocked Readwise API.

Sources are either the real RSSSource over a MockTransport feed or small fake
Source classes (registered into the registry per-test); the sink is always a
real ReadwiseSink over `FakeReadwise`, so payload/status logic is exercised
end to end with zero sleeping.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import ClassVar

import httpx
import pytest

from pulpwise import pipeline
from pulpwise.config import Config, Subscription
from pulpwise.models import ItemRef, Paywalled, RawArticle
from pulpwise.sinks.readwise import ReadwiseAuthError, ReadwiseSink
from pulpwise.sinks.shiori import ShioriSink
from pulpwise.sources import REGISTRY
from pulpwise.sources.base import Source
from pulpwise.state import connect, get_subscription_state, is_seen
from pulpwise.util.dedup import dedup_key
from tests.conftest import FIXTURES, FakeReadwise, FakeShiori

ClientFactory = Callable[[dict[str, str]], httpx.Client]

FEED_URL = "https://example.com/feed"


def _feed() -> str:
    return (FIXTURES / "sample_feed.xml").read_text()


def _rss_config(options: dict[str, str | int] | None = None) -> Config:
    return Config(
        subscriptions=(
            Subscription(name="test", source="rss", url=FEED_URL, options=options or {}),
        )
    )


# ---- fake sources ---------------------------------------------------------------


class StaticSource(Source):
    """fetch_needed=False source: three bare-URL refs derived from the target."""

    name: ClassVar[str] = "fake-static"
    fetch_needed: ClassVar[bool] = False

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return [ItemRef(url=f"{target_url}/post-{i}", title=f"Post {i}") for i in range(3)]

    def fetch(self, ref: ItemRef) -> RawArticle:
        raise NotImplementedError


class GatedSource(Source):
    """Fetching source: one public post, one paywalled."""

    name: ClassVar[str] = "fake-gated"
    fetch_needed: ClassVar[bool] = True

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        published = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        return [
            ItemRef(url=f"{target_url}/free", pub_date=published),
            ItemRef(url=f"{target_url}/paid", pub_date=published),
        ]

    def fetch(self, ref: ItemRef) -> RawArticle:
        if ref.url.endswith("/paid"):
            raise Paywalled("post is paid and cookies did not work", host="pub.example.com")
        return RawArticle(
            title="Free Post",
            body_html="<p>gated-capture body</p>",
            canonical_url=ref.url,
            source_url=ref.url,
            content_gated=True,
        )


class BackSource(Source):
    """Backfillable source with canned newest-first refs."""

    name: ClassVar[str] = "fake-back"
    fetch_needed: ClassVar[bool] = False
    refs: ClassVar[tuple[ItemRef, ...]] = ()

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return list(self.refs)

    def discover_backwards(self, target_url: str) -> Iterable[ItemRef]:
        yield from self.refs

    def fetch(self, ref: ItemRef) -> RawArticle:
        raise NotImplementedError


class ShioriBackSource(Source):
    """Fetch-needed backfill source; Shiori must never call fetch()."""

    name: ClassVar[str] = "fake-shiori-back"
    fetch_needed: ClassVar[bool] = True
    refs: ClassVar[tuple[ItemRef, ...]] = ()

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return self.refs

    def discover_backwards(self, target_url: str) -> Iterable[ItemRef]:
        yield from self.refs

    def fetch(self, ref: ItemRef) -> RawArticle:
        raise AssertionError(f"Shiori must not fetch content for {ref.url}")


# ---- sync -----------------------------------------------------------------------


def test_sync_pushes_unseen_refs_and_records_reader_ids(
    mock_client_factory: ClientFactory,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    client = mock_client_factory({FEED_URL: _feed()})

    total = pipeline.sync(config=_rss_config(), client=client, sink=readwise_sink)

    report = total.reports[0]
    assert report.name == "test"
    assert (report.new_items, report.skipped, report.errors) == (2, 0, 0)

    pushed_urls = {str(p["url"]) for p in fake_readwise.save_payloads}
    assert pushed_urls == {"https://example.com/full", "https://example.com/teaser"}
    # RSS items are bare-URL saves: Reader extracts server-side.
    assert all("html" not in p for p in fake_readwise.save_payloads)

    with connect() as conn:
        for url in pushed_urls:
            assert is_seen(conn, dedup_key(url)) is not None
        rows = conn.execute("SELECT readwise_id, submission_kind FROM items").fetchall()
        assert {row["readwise_id"] for row in rows} == {"doc-1", "doc-2"}
        assert {row["submission_kind"] for row in rows} == {"url"}


def test_sync_dedups_on_second_run(
    mock_client_factory: ClientFactory,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    client = mock_client_factory({FEED_URL: _feed()})

    pipeline.sync(config=_rss_config(), client=client, sink=readwise_sink)
    second = pipeline.sync(config=_rss_config(), client=client, sink=readwise_sink)

    assert second.total_new == 0
    assert second.total_skipped == 2
    assert len(fake_readwise.save_payloads) == 2  # nothing was re-pushed


def test_sync_skips_disabled_subscriptions(
    mock_client_factory: ClientFactory,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    """Disabled subs produce no discovery and no report - not even an error one.

    The disabled sub's URL is absent from the mock transport, so any attempt
    to sync it would surface as a feed-level error report.
    """
    config = Config(
        subscriptions=(
            Subscription(name="on", source="rss", url=FEED_URL),
            Subscription(
                name="off", source="rss", url="https://unreachable.example/feed", disabled=True
            ),
        )
    )
    client = mock_client_factory({FEED_URL: _feed()})

    total = pipeline.sync(config=config, client=client, sink=readwise_sink)

    assert [r.name for r in total.reports] == ["on"]
    assert total.total_errors == 0
    assert len(fake_readwise.save_payloads) == 2  # only the enabled feed's items


def test_sync_applies_location_and_tags_options(
    mock_client_factory: ClientFactory,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    client = mock_client_factory({FEED_URL: _feed()})
    config = _rss_config(options={"location": "later", "tags": "tech, essays"})

    pipeline.sync(config=config, client=client, sink=readwise_sink)

    for payload in fake_readwise.save_payloads:
        assert payload["location"] == "later"
        assert payload["tags"] == ["tech", "essays"]


def test_sync_maps_inbox_location_alias_to_new(
    mock_client_factory: ClientFactory,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    """`location = "inbox"` in config routes saves to Reader's "new"
    location - "inbox" is what Reader's UI calls it."""
    client = mock_client_factory({FEED_URL: _feed()})
    config = _rss_config(options={"location": "inbox"})

    pipeline.sync(config=config, client=client, sink=readwise_sink)

    assert fake_readwise.save_payloads  # sanity: something was pushed
    for payload in fake_readwise.save_payloads:
        assert payload["location"] == "new"


def test_sync_routes_shiori_destination_as_url_only(
    monkeypatch: pytest.MonkeyPatch,
    fake_shiori: FakeShiori,
    shiori_sink: ShioriSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-gated", GatedSource)
    config = Config(
        subscriptions=(
            Subscription(
                name="pub",
                source="fake-gated",
                url="https://pub.example.com",
                options={"location": "shiori", "tags": "must-not-leak"},
            ),
        )
    )

    total = pipeline.sync(config=config, sink=shiori_sink)

    assert total.total_new == 2
    assert fake_shiori.save_payloads == [
        {"url": "https://pub.example.com/free"},
        {"url": "https://pub.example.com/paid"},
    ]
    with connect() as conn:
        rows = conn.execute(
            "SELECT destination, readwise_id, readwise_url, submission_kind, pub_date "
            "FROM items ORDER BY id"
        ).fetchall()
        assert {row["destination"] for row in rows} == {"shiori"}
        assert {row["readwise_id"] for row in rows} == {"link-1", "link-2"}
        assert {row["readwise_url"] for row in rows} == {
            "https://pub.example.com/free",
            "https://pub.example.com/paid",
        }
        assert {row["submission_kind"] for row in rows} == {"url"}
        assert {row["pub_date"] for row in rows} == {"2026-07-17T12:00:00+00:00"}


def test_shiori_only_sync_does_not_require_readwise_token(
    monkeypatch: pytest.MonkeyPatch,
    fake_shiori: FakeShiori,
    shiori_sink: ShioriSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-gated", GatedSource)
    monkeypatch.setattr(
        ShioriSink,
        "from_config",
        classmethod(lambda cls, cfg, client=None: shiori_sink),
    )
    config = Config(
        subscriptions=(
            Subscription(
                name="pub",
                source="fake-gated",
                url="https://pub.example.com",
                options={"location": "shiori"},
            ),
        )
    )

    total = pipeline.sync(config=config)

    assert total.total_new == 2
    assert fake_shiori.save_payloads == [
        {"url": "https://pub.example.com/free"},
        {"url": "https://pub.example.com/paid"},
    ]


def test_mixed_sync_builds_and_reuses_each_provider_sink(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
    fake_shiori: FakeShiori,
    shiori_sink: ShioriSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-static", StaticSource)
    readwise_builds: list[Config] = []
    shiori_builds: list[Config] = []

    def build_readwise(cls: type[ReadwiseSink], cfg: Config) -> ReadwiseSink:
        del cls
        readwise_builds.append(cfg)
        return readwise_sink

    def build_shiori(cls: type[ShioriSink], cfg: Config) -> ShioriSink:
        del cls
        shiori_builds.append(cfg)
        return shiori_sink

    monkeypatch.setattr(ReadwiseSink, "from_config", classmethod(build_readwise))
    monkeypatch.setattr(ShioriSink, "from_config", classmethod(build_shiori))
    config = Config(
        subscriptions=(
            Subscription(name="reader-a", source="fake-static", url="https://reader-a.example"),
            Subscription(name="reader-b", source="fake-static", url="https://reader-b.example"),
            Subscription(
                name="shiori-a",
                source="fake-static",
                url="https://shiori-a.example",
                options={"location": "shiori"},
            ),
            Subscription(
                name="shiori-b",
                source="fake-static",
                url="https://shiori-b.example",
                options={"location": "shiori"},
            ),
        )
    )

    total = pipeline.sync(config=config)

    assert total.total_new == 12
    assert len(readwise_builds) == 1
    assert len(shiori_builds) == 1
    assert {str(payload["url"]).split("/")[2] for payload in fake_readwise.save_payloads} == {
        "reader-a.example",
        "reader-b.example",
    }
    assert {str(payload["url"]).split("/")[2] for payload in fake_shiori.save_payloads} == {
        "shiori-a.example",
        "shiori-b.example",
    }
    assert all(set(payload) == {"url"} for payload in fake_shiori.save_payloads)


def test_sync_defaults_to_feed_location(
    mock_client_factory: ClientFactory,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    """Unconfigured subscriptions land in Reader's Feed section - pulpwise
    acts as a feed reader in front of Reader, so the inbox stays quiet
    unless a subscription is explicitly routed there."""
    client = mock_client_factory({FEED_URL: _feed()})

    pipeline.sync(config=_rss_config(), client=client, sink=readwise_sink)

    assert fake_readwise.save_payloads  # sanity: something was pushed
    for payload in fake_readwise.save_payloads:
        assert payload["location"] == "feed"


def test_sync_bad_location_option_fails_subscription_not_run(
    mock_client_factory: ClientFactory,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    """Junk options.location is a ConfigError: the subscription is reported as
    an error (no pushes), the run itself survives, and the next subscription
    still syncs."""
    client = mock_client_factory({FEED_URL: _feed()})
    config = Config(
        subscriptions=(
            Subscription(name="bad", source="rss", url=FEED_URL, options={"location": "shortlist"}),
            Subscription(name="good", source="rss", url=FEED_URL),
        )
    )

    total = pipeline.sync(config=config, client=client, sink=readwise_sink)

    by_name = {r.name: r for r in total.reports}
    assert by_name["bad"].errors == 1
    assert by_name["bad"].new_items == 0
    assert "options.location" in by_name["bad"].error_messages[0]
    assert by_name["good"].new_items == 2
    with connect() as conn:
        state = get_subscription_state(conn, "bad")
        assert state is not None
        assert state.last_status == "error"


def test_sync_feed_level_error_does_not_abort_run(
    mock_client_factory: ClientFactory, readwise_sink: ReadwiseSink
) -> None:
    client = mock_client_factory({FEED_URL: _feed()})  # broken.example 404s
    config = Config(
        subscriptions=(
            Subscription(name="bad", source="rss", url="https://broken.example.com/feed"),
            Subscription(name="good", source="rss", url=FEED_URL),
        )
    )

    total = pipeline.sync(config=config, client=client, sink=readwise_sink)

    by_name = {r.name: r for r in total.reports}
    assert by_name["bad"].errors == 1
    assert by_name["good"].new_items == 2
    assert by_name["good"].errors == 0


def test_sync_missing_token_raises_before_any_discovery(
    mock_client_factory: ClientFactory,
) -> None:
    """No sink and no configured token: fail once, loudly, up front."""
    client = mock_client_factory({FEED_URL: _feed()})

    with pytest.raises(ReadwiseAuthError):
        pipeline.sync(config=_rss_config(), client=client)


def test_sync_rate_limited_sink_aborts_subscription_with_deferral(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-static", StaticSource)
    fake_readwise.save_responses.append(httpx.Response(429, headers={"Retry-After": "999"}))
    config = Config(
        subscriptions=(
            Subscription(name="pub", source="fake-static", url="https://pub.example.com"),
        )
    )

    total = pipeline.sync(config=config, sink=readwise_sink)

    report = total.reports[0]
    assert report.new_items == 0
    assert report.errors == 1  # one rate-limit event, not one error per item
    assert "3 item(s) deferred to the next sync run" in report.error_messages[0]
    assert len(fake_readwise.save_payloads) == 1  # items 2 and 3 never pushed
    # Unpushed refs are not in the ledger, so the next sync picks them up.
    with connect() as conn:
        assert is_seen(conn, dedup_key("https://pub.example.com/post-0")) is None


def test_sync_readwise_breaker_fails_later_subscriptions_fast(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    """After Readwise trips, remaining subscriptions error instantly - no
    stacked Retry-After waits, no further POSTs."""
    monkeypatch.setitem(REGISTRY, "fake-static", StaticSource)
    fake_readwise.save_responses.append(httpx.Response(429, headers={"Retry-After": "999"}))
    config = Config(
        subscriptions=(
            Subscription(name="first", source="fake-static", url="https://a.example.com"),
            Subscription(name="second", source="fake-static", url="https://b.example.com"),
        )
    )

    total = pipeline.sync(config=config, sink=readwise_sink)

    by_name = {r.name: r for r in total.reports}
    assert by_name["first"].errors == 1
    assert by_name["second"].errors == 1
    assert "cooling down" in by_name["second"].error_messages[0]
    assert len(fake_readwise.save_payloads) == 1  # the tripping POST was the only one


def test_sync_buckets_paywalled_items_without_erroring(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-gated", GatedSource)
    config = Config(
        subscriptions=(
            Subscription(name="pub", source="fake-gated", url="https://pub.example.com"),
        )
    )

    total = pipeline.sync(config=config, sink=readwise_sink)

    report = total.reports[0]
    assert report.new_items == 1
    assert report.errors == 0  # paywalled is a setup issue, not an error
    assert len(report.paywalled) == 1
    assert report.paywalled[0].url == "https://pub.example.com/paid"
    assert report.paywalled[0].host == "pub.example.com"

    # The gated capture went up as an HTML content submission.
    payload = fake_readwise.save_payloads[0]
    assert payload["html"] == "<p>gated-capture body</p>"
    assert payload["should_clean_html"] is True

    with connect() as conn:
        state = get_subscription_state(conn, "pub")
        assert state is not None
        assert state.last_status == "ok"


# ---- backfill -------------------------------------------------------------------


def _back_refs(n: int, year: int = 2026) -> tuple[ItemRef, ...]:
    """Newest-first refs, one month apart."""
    return tuple(
        ItemRef(
            url=f"https://pub.example.com/{year}/archive-{i}",
            title=f"Archive {i}",
            pub_date=datetime(year, 12 - i, 1, tzinfo=UTC),
        )
        for i in range(n)
    )


def _back_sub() -> Subscription:
    return Subscription(name="pub", source="fake-back", url="https://pub.example.com")


def test_shiori_backfill_saves_discovered_urls_without_fetching_content(
    monkeypatch: pytest.MonkeyPatch,
    fake_shiori: FakeShiori,
    shiori_sink: ShioriSink,
) -> None:
    refs = _back_refs(2)
    monkeypatch.setattr(ShioriBackSource, "refs", refs)
    source = ShioriBackSource(client=None)
    sub = Subscription(
        name="pub",
        source="fake-shiori-back",
        url="https://pub.example.com",
        options={"location": "shiori"},
    )

    with connect() as conn:
        report = pipeline._backfill_with_source(
            sub,
            conn,
            source,
            shiori_sink,
            max_new=None,
            since_iso=None,
        )
        destinations = {row["destination"] for row in conn.execute("SELECT destination FROM items")}

    assert report.new_items == 2
    assert fake_shiori.save_payloads == [{"url": ref.url} for ref in refs]
    assert destinations == {"shiori"}


def test_backfill_stops_at_max_new(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-back", BackSource)
    monkeypatch.setattr(BackSource, "refs", _back_refs(5))
    sub = _back_sub()

    report = pipeline.backfill(
        sub, config=Config(subscriptions=(sub,)), sink=readwise_sink, max_new=2
    )

    assert report.stopped_reason == "max_new"
    assert report.new_items == 2
    assert len(fake_readwise.save_payloads) == 2


def test_backfill_stops_at_since_date_floor(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-back", BackSource)
    monkeypatch.setattr(BackSource, "refs", _back_refs(4))  # Dec, Nov, Oct, Sep
    sub = _back_sub()

    report = pipeline.backfill(
        sub,
        config=Config(subscriptions=(sub,)),
        sink=readwise_sink,
        max_new=None,
        since_iso="2026-10-15T00:00:00+00:00",
    )

    assert report.stopped_reason == "since"
    assert report.new_items == 2  # Dec + Nov; Oct is older than the floor
    assert len(fake_readwise.save_payloads) == 2


def test_backfill_exhausts_the_archive(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-back", BackSource)
    monkeypatch.setattr(BackSource, "refs", _back_refs(3))
    sub = _back_sub()

    report = pipeline.backfill(
        sub, config=Config(subscriptions=(sub,)), sink=readwise_sink, max_new=None
    )

    assert report.stopped_reason == "exhausted"
    assert report.new_items == 3
    assert report.pages_walked == 1


def test_backfill_skips_already_ingested_but_keeps_walking(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-back", BackSource)
    refs = _back_refs(3)
    monkeypatch.setattr(BackSource, "refs", refs)
    sub = _back_sub()
    cfg = Config(subscriptions=(sub,))

    # Pre-ingest the middle ref, then walk: it's skipped, the rest push.
    pipeline.backfill(sub, config=cfg, sink=readwise_sink, max_new=None)
    fake_readwise.save_payloads.clear()
    monkeypatch.setattr(BackSource, "refs", (*refs, *_back_refs(1, year=2025)))

    report = pipeline.backfill(sub, config=cfg, sink=readwise_sink, max_new=None)

    assert report.skipped_already_ingested == 3
    assert report.new_items == 1  # the 2025 hole got filled
    assert report.stopped_reason == "exhausted"


def test_backfill_stops_on_readwise_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    fake_readwise: FakeReadwise,
    readwise_sink: ReadwiseSink,
) -> None:
    monkeypatch.setitem(REGISTRY, "fake-back", BackSource)
    monkeypatch.setattr(BackSource, "refs", _back_refs(3))
    fake_readwise.save_responses.append(httpx.Response(429, headers={"Retry-After": "999"}))
    sub = _back_sub()

    report = pipeline.backfill(
        sub, config=Config(subscriptions=(sub,)), sink=readwise_sink, max_new=None
    )

    assert report.stopped_reason == "rate_limited"
    assert report.errors == 1
    assert report.new_items == 0
    assert "rate limited by Readwise" in report.error_messages[0]
    assert len(fake_readwise.save_payloads) == 1  # the walk stopped at the first item


def test_backfill_unsupported_source_raises(readwise_sink: ReadwiseSink) -> None:
    sub = Subscription(name="feed", source="rss", url=FEED_URL)
    with pytest.raises(pipeline.BackfillUnsupported, match="doesn't support backfill"):
        pipeline.backfill(sub, config=Config(subscriptions=(sub,)), sink=readwise_sink)


def test_backfill_unknown_source_raises(readwise_sink: ReadwiseSink) -> None:
    sub = Subscription(name="x", source="nonexistent", url="https://x.example")
    with pytest.raises(pipeline.BackfillUnsupported, match="unknown source"):
        pipeline.backfill(sub, config=Config(subscriptions=(sub,)), sink=readwise_sink)
