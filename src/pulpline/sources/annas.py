"""Anna's Archive download source via the donation `fast_download.json` API.

Anna's `fast_download.json` is the legitimate, donation-gated download
endpoint - it returns a JSON envelope `{download_url, error}`, and we
follow the URL for the actual bytes. No CAPTCHA, no cookies; just the
secret key the user sets in `[auth.annas].api_key` (or via the
`PULPLINE_ANNAS_API_KEY` env var for CI / secret managers).

Search lives in `searchers.annas.AnnaSearcher`; this module is only the
URL-routed download path. `pulp add https://annas-archive.<tld>/md5/<hash>`
hits this code as a one-shot, and so does the search-pick flow once the
user picks a result.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

import httpx

from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.searchers.annas import DEFAULT_MIRRORS
from pulpline.sources.base import Source

if TYPE_CHECKING:
    from pulpline.config import Config, Subscription

_MD5_PATH = re.compile(r"^/md5/([0-9a-f]{16,})", re.IGNORECASE)
_API_PATH = "/dyn/api/fast_download.json"
_ENV_KEY = "PULPLINE_ANNAS_API_KEY"


class AnnaSource(Source):
    name: ClassVar[str] = "annas"

    def __init__(
        self,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        mirrors: tuple[str, ...] = DEFAULT_MIRRORS,
    ) -> None:
        super().__init__(client=client)
        self._api_key = api_key
        self._mirrors = mirrors
        # Pipeline reads `source.extension`; we override the inherited
        # ClassVar with an instance attribute so we can update it once
        # fast_download.json reveals the real file type. Default "epub"
        # is the most common case; render() rewrites for pdf / mobi / cbz.
        self._extension = "epub"
        # md5 -> (download_url, filename); populated by fetch(), drained by
        # render(). Keeps `render()` from re-paying the API quota for one
        # logical download.
        self._download_cache: dict[str, tuple[str, str | None]] = {}

    # `extension` shadows the parent ClassVar via a property so the pipeline
    # reads the actual file type once we know it. The setter exists only to
    # keep mypy happy with the parent's writable attribute contract; we
    # update via the underlying `_extension` from inside this class.
    @property
    def extension(self) -> str:
        return self._extension

    @extension.setter
    def extension(self, value: str) -> None:
        self._extension = value

    @classmethod
    def matches_url(cls, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if not host.startswith("annas-archive."):
            return False
        return bool(_MD5_PATH.match(urlsplit(url).path))

    @classmethod
    def is_subscribable(cls, url: str) -> bool:
        del url
        return False

    @classmethod
    def default_subscription_name(cls, url: str) -> str:
        del url
        return "annas"

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        client: httpx.Client | None = None,
        subscription: Subscription | None = None,
    ) -> AnnaSource:
        del subscription
        auth = cfg.auth_for("annas")
        api_key = os.environ.get(_ENV_KEY) or auth.get("api_key") or None
        mirrors_raw = auth.get("mirrors")
        mirrors = tuple(_split_mirrors(mirrors_raw)) if mirrors_raw else DEFAULT_MIRRORS
        return cls(client=client, api_key=api_key, mirrors=mirrors)

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        md5 = _md5_from_url(target_url)
        yield ItemRef(url=f"https://annas-archive.{self._mirrors[0]}/md5/{md5}", guid=md5)

    def fetch(self, ref: ItemRef) -> RawArticle:
        # The pipeline writes `<article.title>.<source.extension>`, both of
        # which we only learn from the fast_download response (its filename
        # in the download URL path). So fetch() makes the API call now and
        # caches the URL for render() to drain. Cost: one more API hit only
        # if the dedup check upstream didn't already short-circuit.
        if not self._api_key:
            raise FetchError(
                "Anna's Archive download requires an API key. "
                "Set [auth.annas].api_key in config (or PULPLINE_ANNAS_API_KEY) "
                "after donating at https://annas-archive.li/donate."
            )

        md5 = _md5_from_url(ref.url)
        download_url, filename = self._request_download_url(md5)
        self._download_cache[md5] = (download_url, filename)

        title = _title_from_filename(filename) if filename else md5
        if filename:
            ext = _extension_from_filename(filename)
            if ext:
                self._extension = ext

        return RawArticle(
            title=title or md5,
            body_html="",
            canonical_url=ref.url,
            source_url=ref.url,
            publisher="Anna's Archive",
        )

    def render(self, article: RawArticle) -> bytes:
        md5 = _md5_from_url(article.canonical_url)
        cached = self._download_cache.get(md5)
        if cached is None:
            # Defensive: pipeline always calls fetch() first, but a direct
            # render() call would land here. Re-do the API hit.
            cached = self._request_download_url(md5)
        download_url, _filename = cached

        try:
            response = self.client.get(download_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(
                f"Anna download failed for md5 {md5}: {_redact(exc, self._api_key)}"
            ) from None
        return response.content

    def _request_download_url(self, md5: str) -> tuple[str, str | None]:
        """Hit fast_download.json across mirrors until one succeeds.

        Returns (download_url, suggested_filename_or_None). The filename is
        scraped from a Content-Disposition header on the redirect target if
        present; we don't trust it for security but use it for the extension.
        """
        last_error: Exception | None = None
        for tld in self._mirrors:
            api_url = f"https://annas-archive.{tld}{_API_PATH}"
            try:
                response = self.client.get(
                    api_url,
                    params={"md5": md5, "key": self._api_key},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = exc
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                last_error = exc
                continue

            if not isinstance(payload, dict):
                last_error = ExtractionError(f"fast_download.json returned non-object on .{tld}")
                continue

            error_msg = payload.get("error")
            if error_msg:
                # API-level error (quota, bad key, unknown md5). Don't leak the
                # key value back; surface only the API's own message.
                raise FetchError(f"Anna API error: {error_msg}")

            url = payload.get("download_url")
            if not isinstance(url, str) or not url:
                last_error = ExtractionError(
                    f"fast_download.json on .{tld} returned no download_url"
                )
                continue

            # Anna includes the filename in the Content-Disposition of the
            # eventual download. We can't read it without HEAD-ing, so for
            # now infer the extension from the URL path itself.
            filename = _filename_from_url(url)
            return url, filename

        if last_error is not None:
            raise FetchError(
                f"all Anna's mirrors failed: {_redact(last_error, self._api_key)}"
            ) from None
        raise FetchError("no Anna's mirrors configured")


def _md5_from_url(url: str) -> str:
    match = _MD5_PATH.match(urlsplit(url).path)
    if not match:
        raise ExtractionError(f"not an Anna md5 URL: {url}")
    return match.group(1).lower()


def _split_mirrors(raw: str) -> list[str]:
    return [p.strip().lstrip(".") for p in raw.replace(",", " ").split() if p.strip()]


def _filename_from_url(url: str) -> str | None:
    path = urlsplit(url).path
    if not path:
        return None
    last = path.rsplit("/", 1)[-1]
    return last or None


_EXTENSION_RX = re.compile(r"\.([A-Za-z0-9]{2,5})$")


def _title_from_filename(filename: str) -> str | None:
    """Best-effort title from the filename Anna's CDN serves.

    Strips the extension and replaces underscores with spaces. The pipeline
    sanitizes the result again before writing, so we don't need to police
    every weird character here.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = stem.replace("_", " ").strip()
    return stem or None


_KNOWN_EXTENSIONS = frozenset(
    {"epub", "pdf", "mobi", "azw3", "azw", "djvu", "cbz", "cbr", "fb2", "doc", "docx", "txt"}
)


def _extension_from_filename(filename: str) -> str | None:
    match = _EXTENSION_RX.search(filename)
    if not match:
        return None
    ext = match.group(1).lower()
    # Filter to known ebook/document extensions to avoid junk like .com / .org.
    if ext in _KNOWN_EXTENSIONS:
        return ext
    return None


def _redact(exc: object, api_key: str | None) -> str:
    """Strip the API key from any exception message before surfacing it."""
    text = str(exc)
    if api_key:
        text = text.replace(api_key, "<redacted>")
    return text
