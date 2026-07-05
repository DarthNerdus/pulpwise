"""Bulk-import a user's Substack subscriptions into pulpwise's TOML config.

Calls Substack's `/api/v1/user/<username>/public_profile` endpoint, which
returns the user's full subscriptions list when the request carries that
user's session cookies. We translate each subscription into a
`[[subscriptions]]` entry with `source = "substack"` so future sync uses the
auth-aware SubstackSource.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pulpwise.auth import AuthError, load_cookies
from pulpwise.config import Config, ConfigError, Subscription, add_subscription, save_config
from pulpwise.importers import parse_selection
from pulpwise.models import RateLimited
from pulpwise.util.http import build_client

__all__ = [
    "SubstackAutoOutcome",
    "SubstackPublication",
    "auto_reconcile",
    "auto_reconcile_enabled",
    "list_user_subscriptions",
    "parse_selection",
]

# Config values (of `[auth.substack].auto_reconcile`) read as "on". TOML
# booleans are normalized to "true"/"false" strings by the config loader.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class SubstackPublication:
    name: str
    url: str
    paid: bool


@dataclass(frozen=True, slots=True)
class SubstackAutoOutcome:
    """Result of a non-interactive auto-reconcile pass.

    `skipped` means `[auth.substack]` isn't configured — distinct from an
    error because there's nothing to retry; the caller should just proceed
    with whatever it was doing. `error` is a human-readable string suitable
    for a CLI line or a TUI notification.
    """

    added: tuple[str, ...] = ()
    already_present: int = 0
    name_collisions: int = 0
    error: str | None = None
    skipped: bool = False


def list_user_subscriptions(
    username: str,
    cookies: dict[str, str],
    client: httpx.Client,
) -> list[SubstackPublication]:
    """Return the publications `username` is subscribed to.

    The response shape mirrors Substack's public-profile endpoint. The
    `subscriptions` array is only populated when the request is authenticated
    as that user (i.e. cookies belong to them). Cookies are attached to the
    client jar with `.substack.com` domain scope - httpx 0.28 deprecated
    per-request `cookies=` so the supported pattern is jar-with-scope.
    """
    for name, value in cookies.items():
        client.cookies.set(name, value, domain=".substack.com")
    endpoint = f"https://substack.com/api/v1/user/{username}/public_profile"
    response = client.get(endpoint)
    response.raise_for_status()
    data = response.json()

    subs: list[SubstackPublication] = []
    for entry in data.get("subscriptions", []) or []:
        if not isinstance(entry, dict):
            continue
        pub = entry.get("publication") or {}
        if not isinstance(pub, dict):
            continue
        pub_name_raw = pub.get("name")
        if not isinstance(pub_name_raw, str) or not pub_name_raw.strip():
            continue
        pub_name = pub_name_raw.strip()
        custom = pub.get("custom_domain")
        subdomain = pub.get("subdomain")
        if isinstance(custom, str) and custom.strip():
            domain = custom.strip()
        elif isinstance(subdomain, str) and subdomain.strip():
            domain = f"{subdomain.strip()}.substack.com"
        else:
            continue
        url = domain if "://" in domain else f"https://{domain}"
        membership = entry.get("membership_state", "")
        # Substack's exact set of paid-vs-free flags isn't documented;
        # `free_subscribed` is the unpaid baseline.
        paid = isinstance(membership, str) and membership not in {"free_subscribed", ""}
        subs.append(SubstackPublication(name=pub_name, url=url, paid=paid))
    return subs


_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug_from_title(text: str) -> str:
    """Lowercased alphanumeric+hyphen slug suitable as a subscription name."""
    s = _SLUG_NON_ALNUM.sub("-", text.lower()).strip("-")
    return s or "feed"


def auto_reconcile_enabled(config: Config) -> bool:
    """True when the user opted into background follow-list reconciliation.

    Controlled by `[auth.substack].auto_reconcile` (`true`, or the strings
    "1"/"yes"/"on"). Default OFF: silently adding every followed publication
    as a subscription is a mass side effect nobody should get from just
    pressing sync - with a fresh config it re-adds the user's *entire*
    follow list. The explicit paths (`pulpwise import substack`, and
    `--auto` for cron) work regardless of this setting; it gates only the
    TUI's convenience pass before sync.
    """
    value = config.auth_for("substack").get("auto_reconcile")
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def auto_reconcile(
    config: Config,
    *,
    client: httpx.Client | None = None,
) -> tuple[Config, SubstackAutoOutcome]:
    """Pick up newly-followed Substack publications without a prompt.

    Reads `[auth.substack].username` + `cookies_path` from config, fetches
    the user's current follow list, and adds any publications that aren't
    already a subscription (matched by hostname). Persists the updated
    config and returns it, so callers can use the new config for a
    subsequent sync without reloading.

    Returns `(config, SubstackAutoOutcome)`. Failures bubble out as
    `outcome.error` rather than raising — this is called from the TUI's
    sync trigger right before `pipeline.sync`, and a Substack hiccup
    should not block syncing the rest of your subscriptions.

    If `[auth.substack]` isn't configured, returns immediately with
    `outcome.skipped=True`. Idempotent: a follow already present as a
    subscription with the same hostname is counted as `already_present`.
    """
    auth = config.auth_for("substack")
    cfg_username = auth.get("username")
    cfg_cookies_path = auth.get("cookies_path")
    if not isinstance(cfg_username, str) or not isinstance(cfg_cookies_path, str):
        return config, SubstackAutoOutcome(skipped=True)

    cookies_path = Path(cfg_cookies_path).expanduser()
    try:
        cookies = load_cookies(cookies_path)
    except AuthError as exc:
        return config, SubstackAutoOutcome(error=f"substack auth: {exc}")

    owns_client = client is None
    if client is None:
        client = build_client(scope="substack")
    try:
        try:
            pubs = list_user_subscriptions(cfg_username, cookies, client)
        except (httpx.HTTPError, RateLimited) as exc:
            # RateLimited isn't an httpx error and would otherwise fly out of
            # here and abort the TUI's whole sync run - but this function's
            # contract is that a Substack hiccup must NOT block syncing.
            return config, SubstackAutoOutcome(error=f"substack follows fetch failed: {exc}")
    finally:
        if owns_client:
            client.close()

    if not pubs:
        # Substack returns 200-with-empty rather than 401 when cookies have
        # silently expired; the only signal is the empty array. Surface
        # this as a warning so the user can re-export rather than wonder
        # why nothing's appearing.
        return config, SubstackAutoOutcome(
            error="substack returned no follows (cookies may be expired)"
        )

    existing_hosts = {urlsplit(s.url).hostname for s in config.subscriptions}
    new_pubs = [p for p in pubs if urlsplit(p.url).hostname not in existing_hosts]
    already = len(pubs) - len(new_pubs)

    if not new_pubs:
        return config, SubstackAutoOutcome(already_present=already)

    added: list[str] = []
    collisions = 0
    new_config = config
    for pub in new_pubs:
        sub_name = _slug_from_title(pub.name)
        sub = Subscription(name=sub_name, source="substack", url=pub.url)
        try:
            new_config = add_subscription(new_config, sub)
            added.append(sub_name)
        except ConfigError:
            collisions += 1

    if added:
        save_config(new_config)

    return new_config, SubstackAutoOutcome(
        added=tuple(added),
        already_present=already,
        name_collisions=collisions,
    )
