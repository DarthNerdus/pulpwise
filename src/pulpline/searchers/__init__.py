"""Searcher plugins. Interactive query -> result-list -> pick contracts.

A Searcher is *not* a Source: it answers `search(query)` and returns
candidate hits for the user to pick from. The pick is then turned into a URL
(usually source-specific - e.g. an Anna's Archive `/md5/<hash>` URL) and
handed back to `pipeline.add_once`, which routes through the matching
Source for the actual download.

Splitting search out keeps the Source ABC honest: `discover()` is for
recurring feeds, not query results.
"""

from __future__ import annotations

from pulpline.searchers.annas import AnnaSearcher
from pulpline.searchers.base import Searcher, SearchResult

REGISTRY: dict[str, type[Searcher]] = {
    AnnaSearcher.name: AnnaSearcher,
}


def get_searcher(name: str) -> type[Searcher]:
    """Look up a searcher class by registry name."""
    if name not in REGISTRY:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown searcher {name!r}; known: {known}")
    return REGISTRY[name]


__all__ = [
    "REGISTRY",
    "AnnaSearcher",
    "SearchResult",
    "Searcher",
    "get_searcher",
]
