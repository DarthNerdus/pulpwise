"""Searcher ABC. The contract every interactive search backend implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import httpx

if TYPE_CHECKING:
    from pulpline.config import Config


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One candidate row in a search result list.

    `target_url` is the URL pulpline will hand to `pipeline.add_once` if the
    user picks this row - it must route to a Source via that source's
    `matches_url`. For Anna's Archive that's `https://annas-archive.<tld>/md5/<hash>`.

    Fields beyond `target_url` are display-only: they shape what the user
    sees in the picker but don't change the download path.
    """

    target_url: str
    title: str
    authors: str | None = None
    year: str | None = None
    language: str | None = None
    extension: str | None = None  # epub, pdf, mobi, ...
    size: str | None = None  # human-formatted: "4.2 MB"
    publisher: str | None = None


class Searcher(ABC):
    """Interactive search backend.

    Each implementation owns one external service (Anna's Archive, libgen,
    arXiv search, ...). `search()` runs the query and returns candidates;
    the CLI prompts the user to pick one and ingests `result.target_url`
    via the existing one-shot pipeline.
    """

    name: ClassVar[str]

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> httpx.Client:
        """Create the default httpx client for this searcher.

        Override if the service needs a special UA (Anna's Archive uses a
        browser UA to satisfy DDoS-Guard) or other client config.
        """
        from pulpline.util.http import build_client

        return build_client()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Searcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        client: httpx.Client | None = None,
    ) -> Searcher:
        """Construct from loaded Config. Default: ignore everything."""
        del cfg
        return cls(client=client)

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        content: str | None = None,
        extension: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> Iterable[SearchResult]:
        """Run a query and yield candidate results.

        Args are kept generic across backends; each searcher decides which
        filters apply to its service. Unused kwargs should be silently
        ignored, not error.
        """
