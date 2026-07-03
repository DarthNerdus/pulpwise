"""Tests for `pulp migrate --rebuild` (cli._rebuild_items)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

import pytest

from pulpline import cli
from pulpline.config import Config, Paths, Subscription, save_config
from pulpline.models import ItemRef, RawArticle
from pulpline.sources import REGISTRY
from pulpline.sources.base import Source
from pulpline.state import ItemRecord, connect, record_item
from pulpline.util.dedup import dedup_key


class RebuildSource(Source):
    """Every item renders the same title but distinct content per URL."""

    name: ClassVar[str] = "fake-rebuild"

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        return []

    def fetch(self, ref: ItemRef) -> RawArticle:
        return RawArticle(
            title="Digest",
            body_html="<p>x</p>",
            canonical_url=ref.url,
            source_url="fake://feed",
        )

    def render(self, article: RawArticle) -> bytes:
        return f"content-{article.canonical_url}".encode()


def test_rebuild_preserves_same_title_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ledger items sharing a title must not collapse onto one file.

    Regression: rebuild used to write both to `Digest.epub` (overwrite) and
    then unlink the second item's old disambiguated file - destroying one
    item's content entirely.
    """
    monkeypatch.setitem(REGISTRY, "fake-rebuild", RebuildSource)
    out = tmp_path / "out"
    out.mkdir(parents=True)

    sub = Subscription(name="digest", source="fake-rebuild", url="fake://feed", output_dir=str(out))
    save_config(Config(paths=Paths(output_dir=str(tmp_path)), subscriptions=(sub,)))

    path_a = out / "Digest.epub"
    path_b = out / "Digest (2).epub"
    path_a.write_bytes(b"old-a")
    path_b.write_bytes(b"old-b")
    with connect() as conn:
        for url, path in (("fake://a", path_a), ("fake://b", path_b)):
            record_item(
                conn,
                ItemRecord(
                    subscription_name="digest",
                    source_url="fake://feed",
                    dedup_key=dedup_key(url),
                    canonical_url=url,
                    title="Digest",
                    pub_date=None,
                    output_path=str(path),
                ),
            )

    cli._rebuild_items(name="digest")

    files = {p.name: p.read_bytes() for p in out.iterdir()}
    assert len(files) == 2, f"an item's file was destroyed: {sorted(files)}"
    assert set(files.values()) == {b"content-fake://a", b"content-fake://b"}

    with connect() as conn:
        rows = conn.execute("SELECT output_path FROM items").fetchall()
    paths = {str(row["output_path"]) for row in rows}
    assert len(paths) == 2
    assert all(Path(p).exists() for p in paths)
