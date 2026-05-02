"""Source plugins. In-tree registry for v0.1.

Promote to entry-points (`pulpline.sources` group) when a third-party plugin ships.
"""

from __future__ import annotations

from pulpline.sources.base import Source
from pulpline.sources.url import URLSource

REGISTRY: dict[str, type[Source]] = {
    URLSource.name: URLSource,
}


def get_source(name: str) -> type[Source]:
    """Look up a source class by registry name."""
    if name not in REGISTRY:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown source {name!r}; known: {known}")
    return REGISTRY[name]


__all__ = ["REGISTRY", "Source", "URLSource", "get_source"]
