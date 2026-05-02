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


def parse_selection(text: str, total: int) -> set[int]:
    """Parse user range input like '1,3,5-7' or 'all' or 'none' into 1-based indices."""
    s = text.strip().lower()
    if not s or s == "none":
        return set()
    if s == "all":
        return set(range(1, total + 1))

    chosen: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ValueError(f"bad range fragment: {part!r}") from exc
            if lo > hi:
                lo, hi = hi, lo
            chosen.update(i for i in range(lo, hi + 1) if 1 <= i <= total)
        else:
            try:
                i = int(part)
            except ValueError as exc:
                raise ValueError(f"bad index: {part!r}") from exc
            if 1 <= i <= total:
                chosen.add(i)
    return chosen
