"""SLUM (Shadow Library Uptime Monitor) integration.

Live mirror discovery for Anna's Archive. SLUM tracks per-mirror health
on a public Uptime Kuma instance at https://open-slum.org/. We hit two
endpoints:

1. ``/api/status-page/slum`` - list of monitors with id + name. Anna's
   monitors are named like ``"Anna's Archive GL"``, ``"Anna's Archive VG"`` -
   the trailing token is the TLD.
2. ``/api/status-page/heartbeat/slum`` - latest heartbeat per monitor;
   ``status == 1`` means up, ``0`` means down (Uptime Kuma convention).

Result is cached to ``~/.cache/pulpline/slum.json`` for 24h so we don't
hammer SLUM on every ``pulp search anna``. Falls back to a hardcoded
list on any failure - SLUM going down should not break pulpline.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from pulpline.util.http import build_client
from pulpline.util.logging import get_logger

_log = get_logger("slum")

SLUM_BASE = "https://open-slum.org"
_STATUS_PAGE = "/api/status-page/slum"
_HEARTBEAT = "/api/status-page/heartbeat/slum"
_CACHE_TTL = 24 * 3600  # 24h
_CACHE_FILENAME = "slum.json"

HARDCODED_FALLBACK: tuple[str, ...] = ("gl", "pk", "gd")
"""Mirrors to use when SLUM is unreachable. Matches what the human user
explicitly verified worked in May 2026; intentionally drops `.li` (sold
into a parked-domain redirect) and `.vg` (down at time of last check)."""


def split_mirrors(raw: str) -> list[str]:
    """Parse a comma/space-separated mirror string into a list of TLDs.

    Used by config-driven mirror overrides like
    ``mirrors = "gl, pk, gd"`` or ``mirrors = "gl pk gd"``.
    """
    return [p.strip().lstrip(".") for p in raw.replace(",", " ").split() if p.strip()]


def default_cache_dir() -> Path:
    """XDG cache dir for pulpline. `PULPLINE_CACHE_DIR` env var overrides."""
    override = os.environ.get("PULPLINE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "pulpline"


def discover_anna_mirrors(
    *,
    fallback: tuple[str, ...] = HARDCODED_FALLBACK,
    client: httpx.Client | None = None,
    cache_path: Path | None = None,
    ttl: int = _CACHE_TTL,
) -> tuple[str, ...]:
    """Return TLDs of currently-up Anna's Archive mirrors.

    Reads from local cache when fresh; otherwise refreshes from SLUM.
    Returns ``fallback`` on any failure (network error, JSON parse error,
    SLUM redesign that breaks our shape assumptions, zero up mirrors).
    """
    path = cache_path or (default_cache_dir() / _CACHE_FILENAME)
    cached = _read_cache(path, ttl)
    if cached is not None:
        _log.debug("SLUM cache hit: %s", cached)
        return cached

    try:
        mirrors = _fetch_from_slum(client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        _log.warning("SLUM fetch failed: %s; using fallback %s", exc, fallback)
        return fallback

    if not mirrors:
        _log.warning("SLUM reported zero up Anna mirrors; using fallback %s", fallback)
        return fallback

    _log.info("SLUM discovered up mirrors: %s", mirrors)
    _write_cache(path, mirrors)
    return mirrors


def _fetch_from_slum(client: httpx.Client | None = None) -> tuple[str, ...]:
    """Hit SLUM's two endpoints, return TLDs of `status==1` Anna monitors."""
    own_client = client is None
    c = client or build_client()
    try:
        status_resp = c.get(SLUM_BASE + _STATUS_PAGE)
        status_resp.raise_for_status()
        status = status_resp.json()
        hb_resp = c.get(SLUM_BASE + _HEARTBEAT)
        hb_resp.raise_for_status()
        heartbeats = hb_resp.json()
    finally:
        if own_client:
            c.close()

    monitors: list[dict[str, object]] = []
    for group in status.get("publicGroupList", []):
        if isinstance(group, dict) and "anna" in (group.get("name") or "").lower():
            ml = group.get("monitorList") or []
            if isinstance(ml, list):
                monitors.extend(m for m in ml if isinstance(m, dict))

    hb_list = heartbeats.get("heartbeatList", {})
    if not isinstance(hb_list, dict):
        return ()

    up_tlds: list[str] = []
    for m in monitors:
        name = m.get("name") or ""
        if not isinstance(name, str):
            continue
        # "Anna's Archive GL" -> "gl"
        tokens = name.split()
        if not tokens:
            continue
        tld = tokens[-1].lower().lstrip(".")
        if not tld.isalpha() or len(tld) > 4:
            continue

        beats = hb_list.get(str(m.get("id", "")))
        if not isinstance(beats, list) or not beats:
            continue
        latest = beats[-1]
        if isinstance(latest, dict) and latest.get("status") == 1:
            up_tlds.append(tld)

    return tuple(up_tlds)


def _read_cache(path: Path, ttl: int) -> tuple[str, ...] | None:
    """Return cached mirror list if cache file exists and is younger than `ttl`."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > ttl:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    mirrors = data.get("mirrors") if isinstance(data, dict) else None
    if isinstance(mirrors, list) and all(isinstance(m, str) for m in mirrors):
        return tuple(mirrors)
    return None


def _write_cache(path: Path, mirrors: tuple[str, ...]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mirrors": list(mirrors), "fetched_at": time.time()}),
            encoding="utf-8",
        )
    except OSError as exc:
        _log.warning("could not write SLUM cache to %s: %s", path, exc)
