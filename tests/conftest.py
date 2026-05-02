"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_html() -> str:
    return (FIXTURES / "sample_article.html").read_text(encoding="utf-8")


@pytest.fixture
def mock_client_factory() -> Callable[..., httpx.Client]:
    """Build an httpx.Client backed by a MockTransport.

    Usage:
        client = mock_client_factory({"https://example.com/article": SAMPLE_HTML})
    """

    def _factory(routes: dict[str, str], status: int = 200) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url in routes:
                return httpx.Response(status, html=routes[url])
            return httpx.Response(404, text=f"unmocked URL: {url}")

        return httpx.Client(transport=httpx.MockTransport(handler))

    return _factory
