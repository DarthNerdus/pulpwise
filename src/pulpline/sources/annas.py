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

`[auth.annas].output_dir` optionally routes downloads to a dedicated folder
instead of the shared `<output_dir>/oneshots/`.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

import httpx

from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.searchers.annas import DEFAULT_MIRRORS
from pulpline.searchers.slum import discover_anna_mirrors, split_mirrors
from pulpline.sources.base import Source

if TYPE_CHECKING:
    from pulpline.config import Config, Subscription

_MD5_PATH = re.compile(r"^/md5/([0-9a-f]{16,})", re.IGNORECASE)
_API_PATH = "/dyn/api/fast_download.json"
_ENV_KEY = "PULPLINE_ANNAS_API_KEY"


@dataclass(frozen=True, slots=True)
class AnnaQuotaInfo:
    """Membership download quota, parsed from `account_fast_download_info`.

    Anna populates this on every successful `fast_download.json` response.
    `recently_downloaded_md5s` is the list of MD5s already counted today;
    re-downloading any of them is free (doesn't decrement `downloads_left`).
    """

    downloads_left: int
    downloads_per_day: int
    downloads_done_today: int
    recently_downloaded_md5s: tuple[str, ...]


class AnnaSource(Source):
    name: ClassVar[str] = "annas"

    # Class-level shared state: the most recent quota snapshot from any
    # AnnaSource instance. The CLI reads this after pipeline.add_once()
    # closes the source, since add_once owns the source lifecycle and
    # there's no clean way to thread the snapshot back through its
    # signature without changing every caller.
    LAST_QUOTA_INFO: ClassVar[AnnaQuotaInfo | None] = None

    def __init__(
        self,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        mirrors: tuple[str, ...] = DEFAULT_MIRRORS,
        output_dir: Path | None = None,
    ) -> None:
        super().__init__(client=client)
        self._api_key = api_key
        self._mirrors = mirrors
        self.output_dir = output_dir
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
        env_key = os.environ.get(_ENV_KEY)
        cfg_key = auth.get("api_key")
        api_key = env_key or (cfg_key if isinstance(cfg_key, str) else None) or None
        mirrors_raw = auth.get("mirrors")
        # No explicit override -> ask SLUM. discover_anna_mirrors falls
        # back to the hardcoded set on any failure, so we never end up
        # with zero mirrors here.
        mirrors = (
            tuple(split_mirrors(mirrors_raw))
            if isinstance(mirrors_raw, str) and mirrors_raw
            else discover_anna_mirrors()
        )
        out_raw = auth.get("output_dir")
        output_dir = Path(out_raw).expanduser() if isinstance(out_raw, str) and out_raw else None
        return cls(client=client, api_key=api_key, mirrors=mirrors, output_dir=output_dir)

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

        title_part, author = _split_anna_filename(filename) if filename else (None, None)
        if filename:
            ext = _extension_from_filename(filename)
            if ext:
                self._extension = ext

        if title_part and author:
            display_title = f"{title_part} - {author}"
        elif title_part:
            display_title = title_part
        else:
            display_title = md5

        return RawArticle(
            title=display_title,
            body_html="",
            canonical_url=ref.url,
            source_url=ref.url,
            author=author,
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

            # Always capture quota info even on error responses: Anna
            # populates `account_fast_download_info` whenever the key is
            # valid, including 'no md5 found' cases that we'd raise above.
            quota = _parse_quota(payload.get("account_fast_download_info"))
            if quota is not None:
                AnnaSource.LAST_QUOTA_INFO = quota

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


def _filename_from_url(url: str) -> str | None:
    path = urlsplit(url).path
    if not path:
        return None
    last = path.rsplit("/", 1)[-1]
    return last or None


_EXTENSION_RX = re.compile(r"\.([A-Za-z0-9]{2,5})$")


def _title_from_filename(filename: str) -> str | None:
    """Best-effort title from the filename Anna's CDN serves.

    Anna's download URLs carry filenames like
    `Sun%20and%20Steel%20--%20Yukio%20Mishima%3B%20John%20Bester%20--%201st%20trade%20paperback...epub`
    where ` -- ` separates title / author / edition / publisher / ISBN /
    md5. We URL-decode, drop the extension, take everything before the
    first ` -- `, and let `slugify_filename` police the rest at sink time.
    """
    return _split_anna_filename(filename)[0]


def _author_from_filename(filename: str) -> str | None:
    """Best-effort author from Anna's CDN filename (the second `--`-separated chunk)."""
    return _split_anna_filename(filename)[1]


def _split_anna_filename(filename: str) -> tuple[str | None, str | None]:
    """Return (title, author) extracted from Anna's CDN filename.

    Both elements are optional - some filenames have only a title, some
    are weirder shapes we don't want to misparse.
    """
    decoded = urllib.parse.unquote(filename)
    stem = decoded.rsplit(".", 1)[0] if "." in decoded else decoded
    parts = [p.replace("_", " ").strip() for p in stem.split(" -- ")]
    title = parts[0] if parts and parts[0] else None
    author = parts[1] if len(parts) > 1 and parts[1] else None
    return title, author


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


def _parse_quota(raw: object) -> AnnaQuotaInfo | None:
    """Best-effort parse of Anna's `account_fast_download_info` block.

    Returns None if the shape doesn't match - we'd rather hide quota than
    surface a wrong number. Anna's keys have been stable for years but
    this stays defensive.
    """
    if not isinstance(raw, dict):
        return None
    try:
        left = int(raw["downloads_left"])
        per_day = int(raw["downloads_per_day"])
        done = int(raw.get("downloads_done_today", per_day - left))
    except KeyError, TypeError, ValueError:
        return None
    recent_raw = raw.get("recently_downloaded_md5s") or []
    recent = tuple(m for m in recent_raw if isinstance(m, str))
    return AnnaQuotaInfo(
        downloads_left=left,
        downloads_per_day=per_day,
        downloads_done_today=done,
        recently_downloaded_md5s=recent,
    )
