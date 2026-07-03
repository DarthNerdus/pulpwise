"""Tests for FilesystemSink."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pulpline.models import RawArticle
from pulpline.sinks.filesystem import FilesystemSink


def _article(title: str = "Hello World", pub_date: datetime | None = None) -> RawArticle:
    return RawArticle(
        title=title,
        body_html="<p>x</p>",
        canonical_url="https://example.com/",
        source_url="direct",
        pub_date=pub_date,
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


def test_collision_suffixes_with_pub_date(tmp_path: Path) -> None:
    """Recurring newsletter subjects must not clobber earlier issues."""
    sink = FilesystemSink(tmp_path)
    first = sink.write(
        _article("Money Stuff", pub_date=datetime(2026, 6, 1, tzinfo=UTC)), b"one", "epub"
    )
    second = sink.write(
        _article("Money Stuff", pub_date=datetime(2026, 6, 8, tzinfo=UTC)), b"two", "epub"
    )
    assert first.name == "Money Stuff.epub"
    assert second.name == "Money Stuff (2026-06-08).epub"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_collision_without_pub_date_uses_counters(tmp_path: Path) -> None:
    sink = FilesystemSink(tmp_path)
    sink.write(_article("Digest"), b"one", "epub")
    second = sink.write(_article("Digest"), b"two", "epub")
    third = sink.write(_article("Digest"), b"three", "epub")
    assert second.name == "Digest (2).epub"
    assert third.name == "Digest (3).epub"


def test_collision_same_date_falls_back_to_counter(tmp_path: Path) -> None:
    sink = FilesystemSink(tmp_path)
    when = datetime(2026, 6, 8, tzinfo=UTC)
    sink.write(_article("Digest", pub_date=when), b"one", "epub")
    sink.write(_article("Digest", pub_date=when), b"two", "epub")
    third = sink.write(_article("Digest", pub_date=when), b"three", "epub")
    assert third.name == "Digest (2026-06-08) (2).epub"


def test_unlink_then_write_reclaims_name(tmp_path: Path) -> None:
    """`pulp migrate --rebuild` unlinks the item's own file before rewriting,
    so the rebuilt file reclaims its name instead of picking up a suffix."""
    sink = FilesystemSink(tmp_path)
    first = sink.write(_article(), b"first", extension="epub")
    first.unlink()
    path = sink.write(_article(), b"second", extension="epub")
    assert path.name == "Hello World.epub"
    assert path.read_bytes() == b"second"
    assert len(list(tmp_path.iterdir())) == 1
