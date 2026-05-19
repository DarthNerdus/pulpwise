"""Tests for SubstackSource - mocked httpx against Substack's API shape."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pulpline.auth import CookieEntry
from pulpline.config import Config
from pulpline.models import ExtractionError, FetchError, ItemRef
from pulpline.sources.substack import SubstackSource


def _archive_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": 101,
            "canonical_url": "https://samkriss.substack.com/p/post-one",
            "title": "Post One",
            "post_date": "2026-04-30T12:00:00.000Z",
        },
        {
            "id": 102,
            "canonical_url": "https://samkriss.substack.com/p/post-two",
            "title": "Post Two",
            "post_date": "2026-04-29T12:00:00.000Z",
        },
    ]


def _post_payload(*, paywalled: bool = False, body: str = "<p>body</p>") -> dict[str, Any]:
    return {
        "title": "Post One",
        "canonical_url": "https://samkriss.substack.com/p/post-one",
        "body_html": "" if paywalled else body,
        "audience": "only_paid" if paywalled else "everyone",
        "post_date": "2026-04-30T12:00:00.000Z",
        "publishedBylines": [{"name": "Sam Kriss"}],
        "publication": {"name": "Numb at the Lodge"},
    }


def _routed_client(routes: dict[str, Any]) -> httpx.Client:
    """Build an httpx.Client whose MockTransport returns JSON for matching URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url in routes:
            return httpx.Response(200, json=routes[url])
        return httpx.Response(404, text=f"unmocked: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_yields_itemrefs_for_each_post() -> None:
    base = "https://samkriss.substack.com"
    client = _routed_client({f"{base}/api/v1/archive": _archive_payload()})

    with SubstackSource(client=client) as source:
        refs = list(source.discover(base))

    assert {r.url for r in refs} == {
        f"{base}/p/post-one",
        f"{base}/p/post-two",
    }
    titles = {r.title for r in refs}
    assert "Post One" in titles


def test_discover_raises_fetch_error_on_http_failure() -> None:
    client = _routed_client({})  # 404 everything
    with SubstackSource(client=client) as source, pytest.raises(FetchError):
        list(source.discover("https://samkriss.substack.com"))


def test_fetch_returns_rawarticle_for_public_post() -> None:
    base = "https://samkriss.substack.com"
    client = _routed_client({f"{base}/api/v1/posts/post-one": _post_payload()})

    with SubstackSource(client=client) as source:
        article = source.fetch(ItemRef(url=f"{base}/p/post-one"))

    assert article.title == "Post One"
    assert article.author == "Sam Kriss"
    assert article.publisher == "Numb at the Lodge"
    assert article.body_html == "<p>body</p>"
    assert article.canonical_url == f"{base}/p/post-one"
    assert article.pub_date is not None
    assert article.pub_date.year == 2026


def test_fetch_paywalled_without_cookies_raises_paywalled_with_host() -> None:
    """Empty body + audience=only_paid + no cookies -> Paywalled with host attached.

    The orchestrator buckets these separately from generic errors so the
    CLI can render a single 'fix your cookies for X' hint per host.
    """
    from pulpline.models import Paywalled

    base = "https://samkriss.substack.com"
    client = _routed_client({f"{base}/api/v1/posts/post-one": _post_payload(paywalled=True)})
    with (
        SubstackSource(client=client) as source,
        pytest.raises(Paywalled) as exc_info,
    ):
        source.fetch(ItemRef(url=f"{base}/p/post-one"))
    # Paywalled subclasses ExtractionError so existing catches keep working.
    assert isinstance(exc_info.value, ExtractionError)
    assert exc_info.value.host == "samkriss.substack.com"


def test_from_config_loads_cookies_when_configured(tmp_path: Any) -> None:
    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text(
        json.dumps([{"name": "substack.sid", "value": "abc"}]),
        encoding="utf-8",
    )
    cfg = Config(auth={"substack": {"cookies_path": str(cookies_file)}})

    source = SubstackSource.from_config(cfg)
    assert source._cookies is not None
    assert {(c.name, c.value) for c in source._cookies} == {("substack.sid", "abc")}


def test_from_config_no_cookies_when_not_configured() -> None:
    cfg = Config()
    source = SubstackSource.from_config(cfg)
    assert source._cookies is None


def test_fetch_resolves_cross_post_url_via_by_id_endpoint() -> None:
    """A `<pub>/cp/<id>` cross-post URL routes through /api/v1/posts/by-id/<id>,
    same as the home-post case but originating from a regular subscription
    sync rather than the saves feed."""
    cp_url = "https://samkriss.substack.com/cp/189381253"
    by_id = "https://substack.com/api/v1/posts/by-id/189381253"
    resolved_post = "https://samkriss.substack.com/api/v1/posts/childs-play"

    routes: dict[str, Any] = {
        by_id: {
            "post": {
                "id": 189381253,
                "slug": "childs-play",
                "canonical_url": cp_url,
            },
            "publication": {
                "subdomain": "samkriss",
                "custom_domain": None,
            },
        },
        resolved_post: {
            "title": "Child's Play",
            "canonical_url": "https://samkriss.substack.com/p/childs-play",
            "body_html": "<p>body</p>",
            "audience": "everyone",
            "post_date": "2026-02-27T12:00:00.000Z",
            "publishedBylines": [{"name": "Sam Kriss"}],
            "publication": {"name": "Numb at the Lodge"},
        },
    }

    with SubstackSource(
        client=_routed_client(routes),
        cookies=[CookieEntry(name="session", value="x", domain=".substack.com")],
    ) as source:
        article = source.fetch(ItemRef(url=cp_url))

    assert article.title == "Child's Play"
    assert article.body_html == "<p>body</p>"
    assert article.author == "Sam Kriss"


def test_discover_backwards_paginates_via_offset_until_empty_page() -> None:
    """discover_backwards walks offset=0, 25, 50, ... until the archive returns []."""
    base = "https://samkriss.substack.com"

    # Three full pages of distinct posts, then an empty page that should stop iteration.
    def page(start: int, count: int) -> list[dict[str, Any]]:
        return [
            {
                "id": start + i,
                "canonical_url": f"{base}/p/post-{start + i}",
                "title": f"Post {start + i}",
                "post_date": f"2026-04-{30 - (start + i):02d}T12:00:00.000Z",
            }
            for i in range(count)
        ]

    pages_by_offset: dict[str, list[dict[str, Any]]] = {
        "": page(1, 25),  # offset omitted -> first page
        "0": page(1, 25),
        "25": page(26, 25),
        "50": page(51, 5),  # partial page
        "55": [],  # exhausted
    }
    seen_offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/archive":
            return httpx.Response(404)
        offset = request.url.params.get("offset", "")
        seen_offsets.append(offset)
        return httpx.Response(200, json=pages_by_offset.get(offset, []))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SubstackSource(client=client) as source:
        refs = list(source.discover_backwards(base))

    # 25 + 25 + 5 = 55 refs across 4 requests (including the terminating empty page).
    assert len(refs) == 55
    # First page may use empty offset or "0"; subsequent must use offset.
    assert any(o in {"", "0"} for o in seen_offsets[:1])
    assert "25" in seen_offsets
    assert "50" in seen_offsets
    assert "55" in seen_offsets  # the empty page that stops the loop


def test_discover_backwards_stops_immediately_on_empty_first_page() -> None:
    """A publication with zero posts returns [] right away; iterator must terminate."""
    base = "https://empty.substack.com"
    client = _routed_client({f"{base}/api/v1/archive": []})
    with SubstackSource(client=client) as source:
        refs = list(source.discover_backwards(base))
    assert refs == []


def test_cookies_threaded_through_to_request() -> None:
    """Verify cookies actually reach the API call."""
    base = "https://samkriss.substack.com"
    seen: dict[str, str | None] = {"cookie_header": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie_header"] = request.headers.get("cookie")
        return httpx.Response(200, json=_archive_payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SubstackSource(
        client=client,
        cookies=[CookieEntry(name="substack.sid", value="secret", domain=".substack.com")],
    ) as source:
        list(source.discover(base))

    assert seen["cookie_header"] is not None
    assert "substack.sid=secret" in seen["cookie_header"]
