"""Shared httpx client used by every source plugin."""

from __future__ import annotations

import httpx

from pulpline import __version__

USER_AGENT = f"pulpline/{__version__}"
# Browser UA for hosts behind DDoS-Guard / Cloudflare anti-bot fingerprinting
# (Anna's Archive in particular). Pulpline's default UA is honest about being
# a bot, which is what we want everywhere except for these specific hosts.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0


def build_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """Construct a configured sync httpx client."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )


def build_browser_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """Like `build_client`, but advertises a browser User-Agent.

    Use this only for hosts that block on UA fingerprinting (Anna's Archive
    behind DDoS-Guard). Pulpline prefers an honest UA everywhere else.
    """
    return httpx.Client(
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )
