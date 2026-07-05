"""Tests for the source plugin registry."""

from __future__ import annotations

import pytest

from pulpwise.sources import REGISTRY, ArXivSource, URLSource, get_source, pick_source_for_url


def test_registry_contains_exactly_the_shipped_sources() -> None:
    assert set(REGISTRY) == {"url", "rss", "substack", "substack-saved", "arxiv", "email"}


def test_registry_keys_match_source_names() -> None:
    for key, cls in REGISTRY.items():
        assert cls.name == key


def test_get_source_returns_class() -> None:
    assert get_source("url") is URLSource


def test_get_source_raises_on_unknown() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        get_source("nonexistent")


def test_pick_source_for_url_routes_arxiv() -> None:
    assert pick_source_for_url("https://arxiv.org/abs/2401.12345") is ArXivSource


def test_pick_source_for_url_falls_back_to_url_source() -> None:
    assert pick_source_for_url("https://example.com/some-article") is URLSource
