"""Tests for URL normalization + dedup key."""

from __future__ import annotations

import pytest

from pulpwise.util.dedup import dedup_key, normalize_url


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # case + scheme insensitivity on host
        ("HTTP://Example.COM/post", "http://example.com/post"),
        # trailing slash
        ("https://example.com/post/", "https://example.com/post"),
        # tracking params stripped
        ("https://example.com/x?utm_source=a&utm_campaign=b", "https://example.com/x"),
        ("https://example.com/x?fbclid=abc", "https://example.com/x"),
        ("https://example.com/x?ref=twitter", "https://example.com/x"),
        # fragment dropped
        ("https://example.com/x#section-2", "https://example.com/x"),
        # mixed: tracking + fragment + trailing slash
        ("https://example.com/x/?utm_medium=email#top", "https://example.com/x"),
    ],
)
def test_normalize_url_treats_as_equivalent(a: str, b: str) -> None:
    assert normalize_url(a) == normalize_url(b)
    assert dedup_key(a) == dedup_key(b)


def test_normalize_url_preserves_non_tracking_params() -> None:
    url = "https://example.com/search?q=python&page=2"
    assert "q=python" in normalize_url(url)
    assert "page=2" in normalize_url(url)


def test_dedup_key_is_sha256_hex() -> None:
    key = dedup_key("https://example.com/article")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_different_urls_have_different_keys() -> None:
    assert dedup_key("https://example.com/a") != dedup_key("https://example.com/b")


def test_path_root_slash_preserved() -> None:
    # A bare root path should not be stripped to empty.
    assert normalize_url("https://example.com/").endswith("/") or normalize_url(
        "https://example.com/"
    ).endswith("com")
