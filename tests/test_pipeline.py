"""Integration test for pipeline.add_once - real trafilatura, real ebooklib, mocked HTTP."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from ebooklib import epub

from pulpline import pipeline
from pulpline.config import Config, Paths

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


def test_add_once_falls_back_to_config_paths_output_dir(
    tmp_path: Path,
    mock_client_factory: ClientFactory,
    sample_html: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit arg or env var, one-shots follow `paths.output_dir`."""
    monkeypatch.delenv("PULPLINE_OUTPUT_DIR")
    cfg = Config(paths=Paths(output_dir=str(tmp_path / "from-config")))
    url = "https://example.com/article"
    client = mock_client_factory({url: sample_html})

    out_path = pipeline.add_once(url, client=client, config=cfg)

    assert out_path.parent == tmp_path / "from-config" / "oneshots"


def test_add_once_honors_source_output_dir_override(tmp_path: Path) -> None:
    """[auth.annas].output_dir routes annas downloads to a dedicated folder,
    even when a base output_dir is passed explicitly."""
    md5 = "abcdef0123456789abcdef0123456789"
    url = f"https://annas-archive.gl/md5/{md5}"
    api_url = "https://annas-archive.gl/dyn/api/fast_download.json"
    download_url = "https://download.example/server/A_Real_Book.epub"

    def handler(request: httpx.Request) -> httpx.Response:
        target = str(request.url).split("?")[0]
        if target == api_url:
            return httpx.Response(200, json={"download_url": download_url})
        if target == download_url:
            return httpx.Response(200, content=b"fake epub bytes")
        return httpx.Response(404, text=f"unmocked: {target}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    books_dir = tmp_path / "books"
    cfg = Config(
        auth={"annas": {"api_key": "test-key", "mirrors": "gl", "output_dir": str(books_dir)}}
    )

    out_path = pipeline.add_once(url, output_dir=tmp_path, client=client, config=cfg)

    assert out_path.parent == books_dir
    assert out_path.suffix == ".epub"
    assert out_path.read_bytes() == b"fake epub bytes"
