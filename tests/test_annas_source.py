"""Tests for AnnaSource - mocked httpx against fast_download.json shape."""

from __future__ import annotations

import httpx
import pytest

from pulpline.config import Config
from pulpline.models import FetchError, ItemRef
from pulpline.searchers.annas import DEFAULT_MIRRORS
from pulpline.sources.annas import (
    AnnaQuotaInfo,
    AnnaSource,
    _author_from_filename,
    _extension_from_filename,
    _md5_from_url,
    _parse_quota,
    _redact,
    _title_from_filename,
)

MD5 = "abcdef0123456789abcdef0123456789"
TARGET_URL = f"https://annas-archive.gl/md5/{MD5}"
FAKE_KEY = "test-key-not-real"


def _routed_client(routes: dict[str, httpx.Response]) -> httpx.Client:
    """MockTransport that returns canned responses keyed by URL (sans query)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url in routes:
            return routes[url]
        return httpx.Response(404, text=f"unmocked: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_matches_url_accepts_md5_paths_on_known_mirrors() -> None:
    for tld in DEFAULT_MIRRORS:
        assert AnnaSource.matches_url(f"https://annas-archive.{tld}/md5/{MD5}")
    assert AnnaSource.matches_url(f"https://annas-archive.gl/md5/{MD5}/some-title")


def test_matches_url_rejects_non_md5_paths() -> None:
    assert not AnnaSource.matches_url("https://annas-archive.gl/search?q=foo")
    assert not AnnaSource.matches_url("https://annas-archive.gl/")
    assert not AnnaSource.matches_url("https://example.com/md5/abc")


def test_is_subscribable_always_false() -> None:
    assert not AnnaSource.is_subscribable(TARGET_URL)


def test_md5_extraction_helper() -> None:
    assert _md5_from_url(TARGET_URL) == MD5
    with pytest.raises(Exception):
        _md5_from_url("https://example.com/foo")


def test_extension_from_filename_filters_junk() -> None:
    assert _extension_from_filename("Designing_Data.epub") == "epub"
    assert _extension_from_filename("paper.PDF") == "pdf"
    assert _extension_from_filename("noext") is None
    assert _extension_from_filename("annas-archive.org") is None  # not a known ext


def test_title_from_filename_strips_ext_and_decodes_underscores() -> None:
    assert _title_from_filename("Designing_Data.epub") == "Designing Data"
    assert _title_from_filename("paper.pdf") == "paper"


def test_title_from_filename_decodes_percent_and_trims_at_anna_separator() -> None:
    """Anna serves filenames like 'Sun%20and%20Steel%20--%20Yukio%20Mishima...epub'."""
    raw = (
        "Sun%20and%20Steel%20--%20Yukio%20Mishima%3B%20John%20Bester"
        "%20--%201st%20trade%20paperback%20ed%2C%20Tokyo.epub"
    )
    assert _title_from_filename(raw) == "Sun and Steel"


def test_author_from_filename_extracts_second_anna_field() -> None:
    raw = (
        "Sun%20and%20Steel%20--%20Yukio%20Mishima%3B%20John%20Bester"
        "%20--%201st%20trade%20paperback%20ed.epub"
    )
    assert _author_from_filename(raw) == "Yukio Mishima; John Bester"


def test_author_from_filename_none_when_only_title() -> None:
    assert _author_from_filename("just_a_title.epub") is None


def test_redact_strips_api_key_from_messages() -> None:
    assert _redact(f"oops {FAKE_KEY} bad", FAKE_KEY) == "oops <redacted> bad"
    assert _redact("no key here", FAKE_KEY) == "no key here"
    assert _redact("anything", None) == "anything"


def test_fetch_without_api_key_raises_clear_error() -> None:
    with AnnaSource() as source, pytest.raises(FetchError, match="API key"):
        source.fetch(ItemRef(url=TARGET_URL))


def test_fetch_calls_fast_download_and_extracts_metadata() -> None:
    api_url = "https://annas-archive.gl/dyn/api/fast_download.json"
    download_url = "https://download.example/server/Designing_Data_Intensive_Applications.epub"
    routes = {api_url: httpx.Response(200, json={"download_url": download_url})}

    with AnnaSource(client=_routed_client(routes), api_key=FAKE_KEY) as source:
        article = source.fetch(ItemRef(url=TARGET_URL))

    assert article.title == "Designing Data Intensive Applications"
    assert article.author is None
    assert article.canonical_url == TARGET_URL
    assert article.publisher == "Anna's Archive"
    assert source.extension == "epub"


def test_fetch_combines_title_and_author_from_anna_filename() -> None:
    api_url = "https://annas-archive.gl/dyn/api/fast_download.json"
    download_url = (
        "https://download.example/server/"
        "Sun%20and%20Steel%20--%20Yukio%20Mishima%20--%202003%20--%20Kodansha.epub"
    )
    routes = {api_url: httpx.Response(200, json={"download_url": download_url})}

    with AnnaSource(client=_routed_client(routes), api_key=FAKE_KEY) as source:
        article = source.fetch(ItemRef(url=TARGET_URL))

    assert article.title == "Sun and Steel - Yukio Mishima"
    assert article.author == "Yukio Mishima"


def test_render_downloads_bytes_from_cached_url() -> None:
    api_url = "https://annas-archive.gl/dyn/api/fast_download.json"
    download_url = "https://download.example/server/book.pdf"
    body = b"%PDF-1.4 fake bytes"
    routes = {
        api_url: httpx.Response(200, json={"download_url": download_url}),
        download_url: httpx.Response(200, content=body),
    }

    with AnnaSource(client=_routed_client(routes), api_key=FAKE_KEY) as source:
        article = source.fetch(ItemRef(url=TARGET_URL))
        content = source.render(article)

    assert content == body
    assert source.extension == "pdf"


def test_fetch_falls_over_to_next_mirror_on_http_error() -> None:
    fallback_api = f"https://annas-archive.{DEFAULT_MIRRORS[1]}/dyn/api/fast_download.json"
    download_url = "https://download.example/book.epub"

    routes = {
        f"https://annas-archive.{DEFAULT_MIRRORS[0]}/dyn/api/fast_download.json": httpx.Response(
            503, text="down"
        ),
        fallback_api: httpx.Response(200, json={"download_url": download_url}),
    }

    with AnnaSource(client=_routed_client(routes), api_key=FAKE_KEY) as source:
        article = source.fetch(ItemRef(url=TARGET_URL))

    assert article.canonical_url == TARGET_URL


def test_fetch_surfaces_api_error_field_without_leaking_key() -> None:
    api_url = "https://annas-archive.gl/dyn/api/fast_download.json"
    routes = {api_url: httpx.Response(200, json={"error": "Quota exceeded today."})}

    with (
        AnnaSource(client=_routed_client(routes), api_key=FAKE_KEY) as source,
        pytest.raises(FetchError) as exc_info,
    ):
        source.fetch(ItemRef(url=TARGET_URL))
    assert "Quota exceeded" in str(exc_info.value)
    assert FAKE_KEY not in str(exc_info.value)


def test_fetch_raises_when_all_mirrors_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        AnnaSource(client=client, api_key=FAKE_KEY) as source,
        pytest.raises(FetchError, match="all Anna's mirrors failed"),
    ):
        source.fetch(ItemRef(url=TARGET_URL))


def test_from_config_reads_api_key_and_mirrors() -> None:
    cfg = Config(auth={"annas": {"api_key": FAKE_KEY, "mirrors": "gl, pk"}})
    source = AnnaSource.from_config(cfg)
    assert source._api_key == FAKE_KEY
    assert source._mirrors == ("gl", "pk")


def test_from_config_without_mirrors_consults_slum(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no [auth.annas].mirrors override, SLUM discovery runs."""
    called: dict[str, int] = {"n": 0}

    def fake_discover(*args: object, **kwargs: object) -> tuple[str, ...]:
        called["n"] += 1
        return ("vg", "pk")  # arbitrary - just verify the wire-up

    monkeypatch.setattr("pulpline.sources.annas.discover_anna_mirrors", fake_discover)

    cfg = Config(auth={"annas": {"api_key": FAKE_KEY}})  # no mirrors override
    source = AnnaSource.from_config(cfg)
    assert called["n"] == 1
    assert source._mirrors == ("vg", "pk")


def test_parse_quota_extracts_membership_fields() -> None:
    raw = {
        "downloads_left": 23,
        "downloads_per_day": 25,
        "downloads_done_today": 2,
        "recently_downloaded_md5s": ["aaa", "bbb"],
    }
    quota = _parse_quota(raw)
    assert quota == AnnaQuotaInfo(
        downloads_left=23,
        downloads_per_day=25,
        downloads_done_today=2,
        recently_downloaded_md5s=("aaa", "bbb"),
    )


def test_parse_quota_returns_none_on_bad_shape() -> None:
    assert _parse_quota(None) is None
    assert _parse_quota("not a dict") is None
    assert _parse_quota({}) is None
    assert _parse_quota({"downloads_left": "twenty-three"}) is None


def test_fetch_records_quota_info_on_class_attr() -> None:
    """fast_download.json's quota block lands on AnnaSource.LAST_QUOTA_INFO."""
    AnnaSource.LAST_QUOTA_INFO = None  # reset shared state
    api_url = "https://annas-archive.gl/dyn/api/fast_download.json"
    routes = {
        api_url: httpx.Response(
            200,
            json={
                "download_url": "https://download.example/server/book.epub",
                "account_fast_download_info": {
                    "downloads_left": 22,
                    "downloads_per_day": 25,
                    "downloads_done_today": 3,
                    "recently_downloaded_md5s": ["abc"],
                },
            },
        )
    }
    with AnnaSource(client=_routed_client(routes), api_key=FAKE_KEY) as source:
        source.fetch(ItemRef(url=TARGET_URL))

    quota: AnnaQuotaInfo | None = AnnaSource.LAST_QUOTA_INFO
    assert quota is not None
    assert quota.downloads_left == 22
    assert quota.downloads_per_day == 25


def test_from_config_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULPLINE_ANNAS_API_KEY", "env-key-not-real")
    cfg = Config()  # no auth
    source = AnnaSource.from_config(cfg)
    assert source._api_key == "env-key-not-real"
    assert source._mirrors == DEFAULT_MIRRORS
