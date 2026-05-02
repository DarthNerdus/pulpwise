"""Integration test for pipeline.add_once - real trafilatura, real ebooklib, mocked HTTP."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from ebooklib import epub

from pulpline import pipeline

ClientFactory = Callable[[dict[str, str]], httpx.Client]


def test_add_once_writes_a_real_epub(
    tmp_path: Path, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    url = "https://example.com/article"
    client = mock_client_factory({url: sample_html})

    out_path = pipeline.add_once(url, output_dir=tmp_path, client=client)

    assert out_path.exists()
    assert out_path.suffix == ".epub"
    # One-shots land in the `oneshots/` subfolder under the configured output dir.
    assert out_path.parent == tmp_path / "oneshots"

    book = epub.read_epub(str(out_path))
    titles = book.get_metadata("DC", "title")
    assert titles
    assert titles[0][0] == "The End of the Beginning"


def test_add_once_uses_default_output_dir_when_none(
    tmp_path: Path,
    mock_client_factory: ClientFactory,
    sample_html: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PULPLINE_OUTPUT_DIR", str(tmp_path))
    url = "https://example.com/article"
    client = mock_client_factory({url: sample_html})

    out_path = pipeline.add_once(url, client=client)

    assert out_path.parent == tmp_path / "oneshots"
