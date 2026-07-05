"""RSS / Atom feed source.

Two-phase shape pays off here: `discover` parses the feed once (one HTTP call,
yields N ItemRefs), the orchestrator dedups against the items table, and only
unseen entries are pushed to Readwise. RSS items are bare-URL saves - the
entry link is a public web URL, and Reader's own parser does the extraction
server-side - so this source never fetches article bodies at all.

Per-subscription `categories` / `exclude_categories` options filter entries by
their feed categories during `discover`, so filtered-out items never reach
the dedup ledger or Readwise. This is the mechanism that lets Pulp Wise act
as the RSS subscription manager in front of Reader: subscribe here, filter
here, and only the entries worth reading land in the Reader account.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import feedparser
import httpx

from pulpwise.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpwise.sources.base import Source

if TYPE_CHECKING:
    from pulpwise.config import Config, Subscription


class RSSSource(Source):
    name: ClassVar[str] = "rss"
    fetch_needed: ClassVar[bool] = False

    def __init__(
        self,
        client: httpx.Client | None = None,
        categories: frozenset[str] | None = None,
        exclude_categories: frozenset[str] | None = None,
    ) -> None:
        super().__init__(client=client)
        self._categories = categories
        self._exclude_categories = exclude_categories

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        client: httpx.Client | None = None,
        subscription: Subscription | None = None,
    ) -> RSSSource:
        del cfg
        opts = subscription.options if subscription is not None else {}
        return cls(
            client=client,
            categories=_parse_category_option(opts.get("categories")),
            exclude_categories=_parse_category_option(opts.get("exclude_categories")),
        )

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        try:
            response = self.client.get(target_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch feed {target_url}: {exc}") from exc

        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise ExtractionError(f"feed parse failed for {target_url}: {feed.bozo_exception}")

        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            if not self._passes_category_filter(entry):
                continue
            yield ItemRef(
                url=link,
                title=entry.get("title"),
                pub_date=_struct_to_datetime(entry.get("published_parsed")),
                guid=entry.get("id") or entry.get("guid"),
            )

    def fetch(self, ref: ItemRef) -> RawArticle:
        raise NotImplementedError("RSS items are bare-URL saves; nothing to fetch")

    def _passes_category_filter(self, entry: dict[str, Any]) -> bool:
        if self._categories is None and self._exclude_categories is None:
            return True
        entry_categories = _entry_categories(entry)
        if self._exclude_categories and entry_categories & self._exclude_categories:
            return False
        if self._categories is not None:
            return bool(entry_categories & self._categories)
        return True


def _parse_category_option(value: str | int | None) -> frozenset[str] | None:
    """Parse a comma-separated category option into a lowercase set, or None if unset.

    None (option absent or blank) means "no constraint"; an empty set from
    `categories = ","` would silently drop everything, so blanks collapse to
    None rather than an empty filter.
    """
    if value is None:
        return None
    names = frozenset(name.strip().lower() for name in str(value).split(",") if name.strip())
    return names or None


def _entry_categories(entry: dict[str, Any]) -> frozenset[str]:
    """Lowercased category names for a feed entry.

    feedparser normalizes RSS `<category>`, Atom `<category term=...>`, and
    `<dc:subject>` into `entry.tags[].term`.
    """
    names = set()
    for tag in entry.get("tags", []) or []:
        term = tag.get("term") if isinstance(tag, dict) else None
        if term:
            names.add(str(term).strip().lower())
    return frozenset(names)


def _struct_to_datetime(value: time.struct_time | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except TypeError, ValueError:
        return None
