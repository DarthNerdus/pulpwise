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


def test_fetch_paywalled_without_cookies_raises_with_clear_message() -> None:
    base = "https://samkriss.substack.com"
    client = _routed_client({f"{base}/api/v1/posts/post-one": _post_payload(paywalled=True)})
    with (
        SubstackSource(client=client) as source,
        pytest.raises(ExtractionError, match="paywalled"),
    ):
        source.fetch(ItemRef(url=f"{base}/p/post-one"))


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
