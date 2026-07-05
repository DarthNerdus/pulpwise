"""URL normalization + deterministic dedup keys.

Single source of truth for "are these two URLs the same article?" - shared by
URLSource, RSSSource, and any future source. If sources used independent
normalization rules, global dedup would silently miss cross-source duplicates.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAM = re.compile(r"^(utm_.*|fbclid|mc_.*|ref|gclid|igshid)$", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Return a canonical form suitable for hashing.

    Lowercases scheme + host, strips tracking query params, drops fragments,
    trims a trailing slash from the path. Other query params are preserved
    in their original order to keep the result deterministic across runs.
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM.match(k)
    ]
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))


def dedup_key(url: str) -> str:
    """Stable sha256 hex digest of the normalized URL."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()
