"""TOML configuration: subscriptions list + auth.

TOML is the source of truth for *what to subscribe to*. Auto-created on first
load with a commented example so the user has something to copy. Saving uses
an atomic tmp-then-rename to avoid leaving a half-written config behind on
crash. SQLite (state.py) handles *what has happened*; do not mirror it here.

Legacy pulpline keys (`[paths]`, per-subscription `output_dir`) are accepted
and ignored on load so a copied-over config doesn't crash - Pulp Wise has no
filesystem output. They are dropped on the next save.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import tomli_w

_DEFAULT_CONFIG_TEMPLATE = """# pulpwise config. Edit by hand, or via `pulpwise add` / `remove`.

# Readwise access token - get one at https://readwise.io/access_token.
# Either inline:
# [auth.readwise]
# token = "XXX"
# ...or a path to a file holding just the token (chmod 600 it):
# [auth.readwise]
# token_path = "~/.config/pulpwise/readwise_token"

# Example subscription (uncomment and edit):
# [[subscriptions]]
# name = "stratechery"
# source = "rss"
# url = "https://stratechery.com/feed"
#
# [subscriptions.options]
# location = "feed"           # where saves land in Reader: new (alias: inbox) | later | archive | feed
# tags = "tech, essays"       # comma-separated Reader tags for this subscription
"""


class ConfigError(Exception):
    """Raised when the config file is malformed or violates an invariant."""


@dataclass(frozen=True, slots=True)
class Subscription:
    name: str
    source: str
    url: str
    # Per-subscription options. Source plugins read source-specific keys
    # (email `since_days`, RSS `categories`); the pipeline reads the
    # Readwise routing keys (`location`, `tags`). The core schema never
    # names source-specific keys - plugins own their shapes.
    options: dict[str, str | int] = field(default_factory=dict)

    def option(self, key: str) -> str | int | None:
        """Read an option, returning None if unset. Convenience for source plugins."""
        return self.options.get(key)


@dataclass(frozen=True, slots=True)
class Config:
    """Loaded TOML config.

    `auth` is intentionally generic: a `{source_name: {key: value}}` mapping
    that source plugins (and the Readwise sink) read whatever shape they
    need from. The core never names a specific source - SubstackSource owns
    what `auth["substack"]` means, the Readwise sink owns `auth["readwise"]`.
    """

    auth: dict[str, dict[str, str | list[str]]] = field(default_factory=dict)
    subscriptions: tuple[Subscription, ...] = ()

    def auth_for(self, source: str) -> dict[str, str | list[str]]:
        """Return the auth subtable for a source, empty if unset.

        Values can be either strings (`token`, `cookies_path`, ...) or
        lists of strings (`extra_cookies_paths`); each consumer is
        responsible for type-checking what it reads.
        """
        return self.auth.get(source, {})

    def find(self, name: str) -> Subscription | None:
        for sub in self.subscriptions:
            if sub.name == name:
                return sub
        return None


def default_config_path() -> Path:
    """Where pulpwise reads/writes its TOML config.

    Honors `PULPWISE_CONFIG_PATH` for tests / CI. Otherwise: XDG default.
    """
    override = os.environ.get("PULPWISE_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "pulpwise" / "config.toml"


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

    raw: dict[str, object] = {}
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
    # `paths` is a legacy pulpline table (output_dir for the file sink).
    # Tolerated so copied-over configs load; its contents are ignored.
    paths_raw = raw.get("paths")
    if paths_raw is not None and not isinstance(paths_raw, dict):
        raise ConfigError("`paths` must be a table")

    auth = _auth_from_raw(raw.get("auth") or {})

    subs_raw = raw.get("subscriptions") or []
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

    return Config(auth=auth, subscriptions=tuple(subs))


def _auth_from_raw(raw: object) -> dict[str, dict[str, str | list[str]]]:
    """Validate `[auth]` is `{source: {key: str | list[str]}}`.

    Strings cover the common cases (`token`, `cookies_path`); lists of
    strings cover the rare multi-value case (`extra_cookies_paths` for
    Substack publications on multiple custom domains). TOML booleans are
    accepted and normalized to "true"/"false" strings, so flag-shaped
    settings (`auto_reconcile = true`) can be written the natural way
    without widening the value type consumers see. Core code stays
    source-agnostic.
    """
    if not isinstance(raw, dict):
        raise ConfigError("`auth` must be a table")
    out: dict[str, dict[str, str | list[str]]] = {}
    for source_name, sub in raw.items():
        if not isinstance(sub, dict):
            raise ConfigError(f"`auth.{source_name}` must be a table")
        validated: dict[str, str | list[str]] = {}
        for k, v in sub.items():
            # bool before str: not a str subclass, but check explicitly for
            # clarity (bool IS an int subclass, and ints are rejected here).
            if isinstance(v, bool):
                validated[k] = "true" if v else "false"
            elif isinstance(v, str):
                validated[k] = v
            elif isinstance(v, list) and all(isinstance(item, str) for item in v):
                validated[k] = list(v)
            else:
                raise ConfigError(f"`auth.{source_name}.{k}` must be a string or list of strings")
        out[source_name] = validated
    return out


def _sub_from_dict(raw: dict[str, object], index: int) -> Subscription:
    for required in ("name", "source", "url"):
        if required not in raw:
            raise ConfigError(f"subscriptions[{index}] missing required field {required!r}")
        if not isinstance(raw[required], str) or not raw[required]:
            raise ConfigError(f"subscriptions[{index}].{required} must be a non-empty string")

    # Legacy pulpline per-subscription key; ignored (no filesystem output).
    output_dir = raw.get("output_dir")
    if output_dir is not None and not isinstance(output_dir, str):
        raise ConfigError(f"subscriptions[{index}].output_dir must be a string")

    options: dict[str, str | int] = {}

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
        options=options,
    )


def _sub_to_dict(sub: Subscription) -> dict[str, object]:
    out: dict[str, object] = {"name": sub.name, "source": sub.source, "url": sub.url}
    if sub.options:
        out["options"] = dict(sub.options)
    return out
