"""Shared pytest fixtures."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from pulpwise.sinks.readwise import ReadwiseSink
from pulpwise.sinks.shiori import ShioriSink
from pulpwise.util.http import reset_rate_limit_state
from pulpwise.util.logging import LOGGER_NAME

FIXTURES = Path(__file__).parent / "fixtures"


class FakeTimer:
    """Deterministic clock + sleep recorder for retry/backoff tests.

    Sleeping advances the clock, so code that sleeps through a cooldown
    window actually gets past it without wall-clock time elapsing.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_timer() -> FakeTimer:
    return FakeTimer()


class FakeReadwise:
    """In-memory Readwise Reader API served over httpx.MockTransport.

    Default behavior mirrors the real API's happy path: POST /api/v3/save/
    answers 201 for a URL it has never seen and 200 (same document id) for a
    repeat, GET /api/v2/auth/ answers `auth_status` (204). Every save payload
    is recorded in `save_payloads` so tests can assert exactly what went over
    the wire. There is deliberately no DELETE handler - Pulp Wise is
    push-only, so any DELETE hitting this fake is a bug (404s loudly).

    Queue `httpx.Response`s on `save_responses` to script deviations (429s,
    401s, garbage bodies); they are popped in order, after the payload is
    recorded.
    """

    def __init__(self) -> None:
        self.save_payloads: list[dict[str, object]] = []
        self.save_auth_headers: list[str] = []
        self.save_responses: list[httpx.Response] = []
        self.auth_status = 204
        self.documents: dict[str, str] = {}  # submitted url -> document id
        self._counter = 0

    def reader_url(self, document_id: str) -> str:
        return f"https://read.readwise.io/read/{document_id}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/v3/save/":
            self.save_auth_headers.append(request.headers.get("Authorization", ""))
            payload = json.loads(request.content.decode("utf-8"))
            self.save_payloads.append(payload)
            if self.save_responses:
                return self.save_responses.pop(0)
            url = str(payload["url"])
            already = url in self.documents
            if not already:
                self._counter += 1
                self.documents[url] = f"doc-{self._counter}"
            doc_id = self.documents[url]
            return httpx.Response(
                200 if already else 201,
                json={"id": doc_id, "url": self.reader_url(doc_id)},
            )
        if request.method == "GET" and path == "/api/v2/auth/":
            return httpx.Response(self.auth_status)
        return httpx.Response(404, text=f"unmocked Readwise endpoint: {request.url}")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def sink(self, timer: FakeTimer, token: str = "test-token", **kwargs: object) -> ReadwiseSink:
        """A real ReadwiseSink wired to this fake API with injected time."""
        return ReadwiseSink(
            token,
            client=self.client(),
            sleep=timer.sleep,
            clock=timer.clock,
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.fixture
def fake_readwise() -> FakeReadwise:
    return FakeReadwise()


@pytest.fixture
def readwise_sink(fake_readwise: FakeReadwise, fake_timer: FakeTimer) -> ReadwiseSink:
    """A ready-to-use sink over `fake_readwise` that never really sleeps."""
    return fake_readwise.sink(fake_timer)


class FakeShiori:
    """In-memory Shiori API that records the exact link-creation request."""

    def __init__(self) -> None:
        self.save_payloads: list[dict[str, object]] = []
        self.save_auth_headers: list[str] = []
        self.save_responses: list[httpx.Response] = []
        self.documents: dict[str, str] = {}
        self._counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/api/links":
            return httpx.Response(404, text=f"unmocked Shiori endpoint: {request.url}")
        self.save_auth_headers.append(request.headers.get("Authorization", ""))
        payload = json.loads(request.content.decode("utf-8"))
        self.save_payloads.append(payload)
        if self.save_responses:
            return self.save_responses.pop(0)
        url = str(payload["url"])
        duplicate = url in self.documents
        if not duplicate:
            self._counter += 1
            self.documents[url] = f"link-{self._counter}"
        body: dict[str, object] = {"success": True, "linkId": self.documents[url]}
        if duplicate:
            body["duplicate"] = True
        return httpx.Response(200, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="https://www.shiori.sh",
            transport=httpx.MockTransport(self.handler),
        )

    def sink(self, timer: FakeTimer, token: str = "test-token") -> ShioriSink:
        return ShioriSink(
            token,
            client=self.client(),
            sleep=timer.sleep,
            clock=timer.clock,
        )


@pytest.fixture
def fake_shiori() -> FakeShiori:
    return FakeShiori()


@pytest.fixture
def shiori_sink(fake_shiori: FakeShiori, fake_timer: FakeTimer) -> ShioriSink:
    return fake_shiori.sink(fake_timer)


@pytest.fixture(autouse=True)
def _fresh_rate_limit_state() -> None:
    """The retry transport keeps process-wide per-host cooldowns; forget them
    between tests so one test's tripped host can't fail-fast another's."""
    reset_rate_limit_state()


@pytest.fixture(autouse=True)
def _reset_pulpwise_logging() -> Iterator[None]:
    """Undo `setup_logging`'s process-global logger mutations after each test.

    setup_logging sets `propagate = False` on the `pulpwise` logger and
    attaches file handlers. Left in place, any test that runs the CLI (which
    calls setup_logging) silently breaks every later `caplog` assertion in
    the session - caplog captures at the root logger, which propagation no
    longer reaches.
    """
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_pulpwise_owned", False):
            logger.removeHandler(handler)
            handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _isolated_pulpwise_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test isolation for pulpwise's persistent state.

    Isolates the state DB (via PULPWISE_STATE_PATH), the user's config file
    (via PULPWISE_CONFIG_PATH), and the log dir. Also strips
    PULPWISE_READWISE_TOKEN so a token in the developer's environment can't
    leak into token-resolution tests (or let a test accidentally resolve a
    real credential).
    """
    monkeypatch.setenv("PULPWISE_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("PULPWISE_CONFIG_PATH", str(tmp_path / "config.toml"))
    # CliRunner-driven tests go through _main, which calls setup_logging at
    # the default XDG path. Point that at tmp_path too so test runs don't
    # leak entries into the developer's real ~/.local/state/pulpwise/log/.
    monkeypatch.setenv("PULPWISE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("PULPWISE_READWISE_TOKEN", raising=False)
    monkeypatch.delenv("PULPWISE_SHIORI_TOKEN", raising=False)


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
