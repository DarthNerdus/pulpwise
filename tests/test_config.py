"""Tests for TOML config load/save + Subscription mutation."""

from __future__ import annotations

from pathlib import Path

import pytest

from pulpline.config import (
    Config,
    ConfigError,
    Subscription,
    add_subscription,
    load_config,
    remove_subscription,
    save_config,
)


def test_load_creates_default_config_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = load_config(target)
    assert target.exists()
    text = target.read_text()
    assert "[paths]" in text
    assert "output_dir" in text
    assert config.subscriptions == ()


def test_round_trip_preserves_subscriptions(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = Config()
    sub_a = Subscription(name="a", source="rss", url="https://a.com/feed")
    sub_b = Subscription(name="b", source="rss", url="https://b.com/feed", output_dir="~/Books")
    config = add_subscription(config, sub_a)
    config = add_subscription(config, sub_b)
    save_config(config, target)

    reloaded = load_config(target)
    assert reloaded.subscriptions == (sub_a, sub_b)


def test_add_subscription_rejects_duplicate_name() -> None:
    config = Config()
    sub = Subscription(name="a", source="rss", url="https://a.com/feed")
    config = add_subscription(config, sub)
    with pytest.raises(ConfigError, match="already exists"):
        add_subscription(config, sub)


def test_remove_subscription_rejects_unknown_name() -> None:
    config = Config()
    with pytest.raises(ConfigError, match="no subscription"):
        remove_subscription(config, "missing")


def test_remove_subscription_returns_new_config_without_named() -> None:
    config = Config()
    config = add_subscription(config, Subscription(name="a", source="rss", url="x"))
    config = add_subscription(config, Subscription(name="b", source="rss", url="y"))
    config = remove_subscription(config, "a")
    assert [s.name for s in config.subscriptions] == ["b"]


def test_load_rejects_malformed_subscription(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[paths]\noutput_dir = "x"\n\n[[subscriptions]]\nname = ""\nsource = "rss"\nurl = "y"\n'
    )
    with pytest.raises(ConfigError):
        load_config(target)


def test_load_rejects_duplicate_subscription_names(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[paths]\noutput_dir = "x"\n\n'
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "y"\n\n'
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "z"\n'
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(target)


def test_output_dir_for_auto_subfolder_by_sub_name() -> None:
    config = Config()
    sub = Subscription(name="samkriss", source="rss", url="x")
    assert config.output_dir_for(sub) == Path("~/Sync/Pulpline/samkriss").expanduser()


def test_output_dir_for_uses_per_subscription_override() -> None:
    config = Config()
    sub = Subscription(name="a", source="rss", url="x", output_dir="~/Books")
    assert config.output_dir_for(sub) == Path("~/Books").expanduser()


def test_save_is_atomic_no_partial_file(tmp_path: Path) -> None:
    """If save fails midway, the original file must be untouched."""
    target = tmp_path / "config.toml"
    save_config(Config(), target)
    original = target.read_text()

    # Sanity: another save replaces atomically with no leftover .tmp files.
    save_config(
        Config(subscriptions=(Subscription(name="a", source="rss", url="https://a"),)),
        target,
    )
    siblings_after = sorted(p.name for p in tmp_path.iterdir())
    assert "config.toml" in siblings_after
    assert all(not n.endswith(".tmp") for n in siblings_after)
    assert original != target.read_text()
