"""Tests for SubstackSavedSource - the saves-list discover path.

Fetch logic is inherited from SubstackSource and exercised in
test_substack_source.py; here we focus on what's new: saves-endpoint URL
matching, the cookies-required guard, response-shape coercion, and
iteration over the resulting refs.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pulpwise.auth import CookieEntry
from pulpwise.models import ExtractionError, FetchError, ItemRef
from pulpwise.sources.substack import SubstackSavedSource

SAVES_API = "https://substack.com/api/v1/reader/posts"
SAVES_URL = "https://substack.com/inbox/saved"


def _post(slug: str, title: str, post_id: int) -> dict[str, Any]:
    return {
        "id": post_id,
        "canonical_url": f"https://samkriss.substack.com/p/{slug}",
        "title": title,
        "post_date": "2026-04-01T12:00:00.000Z",
    }


def _routed_client(routes: dict[str, Any], status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url in routes:
            return httpx.Response(status, json=routes[url])
        return httpx.Response(404, text=f"unmocked: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_matches_url_recognizes_inbox_saved() -> None:
    assert SubstackSavedSource.matches_url("https://substack.com/inbox/saved")
    assert SubstackSavedSource.matches_url("https://www.substack.com/inbox/saved/")


def test_matches_url_rejects_publication_urls() -> None:
    assert not SubstackSavedSource.matches_url("https://samkriss.substack.com")
    assert not SubstackSavedSource.matches_url("https://substack.com/")
    assert not SubstackSavedSource.matches_url("https://substack.com/inbox")


def test_is_subscribable_true() -> None:
    assert SubstackSavedSource.is_subscribable(SAVES_URL)


def test_default_subscription_name_is_stable() -> None:
    assert SubstackSavedSource.default_subscription_name(SAVES_URL) == "substack-saves"


def test_discover_without_cookies_raises_clear_error() -> None:
    with SubstackSavedSource() as source, pytest.raises(FetchError, match="cookies"):
        list(source.discover(SAVES_URL))


def test_discover_yields_itemref_per_saved_post_flat_list() -> None:
    payload = [_post("a", "Post A", 1), _post("b", "Post B", 2)]
    client = _routed_client({SAVES_API: payload})

    with SubstackSavedSource(
        client=client, cookies=[CookieEntry(name="session", value="x", domain=".substack.com")]
    ) as source:
        refs = list(source.discover(SAVES_URL))

    assert {r.url for r in refs} == {
        "https://samkriss.substack.com/p/a",
        "https://samkriss.substack.com/p/b",
    }


def test_discover_handles_posts_wrapper() -> None:
    """Some Substack reader endpoints wrap the list as {posts: [...]}."""
    payload = {"posts": [_post("c", "Post C", 3)]}
    client = _routed_client({SAVES_API: payload})

    with SubstackSavedSource(
        client=client, cookies=[CookieEntry(name="session", value="x", domain=".substack.com")]
    ) as source:
        refs = list(source.discover(SAVES_URL))

    assert [r.url for r in refs] == ["https://samkriss.substack.com/p/c"]


def test_discover_handles_post_wrapper_per_entry() -> None:
    """Some saved-list shapes nest the post under a 'post' key per entry."""
    payload = [{"post": _post("d", "Post D", 4)}]
    client = _routed_client({SAVES_API: payload})

    with SubstackSavedSource(
        client=client, cookies=[CookieEntry(name="session", value="x", domain=".substack.com")]
    ) as source:
        refs = list(source.discover(SAVES_URL))

    assert [r.url for r in refs] == ["https://samkriss.substack.com/p/d"]


def test_discover_raises_on_garbage_shape() -> None:
    client = _routed_client({SAVES_API: {"unexpected": "format"}})
    with (
        SubstackSavedSource(
            client=client, cookies=[CookieEntry(name="session", value="x", domain=".substack.com")]
        ) as source,
        pytest.raises(ExtractionError, match="unexpected shape"),
    ):
        list(source.discover(SAVES_URL))


def test_fetch_resolves_home_post_url_via_by_id_endpoint() -> None:
    """A `substack.com/home/post/p-<id>` URL routes through /api/v1/posts/by-id/<id>."""
    home_url = "https://substack.com/home/post/p-194527278"
    by_id = "https://substack.com/api/v1/posts/by-id/194527278"
    publication_post = "https://lewislackey.substack.com/api/v1/posts/why-veganism-is-false"

    routes: dict[str, Any] = {
        by_id: {
            "post": {
                "id": 194527278,
                "slug": "why-veganism-is-false",
                "canonical_url": home_url,
            },
            "publication": {
                "id": 3733672,
                "subdomain": "lewislackey",
                "custom_domain": None,
            },
        },
        publication_post: {
            "title": "Why Veganism is False",
            "canonical_url": "https://lewislackey.substack.com/p/why-veganism-is-false",
            "body_html": "<p>real body</p>",
            "audience": "everyone",
            "post_date": "2026-04-17T18:32:45.670Z",
            "publishedBylines": [{"name": "Lewis Lackey"}],
            "publication": {"name": "Lewis' Lackey"},
        },
    }

    with SubstackSavedSource(
        client=_routed_client(routes),
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        article = source.fetch(ItemRef(url=home_url))

    assert article.body_html == "<p>real body</p>"
    assert article.title == "Why Veganism is False"
    assert article.author == "Lewis Lackey"


def test_fetch_keeps_plain_title_with_author_as_metadata() -> None:
    """No more " - <author>" title suffix (that was EPUB filename
    disambiguation); author is a first-class Readwise field instead."""
    base = "https://samkriss.substack.com"
    routes = {
        f"{base}/api/v1/posts/post-one": {
            "title": "Some Saved Post",
            "canonical_url": f"{base}/p/post-one",
            "body_html": "<p>body</p>",
            "audience": "everyone",
            "post_date": "2026-04-30T12:00:00.000Z",
            "publishedBylines": [{"name": "Sam Kriss"}],
            "publication": {"name": "Numb at the Lodge"},
        }
    }
    client = _routed_client(routes)
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        article = source.fetch(ItemRef(url=f"{base}/p/post-one"))

    assert article.title == "Some Saved Post"
    assert article.author == "Sam Kriss"


def test_discover_raises_on_http_failure() -> None:
    client = _routed_client({}, status=500)  # 404s everything because no route
    with (
        SubstackSavedSource(
            client=client, cookies=[CookieEntry(name="session", value="x", domain=".substack.com")]
        ) as source,
        pytest.raises(FetchError, match="failed to list saved"),
    ):
        list(source.discover(SAVES_URL))


def test_discover_filters_posts_by_saved_post_ids() -> None:
    """The reader endpoint mixes real saves with recommendations. We must
    intersect `posts` with the `savedPosts.post_id` set so non-saved items
    (new posts from publications you follow, recs, etc.) don't get
    ingested into the saves bucket."""
    real_save = {**_post("kept", "Real Save", 1001)}
    leaked_rec = {**_post("leaked", "Recommendation Leak", 9999)}
    payload = {
        "posts": [real_save, leaked_rec],
        "savedPosts": [
            {"user_id": 1, "post_id": 1001, "created_at": "2026-05-19T11:00:00.000Z"},
        ],
    }
    client = _routed_client({SAVES_API: payload})
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        refs = list(source.discover(SAVES_URL))

    # Only id=1001 is in savedPosts; id=9999 must be filtered out.
    assert [r.guid for r in refs] == ["1001"]
    assert [r.title for r in refs] == ["Real Save"]


def test_discover_falls_back_to_all_posts_when_saved_posts_key_missing() -> None:
    """Legacy / alternate response shapes don't carry `savedPosts`. In that
    case the filter is a no-op so we don't regress the old behavior."""
    payload = {"posts": [_post("a", "Post A", 1), _post("b", "Post B", 2)]}
    client = _routed_client({SAVES_API: payload})
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        refs = list(source.discover(SAVES_URL))
    # No savedPosts key -> trust all posts.
    assert {r.title for r in refs} == {"Post A", "Post B"}


def test_discover_empty_saved_posts_array_yields_no_refs() -> None:
    """When the user has zero saves, posts may still be non-empty (recs only)
    but savedPosts is []. Nothing should be ingested."""
    payload = {
        "posts": [_post("a", "Recommendation", 1)],
        "savedPosts": [],
    }
    client = _routed_client({SAVES_API: payload})
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        refs = list(source.discover(SAVES_URL))
    assert refs == []


def test_discover_sends_inbox_type_saved_query_param() -> None:
    """Pin the contract with Substack's reader endpoint: inboxType=saved must
    be in the query. The endpoint serves several inbox views; without it we'd
    get the generic inbox feed (saves mixed with new subscription posts).
    Notably `bucket=saved` is NOT the parameter - Substack silently ignores
    unknown params, so that variant *appears* to work while reading the
    wrong feed."""
    seen: dict[str, str | None] = {"query": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode("ascii")
        return httpx.Response(200, json=[_post("a", "Post A", 1)])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        list(source.discover(SAVES_URL))

    assert seen["query"] is not None
    assert "inboxType=saved" in seen["query"]


# ---- discover_backwards: the backfill walk over the saves feed ----


def _saved_page(posts: list[dict[str, Any]], saved_at: list[str], *, more: bool) -> dict[str, Any]:
    """Reader-feed page: posts + the savedPosts join array + `more` flag."""
    return {
        "posts": posts,
        "savedPosts": [
            {"user_id": 1, "post_id": p["id"], "created_at": t}
            for p, t in zip(posts, saved_at, strict=True)
        ],
        "more": more,
    }


def test_discover_backwards_requires_cookies() -> None:
    with SubstackSavedSource() as source, pytest.raises(FetchError, match="cookies"):
        list(source.discover_backwards(SAVES_URL))


def test_discover_backwards_pages_with_after_equal_to_oldest_save_time() -> None:
    """The walk hits the reader feed (never `<url>/api/v1/archive` - the 404
    that broke saved-list backfill), passing `after=<oldest saved_at of the
    previous page>` until `more` is false."""
    pages: dict[str | None, dict[str, Any]] = {
        None: _saved_page(
            [_post("a", "Post A", 1), _post("b", "Post B", 2)],
            ["2026-06-02T00:00:00.000Z", "2026-06-01T00:00:00.000Z"],
            more=True,
        ),
        "2026-06-01T00:00:00.000Z": _saved_page(
            [_post("c", "Post C", 3)],
            ["2026-05-01T00:00:00.000Z"],
            more=False,
        ),
    }
    seen_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).split("?")[0] != SAVES_API:
            return httpx.Response(404, text=f"unmocked: {request.url}")
        seen_queries.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("after")])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        refs = list(source.discover_backwards(SAVES_URL))

    assert [r.title for r in refs] == ["Post A", "Post B", "Post C"]
    assert len(seen_queries) == 2
    assert all(q["inboxType"] == "saved" for q in seen_queries)
    assert "after" not in seen_queries[0]
    assert seen_queries[1]["after"] == "2026-06-01T00:00:00.000Z"


def test_discover_backwards_stops_without_save_timestamps() -> None:
    """`more=true` but no savedPosts to page on (shape change): stop after one
    page instead of refetching the same page forever."""
    payload = {"posts": [_post("a", "Post A", 1)], "more": True}
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(str(request.url))
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        refs = list(source.discover_backwards(SAVES_URL))

    assert [r.title for r in refs] == ["Post A"]
    assert len(requests_made) == 1


def test_discover_backwards_stops_when_after_makes_no_progress() -> None:
    """A server that keeps returning the same page (same oldest save time,
    more=true) must not loop: the no-progress guard ends the walk."""
    payload = _saved_page([_post("a", "Post A", 1)], ["2026-06-01T00:00:00.000Z"], more=True)
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(str(request.url))
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        refs = list(source.discover_backwards(SAVES_URL))

    # Page 1 (after=None) advances to after=T1; page 2 returns the same
    # oldest save time, so the guard stops before a third request.
    assert len(requests_made) == 2
    assert len(refs) == 2  # dup ref is fine - the dedup ledger drops it


def test_discover_backwards_threads_cursor_through_when_present() -> None:
    """When a page carries a `cursor`, the next request passes it along
    (matching the web app, which forwards `result.cursor` when set)."""
    page1 = _saved_page([_post("a", "Post A", 1)], ["2026-06-01T00:00:00.000Z"], more=True)
    page1["cursor"] = "opaque-token"
    page2 = _saved_page([_post("b", "Post B", 2)], ["2026-05-01T00:00:00.000Z"], more=False)
    seen_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(dict(request.url.params))
        return httpx.Response(200, json=page2 if "after" in request.url.params else page1)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SubstackSavedSource(
        client=client,
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        refs = list(source.discover_backwards(SAVES_URL))

    assert [r.title for r in refs] == ["Post A", "Post B"]
    assert "cursor" not in seen_queries[0]
    assert seen_queries[1]["cursor"] == "opaque-token"
