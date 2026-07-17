"""Shiori sink: saves source URLs via POST /api/links.

Shiori performs its own background extraction. Unlike the Readwise sink, this
sink deliberately ignores every content and metadata field on ReaderSubmission
and sends only ``{"url": ...}``. Private bodies fetched with source credentials
must never be forwarded to Shiori.
"""

from __future__ import annotations

import ipaddress
import os
import time
from collections.abc import Callable
from math import ceil
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pulpwise.config import Config
from pulpwise.models import FetchError, RateLimited, ReaderSubmission
from pulpwise.sinks.readwise import PushResult
from pulpwise.util.http import build_client
from pulpwise.util.logging import get_logger

_log = get_logger("shiori")

SAVE_URL = "https://www.shiori.sh/api/links"
TOKEN_ENV_VAR = "PULPWISE_SHIORI_TOKEN"
_RATE_BUCKET = "shiori"
# Shiori allows 30 link creations/minute. Stay below that during long runs.
_SAVE_MIN_INTERVAL = 60.0 / 25.0


class ShioriAuthError(FetchError):
    """API key missing, unreadable, or rejected by Shiori."""


class ShioriSink:
    """Save public source URLs to one Shiori account."""

    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token:
            raise ShioriAuthError("empty Shiori API key")
        self._token = token
        self._client = client if client is not None else build_client(scope=_RATE_BUCKET)
        self._owns_client = client is None
        self._sleep = sleep
        self._clock = clock
        self._next_save_at = 0.0
        self._blocked_until = 0.0

    @classmethod
    def from_config(cls, cfg: Config, client: httpx.Client | None = None) -> ShioriSink:
        return cls(resolve_token(cfg), client=client)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ShioriSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def push(
        self,
        submission: ReaderSubmission,
        *,
        location: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> PushResult:
        """Save only ``submission.url``; content and Reader routing are ignored."""
        del location, tags
        _validate_public_url(submission.url)
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            raise RateLimited(
                f"rate limited by Shiori: cooling down for another {ceil(remaining)}s",
                host=_RATE_BUCKET,
                retry_after=remaining,
            )
        self._pace()
        try:
            response = self._client.post(
                SAVE_URL,
                json={"url": submission.url},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"Shiori save failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise ShioriAuthError(
                "Shiori rejected the API key; generate a fresh key in Shiori Settings"
            )
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response)
            self._blocked_until = self._clock() + retry_after
            raise RateLimited(
                f"rate limited by Shiori: retry after {retry_after:.0f}s",
                host=_RATE_BUCKET,
                retry_after=retry_after,
            )

        data = _response_json(response)
        if response.status_code != 200 or data.get("success") is not True:
            error = data.get("error")
            detail = str(error) if error else response.text[:300]
            raise FetchError(f"Shiori save failed: HTTP {response.status_code}: {detail}")

        link_id = data.get("linkId")
        if not isinstance(link_id, str) or not link_id:
            raise FetchError("Shiori save returned unexpected body: missing linkId")

        result = PushResult(
            document_id=link_id,
            # Shiori does not document a per-link app URL. Opening the saved
            # public source remains useful and avoids inventing an unstable route.
            reader_url=submission.url,
            already_existed=data.get("duplicate") is True,
            kind="url",
        )
        _log.info(
            "shiori save host=%s link=%s%s",
            urlsplit(submission.url).hostname,
            link_id,
            " (already existed)" if result.already_existed else "",
        )
        return result

    def _pace(self) -> None:
        now = self._clock()
        wait = self._next_save_at - now
        if wait > 0:
            self._sleep(wait)
            now = self._next_save_at
        self._next_save_at = now + _SAVE_MIN_INTERVAL


def resolve_token(cfg: Config) -> str:
    """Resolve Shiori API key from env, inline config, or token file."""
    env = os.environ.get(TOKEN_ENV_VAR)
    if env and env.strip():
        return env.strip()

    auth = cfg.auth_for("shiori")
    token = auth.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()

    token_path = auth.get("token_path")
    if isinstance(token_path, str) and token_path.strip():
        path = Path(token_path).expanduser()
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ShioriAuthError(f"cannot read [auth.shiori].token_path {path}: {exc}") from exc
        if content:
            return content
        raise ShioriAuthError(f"[auth.shiori].token_path {path} is empty")

    raise ShioriAuthError(
        "no Shiori API key configured; generate one in Shiori Settings and set "
        "[auth.shiori].token (or token_path) in config.toml, "
        f"or export {TOKEN_ENV_VAR}"
    )


def _validate_public_url(url: str) -> None:
    error = "Shiori requires a public http(s) source URL"
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise FetchError(f"{error}; the discovered URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise FetchError(error)
    if parsed.username is not None or parsed.password is not None:
        raise FetchError(f"{error}; URLs containing credentials are not allowed")

    normalized_host = hostname.rstrip(".").lower()
    if normalized_host == "pulpwise.invalid":
        raise FetchError(f"{error}; this item has no web URL")
    if normalized_host == "localhost" or normalized_host.endswith((".localhost", ".local")):
        raise FetchError(f"{error}; local hosts are not allowed")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        return
    if not address.is_global:
        raise FetchError(f"{error}; private or local IP addresses are not allowed")


def _response_json(response: httpx.Response) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError as exc:
        raise FetchError(f"Shiori save returned unexpected body: {exc}") from exc
    if not isinstance(data, dict):
        raise FetchError("Shiori save returned unexpected body: expected a JSON object")
    return data


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    if value.strip().isdigit():
        return float(value.strip())
    return 60.0
