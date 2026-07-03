"""Filesystem sink: write rendered bytes to a directory path."""

from __future__ import annotations

from datetime import datetime
from itertools import count
from pathlib import Path

from pulpline.models import RawArticle
from pulpline.util.slugify import sanitize_filename


class FilesystemSink:
    """Write rendered files to a local directory.

    Filename is `{sanitize_filename(title)}.{extension}`. When that name is
    taken, the file is disambiguated rather than overwritten - recurring
    newsletter subjects ("Money Stuff", "Your Weekly Digest") collide
    constantly, and silently clobbering issue N-1 with issue N corrupts the
    library (the ledger would keep pointing two items at one file). The
    suffix is the item's publication date when known (`Title (2026-07-03)`),
    falling back to ` (2)`, ` (3)`, ... counters.

    Callers that re-materialize an existing item (`pulp migrate --rebuild`)
    unlink the item's own file first so it reclaims its name instead of
    picking up a fresh suffix.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(self, article: RawArticle, content: bytes, extension: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stem = sanitize_filename(article.title)
        path = self.output_dir / f"{stem}.{extension}"
        if path.exists():
            path = self._collision_path(stem, extension, article.pub_date)
        path.write_bytes(content)
        return path

    def _collision_path(self, stem: str, extension: str, pub_date: datetime | None) -> Path:
        if pub_date is not None:
            dated_stem = f"{stem} ({pub_date.date().isoformat()})"
            candidate = self.output_dir / f"{dated_stem}.{extension}"
            if not candidate.exists():
                return candidate
            stem = dated_stem  # same title, same date: fall through to counters
        for n in count(2):
            candidate = self.output_dir / f"{stem} ({n}).{extension}"
            if not candidate.exists():
                return candidate
        raise AssertionError("unreachable")  # count() never exhausts
