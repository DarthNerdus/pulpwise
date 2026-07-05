"""Cookie-based authentication helpers.

The cookies-file format mirrors what the substack-api package (MIT,
https://github.com/NHagar/substack_api) reads, which in turn matches the
output of common browser cookie-export extensions. Adopting that format
means users can use any of those extensions without hand-editing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pulpwise.models import PulpwiseError


class AuthError(PulpwiseError):
    """Raised when an auth file is missing, unreadable, or malformed."""


@dataclass(frozen=True, slots=True)
class CookieEntry:
    """A browser-exported cookie with its native domain."""

    name: str
    value: str
    domain: str  # `.substack.com`, `.astralcodexten.com`, etc.


def load_cookies(path: Path) -> dict[str, str]:
    """Read a browser-exported cookies JSON file and return a name→value mapping.

    Domain information is discarded - callers that need it (multi-domain
    Substack auth) should use `load_cookies_with_domain` instead.
    """
    return {c.name: c.value for c in load_cookies_with_domain(path)}


def load_cookies_with_domain(path: Path) -> list[CookieEntry]:
    """Read cookies preserving each entry's domain.

    Used when one logical session spans multiple domains (e.g. paid
    Substack publications on custom domains need their own cookies, not
    the substack.com ones).
    """
    expanded = path.expanduser()
    if not expanded.exists():
        raise AuthError(f"cookies file not found: {expanded}")

    try:
        raw = json.loads(expanded.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"could not parse cookies file {expanded}: {exc}") from exc

    if not isinstance(raw, list):
        raise AuthError(f"cookies file {expanded} must be a JSON array")

    out: list[CookieEntry] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise AuthError(f"cookies[{i}] in {expanded} must be an object")
        name = entry.get("name")
        value = entry.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise AuthError(f"cookies[{i}] in {expanded} missing string name/value")
        # Browser exports use the cookie's native domain, e.g.
        # `.substack.com` or `.astralcodexten.com`. Default to
        # `.substack.com` if absent so older single-domain exports still
        # work unchanged.
        domain_raw = entry.get("domain")
        domain = domain_raw if isinstance(domain_raw, str) and domain_raw else ".substack.com"
        out.append(CookieEntry(name=name, value=value, domain=domain))

    return out
