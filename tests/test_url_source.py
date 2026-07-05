"""Tests for URLSource - the single-URL, no-fetch fallback."""

from __future__ import annotations

import httpx
import pytest

from pulpwise.models import ItemRef
from pulpwise.sources.url import URLSource


def test_discover_yields_single_ref() -> None:
    with URLSource(client=httpx.Client()) as source:
        refs = list(source.discover("https://example.com/article"))
    assert refs == [ItemRef(url="https://example.com/article")]


def test_url_source_is_a_no_fetch_source() -> None:
    assert URLSource.fetch_needed is False


def test_fetch_raises_not_implemented() -> None:
    with (
        URLSource(client=httpx.Client()) as source,
        pytest.raises(NotImplementedError, match="bare-URL saves"),
    ):
        source.fetch(ItemRef(url="https://example.com/article"))


def test_submission_for_ref_is_a_bare_url_save() -> None:
    with URLSource(client=httpx.Client()) as source:
        submission = source.submission_for_ref(ItemRef(url="https://example.com/article"))

    assert submission.url == "https://example.com/article"
    assert submission.html is None
    assert submission.kind == "url"
    assert submission.category is None
