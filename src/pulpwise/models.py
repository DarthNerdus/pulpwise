"""Boundary types passed between sources, the pipeline, and the Readwise sink."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class PulpwiseError(Exception):
    """Base exception for pulpwise errors."""


class ExtractionError(PulpwiseError):
    """Raised when content extraction fails (empty body, no parseable text)."""


class FetchError(PulpwiseError):
    """Raised when an HTTP fetch fails or returns a non-success status."""


class ItemSkipped(PulpwiseError):  # noqa: N818 - control-flow signal, not an error
    """A source's fetch decided this item should not be ingested at all.

    Raised when the decision can only be made after downloading the item -
    e.g. an email whose BODYSTRUCTURE didn't parse at discovery turns out
    to be an ebook delivery at fetch time. The pipeline counts it as a
    skip (not an error), acks the source so mark-read policies still
    apply, and does not record it - it isn't content.
    """


class RateLimited(FetchError):  # noqa: N818 - same convention as Paywalled
    """A host answered 429 Too Many Requests and kept answering it through retries.

    Raised by the retrying transport in `pulpwise.util.http` - either after
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
    """Extracted article produced by `Source.fetch`.

    Consumed by `Source.submission_for_article`, which turns it into a
    `ReaderSubmission` for the Readwise sink.
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
    content_gated: bool = False
    """True when a third party (Readwise's server-side fetcher, which carries
    no cookies) could NOT retrieve this content from `canonical_url` - e.g.
    paid Substack posts fetched with the user's session cookies, or email
    bodies that exist only behind the user's IMAP login. Gated articles are
    pushed as HTML content submissions; ungated ones as bare URL saves."""


@dataclass(frozen=True, slots=True)
class ReaderSubmission:
    """One document to push to Readwise Reader.

    `url` is the document's identity in Reader (its server-side dedup key).
    When `html` is None, Reader fetches and parses `url` itself (URL
    submission - only valid for publicly fetchable pages). When `html` is
    set, Reader stores the supplied content and never fetches the URL
    (content submission - the path for paywalled/private content).
    """

    url: str
    html: str | None = None
    title: str | None = None
    author: str | None = None
    summary: str | None = None
    pub_date: datetime | None = None
    category: str | None = None  # Reader category hint (e.g. 'pdf'); None = let Reader guess

    @property
    def kind(self) -> str:
        """'html' for content submissions, 'url' for bare URL saves."""
        return "html" if self.html is not None else "url"
