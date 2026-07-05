"""Tests for TOML config load/save: subscriptions, auth, legacy-key tolerance."""

from __future__ import annotations

from pathlib import Path

import pytest

from pulpwise.config import (
    Config,
    ConfigError,
    Subscription,
    add_subscription,
    load_config,
    remove_subscription,
    save_config,
    set_subscription_disabled,
)


def test_load_creates_default_config_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = load_config(target)
    assert target.exists()
    text = target.read_text()
    assert "auth.readwise" in text  # the template points at token setup
    assert config.subscriptions == ()
    assert config.auth == {}


def test_round_trip_preserves_subscriptions_and_options(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    sub_a = Subscription(name="a", source="rss", url="https://a.com/feed")
    sub_b = Subscription(
        name="b",
        source="email",
        url="imap://mail.example",
        options={"location": "feed", "tags": "tech, essays", "since_days": 30},
    )
    config = add_subscription(add_subscription(Config(), sub_a), sub_b)
    save_config(config, target)

    reloaded = load_config(target)
    assert reloaded.subscriptions == (sub_a, sub_b)
    assert reloaded.find("b").options == {  # type: ignore[union-attr]
        "location": "feed",
        "tags": "tech, essays",
        "since_days": 30,
    }


def test_auth_readwise_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = Config(
        auth={
            "readwise": {"token": "tok-123"},
            "substack": {"cookies_path": "~/c.json", "extra_cookies_paths": ["~/d.json"]},
        }
    )
    save_config(config, target)

    reloaded = load_config(target)
    assert reloaded.auth_for("readwise") == {"token": "tok-123"}
    assert reloaded.auth_for("substack")["extra_cookies_paths"] == ["~/d.json"]
    assert reloaded.auth_for("nonexistent") == {}


def test_auth_toml_booleans_normalize_to_strings(tmp_path: Path) -> None:
    """Flag-shaped auth settings (`auto_reconcile = true`) can be written as
    natural TOML booleans; the loader normalizes them to "true"/"false"
    strings so consumers keep seeing str | list[str]."""
    target = tmp_path / "config.toml"
    target.write_text(
        '[auth.substack]\ncookies_path = "~/c.json"\nauto_reconcile = true\nother = false\n',
        encoding="utf-8",
    )

    loaded = load_config(target)
    assert loaded.auth_for("substack")["auto_reconcile"] == "true"
    assert loaded.auth_for("substack")["other"] == "false"


def test_auth_integers_still_rejected(tmp_path: Path) -> None:
    """bool is an int subclass; make sure accepting bools didn't quietly
    start accepting real integers."""
    target = tmp_path / "config.toml"
    target.write_text("[auth.substack]\nauto_reconcile = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"auth\.substack\.auto_reconcile"):
        load_config(target)


# ---- legacy pulpline keys ---------------------------------------------------------


def test_legacy_paths_table_and_output_dir_load_and_drop_on_save(tmp_path: Path) -> None:
    """A copied-over pulpline config must load cleanly; the file-sink keys
    are ignored and disappear on the next save."""
    target = tmp_path / "config.toml"
    target.write_text(
        '[paths]\noutput_dir = "~/Sync/Pulpwise"\n\n'
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "https://a.com/feed"\n'
        'output_dir = "~/Books"\n',
        encoding="utf-8",
    )

    config = load_config(target)
    assert [s.name for s in config.subscriptions] == ["a"]
    assert not hasattr(config, "paths")

    save_config(config, target)
    text = target.read_text()
    assert "paths" not in text
    assert "output_dir" not in text
    assert load_config(target).subscriptions == config.subscriptions


def test_legacy_paths_must_still_be_a_table(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('paths = "nope"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="`paths` must be a table"):
        load_config(target)


def test_legacy_output_dir_must_still_be_a_string(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "x"\noutput_dir = 5\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="output_dir must be a string"):
        load_config(target)


# ---- mutation helpers ---------------------------------------------------------------


def test_add_subscription_rejects_duplicate_name() -> None:
    config = add_subscription(Config(), Subscription(name="a", source="rss", url="x"))
    with pytest.raises(ConfigError, match="already exists"):
        add_subscription(config, Subscription(name="a", source="rss", url="y"))


def test_remove_subscription_rejects_unknown_name() -> None:
    with pytest.raises(ConfigError, match="no subscription"):
        remove_subscription(Config(), "missing")


def test_remove_subscription_returns_new_config_without_named() -> None:
    config = Config()
    config = add_subscription(config, Subscription(name="a", source="rss", url="x"))
    config = add_subscription(config, Subscription(name="b", source="rss", url="y"))
    config = remove_subscription(config, "a")
    assert [s.name for s in config.subscriptions] == ["b"]


def test_disabled_round_trips_and_defaults_false(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    sub_on = Subscription(name="on", source="rss", url="https://on.example/feed")
    sub_off = Subscription(name="off", source="rss", url="https://off.example/feed", disabled=True)
    save_config(Config(subscriptions=(sub_on, sub_off)), target)

    reloaded = load_config(target)
    assert reloaded.find("on").disabled is False  # type: ignore[union-attr]
    assert reloaded.find("off").disabled is True  # type: ignore[union-attr]
    # Enabled subs don't get a noisy `disabled = false` line written out.
    assert target.read_text().count("disabled") == 1


def test_set_subscription_disabled_toggles_only_named() -> None:
    config = Config()
    config = add_subscription(config, Subscription(name="a", source="rss", url="x"))
    config = add_subscription(config, Subscription(name="b", source="rss", url="y"))
    config = set_subscription_disabled(config, "a", True)
    assert config.find("a").disabled is True  # type: ignore[union-attr]
    assert config.find("b").disabled is False  # type: ignore[union-attr]
    config = set_subscription_disabled(config, "a", False)
    assert config.find("a").disabled is False  # type: ignore[union-attr]


def test_set_subscription_disabled_rejects_unknown_name() -> None:
    with pytest.raises(ConfigError, match="no subscription"):
        set_subscription_disabled(Config(), "missing", True)


# ---- validation -----------------------------------------------------------------------


def test_load_rejects_missing_required_field(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[[subscriptions]]\nname = "a"\nsource = "rss"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="missing required field 'url'"):
        load_config(target)


def test_load_rejects_empty_name(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[[subscriptions]]\nname = ""\nsource = "rss"\nurl = "y"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(target)


def test_load_rejects_duplicate_subscription_names(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "y"\n\n'
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "z"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(target)


def test_load_rejects_non_bool_disabled(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "y"\ndisabled = "yes"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="disabled must be a boolean"):
        load_config(target)


def test_load_rejects_non_table_options(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "y"\noptions = "x"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="options must be a table"):
        load_config(target)


def test_load_rejects_bad_option_value_type(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[[subscriptions]]\nname = "a"\nsource = "rss"\nurl = "y"\n'
        "[subscriptions.options]\nweight = 1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="string or int"):
        load_config(target)


def test_load_rejects_non_table_auth(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('auth = "nope"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="`auth` must be a table"):
        load_config(target)


def test_load_rejects_bad_auth_value_type(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[auth.readwise]\ntoken = 42\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="string or list of strings"):
        load_config(target)


def test_save_is_atomic_no_partial_file(tmp_path: Path) -> None:
    """A second save replaces atomically with no leftover .tmp files."""
    target = tmp_path / "config.toml"
    save_config(Config(), target)
    original = target.read_text()

    save_config(
        Config(subscriptions=(Subscription(name="a", source="rss", url="https://a"),)),
        target,
    )
    siblings_after = sorted(p.name for p in tmp_path.iterdir())
    assert "config.toml" in siblings_after
    assert all(not n.endswith(".tmp") for n in siblings_after)
    assert original != target.read_text()
