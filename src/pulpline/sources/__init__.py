"""Source plugins. In-tree registry for v0.1.

Promote to entry-points (`pulpline.sources` group) when a third-party plugin ships.
"""

from __future__ import annotations

from pulpline.sources.annas import AnnaSource
from pulpline.sources.arxiv import ArXivSource
from pulpline.sources.base import Source
from pulpline.sources.email import EmailSource
from pulpline.sources.mangadex import MangaDexSource
from pulpline.sources.rss import RSSSource
from pulpline.sources.substack import SubstackSavedSource, SubstackSource
from pulpline.sources.url import URLSource

REGISTRY: dict[str, type[Source]] = {
    URLSource.name: URLSource,
    RSSSource.name: RSSSource,
    SubstackSource.name: SubstackSource,
    SubstackSavedSource.name: SubstackSavedSource,
    ArXivSource.name: ArXivSource,
    MangaDexSource.name: MangaDexSource,
    AnnaSource.name: AnnaSource,
    EmailSource.name: EmailSource,
}


def get_source(name: str) -> type[Source]:
    """Look up a source class by registry name."""
    if name not in REGISTRY:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown source {name!r}; known: {known}")
    return REGISTRY[name]


def pick_source_for_url(url: str) -> type[Source]:
    """Return the source class that claims `url`. URLSource is the fallback."""
    for src_name, cls in REGISTRY.items():
        if src_name == URLSource.name:
            continue  # fallback - checked last
        if cls.matches_url(url):
            return cls
    return URLSource


__all__ = [
    "REGISTRY",
    "AnnaSource",
    "ArXivSource",
    "EmailSource",
    "MangaDexSource",
    "RSSSource",
    "Source",
    "SubstackSavedSource",
    "SubstackSource",
    "URLSource",
    "get_source",
    "pick_source_for_url",
]
