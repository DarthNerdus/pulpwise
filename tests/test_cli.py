"""CLI smoke + integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pulpline import __version__, pipeline
from pulpline.cli import app
from pulpline.models import ExtractionError, FetchError

runner = CliRunner()


def test_help_exits_zero_and_mentions_purpose() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Local-first content pipeline" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_add_help_lists_once_flag() -> None:
    result = runner.invoke(app, ["add", "--help"])
    assert result.exit_code == 0
    assert "--once" in result.output


def test_add_without_once_is_phase_2_stub() -> None:
    result = runner.invoke(app, ["add", "https://example.com"])
    assert result.exit_code != 0
    assert "subscriptions" in (result.output + (result.stderr or "")).lower()


def test_unimplemented_phase2_commands_exit_nonzero() -> None:
    for argv in (["sync"], ["list"], ["remove", "foo"]):
        result = runner.invoke(app, argv)
        assert result.exit_code != 0, f"{argv} should not have succeeded yet"


def test_add_once_invokes_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_add_once(url: str, *args: object, **kwargs: object) -> Path:
        captured["url"] = url
        out = tmp_path / "Fake Title.epub"
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", "https://example.com/article", "--once"])
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert captured["url"] == "https://example.com/article"


def test_add_once_reports_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_add_once(*args: object, **kwargs: object) -> Path:
        raise FetchError("boom")

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)
    result = runner.invoke(app, ["add", "https://example.com/x", "--once"])
    assert result.exit_code != 0
    assert "fetch failed" in (result.output + (result.stderr or ""))


def test_add_once_reports_extraction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_add_once(*args: object, **kwargs: object) -> Path:
        raise ExtractionError("no body")

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)
    result = runner.invoke(app, ["add", "https://example.com/x", "--once"])
    assert result.exit_code != 0
    assert "extraction failed" in (result.output + (result.stderr or ""))
