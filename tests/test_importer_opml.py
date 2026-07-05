"""Tests for the OPML importer."""

from __future__ import annotations

from pathlib import Path

import pytest

from pulpwise.importers.opml import OpmlError, parse_opml

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_opml_walks_nested_folders() -> None:
    feeds = parse_opml(FIXTURES / "sample.opml")
    by_title = {f.title: f for f in feeds}

    assert by_title["Stratechery"].feed_url == "https://stratechery.com/feed"
    assert by_title["Stratechery"].folder == "Tech"
    assert by_title["Stratechery"].site_url == "https://stratechery.com"

    assert by_title["Sam Kriss"].folder == "Tech"
    assert by_title["HuggingFace Blog"].folder == "ML"
    assert by_title["Loose feed (no folder)"].folder is None


def test_parse_opml_finds_all_feeds() -> None:
    feeds = parse_opml(FIXTURES / "sample.opml")
    assert len(feeds) == 4


def test_parse_opml_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OpmlError, match="not found"):
        parse_opml(tmp_path / "nope.opml")


def test_parse_opml_invalid_xml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.opml"
    bad.write_text("not xml at all", encoding="utf-8")
    with pytest.raises(OpmlError, match="parse"):
        parse_opml(bad)


def test_parse_opml_empty_body(tmp_path: Path) -> None:
    empty = tmp_path / "empty.opml"
    empty.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head><title>Empty</title></head>'
        "<body></body></opml>",
        encoding="utf-8",
    )
    with pytest.raises(OpmlError, match="no feeds"):
        parse_opml(empty)


def test_parse_opml_missing_body(tmp_path: Path) -> None:
    no_body = tmp_path / "nobody.opml"
    no_body.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/></opml>',
        encoding="utf-8",
    )
    with pytest.raises(OpmlError, match="no <body>"):
        parse_opml(no_body)


def test_parse_opml_expanduser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "feeds.opml"
    target.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body>'
        '<outline text="X" xmlUrl="https://x.example/feed"/>'
        "</body></opml>",
        encoding="utf-8",
    )
    feeds = parse_opml(Path("~/feeds.opml"))
    assert len(feeds) == 1
    assert feeds[0].feed_url == "https://x.example/feed"
