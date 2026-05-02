"""Tests for the syncthing CLI wrapper.

Most behavior is "do the right thing when subprocess fails," which we test
by monkeypatching `subprocess.run`. The success path uses fixture JSON that
matches what `syncthing cli` actually emits.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from pulpline import syncthing


def _make_run(returns: dict[tuple[str, ...], object]) -> Any:
    """Build a fake subprocess.run that returns canned JSON per arg-tuple key.

    `returns` maps args (without the leading 'syncthing cli') to either:
      - a dict/list (gets JSON-dumped to stdout, returncode 0)
      - the string 'fail' (raises CalledProcessError)
      - the string 'missing' (raises FileNotFoundError)
    """

    def _fake(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        key = tuple(cmd[2:])  # strip 'syncthing cli'
        value = returns.get(key)
        if value == "fail":
            raise subprocess.CalledProcessError(1, cmd)
        if value == "missing":
            raise FileNotFoundError("syncthing not found")
        if value is None:
            raise subprocess.CalledProcessError(2, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(value), stderr="")

    return _fake


def test_get_status_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pulpline.syncthing.shutil.which", lambda _name: None)
    status = syncthing.get_status()
    assert status.installed is False
    assert status.running is False
    assert status.devices == ()
    assert status.folders == ()


def test_get_status_when_installed_but_daemon_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pulpline.syncthing.shutil.which", lambda _name: "/usr/local/bin/syncthing")
    monkeypatch.setattr(
        "pulpline.syncthing.subprocess.run",
        _make_run({("show", "system"): "fail"}),
    )
    status = syncthing.get_status()
    assert status.installed is True
    assert status.running is False


def test_get_status_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pulpline.syncthing.shutil.which", lambda _name: "/usr/local/bin/syncthing")
    my_id = "OX722XA-FOOBAR"
    palma_id = "SW3PP32-PALMA"
    monkeypatch.setattr(
        "pulpline.syncthing.subprocess.run",
        _make_run(
            {
                ("show", "system"): {"myID": my_id, "uptime": 100},
                ("show", "version"): {"version": "v2.0.14"},
                ("show", "connections"): {
                    "connections": {
                        palma_id: {
                            "connected": True,
                            "address": "192.168.0.116:22000",
                            "type": "tcp-lan",
                        },
                    }
                },
                ("config", "dump-json"): {
                    "devices": [
                        {"deviceID": my_id, "name": "MyMac"},
                        {"deviceID": palma_id, "name": "Palma2"},
                    ],
                    "folders": [
                        {
                            "id": "abc",
                            "label": "Pulpline",
                            "path": "/home/x/Sync/Pulpline",
                            "type": "sendonly",
                            "devices": [
                                {"deviceID": my_id},
                                {"deviceID": palma_id},
                            ],
                        }
                    ],
                },
            }
        ),
    )

    status = syncthing.get_status()
    assert status.installed is True
    assert status.running is True
    assert status.version == "v2.0.14"
    assert status.my_id == my_id

    by_name = {d.name: d for d in status.devices}
    assert by_name["MyMac"].is_self is True
    assert by_name["MyMac"].online is False  # self never lists in connections
    assert by_name["Palma2"].is_self is False
    assert by_name["Palma2"].online is True
    assert by_name["Palma2"].address == "192.168.0.116:22000"

    assert len(status.folders) == 1
    folder = status.folders[0]
    assert folder.label == "Pulpline"
    assert folder.folder_type == "sendonly"
    assert palma_id in folder.device_ids


def test_get_status_handles_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pulpline.syncthing.shutil.which", lambda _name: "/usr/local/bin/syncthing")

    def _fake(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="not json at all", stderr="")

    monkeypatch.setattr("pulpline.syncthing.subprocess.run", _fake)
    status = syncthing.get_status()
    # show system returned non-JSON → wrapper treats as unreachable
    assert status.running is False
