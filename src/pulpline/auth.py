"""Cookie-based authentication helpers.

The cookies-file format mirrors what the substack-api package (MIT,
https://github.com/NHagar/substack_api) reads, which in turn matches the
output of common browser cookie-export extensions. Adopting that format
means users can use any of those extensions without hand-editing.
"""

from __future__ import annotations

import json
from pathlib import Path

from pulpline.models import PulplineError


class AuthError(PulplineError):
    """Raised when an auth file is missing, unreadable, or malformed."""


def load_cookies(path: Path) -> dict[str, str]:
    """Read a browser-exported cookies JSON file and return a name→value mapping.

    The file is expected to be a JSON array of objects, each with at least a
    `name` and `value` field (and optionally `domain`, `path`, `secure`).
    Only `name` and `value` are needed for httpx's `cookies=` parameter; the
    other fields are kept by browsers for completeness but are not required
    for outbound requests to a known host.
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

    cookies: dict[str, str] = {}
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise AuthError(f"cookies[{i}] in {expanded} must be an object")
        name = entry.get("name")
        value = entry.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise AuthError(f"cookies[{i}] in {expanded} missing string name/value")
        cookies[name] = value

    return cookies
