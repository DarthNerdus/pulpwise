"""Integration tests for pipeline.sync (real RSSSource, real EpubRenderer, mocked HTTP)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from pulpline import pipeline
from pulpline.config import Config, Subscription
from pulpline.state import connect, is_seen
from pulpline.util.http import RetryTransport, _RateLimitState
from tests.conftest import FakeTimer

ClientFactory = Callable[[dict[str, str]], httpx.Client]


def _feed() -> str:
    return (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_text()


def _config(feed_url: str, output_dir: Path) -> Config:
    return Config(
        subscriptions=(
            Subscription(name="test", source="rss", url=feed_url, output_dir=str(output_dir)),
        )
    )


def test_sync_writes_epubs_for_new_items(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    feed_url = "https://example.com/feed"
    out = tmp_path / "out"
    client = mock_client_factory(
        {
            feed_url: _feed(),
            "https://example.com/teaser": sample_html,  # fallback fetch for summary-only
        }
    )

    total = pipeline.sync(config=_config(feed_url, out), client=client)

    assert len(total.reports) == 1
    report = total.reports[0]
    assert report.name == "test"
    assert report.new_items == 2
    assert report.skipped == 0
    assert report.errors == 0

    written = sorted(p.name for p in out.iterdir())
    assert len(written) == 2
    assert all(name.endswith(".epub") for name in written)


def test_sync_dedups_on_second_run(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    feed_url = "https://example.com/feed"
    out = tmp_path / "out"
    client = mock_client_factory(
        {
            feed_url: _feed(),
            "https://example.com/teaser": sample_html,
        }
    )

    pipeline.sync(config=_config(feed_url, out), client=client)
    # Second run: all items now seen, none should be re-fetched / re-written.
    second = pipeline.sync(config=_config(feed_url, out), client=client)

    assert second.total_new == 0
    assert second.total_skipped == 2
    assert second.total_errors == 0


def test_sync_records_items_in_dedup_ledger(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    feed_url = "https://example.com/feed"
    client = mock_client_factory(
        {
            feed_url: _feed(),
            "https://example.com/teaser": sample_html,
        }
    )

    pipeline.sync(config=_config(feed_url, tmp_path / "out"), client=client)

    from pulpline.util.dedup import dedup_key

    with connect() as conn:
        assert is_seen(conn, dedup_key("https://example.com/full")) is not None
        assert is_seen(conn, dedup_key("https://example.com/teaser")) is not None


def test_sync_feed_level_error_does_not_abort_run(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    """A failing feed should be reported as error and skip-and-continue to the next."""
    bad_feed = "https://broken.example.com/feed"
    good_feed = "https://example.com/feed"
    client = mock_client_factory(
        {
            good_feed: _feed(),
            "https://example.com/teaser": sample_html,
        }
    )

    out = tmp_path / "out"
    config = Config(
        subscriptions=(
            Subscription(name="bad", source="rss", url=bad_feed),
            Subscription(name="good", source="rss", url=good_feed, output_dir=str(out)),
        )
    )
    total = pipeline.sync(config=config, client=client)

    by_name = {r.name: r for r in total.reports}
    assert by_name["bad"].errors == 1
    assert by_name["good"].new_items == 2
    assert by_name["good"].errors == 0


def test_add_once_is_idempotent_via_dedup(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    url = "https://example.com/article"
    client = mock_client_factory({url: sample_html})

    first = pipeline.add_once(url, output_dir=tmp_path, client=client)
    second = pipeline.add_once(url, output_dir=tmp_path, client=client)

    assert first == second
    # The article was written exactly once - in the oneshots subfolder.
    assert len(list((tmp_path / "oneshots").glob("*.epub"))) == 1


# ---- rate-limit behavior (RetryTransport wired in, like production clients) ----


def _retry_client(
    handler: Callable[[httpx.Request], httpx.Response],
    timer: FakeTimer,
    scope: str | None = None,
) -> httpx.Client:
    """Mock-backed client with the same retry layer `build_client` installs."""
    transport = RetryTransport(
        httpx.MockTransport(handler),
        scope=scope,
        state=_RateLimitState(clock=timer.clock),
        sleep=timer.sleep,
    )
    return httpx.Client(transport=transport)


def _teaser_feed(feed_path: str, article_urls: list[str]) -> str:
    """RSS feed whose items carry only teasers, forcing per-article fetches."""
    items = "".join(
        f"<item><title>Post {i}</title><link>{url}</link>"
        f"<description>teaser only</description></item>"
        for i, url in enumerate(article_urls)
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>RL Feed</title><link>https://pub.example.com{feed_path}</link>"
        f"{items}</channel></rss>"
    )


def test_sync_stops_subscription_after_persistent_429(
    tmp_path: Path, fake_timer: FakeTimer
) -> None:
    """One rate-limited item must not cascade into requests for the rest."""
    articles = [f"https://pub.example.com/post-{i}" for i in range(3)]
    feed_xml = _teaser_feed("/feed", articles)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/feed":
            return httpx.Response(200, text=feed_xml)
        return httpx.Response(429)

    client = _retry_client(handler, fake_timer)
    config = Config(
        subscriptions=(
            Subscription(
                name="pub",
                source="rss",
                url="https://pub.example.com/feed",
                output_dir=str(tmp_path / "out"),
            ),
        )
    )

    total = pipeline.sync(config=config, client=client)

    report = total.reports[0]
    assert report.new_items == 0
    assert report.errors == 1  # one rate-limit event, not one error per item
    assert "3 item(s) deferred to the next sync run" in report.error_messages[0]
    # Item 1 cost 5 requests (initial + 3 backoff retries + 1 post-cooldown
    # attempt); items 2 and 3 cost zero.
    article_requests = [r for r in requests if r.url.path != "/feed"]
    assert len(article_requests) == 5
    assert all(r.url.path == "/post-0" for r in article_requests)


def test_sync_rate_limited_host_fails_fast_for_later_subscriptions(
    tmp_path: Path, fake_timer: FakeTimer
) -> None:
    """After a host trips, other subscriptions on it make zero HTTP requests."""
    feed_xml = _teaser_feed("/feed", ["https://pub.example.com/post-0"])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/feed":
            return httpx.Response(200, text=feed_xml)
        return httpx.Response(429)

    client = _retry_client(handler, fake_timer)
    config = Config(
        subscriptions=(
            Subscription(
                name="first",
                source="rss",
                url="https://pub.example.com/feed",
                output_dir=str(tmp_path / "out"),
            ),
            Subscription(
                name="second",
                source="rss",
                url="https://pub.example.com/feed2",
                output_dir=str(tmp_path / "out"),
            ),
        )
    )

    total = pipeline.sync(config=config, client=client)

    by_name = {r.name: r for r in total.reports}
    assert by_name["first"].errors == 1
    assert by_name["second"].errors == 1
    assert "rate limited by pub.example.com" in by_name["second"].error_messages[0]
    # The second subscription's feed was never requested.
    assert not any(r.url.path == "/feed2" for r in requests)


def test_backfill_stops_on_rate_limit(tmp_path: Path, fake_timer: FakeTimer) -> None:
    archive = [
        {
            "id": i,
            "title": f"Post {i}",
            "canonical_url": f"https://pub.substack.com/p/post-{i}",
            "post_date": f"2026-01-0{i + 1}T00:00:00Z",
        }
        for i in range(2)
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/archive":
            return httpx.Response(200, json=archive)
        return httpx.Response(429)

    client = _retry_client(handler, fake_timer)
    sub = Subscription(
        name="pub",
        source="substack",
        url="https://pub.substack.com",
        output_dir=str(tmp_path / "out"),
    )

    report = pipeline.backfill(sub, config=Config(subscriptions=(sub,)), client=client)

    assert report.stopped_reason == "rate_limited"
    assert report.errors == 1
    assert report.new_items == 0
    assert "rate limited by pub.substack.com" in report.error_messages[0]
    # Only the first post burned requests; the walk stopped there.
    post_requests = [r for r in requests if r.url.path.startswith("/api/v1/posts/")]
    assert len(post_requests) == 5


def test_backfill_substack_saves_walks_reader_feed_not_archive(tmp_path: Path) -> None:
    """Backfilling the saved-posts subscription must page the reader saves
    feed - not `<url>/api/v1/archive`, which 404s under /inbox/saved (the
    inherited publication walk that broke saved-list backfill)."""
    import json

    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text(
        json.dumps([{"name": "substack.sid", "value": "s", "domain": ".substack.com"}]),
        encoding="utf-8",
    )

    saves_page = {
        "posts": [
            {
                "id": 1,
                "title": "Saved Post",
                "canonical_url": "https://pub.substack.com/p/saved-post",
                "post_date": "2026-01-01T00:00:00.000Z",
            }
        ],
        "savedPosts": [{"user_id": 1, "post_id": 1, "created_at": "2026-06-01T00:00:00.000Z"}],
        "more": False,
    }
    post_body = {
        "title": "Saved Post",
        "canonical_url": "https://pub.substack.com/p/saved-post",
        "body_html": "<p>body</p>",
        "audience": "everyone",
        "post_date": "2026-01-01T00:00:00.000Z",
        "publishedBylines": [{"name": "Author"}],
        "publication": {"name": "Pub"},
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/reader/posts":
            return httpx.Response(200, json=saves_page)
        if request.url.path == "/api/v1/posts/saved-post":
            return httpx.Response(200, json=post_body)
        return httpx.Response(404, text=f"unmocked: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sub = Subscription(
        name="substack-saves",
        source="substack-saved",
        url="https://substack.com/inbox/saved",
        output_dir=str(tmp_path / "out"),
    )
    cfg = Config(
        subscriptions=(sub,),
        auth={"substack": {"cookies_path": str(cookies_file)}},
    )

    report = pipeline.backfill(sub, config=cfg, client=client)

    assert report.stopped_reason == "exhausted"
    assert report.new_items == 1
    assert report.errors == 0
    assert not any("archive" in r.url.path for r in requests)


def test_sync_substack_scope_protects_other_publications(
    tmp_path: Path, fake_timer: FakeTimer
) -> None:
    """One publication trips the shared `substack` bucket; the next
    publication - a different hostname - is skipped with zero requests."""
    archive = [
        {
            "id": 1,
            "title": "Post",
            "canonical_url": "https://foo.substack.com/p/post",
            "post_date": "2026-01-01T00:00:00Z",
        }
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/archive":
            return httpx.Response(200, json=archive)
        return httpx.Response(429)

    client = _retry_client(handler, fake_timer, scope="substack")
    config = Config(
        subscriptions=(
            Subscription(
                name="foo",
                source="substack",
                url="https://foo.substack.com",
                output_dir=str(tmp_path / "out"),
            ),
            Subscription(
                name="bar",
                source="substack",
                url="https://bar.substack.com",
                output_dir=str(tmp_path / "out"),
            ),
        )
    )

    total = pipeline.sync(config=config, client=client)

    by_name = {r.name: r for r in total.reports}
    assert by_name["foo"].errors == 1
    assert by_name["bar"].errors == 1
    assert "rate limited by substack" in by_name["bar"].error_messages[0]
    # bar.substack.com was never contacted - the scope bucket, not the
    # hostname, is what the breaker keys on.
    assert not any(r.url.host == "bar.substack.com" for r in requests)
