"""Source ABC. The contract every plugin (rss, url, MangaDex, arXiv, ...) implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import ClassVar

import httpx

from pulpline.models import ItemRef, RawArticle
from pulpline.util.http import build_client


class Source(ABC):
    """Two-phase content source.

    `discover` is cheap: list items available at a target URL without
    downloading bodies. The orchestrator dedups against persisted state
    (Phase 2+) before calling `fetch`, so only un-seen items pay the
    HTTP/extraction cost.

    For one-shot URL ingestion, `discover` yields exactly one `ItemRef`
    pointing at the URL itself.
    """

    name: ClassVar[str]

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = build_client()
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @abstractmethod
    def discover(self, target_url: str) -> Iterable[ItemRef]:
        """Cheap listing of items available at `target_url`. No body downloads."""

    @abstractmethod
    def fetch(self, ref: ItemRef) -> RawArticle:
        """Download + extract the body for a single item. Expensive."""
