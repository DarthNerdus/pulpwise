"""Tests for ReadwiseSink: payloads, statuses, pacing, 429 handling, token resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from pulpwise.config import Config
from pulpwise.models import FetchError, RateLimited, ReaderSubmission
from pulpwise.sinks.readwise import (
    _SAVE_MIN_INTERVAL,
    ReadwiseAuthError,
    ReadwiseSink,
    resolve_token,
)
from tests.conftest import FakeReadwise, FakeTimer

SUB = ReaderSubmission(url="https://example.com/article")


# ---- statuses ----------------------------------------------------------------


def test_201_means_created(readwise_sink: ReadwiseSink) -> None:
    result = readwise_sink.push(SUB)

    assert result.already_existed is False
    assert result.document_id == "doc-1"
    assert result.reader_url == "https://read.readwise.io/read/doc-1"
    assert result.kind == "url"


def test_200_means_already_existed(readwise_sink: ReadwiseSink) -> None:
    first = readwise_sink.push(SUB)
    second = readwise_sink.push(SUB)

    assert second.already_existed is True
    assert second.document_id == first.document_id


def test_401_raises_auth_error(fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink) -> None:
    fake_readwise.save_responses.append(httpx.Response(401))

    with pytest.raises(ReadwiseAuthError, match=r"readwise\.io/access_token"):
        readwise_sink.push(SUB)


def test_unexpected_status_raises_fetch_error(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.append(httpx.Response(500, text="boom"))

    with pytest.raises(FetchError, match="HTTP 500"):
        readwise_sink.push(SUB)


def test_garbage_body_raises_fetch_error(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.append(httpx.Response(201, json={"unexpected": "shape"}))

    with pytest.raises(FetchError, match="unexpected body"):
        readwise_sink.push(SUB)


def test_empty_token_rejected_at_construction() -> None:
    with pytest.raises(ReadwiseAuthError, match="empty"):
        ReadwiseSink("")


def test_token_is_sent_as_authorization_header(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer
) -> None:
    sink = fake_readwise.sink(fake_timer, token="sekrit")
    sink.push(SUB)

    assert fake_readwise.save_auth_headers == ["Token sekrit"]


# ---- payload building ---------------------------------------------------------


def test_url_mode_payload(fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink) -> None:
    when = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    readwise_sink.push(
        ReaderSubmission(url="https://example.com/post", title="A Post", pub_date=when)
    )

    payload = fake_readwise.save_payloads[0]
    assert payload["url"] == "https://example.com/post"
    assert payload["title"] == "A Post"
    assert payload["published_date"] == when.isoformat()
    assert payload["saved_using"] == "pulpwise"
    assert "html" not in payload
    assert "should_clean_html" not in payload


def test_html_mode_payload(fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink) -> None:
    result = readwise_sink.push(
        ReaderSubmission(
            url="https://pulpwise.invalid/abc123",
            html="<p>gated body</p>",
            title="Gated",
            author="Anna",
            summary="A summary",
        )
    )

    payload = fake_readwise.save_payloads[0]
    assert payload["html"] == "<p>gated body</p>"
    assert payload["should_clean_html"] is True
    assert payload["title"] == "Gated"
    assert payload["author"] == "Anna"
    assert payload["summary"] == "A summary"
    assert result.kind == "html"


def test_clean_html_flag_is_configurable(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer
) -> None:
    sink = fake_readwise.sink(fake_timer, clean_html=False)
    sink.push(ReaderSubmission(url="https://x.example/a", html="<p>x</p>"))

    assert fake_readwise.save_payloads[0]["should_clean_html"] is False


def test_category_location_and_tags_in_payload(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    readwise_sink.push(
        ReaderSubmission(url="https://arxiv.org/pdf/2401.12345", category="pdf"),
        location="feed",
        tags=("papers", "ml"),
    )

    payload = fake_readwise.save_payloads[0]
    assert payload["category"] == "pdf"
    assert payload["location"] == "feed"
    assert payload["tags"] == ["papers", "ml"]


def test_optional_keys_absent_when_unset(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    readwise_sink.push(SUB)

    payload = fake_readwise.save_payloads[0]
    assert set(payload) == {"url", "saved_using"}


def test_invalid_location_raises_value_error_without_network(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    with pytest.raises(ValueError, match="invalid Readwise location"):
        readwise_sink.push(SUB, location="shortlist")

    assert fake_readwise.save_payloads == []


# ---- pacing -------------------------------------------------------------------


def test_second_push_is_paced(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    readwise_sink.push(ReaderSubmission(url="https://x.example/1"))
    assert fake_timer.sleeps == []  # first save goes out immediately

    readwise_sink.push(ReaderSubmission(url="https://x.example/2"))
    assert fake_timer.sleeps == [pytest.approx(_SAVE_MIN_INTERVAL)]
    assert pytest.approx(60.0 / 45.0) == _SAVE_MIN_INTERVAL  # 45 saves/min


def test_no_pacing_sleep_when_enough_time_passed(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    readwise_sink.push(ReaderSubmission(url="https://x.example/1"))
    fake_timer.now += 10.0  # plenty of wall time elapsed between saves

    readwise_sink.push(ReaderSubmission(url="https://x.example/2"))
    assert fake_timer.sleeps == []


# ---- 429 handling -------------------------------------------------------------


def test_429_then_success_waits_out_retry_after(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.append(httpx.Response(429, headers={"Retry-After": "7"}))

    result = readwise_sink.push(SUB)

    assert result.document_id == "doc-1"
    assert fake_timer.sleeps == [7.0]
    assert len(fake_readwise.save_payloads) == 2  # original + one retry


def test_429_without_retry_after_waits_default_60s(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.append(httpx.Response(429))

    result = readwise_sink.push(SUB)

    assert result.document_id == "doc-1"
    assert fake_timer.sleeps == [60.0]


def test_429_twice_raises_rate_limited(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.extend(
        [
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "30"}),
        ]
    )

    with pytest.raises(RateLimited) as excinfo:
        readwise_sink.push(SUB)

    assert excinfo.value.host == "readwise"
    assert excinfo.value.retry_after == 60.0  # cooldown floor beats the 30s header
    assert fake_timer.sleeps == [5.0]  # only the first Retry-After was slept out


def test_breaker_fails_fast_after_trip(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.extend([httpx.Response(429), httpx.Response(429)])
    with pytest.raises(RateLimited):
        readwise_sink.push(SUB)
    posts_so_far = len(fake_readwise.save_payloads)
    sleeps_so_far = list(fake_timer.sleeps)

    with pytest.raises(RateLimited, match="cooling down"):
        readwise_sink.push(ReaderSubmission(url="https://x.example/other"))

    assert len(fake_readwise.save_payloads) == posts_so_far  # zero network
    assert fake_timer.sleeps == sleeps_so_far  # zero sleeping


def test_breaker_clears_after_cooldown(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.extend([httpx.Response(429), httpx.Response(429)])
    with pytest.raises(RateLimited) as excinfo:
        readwise_sink.push(SUB)

    assert excinfo.value.retry_after is not None
    fake_timer.now += excinfo.value.retry_after + 1.0
    result = readwise_sink.push(SUB)
    assert result.document_id == "doc-1"


def test_huge_retry_after_raises_immediately(
    fake_readwise: FakeReadwise, fake_timer: FakeTimer, readwise_sink: ReadwiseSink
) -> None:
    fake_readwise.save_responses.append(httpx.Response(429, headers={"Retry-After": "999"}))

    with pytest.raises(RateLimited, match="asked to wait 999s") as excinfo:
        readwise_sink.push(SUB)

    assert fake_timer.sleeps == []  # never slept
    assert len(fake_readwise.save_payloads) == 1  # never retried
    assert excinfo.value.retry_after == 999.0  # cooldown carries the server's ask


# ---- push-only contract / check_token -------------------------------------------


def test_sink_has_no_delete_capability(readwise_sink: ReadwiseSink) -> None:
    """Pulp Wise is push-only: the sink must not grow a Reader-delete API.

    Deleting in Reader is the user's call; the ledger tombstone is the only
    delete-shaped thing this tool does.
    """
    assert not hasattr(readwise_sink, "delete")


def test_check_token_true_on_204(readwise_sink: ReadwiseSink) -> None:
    assert readwise_sink.check_token() is True


def test_check_token_false_on_401(fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink) -> None:
    fake_readwise.auth_status = 401
    assert readwise_sink.check_token() is False


def test_check_token_inconclusive_on_5xx_raises(
    fake_readwise: FakeReadwise, readwise_sink: ReadwiseSink
) -> None:
    """An outage must not be rendered as 'token rejected' - a 500 is
    inconclusive and raises FetchError instead of returning False."""
    fake_readwise.auth_status = 500
    with pytest.raises(FetchError, match="inconclusive"):
        readwise_sink.check_token()


# ---- resolve_token ------------------------------------------------------------


def test_env_var_wins_over_config_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULPWISE_READWISE_TOKEN", " env-token ")
    cfg = Config(auth={"readwise": {"token": "cfg-token"}})
    assert resolve_token(cfg) == "env-token"


def test_config_token_wins_over_token_path(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    cfg = Config(auth={"readwise": {"token": "cfg-token", "token_path": str(token_file)}})
    assert resolve_token(cfg) == "cfg-token"


def test_token_path_is_read_and_stripped(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("  file-token \n", encoding="utf-8")
    cfg = Config(auth={"readwise": {"token_path": str(token_file)}})
    assert resolve_token(cfg) == "file-token"


def test_empty_token_path_file_raises(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("   \n", encoding="utf-8")
    cfg = Config(auth={"readwise": {"token_path": str(token_file)}})
    with pytest.raises(ReadwiseAuthError, match="is empty"):
        resolve_token(cfg)


def test_unreadable_token_path_raises(tmp_path: Path) -> None:
    cfg = Config(auth={"readwise": {"token_path": str(tmp_path / "nope")}})
    with pytest.raises(ReadwiseAuthError, match="cannot read"):
        resolve_token(cfg)


def test_missing_token_raises_with_setup_hint() -> None:
    with pytest.raises(ReadwiseAuthError, match=r"readwise\.io/access_token"):
        resolve_token(Config())


def test_from_config_uses_resolved_token(
    fake_readwise: FakeReadwise, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULPWISE_READWISE_TOKEN", "env-token")
    sink = ReadwiseSink.from_config(Config(), client=fake_readwise.client())
    sink.push(SUB)
    assert fake_readwise.save_auth_headers == ["Token env-token"]
