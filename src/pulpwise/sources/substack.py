"""Substack source plugin: list/fetch posts via Substack's unofficial JSON API.

Where the generic `rss` source pulls public-only content via /feed, this one
talks to Substack's archive + post endpoints directly. Reading paid content
requires session cookies (configured globally in `[auth.substack].cookies_path`).

Two source variants live here:
  * `SubstackSource` - per-publication archive, indexed by site URL.
  * `SubstackSavedSource` - the user's "saved for later" list across every
    publication, indexed by `https://substack.com/inbox/saved`. Inherits
    SubstackSource's auth + fetch logic; discovery (and backfill's backwards
    walk) go through the reader saves feed instead of a publication archive.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import httpx

from pulpwise.auth import CookieEntry, load_cookies_with_domain
from pulpwise.models import ExtractionError, FetchError, ItemRef, Paywalled, RawArticle
from pulpwise.sources.base import Source

if TYPE_CHECKING:
    from pulpwise.config import Config

# Substack archive paginates; we list the most recent N. Re-running sync is
# cheap (dedup) so we don't paginate exhaustively in v0.2.
_DISCOVER_LIMIT = 25

_SAVED_HOSTS = {"substack.com", "www.substack.com"}
_SAVED_PATH = "/inbox/saved"
# Substack moved the saved-posts feed off `/api/v1/posts/saved` around
# May 2026 (that path now 404s). The current endpoint is the reader
# feed the web app's inbox uses; `inboxType=saved` selects the saves-
# only view. (An earlier revision passed `bucket=saved`, which Substack
# silently ignores - that returned the generic inbox feed, mixing saves
# with new subscription posts.) Auth rides on the user's session
# cookies as before. Response wraps posts under `posts`, which
# `_unwrap_post_list` already handles.
_SAVED_API = "https://substack.com/api/v1/reader/posts"
# The reader endpoint server-side-validates `limit` and rejects values
# >20 with a 400 ("Invalid value"). The old /posts/saved tolerated 25;
# this one doesn't. Keep this separate from `_DISCOVER_LIMIT` so the
# archive feed (which still accepts 25) doesn't get unnecessarily
# narrowed.
_SAVED_DISCOVER_LIMIT = 20
_HOME_POST_RX = re.compile(r"^/home/post/p-(\d+)/?$")
# Cross-post URL pattern: when a Substack publication cross-posts an article
# from another publication, the link uses /cp/<post_id> rather than /p/<slug>.
# Like the home/post case, the standard /api/v1/posts/<slug> endpoint can't
# resolve it (the path segment is an id, not a slug); we route via by-id.
_CROSS_POST_RX = re.compile(r"^/cp/(\d+)/?$")


class SubstackSource(Source):
    name: ClassVar[str] = "substack"
    # Substack rate-limits per IP across ALL of its infrastructure - every
    # publication subdomain, custom domains, and substack.com itself - so
    # all Substack traffic shares one breaker bucket. Without this, a user
    # with 50 subscriptions would trip 50 per-host breakers one at a time.
    rate_limit_scope: ClassVar[str | None] = "substack"
    # Page size discover_backwards fetches per request; the pipeline reads
    # this to report backfill pages-walked honestly.
    backfill_page_size: ClassVar[int] = _DISCOVER_LIMIT

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
        posts = self._archive_page(offset=0, limit=_DISCOVER_LIMIT)
        yield from _yield_post_refs(posts)

    def discover_backwards(self, target_url: str) -> Iterable[ItemRef]:
        """Walk the publication's archive backwards via the `offset` param.

        Yields refs newest-first across the whole archive. Stops when the
        API returns an empty page (we've reached the oldest post). The
        caller is responsible for applying stop conditions (post count,
        date floor, dedup hit) - this method just keeps paginating.

        Each page is _DISCOVER_LIMIT items; we cap there because Substack
        rejects limit>50 with a 400. Going page-by-page keeps memory flat
        regardless of archive size and lets the caller break the iterator
        as soon as it's seen enough.
        """
        self._publication_url = target_url.rstrip("/")
        offset = 0
        while True:
            posts = self._archive_page(offset=offset, limit=_DISCOVER_LIMIT)
            if not posts:
                return
            yielded_this_page = 0
            for ref in _yield_post_refs(posts):
                yielded_this_page += 1
                yield ref
            offset += yielded_this_page or len(posts)

    def _archive_page(self, *, offset: int, limit: int) -> list[Any]:
        """Fetch one page of the publication archive. Returns post dicts."""
        endpoint = f"{self._publication_url}/api/v1/archive"
        params: dict[str, str] = {"sort": "new", "limit": str(limit)}
        if offset:
            params["offset"] = str(offset)
        try:
            response = self.client.get(endpoint, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to list archive {endpoint}: {exc}") from exc
        try:
            posts = response.json()
        except ValueError as exc:
            raise ExtractionError(f"archive {endpoint} did not return JSON") from exc
        if not isinstance(posts, list):
            raise ExtractionError(f"archive {endpoint} returned non-list")
        return posts

    def fetch(self, ref: ItemRef) -> RawArticle:
        # Two URL shapes carry a post *id* instead of a slug, so they can't
        # hit /api/v1/posts/<slug> directly:
        #   - `substack.com/home/post/p-<id>` (Substack-reader canonical for
        #     some saved posts)
        #   - `<pub>/cp/<id>` (a cross-post from another publication)
        # Both resolve via /api/v1/posts/by-id/<id>, which returns the
        # owning publication's subdomain + the post's slug; we then build
        # the canonical /p/<slug> URL and proceed normally.
        fetch_url = ref.url
        post_id = _home_post_id(fetch_url) or _cross_post_id(fetch_url)
        if post_id is not None:
            resolved = self._resolve_post_by_id(post_id)
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

        audience = data.get("audience")
        # Anything but an explicitly-public post is gated: "only_paid",
        # founding-tier posts ("founding"), and subscriber-only variants all
        # come back empty to Readwise's cookie-less server-side fetcher.
        # A missing audience field is treated as public (matches the
        # archive's behavior for plain free posts).
        gated = audience is not None and audience != "everyone"
        body_html = data.get("body_html")
        if not body_html:
            if gated:
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
            # Gated posts were fetched with the user's session cookies;
            # Readwise's server-side fetcher carries none, so their content
            # must ship in the submission. Free posts stay bare-URL saves.
            content_gated=gated,
        )

    def _resolve_post_by_id(self, post_id: str) -> str | None:
        """Resolve an id-bearing URL to the canonical /p/<slug> URL.

        Used for both `substack.com/home/post/p-<id>` (Substack reader's
        canonical for some saved posts) and `<pub>/cp/<id>` (cross-posts
        from another publication). Both shapes carry a post id where the
        slug would normally be; the standard /api/v1/posts/<slug> endpoint
        doesn't accept ids.

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
    # The reader/posts endpoint rejects limit>20, unlike the archive's 25.
    backfill_page_size: ClassVar[int] = _SAVED_DISCOVER_LIMIT

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
        self._require_cookies()
        yield from self._refs_from_payload(self._saved_page())

    def discover_backwards(self, target_url: str) -> Iterable[ItemRef]:
        """Walk the whole saved list backwards, newest saves first.

        Pagination mirrors the web app's infinite scroll on
        substack.com/inbox/saved: each page is requested with
        `after=<oldest save-time on the previous page>` and the response's
        `more` flag says whether older saves remain. The feed is ordered by
        *save time* (`savedPosts[].created_at`), and `after` filters on that
        same timestamp. (Substack's own frontend passes the last item's
        `content_date` instead - which skips saves whose publish date is
        newer than the cutoff; verified empirically. We pass the save time.)

        Refs therefore come in save-time order, not publish order: an old
        post saved yesterday is yielded before a new post saved last month.
        A backfill date floor (`--since`, compared against pub_date) can
        stop the walk before reaching recently-saved older posts.
        """
        self._publication_url = target_url.rstrip("/")
        self._require_cookies()
        after: str | None = None
        cursor: str | None = None
        while True:
            payload = self._saved_page(after=after, cursor=cursor)
            yield from self._refs_from_payload(payload)
            if not isinstance(payload, dict) or not payload.get("more"):
                return
            oldest = _oldest_save_time(payload)
            if oldest is None or oldest == after:
                # No save timestamps to page on (shape change) or no forward
                # progress: stop rather than refetch the same page forever.
                return
            after = oldest
            raw_cursor = payload.get("cursor")
            cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None

    def _require_cookies(self) -> None:
        if not self._cookies:
            raise FetchError(
                "Substack saved-posts requires login cookies. "
                "Set [auth.substack].cookies_path in config (export from your "
                "logged-in browser session)."
            )

    def _saved_page(self, *, after: str | None = None, cursor: str | None = None) -> Any:
        """Fetch one page of the saves feed. Returns the raw JSON payload."""
        params: dict[str, str] = {
            "inboxType": "saved",
            "limit": str(_SAVED_DISCOVER_LIMIT),
        }
        if after:
            params["after"] = after
        if cursor:
            params["cursor"] = cursor
        try:
            response = self.client.get(_SAVED_API, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to list saved posts: {exc}") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise ExtractionError("saved-posts endpoint did not return JSON") from exc

    def _refs_from_payload(self, payload: Any) -> Iterable[ItemRef]:
        # Substack's reverse-engineered shape: usually {posts: [...], ...},
        # but some reader endpoints return a flat list or wrap under
        # `items` / `results`. Accept any of those before failing.
        posts = _unwrap_post_list(payload)
        if posts is None:
            raise ExtractionError(
                f"saved-posts endpoint returned unexpected shape: {type(payload).__name__}"
            )

        # With inboxType=saved the feed is saves-only in practice, but keep
        # intersecting on `savedPosts` (the join table alongside `posts`) as
        # a guard: if Substack ever mixes other content back in, only items
        # whose post_id appears there are real saves. Absent savedPosts
        # (legacy / alternate shape) we trust the full `posts` array.
        if isinstance(payload, dict) and "savedPosts" in payload:
            saved_ids = _extract_saved_post_ids(payload["savedPosts"])
            if saved_ids is not None:
                posts = [p for p in posts if isinstance(p, dict) and p.get("id") in saved_ids]

        return _yield_post_refs(posts)


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


def _oldest_save_time(payload: dict[str, Any]) -> str | None:
    """The saves-feed pagination cursor: oldest `savedPosts[].created_at`.

    The feed is ordered by save time descending and `after` filters on that
    timestamp, so the oldest save time on this page is exactly the `after`
    value that yields the next page. Timestamps are same-format ISO-8601
    Zulu strings from one endpoint, so `min` compares them correctly.
    """
    entries = payload.get("savedPosts")
    if not isinstance(entries, list):
        return None
    times = [
        e["created_at"]
        for e in entries
        if isinstance(e, dict) and isinstance(e.get("created_at"), str) and e["created_at"]
    ]
    return min(times) if times else None


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


def _cross_post_id(url: str) -> str | None:
    """Return the post id if `url` is a `<pub>/cp/<id>` cross-post URL.

    Cross-posts can live on any Substack publication domain (custom or
    subdomain), so we don't restrict by hostname here - we just match the
    path shape. Combined with the regex's anchoring this is unambiguous.
    """
    parts = urlsplit(url)
    match = _CROSS_POST_RX.match(parts.path)
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
