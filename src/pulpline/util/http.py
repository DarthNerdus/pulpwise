"""Shared httpx client used by every source plugin.

Every client built here routes through `RetryTransport`, which adds three
behaviors on top of a plain `httpx.HTTPTransport`:

  * Transient failures retry in place: 429 and 502/503/504 responses are
    re-sent with exponential backoff (jittered, `Retry-After`-aware) before
    the caller ever sees them.
  * A persistent 429 gets one bounded grace period: the transport waits out
    the server's cooldown once (<= `_MAX_COOLDOWN_WAIT`) and tries again, so
    a long sync run survives tripping the limiter mid-way instead of
    deferring half its subscriptions. If the limiter is still angry after
    that - a second exhausted 429 with no success in between - the breaker
    trips for real: `RateLimited` raises, and further requests to the same
    bucket fail fast (no network) until the cooldown passes.
  * Once a bucket has needed either treatment, its requests are paced
    (min gap between sends) so the resume doesn't instantly re-trip.

Breaker state is keyed by *bucket*: the request's host by default, or the
client's `scope` when one is declared. Scope exists because rate limiters
don't always match hostnames - Substack enforces per-IP limits across all
publication subdomains AND custom domains, so 50 subscriptions must share
one breaker ("substack"), not 50 per-host breakers that each rediscover the
same limiter four requests at a time.

The registry is module-global on purpose: each source builds its own
client, but rate limits are a property of the provider, shared by every
client in the process (sync + backfill + the TUI's auto-reconcile).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil

import httpx

from pulpline import __version__
from pulpline.models import RateLimited
from pulpline.util.logging import get_logger

_log = get_logger("http")

USER_AGENT = f"pulpline/{__version__}"
# Browser UA for hosts behind DDoS-Guard / Cloudflare anti-bot fingerprinting
# (Anna's Archive in particular). Pulpline's default UA is honest about being
# a bot, which is what we want everywhere except for these specific hosts.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# Retries after the initial attempt: a persistent 429 costs 4 requests,
# plus 1 more if the wait-through grace period below is still available.
_MAX_RETRIES_429 = 3
_MAX_RETRIES_5XX = 2
# Exponential backoff when the server doesn't send Retry-After: 2s, 4s, 8s.
_BACKOFF_BASE = 2.0
# Never sleep longer than this on a single retry. A Retry-After beyond it
# means "come back much later" - we give up immediately and let the cooldown
# carry the wait instead of blocking a sync mid-item for minutes.
_MAX_RETRY_SLEEP = 120.0
# Minimum cooldown once 429 retries are exhausted. Substack's limiter
# windows are around a minute; the server's own Retry-After wins if longer.
_COOLDOWN_FLOOR = 60.0
# Longest cooldown we'll block a run for via the wait-through grace period.
# Anything longer fails fast so an interactive sync isn't silently frozen.
_MAX_COOLDOWN_WAIT = 120.0
# Min gap between requests to a bucket that has tripped (or been waited
# out) at least once this process. Keeps the resume from re-tripping.
_PACE_INTERVAL = 1.0


class _RateLimitState:
    """Thread-safe per-bucket cooldown + pacing + wait-allowance registry.

    A bucket is a hostname or a provider scope (see module docstring).
    `clock` is injectable (monotonic seconds) so tests can drive time.
    Thread safety matters: the TUI runs sync in worker threads, and every
    client in the process shares the module-level instance below.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._blocked_until: dict[str, float] = {}
        # bucket -> earliest time the next request may be sent. Presence in
        # this dict doubles as "bucket has tripped before, keep pacing it".
        self._next_send: dict[str, float] = {}
        # Buckets whose one wait-through allowance is currently spent.
        # `note_success` restores it: a landed response proves the limiter
        # recovered, so a *later* trip deserves a fresh grace period.
        self._wait_spent: set[str] = set()

    def raise_if_blocked(self, bucket: str) -> None:
        """Raise RateLimited (without any network) while `bucket` cools down."""
        with self._lock:
            until = self._blocked_until.get(bucket)
            if until is None:
                return
            remaining = until - self._clock()
            if remaining <= 0:
                del self._blocked_until[bucket]
                return
        raise RateLimited(
            f"rate limited by {bucket}: cooling down for another {ceil(remaining)}s",
            host=bucket,
            retry_after=remaining,
        )

    def reserve_send_slot(self, bucket: str) -> float:
        """Return seconds the caller must sleep before sending to `bucket`.

        Zero for buckets that have never tripped. For tripped buckets this
        hands out send slots `_PACE_INTERVAL` apart, so concurrent callers
        queue up behind each other instead of bursting.
        """
        with self._lock:
            if bucket not in self._next_send:
                return 0.0
            now = self._clock()
            wait = max(0.0, self._next_send[bucket] - now)
            self._next_send[bucket] = now + wait + _PACE_INTERVAL
            return wait

    def try_claim_wait(self, bucket: str, cooldown: float) -> bool:
        """Claim the bucket's one wait-through allowance.

        True: the caller may sleep out `cooldown` and try once more; pacing
        is armed from the wake-up point. False: the allowance is already
        spent (no success since the last claimed wait), so the caller should
        trip the breaker instead.
        """
        with self._lock:
            if bucket in self._wait_spent:
                return False
            self._wait_spent.add(bucket)
            self._next_send[bucket] = self._clock() + cooldown
            return True

    def note_success(self, bucket: str) -> None:
        """A non-429 response landed: the limiter is happy again."""
        with self._lock:
            self._wait_spent.discard(bucket)

    def trip(self, bucket: str, cooldown: float) -> None:
        with self._lock:
            now = self._clock()
            self._blocked_until[bucket] = now + cooldown
            self._next_send[bucket] = now + cooldown

    def reset(self) -> None:
        with self._lock:
            self._blocked_until.clear()
            self._next_send.clear()
            self._wait_spent.clear()


_RATE_LIMIT_STATE = _RateLimitState()


def reset_rate_limit_state() -> None:
    """Forget all bucket cooldowns, pacing, and wait allowances. Test isolation only."""
    _RATE_LIMIT_STATE.reset()


class RetryTransport(httpx.BaseTransport):
    """Wraps a transport with 429/5xx retries and the bucket breaker above.

    `scope` pins every request through this transport to one breaker bucket
    regardless of hostname (Substack); None buckets per request host. Only
    GET/HEAD requests are retried (everything pulpline sends today); other
    methods pass through untouched apart from the cooldown fail-fast.
    `state` and `sleep` are injectable for tests; production callers take
    the defaults and share the module-level registry.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport | None = None,
        *,
        scope: str | None = None,
        state: _RateLimitState | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner if inner is not None else httpx.HTTPTransport()
        self._scope = scope
        self._state = state if state is not None else _RATE_LIMIT_STATE
        self._sleep = sleep

    def close(self) -> None:
        self._inner.close()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        bucket = self._scope or request.url.host or ""
        self._state.raise_if_blocked(bucket)
        pace = self._state.reserve_send_slot(bucket)
        if pace > 0:
            self._sleep(pace)

        if request.method not in ("GET", "HEAD"):
            return self._inner.handle_request(request)

        seen_429 = 0
        seen_5xx = 0
        while True:
            response = self._inner.handle_request(request)
            if response.status_code not in _RETRYABLE_STATUSES:
                self._state.note_success(bucket)
                return response

            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if response.status_code == 429:
                seen_429 += 1
                asked_to_wait_too_long = retry_after is not None and retry_after > _MAX_RETRY_SLEEP
                if seen_429 > _MAX_RETRIES_429 or asked_to_wait_too_long:
                    response.close()
                    cooldown = max(retry_after or 0.0, _COOLDOWN_FLOOR)
                    if cooldown <= _MAX_COOLDOWN_WAIT and self._state.try_claim_wait(
                        bucket, cooldown
                    ):
                        _log.warning(
                            "HTTP 429 from %s persisted after %d attempt(s); waiting "
                            "out the %.0fs cooldown, then resuming paced (url=%s)",
                            bucket,
                            seen_429,
                            cooldown,
                            request.url,
                        )
                        self._sleep(cooldown)
                        # One more attempt. If it 429s too, we land back here
                        # with the allowance spent and trip for real.
                        continue
                    self._state.trip(bucket, cooldown)
                    _log.warning(
                        "HTTP 429 from %s persisted after %d attempt(s); "
                        "cooling down %.0fs (url=%s)",
                        bucket,
                        seen_429,
                        cooldown,
                        request.url,
                    )
                    raise RateLimited(
                        f"rate limited by {bucket}: HTTP 429 persisted after "
                        f"{seen_429} attempt(s); backing off {ceil(cooldown)}s",
                        host=bucket,
                        retry_after=cooldown,
                    )
                delay = retry_after if retry_after is not None else _backoff_delay(seen_429)
                attempt, limit = seen_429, _MAX_RETRIES_429
            else:
                seen_5xx += 1
                if seen_5xx > _MAX_RETRIES_5XX:
                    # Exhausted: hand the response back and let the caller's
                    # raise_for_status() turn it into a FetchError as before.
                    # Still a success for the wait allowance - the server is
                    # answering, just unwell; that's not rate-limiter anger.
                    self._state.note_success(bucket)
                    return response
                delay = retry_after if retry_after is not None else _backoff_delay(seen_5xx)
                attempt, limit = seen_5xx, _MAX_RETRIES_5XX

            response.close()
            delay = min(delay, _MAX_RETRY_SLEEP)
            _log.info(
                "HTTP %d from %s: retry %d/%d in %.1fs (url=%s)",
                response.status_code,
                bucket,
                attempt,
                limit,
                delay,
                request.url,
            )
            self._sleep(delay)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with up to 25% jitter: ~2s, ~4s, ~8s..."""
    return _BACKOFF_BASE * (2.0 ** (attempt - 1)) * random.uniform(1.0, 1.25)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header: delta-seconds or an HTTP-date.

    Returns seconds to wait (>= 0), or None when absent/unparseable.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (dt - datetime.now(UTC)).total_seconds())


def build_client(timeout: float = DEFAULT_TIMEOUT, *, scope: str | None = None) -> httpx.Client:
    """Construct a configured sync httpx client.

    `scope` names a shared rate-limit bucket for every request this client
    sends, no matter the host (see `RetryTransport`). None = bucket per host.
    """
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
        transport=RetryTransport(scope=scope),
    )


def build_browser_client(
    timeout: float = DEFAULT_TIMEOUT, *, scope: str | None = None
) -> httpx.Client:
    """Like `build_client`, but advertises a browser User-Agent.

    Use this only for hosts that block on UA fingerprinting (Anna's Archive
    behind DDoS-Guard). Pulpline prefers an honest UA everywhere else.
    """
    return httpx.Client(
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
        transport=RetryTransport(scope=scope),
    )
