"""Tests for the URL-only Shiori destination sink."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pulpwise.config import Config
from pulpwise.models import FetchError, RateLimited, ReaderSubmission
from pulpwise.sinks.shiori import (
    _SAVE_MIN_INTERVAL,
    ShioriAuthError,
    ShioriSink,
    resolve_token,
)
from tests.conftest import FakeShiori, FakeTimer

PUBLIC_URL = "https://example.com/article"
SUBMISSION = ReaderSubmission(
    url=PUBLIC_URL,
    html="<p>private article body</p>",
    title="Private title",
    author="Author",
    summary="Summary",
)


def test_push_sends_only_source_url(fake_shiori: FakeShiori, shiori_sink: ShioriSink) -> None:
    result = shiori_sink.push(SUBMISSION)

    assert fake_shiori.save_payloads == [{"url": PUBLIC_URL}]
    assert result.document_id == "link-1"
    assert result.reader_url == PUBLIC_URL
    assert result.kind == "url"
    assert result.already_existed is False


def test_push_uses_bearer_auth(fake_shiori: FakeShiori, fake_timer: FakeTimer) -> None:
    sink = fake_shiori.sink(fake_timer, token="sekrit")

    sink.push(SUBMISSION)

    assert fake_shiori.save_auth_headers == ["Bearer sekrit"]


def test_duplicate_response_is_reported(fake_shiori: FakeShiori, shiori_sink: ShioriSink) -> None:
    first = shiori_sink.push(SUBMISSION)
    second = shiori_sink.push(SUBMISSION)

    assert second.document_id == first.document_id
    assert second.already_existed is True


def test_rejects_unsafe_or_unusable_urls_without_network(
    fake_shiori: FakeShiori, shiori_sink: ShioriSink
) -> None:
    urls = (
        "mid:newsletter@example.com",
        "https://pulpwise.invalid/abc123",
        "https://[malformed",
        "https://user:password@example.com/article",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/admin",
    )
    for url in urls:
        with pytest.raises(FetchError, match="public http"):
            shiori_sink.push(ReaderSubmission(url=url, html="<p>body</p>"))

    assert fake_shiori.save_payloads == []


def test_401_raises_auth_error(fake_shiori: FakeShiori, shiori_sink: ShioriSink) -> None:
    fake_shiori.save_responses.append(
        httpx.Response(401, json={"success": False, "error": "Invalid API key"})
    )

    with pytest.raises(ShioriAuthError, match="rejected"):
        shiori_sink.push(SUBMISSION)


def test_api_error_raises_fetch_error(fake_shiori: FakeShiori, shiori_sink: ShioriSink) -> None:
    fake_shiori.save_responses.append(
        httpx.Response(400, json={"success": False, "error": "Invalid URL"})
    )

    with pytest.raises(FetchError, match="Invalid URL"):
        shiori_sink.push(SUBMISSION)


def test_unexpected_success_body_raises_fetch_error(
    fake_shiori: FakeShiori, shiori_sink: ShioriSink
) -> None:
    fake_shiori.save_responses.append(httpx.Response(200, json={"success": True}))

    with pytest.raises(FetchError, match="unexpected body"):
        shiori_sink.push(SUBMISSION)


def test_second_push_is_paced(
    fake_shiori: FakeShiori,
    fake_timer: FakeTimer,
    shiori_sink: ShioriSink,
) -> None:
    shiori_sink.push(ReaderSubmission(url="https://example.com/one"))
    assert fake_timer.sleeps == []

    shiori_sink.push(ReaderSubmission(url="https://example.com/two"))

    assert fake_timer.sleeps == [pytest.approx(_SAVE_MIN_INTERVAL)]


def test_429_raises_rate_limited_and_blocks_later_pushes(
    fake_shiori: FakeShiori,
    fake_timer: FakeTimer,
    shiori_sink: ShioriSink,
) -> None:
    fake_shiori.save_responses.append(
        httpx.Response(
            429,
            headers={"Retry-After": "12"},
            json={"success": False, "error": "Too many requests"},
        )
    )

    with pytest.raises(RateLimited) as excinfo:
        shiori_sink.push(SUBMISSION)

    assert excinfo.value.host == "shiori"
    assert excinfo.value.retry_after == 12.0
    posts_so_far = len(fake_shiori.save_payloads)
    sleeps_so_far = list(fake_timer.sleeps)

    with pytest.raises(RateLimited, match="cooling down"):
        shiori_sink.push(ReaderSubmission(url="https://example.com/later"))

    assert len(fake_shiori.save_payloads) == posts_so_far
    assert fake_timer.sleeps == sleeps_so_far


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ShioriAuthError, match="empty"):
        ShioriSink("")


def test_env_var_wins_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULPWISE_SHIORI_TOKEN", " env-token ")
    cfg = Config(auth={"shiori": {"token": "config-token"}})

    assert resolve_token(cfg) == "env-token"


def test_token_path_is_read_and_stripped(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(" file-token\n", encoding="utf-8")
    cfg = Config(auth={"shiori": {"token_path": str(token_file)}})

    assert resolve_token(cfg) == "file-token"


def test_missing_token_has_setup_hint() -> None:
    with pytest.raises(ShioriAuthError, match=r"auth\.shiori"):
        resolve_token(Config())
