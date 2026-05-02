"""Pipeline orchestration: compose sources, renderers, sinks.

Phase 1 ships only `add_once` (URL -> EPUB -> filesystem). `sync` and
subscription-aware persistence land in Phase 2.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from pulpline.config import default_output_dir
from pulpline.renderers.epub import EpubRenderer
from pulpline.sinks.filesystem import FilesystemSink
from pulpline.sources.url import URLSource


def add_once(
    url: str,
    output_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch a single URL, extract its article, render EPUB, write to disk.

    Returns the path of the written file. Raises `FetchError` on HTTP failure
    and `ExtractionError` if the page has no extractable content.

    `client` is exposed for tests; production callers leave it None and let
    URLSource construct a default client (which it then owns and closes).
    """
    target = output_dir or default_output_dir()

    with URLSource(client=client) as source:
        refs = list(source.discover(url))
        if len(refs) != 1:
            raise RuntimeError(f"URLSource.discover yielded {len(refs)} items, expected exactly 1")
        article = source.fetch(refs[0])

    renderer = EpubRenderer()
    content = renderer.render(article)

    sink = FilesystemSink(target)
    return sink.write(article, content, renderer.extension)
