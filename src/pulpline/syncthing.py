"""Best-effort syncthing CLI wrapper.

Pulpline's job ends when the EPUB lands in `~/Sync/Pulpline`. Most users pair
that with Syncthing to push to their reader; this module surfaces syncthing's
own state (daemon running, devices online, folders configured) so the TUI's
sync view can show the *full* delivery story rather than just pulpline's half.

Everything here is best-effort: syncthing not installed, daemon not running,
or a malformed CLI response - all return a `SyncthingStatus` with `installed`
or `running` flipped off. The view renders accordingly.

Implementation note: we shell out to `syncthing cli` rather than calling the
REST API directly, so the CLI's own auth handling (reads `~/Library/...
/config.xml` for the API key) does the work for us.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SyncthingDevice:
    device_id: str
    name: str
    is_self: bool
    online: bool
    address: str | None
    conn_type: str | None  # "tcp-lan", "quic-wan", etc.


@dataclass(frozen=True, slots=True)
class SyncthingFolder:
    id: str
    label: str
    path: str
    folder_type: str  # "sendonly", "receiveonly", "sendreceive"
    device_ids: tuple[str, ...]  # all devices this folder is shared with


@dataclass(frozen=True, slots=True)
class SyncthingStatus:
    installed: bool
    running: bool
    version: str | None = None
    my_id: str | None = None
    devices: tuple[SyncthingDevice, ...] = ()
    folders: tuple[SyncthingFolder, ...] = ()


def is_installed() -> bool:
    return shutil.which("syncthing") is not None


def get_status(timeout: float = 5.0) -> SyncthingStatus:
    """Probe syncthing via its CLI and return whatever state is reachable."""
    if not is_installed():
        return SyncthingStatus(installed=False, running=False)

    system = _run_cli(["show", "system"], timeout=timeout)
    if system is None or not isinstance(system, dict):
        # CLI exists but daemon is unreachable.
        return SyncthingStatus(installed=True, running=False)

    my_id = _str_or_none(system.get("myID"))

    version_data = _run_cli(["show", "version"], timeout=timeout)
    version = _str_or_none(version_data.get("version")) if isinstance(version_data, dict) else None

    config = _run_cli(["config", "dump-json"], timeout=timeout)
    connections_data = _run_cli(["show", "connections"], timeout=timeout) or {}
    connections = (
        connections_data.get("connections", {}) if isinstance(connections_data, dict) else {}
    )

    devices = _devices_from_config(config, connections, my_id)
    folders = _folders_from_config(config)

    return SyncthingStatus(
        installed=True,
        running=True,
        version=version,
        my_id=my_id,
        devices=tuple(devices),
        folders=tuple(folders),
    )


def _run_cli(args: list[str], timeout: float = 5.0) -> Any:
    """Run `syncthing cli <args>` and parse stdout as JSON. None on any failure."""
    try:
        result = subprocess.run(
            ["syncthing", "cli", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except FileNotFoundError, OSError, subprocess.SubprocessError:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _devices_from_config(
    config: object, connections: dict[str, Any], my_id: str | None
) -> list[SyncthingDevice]:
    if not isinstance(config, dict):
        return []
    raw_devices = config.get("devices") or []
    if not isinstance(raw_devices, list):
        return []
    out: list[SyncthingDevice] = []
    for d in raw_devices:
        if not isinstance(d, dict):
            continue
        did = _str_or_none(d.get("deviceID"))
        if not did:
            continue
        name = _str_or_none(d.get("name")) or ""
        conn = connections.get(did) if isinstance(connections, dict) else None
        connected = bool(conn.get("connected")) if isinstance(conn, dict) else False
        address = _str_or_none(conn.get("address")) if isinstance(conn, dict) else None
        conn_type = _str_or_none(conn.get("type")) if isinstance(conn, dict) else None
        out.append(
            SyncthingDevice(
                device_id=did,
                name=name,
                is_self=(did == my_id),
                online=connected,
                address=address,
                conn_type=conn_type,
            )
        )
    return out


def _folders_from_config(config: object) -> list[SyncthingFolder]:
    if not isinstance(config, dict):
        return []
    raw_folders = config.get("folders") or []
    if not isinstance(raw_folders, list):
        return []
    out: list[SyncthingFolder] = []
    for f in raw_folders:
        if not isinstance(f, dict):
            continue
        fid = _str_or_none(f.get("id")) or ""
        if not fid:
            continue
        device_ids: list[str] = []
        for dev in f.get("devices") or []:
            if isinstance(dev, dict):
                d_id = _str_or_none(dev.get("deviceID"))
                if d_id:
                    device_ids.append(d_id)
        out.append(
            SyncthingFolder(
                id=fid,
                label=_str_or_none(f.get("label")) or fid,
                path=_str_or_none(f.get("path")) or "",
                folder_type=_str_or_none(f.get("type")) or "",
                device_ids=tuple(device_ids),
            )
        )
    return out


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


# Re-export for compatibility / tests; field is unused at runtime.
_ = field
