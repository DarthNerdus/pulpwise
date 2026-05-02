"""Tests for FilesystemSink."""

from __future__ import annotations

from pathlib import Path

from pulpline.models import RawArticle
from pulpline.sinks.filesystem import FilesystemSink


def _article(title: str = "Hello World") -> RawArticle:
    return RawArticle(
        title=title,
        body_html="<p>x</p>",
        canonical_url="https://example.com/",
        source_url="direct",
    )


def test_write_creates_output_dir(tmp_path: Path) -> None:
    sub = tmp_path / "deep" / "nested" / "out"
    sink = FilesystemSink(sub)
    path = sink.write(_article(), b"fake-epub-bytes", extension="epub")
    assert path.exists()
    assert path == sub / "Hello World.epub"
    assert path.read_bytes() == b"fake-epub-bytes"


def test_write_sanitizes_title(tmp_path: Path) -> None:
    sink = FilesystemSink(tmp_path)
    path = sink.write(_article(title='Risky / Path "Title"'), b"x", extension="epub")
    assert path.parent == tmp_path
    assert "/" not in path.name.replace(".epub", "")
    assert path.exists()


def test_write_overwrites_existing(tmp_path: Path) -> None:
    sink = FilesystemSink(tmp_path)
    sink.write(_article(), b"first", extension="epub")
    path = sink.write(_article(), b"second", extension="epub")
    assert path.read_bytes() == b"second"
