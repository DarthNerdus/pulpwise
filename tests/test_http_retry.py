"""Unit tests for RetryTransport: 429/5xx retries, cooldowns, scopes, pacing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from pulpline.models import FetchError, RateLimited
from pulpline.util.http import RetryTransport, _RateLimitState
from tests.conftest import FakeTimer

URL = "https://pub.example.com/api/v1/archive"


def _make_client(
    script: list[httpx.Response], timer: FakeTimer, *, scope: str | None = None
) -> tuple[httpx.Client, list[httpx.Request]]:
    """Client whose inner transport pops canned responses off `script`.

    Extra requests beyond the script get a 200 so tests over-asserting call
    counts fail on the count, not on an opaque IndexError.
    """
    requests: list[httpx.Request] = []
    remaining = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return remaining.pop(0) if remaining else httpx.Response(200, text="ok")

    transport = RetryTransport(
        httpx.MockTransport(handler),
        scope=scope,
        state=_RateLimitState(clock=timer.clock),
        sleep=timer.sleep,
    )
    return httpx.Client(transport=transport), requests


def test_429_then_success_is_retried(fake_timer: FakeTimer) -> None:
    client, requests = _make_client([httpx.Response(429)], fake_timer)

    response = client.get(URL)

    assert response.status_code == 200
    assert len(requests) == 2
    # First backoff step is 2s with up to 25% jitter.
    assert len(fake_timer.sleeps) == 1
    assert 2.0 <= fake_timer.sleeps[0] <= 2.5


def test_retry_after_seconds_is_honored_exactly(fake_timer: FakeTimer) -> None:
    client, requests = _make_client([httpx.Response(429, headers={"Retry-After": "7"})], fake_timer)

    response = client.get(URL)

    assert response.status_code == 200
    assert len(requests) == 2
    assert fake_timer.sleeps == [7.0]


def test_retry_after_http_date_is_honored(fake_timer: FakeTimer) -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    client, requests = _make_client(
        [httpx.Response(429, headers={"Retry-After": when})], fake_timer
    )

    response = client.get(URL)

    assert response.status_code == 200
    assert len(requests) == 2
    assert len(fake_timer.sleeps) == 1
    assert 0.0 < fake_timer.sleeps[0] <= 30.0


def test_persistent_429_waits_out_one_cooldown_then_raises(fake_timer: FakeTimer) -> None:
    client, requests = _make_client([httpx.Response(429)] * 10, fake_timer)

    with pytest.raises(RateLimited) as excinfo:
        client.get(URL)

    # Initial + 3 backoff retries, then one bonus attempt after waiting out
    # the full cooldown. Still 429 -> the breaker trips for real.
    assert len(requests) == 5
    assert len(fake_timer.sleeps) == 4
    assert fake_timer.sleeps[3] == 60.0  # cooldown floor, no Retry-After sent
    assert excinfo.value.host == "pub.example.com"
    assert excinfo.value.retry_after == 60.0
    # RateLimited must be a FetchError so pre-existing handlers stay compatible.
    assert isinstance(excinfo.value, FetchError)


def test_tripped_bucket_fails_fast_without_network(fake_timer: FakeTimer) -> None:
    client, requests = _make_client([httpx.Response(429)] * 10, fake_timer)
    with pytest.raises(RateLimited):
        client.get(URL)
    sent_so_far = len(requests)

    with pytest.raises(RateLimited) as excinfo:
        client.get(URL)

    assert len(requests) == sent_so_far  # no request was sent
    assert "cooling down" in str(excinfo.value)


def test_huge_retry_after_gives_up_without_sleeping(fake_timer: FakeTimer) -> None:
    client, requests = _make_client(
        [httpx.Response(429, headers={"Retry-After": "3600"})], fake_timer
    )

    with pytest.raises(RateLimited) as excinfo:
        client.get(URL)

    assert len(requests) == 1
    assert fake_timer.sleeps == []
    assert excinfo.value.retry_after == 3600.0


def test_waits_out_first_cooldown_and_recovers(fake_timer: FakeTimer) -> None:
    """The 5th attempt lands after the waited-out cooldown; no error escapes."""
    client, requests = _make_client([httpx.Response(429)] * 4, fake_timer)

    response = client.get(URL)

    assert response.status_code == 200
    assert len(requests) == 5
    assert fake_timer.sleeps[3] == 60.0

    # The bucket is paced from here on: 1s gap between subsequent sends.
    assert client.get(URL).status_code == 200  # first slot is immediate
    assert client.get(URL).status_code == 200
    assert len(requests) == 7
    assert fake_timer.sleeps[-1] == pytest.approx(1.0)


def test_success_restores_the_wait_allowance(fake_timer: FakeTimer) -> None:
    """A landed response proves recovery, so a later trip earns a fresh wait."""
    script = [httpx.Response(429)] * 4 + [httpx.Response(200)] + [httpx.Response(429)] * 4
    client, requests = _make_client(script, fake_timer)

    assert client.get(URL).status_code == 200  # waits once, recovers
    assert client.get(URL).status_code == 200  # trips again, waits again, recovers

    assert len(requests) == 10
    assert fake_timer.sleeps.count(60.0) == 2


def test_scope_shares_one_breaker_across_hosts(fake_timer: FakeTimer) -> None:
    """The 50-substack-subscriptions case: one trip protects every other host."""
    client, requests = _make_client([httpx.Response(429)] * 10, fake_timer, scope="substack")

    with pytest.raises(RateLimited) as excinfo:
        client.get("https://foo.substack.com/api/v1/archive")
    assert excinfo.value.host == "substack"
    sent_so_far = len(requests)

    with pytest.raises(RateLimited) as second:
        client.get("https://bar.substack.com/api/v1/archive")

    assert len(requests) == sent_so_far  # zero network for the second host
    assert "rate limited by substack" in str(second.value)


def test_cooldown_is_per_bucket(fake_timer: FakeTimer) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "a.example.com":
            return httpx.Response(429)
        return httpx.Response(200, text="ok")

    transport = RetryTransport(
        httpx.MockTransport(handler),
        state=_RateLimitState(clock=fake_timer.clock),
        sleep=fake_timer.sleep,
    )
    client = httpx.Client(transport=transport)

    with pytest.raises(RateLimited):
        client.get("https://a.example.com/feed")

    response = client.get("https://b.example.com/feed")
    assert response.status_code == 200
    assert calls.count("b.example.com") == 1


def test_substack_source_builds_scoped_client() -> None:
    from pulpline.sources.substack import SubstackSource

    with SubstackSource() as source:
        transport = source.client._transport
        assert isinstance(transport, RetryTransport)
        assert transport._scope == "substack"


def test_transient_5xx_is_retried(fake_timer: FakeTimer) -> None:
    client, requests = _make_client([httpx.Response(503)], fake_timer)

    response = client.get(URL)

    assert response.status_code == 200
    assert len(requests) == 2


def test_persistent_5xx_returns_response_instead_of_raising(fake_timer: FakeTimer) -> None:
    client, requests = _make_client([httpx.Response(503)] * 10, fake_timer)

    response = client.get(URL)

    # Exhausted retries hand back the response so callers' raise_for_status()
    # produces the same FetchError they produced before this layer existed.
    assert response.status_code == 503
    assert len(requests) == 3  # initial + 2 retries


def test_non_get_requests_pass_through_untouched(fake_timer: FakeTimer) -> None:
    client, requests = _make_client([httpx.Response(429), httpx.Response(200)], fake_timer)

    response = client.post(URL, json={})

    assert response.status_code == 429  # not retried, not raised
    assert len(requests) == 1
    assert fake_timer.sleeps == []
    # And the 429 did not trip the bucket cooldown: a GET still goes out.
    assert client.get(URL).status_code == 200
    assert len(requests) == 2
