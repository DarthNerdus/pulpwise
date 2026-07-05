"""CLI tests for `pulpwise import substack` - config resolution + --auto mode."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pulpwise import cli
from pulpwise.cli import app
from pulpwise.config import (
    Subscription,
    add_subscription,
    load_config,
    save_config,
)
from pulpwise.importers.substack import SubstackPublication

runner = CliRunner()


def _write_cookies(path: Path) -> None:
    """Write a minimal cookies.json the load_cookies parser accepts."""
    path.write_text(
        json.dumps([{"name": "session", "value": "abc", "domain": ".substack.com"}]),
        encoding="utf-8",
    )


def _patch_list_subs(monkeypatch: pytest.MonkeyPatch, pubs: list[SubstackPublication]) -> None:
    monkeypatch.setattr(cli, "list_user_subscriptions", lambda u, c, client: pubs)


def test_auto_adds_only_new_subscriptions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.json"
    _write_cookies(cookies)
    _patch_list_subs(
        monkeypatch,
        [
            SubstackPublication(name="Stratechery", url="https://stratechery.com", paid=True),
            SubstackPublication(name="ACX", url="https://astralcodexten.com", paid=False),
        ],
    )

    # Pre-seed config directly so we don't depend on `pulp add` doing a live
    # fetch to validate the feed.
    save_config(
        add_subscription(
            load_config(),
            Subscription(name="acx", source="rss", url="https://astralcodexten.com/feed"),
        )
    )

    result = runner.invoke(
        app,
        ["import", "substack", "me", "--cookies", str(cookies), "--auto"],
    )
    assert result.exit_code == 0, result.output
    assert "imported 1" in result.output

    cfg = load_config()
    sub_urls = {s.url for s in cfg.subscriptions}
    assert "https://stratechery.com" in sub_urls
    # Pre-existing ACX subscription not touched (still RSS feed URL).
    assert any(s.source == "rss" and "astralcodexten" in s.url for s in cfg.subscriptions)


def test_auto_persists_username_and_cookies_path_to_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.json"
    _write_cookies(cookies)
    _patch_list_subs(
        monkeypatch,
        [SubstackPublication(name="X", url="https://x.example", paid=False)],
    )

    runner.invoke(
        app,
        ["import", "substack", "egor", "--cookies", str(cookies), "--auto"],
    )

    auth = load_config().auth_for("substack")
    assert auth.get("username") == "egor"
    assert auth.get("cookies_path") == str(cookies)


def test_second_run_with_no_args_uses_persisted_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once username + cookies_path are persisted, zero-arg --auto works."""
    cookies = tmp_path / "cookies.json"
    _write_cookies(cookies)
    _patch_list_subs(
        monkeypatch,
        [SubstackPublication(name="X", url="https://x.example", paid=False)],
    )

    # First run with explicit args persists auth.
    runner.invoke(app, ["import", "substack", "egor", "--cookies", str(cookies), "--auto"])

    # Second run with new pubs - just --auto, nothing else.
    _patch_list_subs(
        monkeypatch,
        [
            SubstackPublication(name="X", url="https://x.example", paid=False),
            SubstackPublication(name="Y", url="https://y.example", paid=False),
        ],
    )
    result = runner.invoke(app, ["import", "substack", "--auto"])
    assert result.exit_code == 0, result.output
    assert "imported 1" in result.output


def test_auto_with_nothing_new_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.json"
    _write_cookies(cookies)
    _patch_list_subs(
        monkeypatch,
        [SubstackPublication(name="X", url="https://x.example", paid=False)],
    )

    runner.invoke(app, ["import", "substack", "egor", "--cookies", str(cookies), "--auto"])
    # Re-run with the same one pub.
    result = runner.invoke(app, ["import", "substack", "--auto"])
    assert result.exit_code == 0
    assert "already in config" in result.output


def test_no_username_anywhere_exits_with_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["import", "substack"])
    assert result.exit_code == 2
    assert "username" in (result.output + (result.stderr or ""))


def _seed_substack_auth(extra: dict[str, str | list[str]]) -> None:
    """Write an [auth.substack] table carrying `extra` keys plus stale creds."""
    cfg = load_config()
    substack_auth: dict[str, str | list[str]] = {
        "cookies_path": "/old/cookies.json",
        "username": "olduser",
        **extra,
    }
    new_auth = dict(cfg.auth)
    new_auth["substack"] = substack_auth
    save_config(replace(cfg, auth=new_auth))


def test_import_merges_auth_preserving_extra_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: importing must merge into [auth.substack], not replace the
    table wholesale - a wholesale replace silently deleted extra_cookies_paths."""
    cookies = tmp_path / "cookies.json"
    _write_cookies(cookies)
    _seed_substack_auth({"extra_cookies_paths": ["/extra/a.json", "/extra/b.json"]})

    _patch_list_subs(
        monkeypatch,
        [SubstackPublication(name="X", url="https://x.example", paid=False)],
    )

    result = runner.invoke(app, ["import", "substack", "egor", "--cookies", str(cookies), "--auto"])
    assert result.exit_code == 0, result.output

    auth = load_config().auth_for("substack")
    assert auth["extra_cookies_paths"] == ["/extra/a.json", "/extra/b.json"]
    assert auth["username"] == "egor"
    assert auth["cookies_path"] == str(cookies)


def test_import_noop_path_preserves_extra_auth_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 'all already in config' path persists fresh creds without dropping
    other [auth.substack] keys either."""
    cookies = tmp_path / "cookies.json"
    _write_cookies(cookies)
    _seed_substack_auth({"extra_cookies_paths": ["/extra/a.json"]})
    save_config(
        add_subscription(
            load_config(),
            Subscription(name="x", source="substack", url="https://x.example"),
        )
    )

    _patch_list_subs(
        monkeypatch,
        [SubstackPublication(name="X", url="https://x.example", paid=False)],
    )

    result = runner.invoke(app, ["import", "substack", "egor", "--cookies", str(cookies), "--auto"])
    assert result.exit_code == 0, result.output
    assert "already in config" in result.output

    auth = load_config().auth_for("substack")
    assert auth["extra_cookies_paths"] == ["/extra/a.json"]
    assert auth["username"] == "egor"
    assert auth["cookies_path"] == str(cookies)
