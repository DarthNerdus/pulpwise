"""TOML configuration: subscriptions list + paths.

TOML is the source of truth for *what to subscribe to*. Auto-created on first
load with a commented example so the user has something to copy. Saving uses
an atomic tmp-then-rename to avoid leaving a half-written config behind on
crash. SQLite (state.py) handles *what has happened*; do not mirror it here.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import tomli_w

DEFAULT_OUTPUT_DIR = "~/Sync/Pulpline"

_DEFAULT_CONFIG_TEMPLATE = """# pulpline config. Edit by hand, or via `pulp add` / `pulp remove`.

[paths]
output_dir = "~/Sync/Pulpline"

# Example subscription (uncomment and edit):
# [[subscriptions]]
# name = "stratechery"
# source = "rss"
# url = "https://stratechery.com/feed"
"""


class ConfigError(Exception):
    """Raised when the config file is malformed or violates an invariant."""


@dataclass(frozen=True, slots=True)
class Subscription:
    name: str
    source: str
    url: str
    output_dir: str | None = None  # None means inherit from paths.output_dir
    # Per-subscription source-specific options (e.g. MangaDex `language`,
    # `max_chapters`, `order`). The core never names individual keys -
    # source plugins read whatever shape they need from this dict.
    options: dict[str, str | int] = field(default_factory=dict)

    def option(self, key: str) -> str | int | None:
        """Read an option, returning None if unset. Convenience for source plugins."""
        return self.options.get(key)


@dataclass(frozen=True, slots=True)
class Paths:
    output_dir: str = DEFAULT_OUTPUT_DIR


@dataclass(frozen=True, slots=True)
class Config:
    """Loaded TOML config.

    `auth` is intentionally generic: a `{source_name: {key: value}}` mapping
    that source plugins read whatever shape they need from. The core never
    names a specific source - SubstackSource owns what `auth["substack"]`
    means; the next plugin will own its own subtable.
    """

    paths: Paths = field(default_factory=Paths)
    auth: dict[str, dict[str, str]] = field(default_factory=dict)
    subscriptions: tuple[Subscription, ...] = ()

    def auth_for(self, source: str) -> dict[str, str]:
        """Return the auth subtable for a source, empty if unset."""
        return self.auth.get(source, {})

    def find(self, name: str) -> Subscription | None:
        for sub in self.subscriptions:
            if sub.name == name:
                return sub
        return None

    def output_dir_for(self, sub: Subscription) -> Path:
        """Where this subscription's items should be written.

        If `sub.output_dir` is set explicitly, use it as-is (escape hatch).
        Otherwise, auto-organize: `<paths.output_dir>/<sub.name>/` so each
        subscription gets its own subfolder.
        """
        if sub.output_dir is not None:
            return Path(sub.output_dir).expanduser()
        return Path(self.paths.output_dir).expanduser() / sub.name


def default_config_path() -> Path:
    """Where pulpline reads/writes its TOML config.

    Honors `PULPLINE_CONFIG_PATH` for tests / CI. Otherwise: XDG default.
    """
    override = os.environ.get("PULPLINE_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "pulpline" / "config.toml"


def default_output_dir() -> Path:
    """Compatibility shim used by `add_once` when no config has been loaded."""
    override = os.environ.get("PULPLINE_OUTPUT_DIR")
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_OUTPUT_DIR).expanduser()


def load_config(path: Path | None = None) -> Config:
    """Load config from disk, creating it with defaults if absent."""
    target = path or default_config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        return Config()

    raw = tomllib.loads(target.read_text(encoding="utf-8"))
    return _from_raw(raw)


def save_config(config: Config, path: Path | None = None) -> None:
    """Atomically write `config` to TOML."""
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    raw: dict[str, object] = {
        "paths": {"output_dir": config.paths.output_dir},
    }
    if config.auth:
        raw["auth"] = {k: dict(v) for k, v in config.auth.items()}
    if config.subscriptions:
        raw["subscriptions"] = [_sub_to_dict(s) for s in config.subscriptions]

    fd, tmp_name = tempfile.mkstemp(prefix=".config.", suffix=".toml.tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(raw, fh)
        tmp_path.replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def add_subscription(config: Config, sub: Subscription) -> Config:
    """Return a new Config with `sub` appended. Raises if name collides."""
    if config.find(sub.name) is not None:
        raise ConfigError(f"subscription named {sub.name!r} already exists")
    return replace(config, subscriptions=(*config.subscriptions, sub))


def remove_subscription(config: Config, name: str) -> Config:
    """Return a new Config with the named subscription removed."""
    if config.find(name) is None:
        raise ConfigError(f"no subscription named {name!r}")
    remaining = tuple(s for s in config.subscriptions if s.name != name)
    return replace(config, subscriptions=remaining)


def _from_raw(raw: dict[str, object]) -> Config:
    paths_raw = raw.get("paths") or {}
    if not isinstance(paths_raw, dict):
        raise ConfigError("`paths` must be a table")
    output_dir = paths_raw.get("output_dir", DEFAULT_OUTPUT_DIR)
    if not isinstance(output_dir, str):
        raise ConfigError("`paths.output_dir` must be a string")

    auth = _auth_from_raw(raw.get("auth") or {})

    subs_raw = raw.get("subscriptions") or []
    if not isinstance(subs_raw, list):
        raise ConfigError("`subscriptions` must be an array of tables")

    if not isinstance(subs_raw, list):
        raise ConfigError("`subscriptions` must be an array of tables")

    subs: list[Subscription] = []
    seen_names: set[str] = set()
    for i, sub_raw in enumerate(subs_raw):
        if not isinstance(sub_raw, dict):
            raise ConfigError(f"subscriptions[{i}] must be a table")
        sub = _sub_from_dict(sub_raw, i)
        if sub.name in seen_names:
            raise ConfigError(f"duplicate subscription name {sub.name!r}")
        seen_names.add(sub.name)
        subs.append(sub)

    return Config(paths=Paths(output_dir=output_dir), auth=auth, subscriptions=tuple(subs))


def _auth_from_raw(raw: object) -> dict[str, dict[str, str]]:
    """Validate `[auth]` is `{source: {key: str}}`. Core code stays source-agnostic."""
    if not isinstance(raw, dict):
        raise ConfigError("`auth` must be a table")
    out: dict[str, dict[str, str]] = {}
    for source_name, sub in raw.items():
        if not isinstance(sub, dict):
            raise ConfigError(f"`auth.{source_name}` must be a table")
        validated: dict[str, str] = {}
        for k, v in sub.items():
            if not isinstance(v, str):
                raise ConfigError(f"`auth.{source_name}.{k}` must be a string")
            validated[k] = v
        out[source_name] = validated
    return out


def _sub_from_dict(raw: dict[str, object], index: int) -> Subscription:
    for required in ("name", "source", "url"):
        if required not in raw:
            raise ConfigError(f"subscriptions[{index}] missing required field {required!r}")
        if not isinstance(raw[required], str) or not raw[required]:
            raise ConfigError(f"subscriptions[{index}].{required} must be a non-empty string")

    output_dir = raw.get("output_dir")
    if output_dir is not None and not isinstance(output_dir, str):
        raise ConfigError(f"subscriptions[{index}].output_dir must be a string")

    options: dict[str, str | int] = {}

    # Legacy top-level fields from earlier versions of pulpline.
    # On next save we'll rewrite them under [subscriptions.options].
    for legacy_key in ("language", "max_chapters", "order"):
        if legacy_key in raw:
            value = raw[legacy_key]
            if not isinstance(value, (str, int)):
                raise ConfigError(f"subscriptions[{index}].{legacy_key} must be a string or int")
            options[legacy_key] = value

    nested = raw.get("options")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ConfigError(f"subscriptions[{index}].options must be a table")
        for k, v in nested.items():
            if not isinstance(v, (str, int)):
                raise ConfigError(f"subscriptions[{index}].options.{k} must be a string or int")
            options[k] = v

    return Subscription(
        name=raw["name"],  # type: ignore[arg-type]
        source=raw["source"],  # type: ignore[arg-type]
        url=raw["url"],  # type: ignore[arg-type]
        output_dir=output_dir,
        options=options,
    )


def _sub_to_dict(sub: Subscription) -> dict[str, object]:
    out: dict[str, object] = {"name": sub.name, "source": sub.source, "url": sub.url}
    if sub.output_dir is not None:
        out["output_dir"] = sub.output_dir
    if sub.options:
        out["options"] = dict(sub.options)
    return out
