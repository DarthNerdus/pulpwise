"""CLI smoke + integration tests."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pulpwise import __version__, cli, pipeline
from pulpwise.cli import app
from pulpwise.models import ExtractionError, FetchError
from pulpwise.sinks.readwise import ReadwiseAuthError

runner = CliRunner()

_ANSI_RX = re.compile(r"\x1b\[[0-9;]*m")

_READER_URL = "https://read.readwise.io/read/01abc"


def _help_text(output: str) -> str:
    """Normalize Rich-formatted typer help so substring asserts work in CI.

    On narrow CI terminals, Rich wraps option names across lines with ANSI
    color codes between characters, so '--once' can appear as
    '--\x1b[0m\nonce' and a literal `in` check fails. This collapses both.
    """
    return _ANSI_RX.sub("", output).replace("\n", " ")


class _FakeSink:
    """Stands in for the shared batch ReadwiseSink; records close()."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_sink(monkeypatch: pytest.MonkeyPatch) -> _FakeSink:
    """Replace cli.ReadwiseSink so one-shot dispatch never needs a real token."""
    sink = _FakeSink()
    monkeypatch.setattr(
        cli, "ReadwiseSink", SimpleNamespace(from_config=lambda cfg, client=None: sink)
    )
    return sink


def test_help_exits_zero_and_mentions_purpose() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Readwise Reader" in _help_text(result.output)


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_add_help_lists_once_flag() -> None:
    result = runner.invoke(app, ["add", "--help"])
    assert result.exit_code == 0
    assert "--once" in _help_text(result.output)


def test_tui_help_names_all_views() -> None:
    result = runner.invoke(app, ["tui", "--help"])
    assert result.exit_code == 0
    text = _help_text(result.output)
    for view in ("Library", "Subscriptions", "Sync", "Stats"):
        assert view in text


def test_add_once_prints_reader_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)
    captured: dict[str, object] = {}

    def fake_add_once(url: str, *args: object, **kwargs: object) -> pipeline.AddResult:
        captured["url"] = url
        return pipeline.AddResult(reader_url=_READER_URL, deduped=False)

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", "https://example.com/article", "--once"])
    assert result.exit_code == 0, result.output
    assert f"→ Readwise: {_READER_URL}" in result.output
    assert captured["url"] == "https://example.com/article"


def test_add_once_passes_location_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)
    captured: dict[str, object] = {}

    def fake_add_once(url: str, *args: object, **kwargs: object) -> pipeline.AddResult:
        captured["location"] = kwargs.get("location")
        return pipeline.AddResult(reader_url=_READER_URL, deduped=False)

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(
        app, ["add", "https://example.com/article", "--once", "--location", "new"]
    )
    assert result.exit_code == 0, result.output
    assert captured["location"] == "new"


def test_add_once_defaults_location_to_none_meaning_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --location the CLI passes None; pipeline.add_once resolves
    that to DEFAULT_LOCATION (Reader's Feed)."""
    _patch_sink(monkeypatch)
    captured: dict[str, object] = {"location": "sentinel"}

    def fake_add_once(url: str, *args: object, **kwargs: object) -> pipeline.AddResult:
        captured["location"] = kwargs.get("location")
        return pipeline.AddResult(reader_url=_READER_URL, deduped=False)

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", "https://example.com/article", "--once"])
    assert result.exit_code == 0, result.output
    assert captured["location"] is None
    assert pipeline.DEFAULT_LOCATION == "feed"


def test_add_rejects_invalid_location(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)

    result = runner.invoke(
        app, ["add", "https://example.com/article", "--once", "--location", "shortlist"]
    )
    assert result.exit_code == 2
    assert "--location must be one of" in result.output


def test_add_once_deduped_reports_existing_document(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)

    def fake_add_once(*args: object, **kwargs: object) -> pipeline.AddResult:
        return pipeline.AddResult(reader_url=_READER_URL, deduped=True)

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", "https://example.com/article", "--once"])
    assert result.exit_code == 0, result.output
    assert f"already pushed → {_READER_URL}" in result.output


def test_add_once_notes_when_readwise_already_had_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)

    def fake_add_once(*args: object, **kwargs: object) -> pipeline.AddResult:
        return pipeline.AddResult(reader_url=_READER_URL, deduped=False, already_in_readwise=True)

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", "https://example.com/article", "--once"])
    assert result.exit_code == 0, result.output
    assert f"→ Readwise: {_READER_URL}" in result.output
    assert "Readwise already had this URL" in result.output


def test_add_once_reports_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)

    def fake_add_once(*args: object, **kwargs: object) -> pipeline.AddResult:
        raise FetchError("boom")

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)
    result = runner.invoke(app, ["add", "https://example.com/x", "--once"])
    assert result.exit_code != 0
    assert "fetch failed" in (result.output + (result.stderr or ""))


def test_add_once_reports_extraction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)

    def fake_add_once(*args: object, **kwargs: object) -> pipeline.AddResult:
        raise ExtractionError("no body")

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)
    result = runner.invoke(app, ["add", "https://example.com/x", "--once"])
    assert result.exit_code != 0
    assert "extraction failed" in (result.output + (result.stderr or ""))


def test_add_once_reports_auth_error_with_setup_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sink(monkeypatch)

    def fake_add_once(*args: object, **kwargs: object) -> pipeline.AddResult:
        raise ReadwiseAuthError(
            "no Readwise access token configured; get one at https://readwise.io/access_token"
        )

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)
    result = runner.invoke(app, ["add", "https://example.com/x", "--once"])
    assert result.exit_code != 0
    assert "readwise.io/access_token" in (result.output + (result.stderr or ""))


def test_add_batch_shares_one_sink_across_oneshots(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-URL `--once` batch builds ONE ReadwiseSink and passes it to every
    add_once call, so save pacing and the 429 breaker span the whole batch."""
    created: list[_FakeSink] = []

    def fake_from_config(cfg: object, client: object = None) -> _FakeSink:
        sink = _FakeSink()
        created.append(sink)
        return sink

    monkeypatch.setattr(cli, "ReadwiseSink", SimpleNamespace(from_config=fake_from_config))

    seen_sinks: list[object] = []

    def fake_add_once(url: str, *args: object, **kwargs: object) -> pipeline.AddResult:
        seen_sinks.append(kwargs.get("sink"))
        return pipeline.AddResult(reader_url=_READER_URL, deduped=False)

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    urls = ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
    result = runner.invoke(app, ["add", *urls, "--once"])

    assert result.exit_code == 0, result.output
    assert len(created) == 1  # exactly one sink for the whole batch
    assert seen_sinks == [created[0]] * 3  # the same object went to every add_once
    assert created[0].closed  # and it was closed when the batch finished


def test_add_once_multi_item_url_suggests_subscribing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`add --once` on a URL whose discover yields != 1 item (arXiv API query)
    fails that URL with a subscribe hint; the rest of the batch still runs."""
    _patch_sink(monkeypatch)
    query = "http://export.arxiv.org/api/query?search_query=cat:cs.AI"
    good = "https://example.com/article"

    def fake_add_once(url: str, *args: object, **kwargs: object) -> pipeline.AddResult:
        if url == query:
            raise RuntimeError("ArXivSource.discover yielded 25 items, expected exactly 1")
        return pipeline.AddResult(reader_url=_READER_URL, deduped=False)

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", query, good, "--once"])
    output = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "subscribe instead" in output
    assert not isinstance(result.exception, RuntimeError)  # no traceback abort
    assert "1 ok, 1 failed" in result.output
