"""Shared httpx client used by every source plugin."""

from __future__ import annotations

import httpx

from pulpline import __version__

USER_AGENT = f"pulpline/{__version__}"
DEFAULT_TIMEOUT = 30.0


def build_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """Construct a configured sync httpx client."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )
