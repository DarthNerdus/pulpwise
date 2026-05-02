"""Tests for the cookie loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulpline.auth import AuthError, load_cookies


def _write(path: Path, content: object) -> Path:
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_load_cookies_returns_name_value_dict(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "c.json",
        [
            {"name": "substack.sid", "value": "abc", "domain": ".substack.com"},
            {"name": "substack.lli", "value": "xyz"},
        ],
    )
    assert load_cookies(f) == {"substack.sid": "abc", "substack.lli": "xyz"}


def test_load_cookies_missing_file(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="not found"):
        load_cookies(tmp_path / "nope.json")


def test_load_cookies_not_json(tmp_path: Path) -> None:
    f = tmp_path / "c.json"
    f.write_text("not json", encoding="utf-8")
    with pytest.raises(AuthError, match="parse"):
        load_cookies(f)


def test_load_cookies_not_a_list(tmp_path: Path) -> None:
    f = _write(tmp_path / "c.json", {"name": "x", "value": "y"})
    with pytest.raises(AuthError, match="JSON array"):
        load_cookies(f)


def test_load_cookies_missing_value(tmp_path: Path) -> None:
    f = _write(tmp_path / "c.json", [{"name": "x"}])
    with pytest.raises(AuthError, match="name/value"):
        load_cookies(f)


def test_load_cookies_expanduser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "cookies.json"
    _write(target, [{"name": "k", "value": "v"}])
    assert load_cookies(Path("~/cookies.json")) == {"k": "v"}
