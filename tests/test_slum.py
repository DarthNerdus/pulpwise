"""Tests for `searchers.slum.discover_anna_mirrors` and helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from pulpline.searchers.slum import (
    HARDCODED_FALLBACK,
    default_cache_dir,
    discover_anna_mirrors,
    split_mirrors,
)


def _slum_responses(*, statuses: dict[int, int]) -> dict[str, Any]:
    """Build a (status_page, heartbeat) JSON pair for given monitor statuses.

    `statuses` maps monitor_id -> 1 (up) or 0 (down). Each id gets named
    'Anna's Archive XX' where XX is derived from the id (52->GL, 53->VG,
    54->PK, 55->GD; matches real SLUM ids).
    """
    id_to_tld = {52: "GL", 53: "VG", 54: "PK", 55: "GD"}
    monitors = [
        {"id": mid, "name": f"Anna's Archive {id_to_tld[mid]}", "type": "keyword"}
        for mid in statuses
    ]
    status_page = {
        "publicGroupList": [
            {
                "name": "Anna's Archive",
                "monitorList": monitors,
            }
        ]
    }
    heartbeat = {
        "heartbeatList": {
            str(mid): [{"status": s, "ping": 100, "msg": ""}] for mid, s in statuses.items()
        }
    }
    return {"status_page": status_page, "heartbeat": heartbeat}


def _slum_client(payloads: dict[str, Any]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/status-page/slum":
            return httpx.Response(200, json=payloads["status_page"])
        if path == "/api/status-page/heartbeat/slum":
            return httpx.Response(200, json=payloads["heartbeat"])
        return httpx.Response(404, text=f"unmocked: {path}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_split_mirrors_handles_commas_spaces_and_dots() -> None:
    assert split_mirrors("gl, pk, gd") == ["gl", "pk", "gd"]
    assert split_mirrors("gl pk gd") == ["gl", "pk", "gd"]
    assert split_mirrors(".gl, .pk") == ["gl", "pk"]
    assert split_mirrors("") == []


def test_default_cache_dir_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULPLINE_CACHE_DIR", "/tmp/pulpline-tests-cache")
    assert default_cache_dir() == Path("/tmp/pulpline-tests-cache")


def test_discover_returns_only_up_mirrors(tmp_path: Path) -> None:
    # GL/PK/GD up, VG down (matches the live state we observed).
    payloads = _slum_responses(statuses={52: 1, 53: 0, 54: 1, 55: 1})
    client = _slum_client(payloads)
    cache = tmp_path / "slum.json"

    result = discover_anna_mirrors(client=client, cache_path=cache)
    assert result == ("gl", "pk", "gd")


def test_discover_returns_empty_response_falls_back(tmp_path: Path) -> None:
    """If SLUM reports zero up monitors, return the hardcoded fallback."""
    payloads = _slum_responses(statuses={52: 0, 53: 0, 54: 0, 55: 0})
    client = _slum_client(payloads)
    result = discover_anna_mirrors(client=client, cache_path=tmp_path / "slum.json")
    assert result == HARDCODED_FALLBACK


def test_discover_caches_result_and_re_reads_when_fresh(tmp_path: Path) -> None:
    payloads = _slum_responses(statuses={52: 1, 53: 0, 54: 1, 55: 1})
    cache = tmp_path / "slum.json"

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if request.url.path == "/api/status-page/slum":
            return httpx.Response(200, json=payloads["status_page"])
        return httpx.Response(200, json=payloads["heartbeat"])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = discover_anna_mirrors(client=client, cache_path=cache)
    second = discover_anna_mirrors(client=client, cache_path=cache)

    assert first == second == ("gl", "pk", "gd")
    # First call hits both endpoints (count=2); second should be cache-only (still 2).
    assert call_count == 2


def test_discover_refreshes_when_cache_stale(tmp_path: Path) -> None:
    """A cache file older than `ttl` should be ignored."""
    cache = tmp_path / "slum.json"
    cache.write_text(json.dumps({"mirrors": ["stale"], "fetched_at": 0}), encoding="utf-8")
    # Backdate so it's stale relative to ttl=1.
    old = time.time() - 3600
    import os

    os.utime(cache, (old, old))

    payloads = _slum_responses(statuses={52: 1, 54: 1})
    client = _slum_client(payloads)
    result = discover_anna_mirrors(client=client, cache_path=cache, ttl=1)
    assert result == ("gl", "pk")


def test_discover_falls_back_on_http_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = discover_anna_mirrors(client=client, cache_path=tmp_path / "slum.json")
    assert result == HARDCODED_FALLBACK


def test_discover_falls_back_on_malformed_json(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json{{{", headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = discover_anna_mirrors(client=client, cache_path=tmp_path / "slum.json")
    assert result == HARDCODED_FALLBACK
