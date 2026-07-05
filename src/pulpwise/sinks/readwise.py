"""Readwise Reader sink: pushes documents via POST /api/v3/save/.

Two submission modes, chosen by the `ReaderSubmission` it receives:

  * URL save (`html is None`): Reader fetches and parses the URL itself.
    Only valid for publicly fetchable pages.
  * Content save (`html` set): Reader stores the supplied HTML and never
    fetches the URL. The path for paywalled/private content. `url` is
    still required by the API - it is the document's server-side dedup
    key, so it must be deterministic across runs.

Duplicate detection is the response status: 201 = created, 200 = a
document with that exact URL already existed (Reader bumps it, no new
document). Reader's URL match is byte-exact, so callers should submit
normalized URLs.

Pulp Wise is push-only by design: this sink never deletes or mutates
existing Reader documents. Removing something from Reader is the user's
call, made in Reader; the local ledger's tombstones are what keep sync
from pushing it back.

Rate limit (per access token, verified against readwise.io/reader_api):
save is 50/min. The sink self-paces under the limit and honors 429
Retry-After once before raising `RateLimited`, which the pipeline treats
like any other rate-limited provider (stop the subscription, retry next
run). POSTs bypass `RetryTransport`'s automatic retries (GET/HEAD only),
so the handling lives here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING

import httpx

from pulpwise.models import FetchError, RateLimited, ReaderSubmission
from pulpwise.util.http import build_client
from pulpwise.util.logging import get_logger

if TYPE_CHECKING:
    from pulpwise.config import Config

_log = get_logger("readwise")

SAVE_URL = "https://readwise.io/api/v3/save/"
AUTH_CHECK_URL = "https://readwise.io/api/v2/auth/"
TOKEN_ENV_VAR = "PULPWISE_READWISE_TOKEN"

#: Valid `location` targets on save. The API also knows "shortlist", but only
#: as a list filter - documents cannot be created there.
SAVE_LOCATIONS = frozenset({"new", "later", "archive", "feed"})

# Reader allows 50 saves/min per token; pace at 45/min so a long backfill
# never trips the limiter in the first place.
_SAVE_MIN_INTERVAL = 60.0 / 45.0
# On 429, sleep out one server-provided Retry-After (bounded) and retry once
# before giving up on the run.
_MAX_RETRY_AFTER_WAIT = 120.0
_RATE_BUCKET = "readwise"


class ReadwiseAuthError(FetchError):
    """Token missing, unreadable, or rejected by Readwise."""


@dataclass(frozen=True, slots=True)
class PushResult:
    """Outcome of one save: the Reader document and whether it already existed."""

    document_id: str
    reader_url: str
    already_existed: bool
    kind: str  # 'url' | 'html'


class ReadwiseSink:
    """Pushes `ReaderSubmission`s to one Readwise account.

    Build one sink per run (sync/backfill/one-shot) so all saves share the
    pacing clock. `sleep`/`clock` are injectable for tests.
    """

    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        *,
        clean_html: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token:
            raise ReadwiseAuthError("empty Readwise token")
        self._token = token
        self._client = client if client is not None else build_client(scope=_RATE_BUCKET)
        self._owns_client = client is None
        self._clean_html = clean_html
        self._sleep = sleep
        self._clock = clock
        self._next_save_at = 0.0
        # Once a 429 persists, later pushes through this sink fail fast
        # (no network, no sleep) until the cooldown passes - so a sync run
        # with 30 subscriptions doesn't stack thirty Retry-After waits.
        self._blocked_until = 0.0

    @classmethod
    def from_config(cls, cfg: Config, client: httpx.Client | None = None) -> ReadwiseSink:
        """Build a sink from `[auth.readwise]` / the environment.

        Token resolution order: `PULPWISE_READWISE_TOKEN` env var, then
        `[auth.readwise].token`, then the file at `[auth.readwise].token_path`.
        Raises `ReadwiseAuthError` with a setup hint when none is configured.
        """
        token = resolve_token(cfg)
        return cls(token, client=client)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ReadwiseSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def check_token(self) -> bool:
        """True if Readwise accepts the token (204), False if it rejects it (401).

        Anything else - a 5xx during an outage, a network failure - raises
        FetchError: the check is *inconclusive*, and callers must not render
        that as "token rejected" (the advice to regenerate a perfectly valid
        token would be wrong).
        """
        try:
            response = self._client.get(AUTH_CHECK_URL, headers=self._headers())
        except httpx.HTTPError as exc:
            raise FetchError(f"Readwise auth check failed: {exc}") from exc
        if response.status_code == 204:
            return True
        if response.status_code == 401:
            return False
        raise FetchError(f"Readwise auth check inconclusive: HTTP {response.status_code}")

    def push(
        self,
        submission: ReaderSubmission,
        *,
        location: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> PushResult:
        """Save one document. Returns the Reader document id + app URL.

        Raises `ReadwiseAuthError` on 401, `RateLimited` when a 429
        persists past one waited-out Retry-After, and `FetchError` for
        anything else unexpected.
        """
        if location is not None and location not in SAVE_LOCATIONS:
            valid = ", ".join(sorted(SAVE_LOCATIONS))
            raise ValueError(f"invalid Readwise location {location!r}; valid: {valid}")
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            raise RateLimited(
                f"rate limited by Readwise: cooling down for another {ceil(remaining)}s",
                host=_RATE_BUCKET,
                retry_after=remaining,
            )
        payload = self._payload(submission, location=location, tags=tags)

        self._pace()
        response = self._post(payload)
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response)
            if retry_after > _MAX_RETRY_AFTER_WAIT:
                raise self._trip(retry_after, f"asked to wait {ceil(retry_after)}s")
            _log.warning(
                "Readwise 429; waiting %.0fs then retrying once (url=%s)",
                retry_after,
                submission.url,
            )
            self._sleep(retry_after)
            response = self._post(payload)
            if response.status_code == 429:
                raise self._trip(
                    _retry_after_seconds(response),
                    "429 persisted after waiting out Retry-After",
                )

        if response.status_code == 401:
            raise ReadwiseAuthError(
                "Readwise rejected the access token (HTTP 401); "
                "get a fresh one at https://readwise.io/access_token"
            )
        if response.status_code not in (200, 201):
            body = response.text[:300]
            raise FetchError(f"Readwise save failed: HTTP {response.status_code}: {body}")

        try:
            data = response.json()
            document_id = str(data["id"])
            reader_url = str(data["url"])
        except (ValueError, KeyError, TypeError) as exc:
            raise FetchError(f"Readwise save returned unexpected body: {exc}") from exc

        result = PushResult(
            document_id=document_id,
            reader_url=reader_url,
            already_existed=response.status_code == 200,
            kind=submission.kind,
        )
        _log.info(
            "readwise save %s url=%s doc=%s%s",
            result.kind,
            submission.url,
            document_id,
            " (already existed)" if result.already_existed else "",
        )
        return result

    def _payload(
        self,
        submission: ReaderSubmission,
        *,
        location: str | None,
        tags: tuple[str, ...],
    ) -> dict[str, object]:
        payload: dict[str, object] = {"url": submission.url, "saved_using": "pulpwise"}
        if submission.html is not None:
            payload["html"] = submission.html
            payload["should_clean_html"] = self._clean_html
        if submission.title:
            payload["title"] = submission.title
        if submission.author:
            payload["author"] = submission.author
        if submission.summary:
            payload["summary"] = submission.summary
        if submission.pub_date is not None:
            payload["published_date"] = submission.pub_date.isoformat()
        if submission.category is not None:
            payload["category"] = submission.category
        if location is not None:
            payload["location"] = location
        if tags:
            payload["tags"] = list(tags)
        return payload

    def _post(self, payload: dict[str, object]) -> httpx.Response:
        try:
            return self._client.post(SAVE_URL, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise FetchError(f"Readwise save failed: {exc}") from exc

    def _trip(self, retry_after: float, reason: str) -> RateLimited:
        """Arm the fail-fast breaker and build the RateLimited to raise."""
        cooldown = max(retry_after, 60.0)
        self._blocked_until = self._clock() + cooldown
        return RateLimited(
            f"rate limited by Readwise: {reason}",
            host=_RATE_BUCKET,
            retry_after=cooldown,
        )

    def _pace(self) -> None:
        """Keep consecutive saves at least `_SAVE_MIN_INTERVAL` apart."""
        now = self._clock()
        wait = self._next_save_at - now
        if wait > 0:
            self._sleep(wait)
            now = self._next_save_at
        self._next_save_at = now + _SAVE_MIN_INTERVAL

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._token}"}


def resolve_token(cfg: Config) -> str:
    """Resolve the Readwise access token from env or `[auth.readwise]`.

    Order: `PULPWISE_READWISE_TOKEN` env var wins (keeps secrets out of
    files in CI/cron), then the inline `token` key, then `token_path`
    pointing at a file holding just the token (the `chmod 600` option,
    mirroring `[auth.email].password_path`).
    """
    import os
    from pathlib import Path

    env = os.environ.get(TOKEN_ENV_VAR)
    if env and env.strip():
        return env.strip()

    auth = cfg.auth_for("readwise")
    token = auth.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()

    token_path = auth.get("token_path")
    if isinstance(token_path, str) and token_path.strip():
        path = Path(token_path).expanduser()
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ReadwiseAuthError(
                f"cannot read [auth.readwise].token_path {path}: {exc}"
            ) from exc
        if content:
            return content
        raise ReadwiseAuthError(f"[auth.readwise].token_path {path} is empty")

    raise ReadwiseAuthError(
        "no Readwise access token configured; get one at https://readwise.io/access_token "
        "and set [auth.readwise].token (or token_path) in config.toml, "
        f"or export {TOKEN_ENV_VAR}"
    )


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    if value.strip().isdigit():
        return float(value.strip())
    return 60.0
