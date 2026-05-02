"""Bulk-import a user's Substack subscriptions into pulpline's TOML config.

Calls Substack's `/api/v1/user/<username>/public_profile` endpoint, which
returns the user's full subscriptions list when the request carries that
user's session cookies. We translate each subscription into a
`[[subscriptions]]` entry with `source = "substack"` so future sync uses the
auth-aware SubstackSource.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from pulpline.importers import parse_selection

__all__ = ["SubstackPublication", "list_user_subscriptions", "parse_selection"]


@dataclass(frozen=True, slots=True)
class SubstackPublication:
    name: str
    url: str
    paid: bool


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
