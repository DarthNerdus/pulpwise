"""Generic URL source: one public web page, submitted as a bare-URL save."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from pulpwise.models import ItemRef, RawArticle
from pulpwise.sources.base import Source


class URLSource(Source):
    """Single-URL fallback. `discover` yields exactly one ItemRef pointing at the URL.

    No fetch, no extraction: Readwise Reader parses the page server-side.
    This source is only for publicly fetchable pages. Gated content is not
    routed here by URL - it is handled only by the subscription sources
    that can authenticate (Substack via config/saved list, email via IMAP).
    """

    name: ClassVar[str] = "url"
    fetch_needed: ClassVar[bool] = False

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return (ItemRef(url=target_url),)

    def fetch(self, ref: ItemRef) -> RawArticle:
        raise NotImplementedError("URL one-shots are bare-URL saves; nothing to fetch")
