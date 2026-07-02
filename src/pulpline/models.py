"""Boundary types passed between sources, renderers, and sinks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class PulplineError(Exception):
    """Base exception for pulpline errors."""


class ExtractionError(PulplineError):
    """Raised when content extraction fails (empty body, no parseable text)."""


class FetchError(PulplineError):
    """Raised when an HTTP fetch fails or returns a non-success status."""


class RateLimited(FetchError):  # noqa: N818 - same convention as Paywalled
    """A host answered 429 Too Many Requests and kept answering it through retries.

    Raised by the retrying transport in `pulpline.util.http` - either after
    backoff retries are exhausted, or instantly (no network) while the host
    is still inside a cooldown window from an earlier trip. Deliberately NOT
    an `httpx.HTTPError`: source plugins wrap those in generic FetchErrors,
    but this one flies past them so the pipeline can branch on it and stop
    the current subscription instead of hammering the remaining items into
    the same wall.

    `host` is the rate-limiting bucket - a hostname, or a provider scope
    like "substack" when the source declares one (Substack's limiter spans
    all its publication domains). `retry_after` is how many seconds the
    transport will keep refusing requests to that bucket.
    """

    def __init__(self, message: str, host: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.host = host
        self.retry_after = retry_after


class Paywalled(ExtractionError):  # noqa: N818 - "Error" suffix would be redundant + uglier
    """A specific kind of ExtractionError: post is paid + auth didn't work.

    Carries `host` so the orchestrator can group affected items by domain
    and surface a single setup hint per host instead of repeating it once
    per item. Subclassing ExtractionError keeps backwards-compatible
    catches; new code can branch on this type to bucket separately.
    """

    def __init__(self, message: str, host: str) -> None:
        super().__init__(message)
        self.host = host


@dataclass(frozen=True, slots=True)
class ItemRef:
    """Lightweight reference to an item, produced by `Source.discover`.

    Used for dedup (Phase 2) before committing to an expensive body fetch.
    Sources should fill what they cheaply have - URL is the only required field.
    """

    url: str
    title: str | None = None
    pub_date: datetime | None = None
    guid: str | None = None  # RSS <guid> / Atom <id> when present; informational only


@dataclass(frozen=True, slots=True)
class RawArticle:
    """Extracted article ready for rendering.

    Produced by `Source.fetch`, consumed by `Renderer.render`.
    """

    title: str
    body_html: str
    canonical_url: str
    source_url: str  # feed URL for RSS items, article URL for one-shots ("direct")
    author: str | None = None
    publisher: str | None = None
    pub_date: datetime | None = None
    language: str = "en"
    subscription_name: str | None = None  # None for `--once` ingestions
