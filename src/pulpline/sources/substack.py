"""Substack source plugin: list/fetch posts via Substack's unofficial JSON API.

Where the generic `rss` source pulls public-only content via /feed, this one
talks to Substack's archive + post endpoints directly. Reading paid content
requires session cookies (configured globally in `[auth.substack].cookies_path`).

Two source variants live here:
  * `SubstackSource` - per-publication archive, indexed by site URL.
  * `SubstackSavedSource` - the user's "saved for later" list across every
    publication, indexed by `https://substack.com/inbox/saved`. Inherits
    SubstackSource's auth + fetch logic; only the discover endpoint differs.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import httpx

from pulpline.auth import load_cookies
from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.sources.base import Source

if TYPE_CHECKING:
    from pulpline.config import Config

# Substack archive paginates; we list the most recent N. Re-running sync is
# cheap (dedup) so we don't paginate exhaustively in v0.2.
_DISCOVER_LIMIT = 25

_SAVED_HOSTS = {"substack.com", "www.substack.com"}
_SAVED_PATH = "/inbox/saved"
_SAVED_API = "https://substack.com/api/v1/posts/saved"


class SubstackSource(Source):
    name: ClassVar[str] = "substack"

    def __init__(
        self,
        client: httpx.Client | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        super().__init__(client=client)
        self._cookies = cookies
        self._publication_url: str = ""
        # Attach substack cookies to the client cookie jar with domain scope,
        # so httpx auto-attaches them only on requests to *.substack.com.
        # (httpx 0.28 deprecated per-request `cookies=` for ambiguous-persistence
        # reasons; attaching once with proper scope is the supported path.)
        if cookies:
            for name, value in cookies.items():
                self.client.cookies.set(name, value, domain=".substack.com")

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        client: httpx.Client | None = None,
        subscription: object | None = None,
    ) -> SubstackSource:
        del subscription
        cookies_path = cfg.auth_for("substack").get("cookies_path")
        cookies = load_cookies(Path(cookies_path)) if cookies_path else None
        return cls(client=client, cookies=cookies)

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        self._publication_url = target_url.rstrip("/")
        endpoint = f"{self._publication_url}/api/v1/archive"
        try:
            response = self.client.get(
                endpoint,
                params={"sort": "new", "limit": str(_DISCOVER_LIMIT)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to list archive {endpoint}: {exc}") from exc

        try:
            posts = response.json()
        except ValueError as exc:
            raise ExtractionError(f"archive {endpoint} did not return JSON") from exc

        if not isinstance(posts, list):
            raise ExtractionError(f"archive {endpoint} returned non-list")

        yield from _yield_post_refs(posts)

    def fetch(self, ref: ItemRef) -> RawArticle:
        base = _base_from_url(ref.url)
        slug = _slug_from_url(ref.url)
        endpoint = f"{base}/api/v1/posts/{slug}"

        try:
            response = self.client.get(endpoint)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch {ref.url}: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ExtractionError(f"post {ref.url} did not return JSON") from exc

        body_html = data.get("body_html")
        if not body_html:
            if data.get("audience") == "only_paid" and not self._cookies:
                raise ExtractionError(
                    f"{ref.url} is paywalled; configure [auth.substack].cookies_path"
                )
            raise ExtractionError(f"post {ref.url} has empty body_html")

        return RawArticle(
            title=_str_or_default(data.get("title"), "Untitled"),
            body_html=str(body_html),
            canonical_url=_str_or_default(data.get("canonical_url"), ref.url),
            source_url=self._publication_url or base,
            author=_first_byline_name(data),
            publisher=_publication_name(data) or _hostname(base),
            pub_date=_parse_iso(data.get("post_date")),
            language="en",
        )


class SubstackSavedSource(SubstackSource):
    """Source for the user's "saved for later" Substack posts.

    Subscribe by adding `https://substack.com/inbox/saved` - one
    subscription gives you everything you've saved across publications,
    growing as you save more. Each post is fetched via the parent class's
    logic, so paywalled saves work the same way they do for the
    per-publication source: same `[auth.substack].cookies_path` covers both.
    """

    name: ClassVar[str] = "substack-saved"

    @classmethod
    def matches_url(cls, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/")
        return host in _SAVED_HOSTS and path == _SAVED_PATH

    @classmethod
    def is_subscribable(cls, url: str) -> bool:
        del url
        return True

    @classmethod
    def default_subscription_name(cls, url: str) -> str:
        del url
        return "substack-saves"

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        self._publication_url = target_url.rstrip("/")
        if not self._cookies:
            raise FetchError(
                "Substack saved-posts requires login cookies. "
                "Set [auth.substack].cookies_path in config (export from your "
                "logged-in browser session)."
            )
        try:
            response = self.client.get(_SAVED_API, params={"limit": str(_DISCOVER_LIMIT)})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to list saved posts: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExtractionError("saved-posts endpoint did not return JSON") from exc

        # Substack's reverse-engineered shape: usually a flat list of post
        # objects, but some reader endpoints wrap them under `posts` /
        # `items` / `results`. Accept any of those before failing.
        posts = _unwrap_post_list(payload)
        if posts is None:
            raise ExtractionError(
                f"saved-posts endpoint returned unexpected shape: {type(payload).__name__}"
            )

        yield from _yield_post_refs(posts)


def _unwrap_post_list(payload: object) -> list[Any] | None:
    """Coerce a saves-API response into a list of post dicts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("posts", "items", "results", "saved_posts"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return inner
    return None


def _yield_post_refs(posts: list[Any]) -> Iterable[ItemRef]:
    """Yield ItemRefs from a list of Substack post objects.

    Some saves endpoints nest the post under a per-entry wrapper key
    (`{post: {...}}`); look one level deep before giving up on a row.
    """
    for entry in posts:
        if not isinstance(entry, dict):
            continue
        post = entry
        if "canonical_url" not in post and isinstance(entry.get("post"), dict):
            post = entry["post"]
        url = post.get("canonical_url")
        if not isinstance(url, str) or not url:
            continue
        yield ItemRef(
            url=url,
            title=post.get("title") if isinstance(post.get("title"), str) else None,
            pub_date=_parse_iso(post.get("post_date")),
            guid=str(post["id"]) if post.get("id") is not None else None,
        )


def _slug_from_url(url: str) -> str:
    parts = urlsplit(url).path.rstrip("/").split("/")
    return parts[-1] if parts and parts[-1] else url


def _base_from_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _hostname(url: str) -> str:
    return urlsplit(url).hostname or url


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _str_or_default(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _first_byline_name(data: dict[str, Any]) -> str | None:
    for key in ("publishedBylines", "postBylines"):
        bylines = data.get(key) or []
        if not isinstance(bylines, list):
            continue
        for b in bylines:
            if isinstance(b, dict):
                name = b.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return None


def _publication_name(data: dict[str, Any]) -> str | None:
    pub = data.get("publication") or {}
    if isinstance(pub, dict):
        name = pub.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None
