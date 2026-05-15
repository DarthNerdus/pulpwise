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

from pulpline.auth import CookieEntry
from pulpline.models import ExtractionError, FetchError, ItemRef
from pulpline.sources.substack import SubstackSavedSource

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
    # Saves embed author in title; we already exercise that elsewhere but
    # verify it stacks with home/post resolution.
    assert article.title == "Why Veganism is False - Lewis Lackey"


def test_fetch_embeds_author_in_title_for_saved_one_shots() -> None:
    """Saves land in one folder mixed across publications; filename needs author."""
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

    assert article.title == "Some Saved Post - Sam Kriss"
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


def test_discover_sends_bucket_saved_query_param() -> None:
    """Pin the contract with Substack's reader endpoint: bucket=saved must be in the
    query. The endpoint serves multiple buckets; without bucket=saved we'd get
    a different feed entirely (or a 400)."""
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
    assert "bucket=saved" in seen["query"]
