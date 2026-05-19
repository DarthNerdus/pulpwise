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

import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import httpx

from pulpline.auth import CookieEntry, load_cookies_with_domain
from pulpline.models import ExtractionError, FetchError, ItemRef, Paywalled, RawArticle
from pulpline.sources.base import Source

if TYPE_CHECKING:
    from pulpline.config import Config

# Substack archive paginates; we list the most recent N. Re-running sync is
# cheap (dedup) so we don't paginate exhaustively in v0.2.
_DISCOVER_LIMIT = 25

_SAVED_HOSTS = {"substack.com", "www.substack.com"}
_SAVED_PATH = "/inbox/saved"
# Substack moved the saved-posts feed off `/api/v1/posts/saved` around
# May 2026 (that path now 404s). The current endpoint is a bucketed
# reader feed; passing `bucket=saved` gates the same data behind the
# user's session cookies as before. Response wraps posts under `posts`,
# which `_unwrap_post_list` already handles.
_SAVED_API = "https://substack.com/api/v1/reader/posts"
# The reader endpoint server-side-validates `limit` and rejects values
# >20 with a 400 ("Invalid value"). The old /posts/saved tolerated 25;
# this one doesn't. Keep this separate from `_DISCOVER_LIMIT` so the
# archive feed (which still accepts 25) doesn't get unnecessarily
# narrowed.
_SAVED_DISCOVER_LIMIT = 20
_HOME_POST_RX = re.compile(r"^/home/post/p-(\d+)/?$")


class SubstackSource(Source):
    name: ClassVar[str] = "substack"

    def __init__(
        self,
        client: httpx.Client | None = None,
        cookies: list[CookieEntry] | None = None,
    ) -> None:
        super().__init__(client=client)
        self._cookies = cookies
        self._publication_url: str = ""
        # Attach each cookie with its native domain so a single SubstackSource
        # can hold sessions for substack.com AND custom-domain publications
        # like astralcodexten.com simultaneously. httpx routes each cookie to
        # requests whose host matches the cookie's domain scope.
        if cookies:
            for c in cookies:
                self.client.cookies.set(c.name, c.value, domain=c.domain)

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        client: httpx.Client | None = None,
        subscription: object | None = None,
    ) -> SubstackSource:
        del subscription
        return cls(client=client, cookies=_load_substack_cookies(cfg))

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
        # Some saved posts come back with a Substack-reader canonical URL
        # like `https://substack.com/home/post/p-<id>` instead of the
        # publication's own per-post URL. Those don't resolve via the
        # standard /api/v1/posts/<slug> path. Resolve via posts/by-id to
        # discover the publication's subdomain + the post's slug, then
        # proceed normally.
        fetch_url = ref.url
        home_post_id = _home_post_id(fetch_url)
        if home_post_id is not None:
            resolved = self._resolve_home_post(home_post_id)
            if resolved is None:
                raise ExtractionError(
                    f"could not resolve {ref.url}: post may be deleted "
                    "or its publication isn't accessible to your account."
                )
            fetch_url = resolved

        base = _base_from_url(fetch_url)
        slug = _slug_from_url(fetch_url)
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
            audience = data.get("audience")
            if audience == "only_paid":
                host = _hostname(ref.url)
                if not self._cookies:
                    raise Paywalled(
                        "no cookies configured (set [auth.substack].cookies_path)",
                        host=host,
                    )
                # Cookies are present but the body still came back empty.
                # Most common cause: this publication runs on a custom
                # domain (ACX on astralcodexten.com etc.) and the user's
                # cookies only cover *.substack.com.
                if not host.endswith(".substack.com"):
                    raise Paywalled(
                        f"cookies don't authenticate against {host} (custom domain)",
                        host=host,
                    )
                raise Paywalled("body empty even with cookies attached", host=host)
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

    def _resolve_home_post(self, post_id: str) -> str | None:
        """Resolve `substack.com/home/post/p-<id>` to the publication's URL.

        Hits `/api/v1/posts/by-id/<id>`, reads `publication.subdomain` (or
        `custom_domain`) and `post.slug`, returns the per-publication URL.
        None on any failure - caller raises a friendlier error.
        """
        try:
            response = self.client.get(f"https://substack.com/api/v1/posts/by-id/{post_id}")
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError, ValueError:
            return None

        post = data.get("post") if isinstance(data, dict) else None
        publication = data.get("publication") if isinstance(data, dict) else None
        if not isinstance(post, dict) or not isinstance(publication, dict):
            return None

        slug = post.get("slug")
        if not isinstance(slug, str) or not slug:
            return None

        custom = publication.get("custom_domain")
        subdomain = publication.get("subdomain")
        if isinstance(custom, str) and custom.strip():
            return f"https://{custom.strip()}/p/{slug}"
        if isinstance(subdomain, str) and subdomain.strip():
            return f"https://{subdomain.strip()}.substack.com/p/{slug}"
        return None


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
            response = self.client.get(
                _SAVED_API,
                params={"bucket": "saved", "limit": str(_SAVED_DISCOVER_LIMIT)},
            )
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

        # The reader endpoint with bucket=saved returns mixed content: the
        # user's actual saves PLUS recommendations / new posts from
        # publications they follow. Only items whose post_id appears in
        # `savedPosts` are real saves. When the savedPosts array is present
        # in the payload we use it to filter; if absent (legacy / alternate
        # shape) we fall back to trusting the full `posts` array.
        if isinstance(payload, dict) and "savedPosts" in payload:
            saved_ids = _extract_saved_post_ids(payload["savedPosts"])
            if saved_ids is not None:
                posts = [p for p in posts if isinstance(p, dict) and p.get("id") in saved_ids]

        yield from _yield_post_refs(posts)

    def fetch(self, ref: ItemRef) -> RawArticle:
        """Saves land in one folder mixed across publications, so embed the
        author into the title to disambiguate. Per-publication feeds keep the
        author-less filename because the folder name already carries it."""
        article = super().fetch(ref)
        if article.author:
            return replace(article, title=f"{article.title} - {article.author}")
        return article


def _load_substack_cookies(cfg: Config) -> list[CookieEntry] | None:
    """Merge the primary + every extra cookie file referenced in config.

    Substack publications on custom domains (ACX on astralcodexten.com,
    Stratechery on stratechery.com) use their own session cookies. Users
    export those into separate files and list them under
    `[auth.substack].extra_cookies_paths`. Empty/missing config returns None.
    """
    auth = cfg.auth_for("substack")
    paths: list[Path] = []
    primary = auth.get("cookies_path")
    if isinstance(primary, str) and primary:
        paths.append(Path(primary).expanduser())
    extra = auth.get("extra_cookies_paths")
    if isinstance(extra, list):
        for p in extra:
            if isinstance(p, str):
                paths.append(Path(p).expanduser())

    if not paths:
        return None

    all_cookies: list[CookieEntry] = []
    for path in paths:
        all_cookies.extend(load_cookies_with_domain(path))
    return all_cookies


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


def _extract_saved_post_ids(saved_posts: object) -> set[int] | None:
    """Pull post_id values out of the savedPosts join-table array.

    The reader endpoint returns `savedPosts: [{user_id, post_id, created_at}]`
    alongside the mixed `posts` array; intersecting on post_id is what
    separates real saves from recommendations.

    Returns None when the input shape is unrecognized (caller will then
    treat all posts as saves - the legacy behavior). Returns a possibly
    empty set when the shape is valid; the caller's filter handles that.
    """
    if not isinstance(saved_posts, list):
        return None
    ids: set[int] = set()
    for entry in saved_posts:
        if isinstance(entry, dict) and isinstance(entry.get("post_id"), int):
            ids.add(entry["post_id"])
    return ids


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


def _home_post_id(url: str) -> str | None:
    """Return the post id if `url` is a `substack.com/home/post/p-<id>` URL.

    The Substack reader app sometimes stores the canonical_url for saved
    posts in this shape rather than the per-publication form. The standard
    /api/v1/posts/<slug> endpoint 404s on it; SubstackSource.fetch resolves
    via /api/v1/posts/by-id/<id> when this matches.
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in _SAVED_HOSTS:
        return None
    match = _HOME_POST_RX.match(parts.path)
    return match.group(1) if match else None


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
