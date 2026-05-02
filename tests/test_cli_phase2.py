"""CLI integration tests for Phase 2 commands (subscribe, list, remove, sync)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pulpline import cli
from pulpline.cli import app
from pulpline.config import Config, Subscription, load_config

ClientFactory = Callable[[dict[str, str]], httpx.Client]
runner = CliRunner()


def _feed() -> str:
    return (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_text()


def _patch_build_client(
    monkeypatch: pytest.MonkeyPatch, factory: ClientFactory, routes: dict[str, str]
) -> None:
    """Make `pulpline.cli.build_client` return a mock-transport client."""

    def _factory() -> httpx.Client:
        return factory(routes)

    monkeypatch.setattr(cli, "build_client", _factory)


def test_add_subscribes_with_derived_name(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    result = runner.invoke(app, ["add", feed_url])

    assert result.exit_code == 0, result.output
    assert "subscribed" in result.output.lower()
    config = load_config()
    assert len(config.subscriptions) == 1
    sub = config.subscriptions[0]
    assert sub.url == feed_url
    assert sub.source == "rss"
    # Name derived from feed title "Stratechery by Ben Thompson" -> sluggy
    assert sub.name == "stratechery-by-ben-thompson"


def test_add_with_explicit_name(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    result = runner.invoke(app, ["add", feed_url, "--name", "stratechery"])

    assert result.exit_code == 0, result.output
    config = load_config()
    assert config.subscriptions[0].name == "stratechery"


def test_add_rejects_non_feed(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    url = "https://example.com/article"
    _patch_build_client(
        monkeypatch, mock_client_factory, {url: "<html><body>Not a feed</body></html>"}
    )

    result = runner.invoke(app, ["add", url])
    assert result.exit_code != 0
    output = result.output + (result.stderr or "")
    assert "feed" in output.lower()


def test_add_rejects_duplicate_name(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    runner.invoke(app, ["add", feed_url, "--name", "dup"])
    result = runner.invoke(app, ["add", feed_url, "--name", "dup"])

    assert result.exit_code != 0
    assert "already exists" in (result.output + (result.stderr or "")).lower()


def test_remove_deletes_subscription_from_config(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})
    runner.invoke(app, ["add", feed_url, "--name", "foo"])

    result = runner.invoke(app, ["remove", "foo"])
    assert result.exit_code == 0
    assert load_config().subscriptions == ()


def test_remove_unknown_name_errors() -> None:
    result = runner.invoke(app, ["remove", "ghost"])
    assert result.exit_code != 0


def test_list_empty_says_so() -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "no subscriptions" in result.output.lower()


def test_list_shows_subscription(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})
    runner.invoke(app, ["add", feed_url, "--name", "foo"])

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "foo" in result.output
    assert feed_url in result.output
    assert "never" in result.output  # not yet synced


def test_sync_with_no_subscriptions_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load() -> Config:
        return Config()

    monkeypatch.setattr(cli, "load_config", fake_load)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "no subscriptions" in result.output.lower()


def test_sync_invokes_pipeline_and_summarizes(monkeypatch: pytest.MonkeyPatch) -> None:
    from pulpline import pipeline

    config = Config(subscriptions=(Subscription(name="alpha", source="rss", url="https://a/feed"),))

    def fake_load() -> Config:
        return config

    def fake_sync(config: Config = config, **kwargs: object) -> pipeline.SyncTotal:
        return pipeline.SyncTotal(
            reports=(pipeline.SyncReport(name="alpha", new_items=2, skipped=1, errors=0),)
        )

    monkeypatch.setattr(cli, "load_config", fake_load)
    monkeypatch.setattr(pipeline, "sync", fake_sync)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "2 new" in result.output
    assert "1 skipped" in result.output
    assert "total: 2 new" in result.output
