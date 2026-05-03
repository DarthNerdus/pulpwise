"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_pulpline_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test isolation for pulpline's persistent state.

    Isolates: the state DB (via PULPLINE_STATE_PATH), the user's config file
    (via PULPLINE_CONFIG_PATH), and the one-shot default output dir (via
    PULPLINE_OUTPUT_DIR).

    Does NOT isolate sync output. `pulp sync` writes to `paths.output_dir`
    from the loaded TOML config, which is independent of the env var.
    Tests that exercise sync must pass an explicit `output_dir` on the
    Subscription or set `Config.paths.output_dir` themselves; otherwise EPUBs
    will land in the user's actual `~/Sync/Pulpline/`.
    """
    monkeypatch.setenv("PULPLINE_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("PULPLINE_CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setenv("PULPLINE_OUTPUT_DIR", str(tmp_path / "out"))
    # CliRunner-driven tests go through _main, which calls setup_logging at
    # the default XDG path. Point that at tmp_path too so test runs don't
    # leak entries into the developer's real ~/.local/state/pulpline/log/.
    monkeypatch.setenv("PULPLINE_LOG_DIR", str(tmp_path / "logs"))


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
