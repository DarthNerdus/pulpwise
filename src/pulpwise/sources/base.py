"""Source ABC. The contract every plugin (rss, url, substack, arxiv, email) implements."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, Self
from urllib.parse import urlsplit

import httpx

from pulpwise.models import ItemRef, RawArticle, ReaderSubmission
from pulpwise.util.http import build_client

if TYPE_CHECKING:
    from pulpwise.config import Config, Subscription


def synthetic_url(seed: str) -> str:
    """Deterministic https URL for content that has no fetchable web URL.

    Readwise requires a `url` on every save and uses it as the document's
    server-side dedup key, so the value must be stable across runs - a
    crash-retry of the same item has to hit the duplicate path (HTTP 200)
    instead of creating a second document. Derived from the item's own
    stable identity (e.g. an email's `mid:<Message-ID>` dedup URL).
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"https://pulpwise.invalid/{digest}"


class Source(ABC):
    """Two-phase content source.

    `discover` is cheap: list items available at a target URL without
    downloading bodies. The orchestrator dedups against persisted state
    before doing per-item work, so only un-seen items cost anything.

    Per un-seen item, the pipeline builds a `ReaderSubmission`:
    sources with `fetch_needed = False` map the `ItemRef` straight to a
    bare-URL save via `submission_for_ref` (no fetch at all - Readwise
    does its own extraction server-side); sources that must fetch (to
    resolve URLs, check entitlement, or capture gated content) go through
    `fetch` + `submission_for_article`.

    For one-shot URL ingestion, `discover` yields exactly one `ItemRef`
    pointing at the URL itself.
    """

    name: ClassVar[str]

    fetch_needed: ClassVar[bool] = True
    """False for sources whose discovered URLs are publicly fetchable as-is
    (RSS entries, arXiv papers, plain web pages): the pipeline skips `fetch`
    entirely and submits the URL for Readwise to extract server-side. True
    for sources that must fetch each item first - to resolve gated/reader-app
    URLs, learn paywall status, or capture content Readwise can't reach."""

    rate_limit_scope: ClassVar[str | None] = None
    """Shared rate-limit bucket for every request this source makes, or None.

    None (default) keys the 429 breaker in `pulpwise.util.http` per host -
    right for generic feeds, where different hosts are different servers.
    Sources whose requests all land on one provider's infrastructure behind
    one limiter (Substack: publication subdomains, custom domains, and
    substack.com itself) declare a scope so the first tripped breaker
    protects every subscription on that provider, instead of each host
    rediscovering the same limiter a retry-cycle at a time."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self.last_known_total: int | None = None
        """Optional source-reported total population for the current target.

        Set during `discover()` if the source has a meaningful total. The
        pipeline reads this after discover and persists it via
        `update_subscription_state(total_items=...)`.
        """

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = build_client(scope=self.rate_limit_scope)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        client: httpx.Client | None = None,
        subscription: Subscription | None = None,
    ) -> Source:
        """Construct an instance from the loaded Config and (optionally) a subscription.

        Default impl: ignore everything, just pass the client. Sources that need
        cross-subscription auth (Substack cookies) read from `cfg.auth.*`.
        Sources with per-subscription knobs (email `since_days`, RSS category
        filters) read from `subscription.*`. The subscription is None on
        one-shot paths.
        """
        del cfg, subscription
        return cls(client=client)

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """Return True if this source class should handle `url` during one-shot.

        Default: False. URLSource is the fallback when no other source claims
        the URL, so most sources don't need to override this. ArXiv overrides
        to claim `arxiv.org` URLs.
        """
        del url
        return False

    @classmethod
    def is_subscribable(cls, url: str) -> bool:
        """Return True if `url` is a subscription target (feed-like) for this source.

        Used by the CLI's auto-detect path: when a source claims a URL via
        `matches_url`, we ask whether the URL points at a subscribable
        listing (e.g. an arXiv API query) or a single item (arXiv `/abs/<id>`).

        Default: False. Most sources don't subscribe via URL pattern -
        URLSource never does, RSSSource is auto-detected via feedparser.
        """
        del url
        return False

    @classmethod
    def default_subscription_name(cls, url: str) -> str:
        """Suggest a default subscription name from a URL.

        Used when `pulpwise add <url>` doesn't get an explicit `--name`. Default:
        last non-id-like path segment (good for `/title/<uuid>/berserk` and
        `/sub/foo`-style URLs). Sources whose subscription URL puts the
        identifying info in the query string (arXiv API queries) override.
        """
        parts = urlsplit(url)
        segments = [p for p in parts.path.strip("/").split("/") if p]
        for seg in reversed(segments):
            if seg and not _looks_id_like(seg):
                return _slug(seg)
        host = parts.hostname or "feed"
        return _slug(host.split(".")[0])

    @abstractmethod
    def discover(self, target_url: str) -> Iterable[ItemRef]:
        """Cheap listing of items available at `target_url`. No body downloads."""

    @abstractmethod
    def fetch(self, ref: ItemRef) -> RawArticle:
        """Download + extract the body for a single item. Expensive.

        Only called by the pipeline when `fetch_needed` is True; sources
        that never fetch may raise NotImplementedError.
        """

    def submission_for_ref(self, ref: ItemRef) -> ReaderSubmission:
        """Map a discovered item straight to a bare-URL save (no fetch).

        Used when `fetch_needed` is False. The ref's URL must be publicly
        fetchable - Readwise's server-side parser does the extraction.
        Title/date from the ref ride along as metadata (a client-supplied
        title overwrites Reader's parsed one, so sources should only fill
        `ItemRef.title` with values worth keeping).
        """
        return ReaderSubmission(url=ref.url, title=ref.title, pub_date=ref.pub_date)

    def submission_for_article(self, article: RawArticle) -> ReaderSubmission:
        """Turn a fetched article into a submission, routing on gatedness.

        Publicly fetchable articles become bare-URL saves of their canonical
        URL (Readwise re-extracts server-side - fresher and updatable).
        Gated articles - `content_gated`, or a canonical URL that isn't a
        web URL at all (email's `mid:` scheme) - are pushed as HTML content
        submissions with explicit metadata, under a deterministic synthetic
        URL when no real one exists.
        """
        url = article.canonical_url
        gated = article.content_gated
        if not url.startswith(("http://", "https://")):
            url = synthetic_url(article.canonical_url)
            gated = True
        if not gated:
            return ReaderSubmission(
                url=url,
                title=article.title or None,
                author=article.author,
                pub_date=article.pub_date,
            )
        return ReaderSubmission(
            url=url,
            html=article.body_html,
            title=article.title or None,
            author=article.author,
            pub_date=article.pub_date,
        )


def _looks_id_like(segment: str) -> bool:
    """True for UUIDs, long hex strings, or pure-digit segments."""
    if segment.isdigit():
        return True
    return bool(re.match(r"^[0-9a-f-]{16,}$", segment))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "feed"
