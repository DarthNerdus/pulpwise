"""Tests for the source plugin registry."""

from __future__ import annotations

import pytest

from pulpline.sources import REGISTRY, URLSource, get_source


def test_registry_contains_url_source() -> None:
    assert REGISTRY["url"] is URLSource


def test_get_source_returns_class() -> None:
    assert get_source("url") is URLSource


def test_get_source_raises_on_unknown() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        get_source("nonexistent")
