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
from pulpline.models import ExtractionError, FetchError
from pulpline.sources.substack import SubstackSavedSource

SAVES_API = "https://substack.com/api/v1/posts/saved"
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


def test_discover_raises_on_http_failure() -> None:
    client = _routed_client({}, status=500)  # 404s everything because no route
    with (
        SubstackSavedSource(
            client=client, cookies=[CookieEntry(name="session", value="x", domain=".substack.com")]
        ) as source,
        pytest.raises(FetchError, match="failed to list saved"),
    ):
        list(source.discover(SAVES_URL))
