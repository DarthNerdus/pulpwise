"""OPML bulk import.

OPML is the de-facto interchange format for feed-reader subscription lists -
Reeder, NetNewsWire, Inoreader, Feedly, Substack, and most others all export
it. We parse the standard `<outline xmlUrl="..." text="..."/>` shape, walk
nested outlines (folders/categories), and present each feed as a candidate
for subscription. No third-party deps - stdlib `xml.etree` is enough.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


class OpmlError(Exception):
    """Raised when OPML cannot be parsed or has no feeds."""


@dataclass(frozen=True, slots=True)
class OpmlFeed:
    title: str
    feed_url: str
    site_url: str | None = None
    folder: str | None = None  # the parent outline's text, if nested


def parse_opml(path: Path) -> list[OpmlFeed]:
    """Parse an OPML file and return all feeds it advertises (depth-first)."""
    expanded = path.expanduser()
    if not expanded.exists():
        raise OpmlError(f"OPML file not found: {expanded}")
    try:
        tree = ET.parse(expanded)
    except ET.ParseError as exc:
        raise OpmlError(f"could not parse OPML {expanded}: {exc}") from exc

    body = tree.getroot().find("body")
    if body is None:
        raise OpmlError(f"OPML {expanded} has no <body>")

    feeds: list[OpmlFeed] = []
    _walk(body, folder=None, out=feeds)
    if not feeds:
        raise OpmlError(f"OPML {expanded} contains no feeds (no `xmlUrl` outlines)")
    return feeds


def _walk(node: ET.Element, folder: str | None, out: list[OpmlFeed]) -> None:
    for outline in node.findall("outline"):
        xml_url = outline.get("xmlUrl")
        text = outline.get("text") or outline.get("title") or ""
        if xml_url:
            out.append(
                OpmlFeed(
                    title=text.strip() or xml_url,
                    feed_url=xml_url,
                    site_url=outline.get("htmlUrl"),
                    folder=folder,
                )
            )
        else:
            # Folder / category. Recurse with the new folder context.
            _walk(outline, folder=text.strip() or folder, out=out)
