"""Boundary types passed between sources, renderers, and sinks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class PulplineError(Exception):
    """Base exception for pulpline errors."""


class ExtractionError(PulplineError):
    """Raised when content extraction fails (empty body, no parseable text)."""


class FetchError(PulplineError):
    """Raised when an HTTP fetch fails or returns a non-success status."""


@dataclass(frozen=True, slots=True)
class ItemRef:
    """Lightweight reference to an item, produced by `Source.discover`.

    Used for dedup (Phase 2) before committing to an expensive body fetch.
    Sources should fill what they cheaply have - URL is the only required field.
    """

    url: str
    title: str | None = None
    pub_date: datetime | None = None
    guid: str | None = None  # RSS <guid> / Atom <id> when present; informational only


@dataclass(frozen=True, slots=True)
class RawArticle:
    """Extracted article ready for rendering.

    Produced by `Source.fetch`, consumed by `Renderer.render`.
    """

    title: str
    body_html: str
    canonical_url: str
    source_url: str  # feed URL for RSS items, article URL for one-shots ("direct")
    author: str | None = None
    publisher: str | None = None
    pub_date: datetime | None = None
    language: str = "en"
    subscription_name: str | None = None  # None for `--once` ingestions
