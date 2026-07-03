"""Source ABC. The contract every plugin (rss, url, MangaDex, arXiv, ...) implements."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self
from urllib.parse import urlsplit

import httpx

from pulpline.models import ItemRef, RawArticle
from pulpline.util.http import build_client

if TYPE_CHECKING:
    from pulpline.config import Config, Subscription


class Source(ABC):
    """Two-phase content source.

    `discover` is cheap: list items available at a target URL without
    downloading bodies. The orchestrator dedups against persisted state
    (Phase 2+) before calling `fetch`, so only un-seen items pay the
    HTTP/extraction cost.

    For one-shot URL ingestion, `discover` yields exactly one `ItemRef`
    pointing at the URL itself.
    """

    name: ClassVar[str]
    extension: ClassVar[str] = "epub"
    """File extension for items this source produces. Most sources emit EPUBs;
    binary-format sources (arXiv -> PDF, MangaDex -> CBZ) override this."""

    rate_limit_scope: ClassVar[str | None] = None
    """Shared rate-limit bucket for every request this source makes, or None.

    None (default) keys the 429 breaker in `pulpline.util.http` per host -
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

        Set during `discover()` if the source has a meaningful total (e.g.
        MangaDex's `/feed` returns `total` indicating chapter count). The
        pipeline reads this after discover and persists it via
        `update_subscription_state(total_items=...)`.
        """
        self.output_dir: Path | None = None
        """Optional source-level output directory override.

        None (default) lets the pipeline pick the destination. Sources whose
        config names an explicit destination (Anna's Archive's
        `[auth.annas].output_dir`) set an expanded absolute path here in
        `from_config`; `add_once` then writes there instead of
        `<output_dir>/oneshots/`.
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
        Sources with per-subscription knobs (MangaDex language) read from
        `subscription.*`. The subscription is None on one-shot paths.
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
        listing (e.g. arXiv API query, MangaDex title page) or a single item
        (arXiv `/abs/<id>`, MangaDex `/chapter/<id>`).

        Default: False. Most sources don't subscribe via URL pattern -
        URLSource never does, RSSSource is auto-detected via feedparser.
        """
        del url
        return False

    @classmethod
    def default_subscription_name(cls, url: str) -> str:
        """Suggest a default subscription name from a URL.

        Used when `pulp add <url>` doesn't get an explicit `--name`. Default:
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
        """Download + extract the body for a single item. Expensive."""

    def render(self, article: RawArticle) -> bytes:
        """Render the fetched article to deliverable bytes.

        Default: render the article's HTML body to EPUB via ebooklib. Binary
        sources (arXiv PDFs, future MangaDex CBZs) override this to return
        the raw bytes directly. The result is paired with `cls.extension`
        when the sink writes the file.
        """
        from pulpline.renderers.epub import EpubRenderer

        return EpubRenderer().render(article)


def _looks_id_like(segment: str) -> bool:
    """True for UUIDs, long hex strings, or pure-digit segments."""
    if segment.isdigit():
        return True
    return bool(re.match(r"^[0-9a-f-]{16,}$", segment))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "feed"
