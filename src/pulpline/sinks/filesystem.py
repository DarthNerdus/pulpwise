"""Filesystem sink: write rendered bytes to a directory path."""

from __future__ import annotations

from pathlib import Path

from pulpline.models import RawArticle
from pulpline.util.slugify import sanitize_filename


class FilesystemSink:
    """Write rendered files to a local directory.

    Phase 1: filename is `{sanitize_filename(title)}.{extension}`. If a file with
    the same name already exists, it is overwritten - this makes one-shot
    re-ingestion of the same URL idempotent. Phase 2 will switch to dedup-key
    aware collision handling once persistence lands.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(self, article: RawArticle, content: bytes, extension: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{sanitize_filename(article.title)}.{extension}"
        path = self.output_dir / filename
        path.write_bytes(content)
        return path
