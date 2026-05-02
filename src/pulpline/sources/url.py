"""Generic URL source: fetch one HTTP page and extract its article via trafilatura."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from pulpline.models import ItemRef, RawArticle
from pulpline.sources.base import Source
from pulpline.util.extract import fetch_article


class URLSource(Source):
    """Single-URL fetcher. `discover` yields exactly one ItemRef pointing at the URL."""

    name: ClassVar[str] = "url"

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return (ItemRef(url=target_url),)

    def fetch(self, ref: ItemRef) -> RawArticle:
        return fetch_article(ref.url, self.client, source_url="direct")
