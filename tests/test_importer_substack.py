"""Tests for the Substack bulk importer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pulpline.config import Config, Subscription, load_config
from pulpline.importers.substack import (
    SubstackPublication,
    auto_reconcile,
    list_user_subscriptions,
    parse_selection,
)


def _profile_payload() -> dict[str, Any]:
    return {
        "subscriptions": [
            {
                "publication": {
                    "name": "Numb at the Lodge",
                    "subdomain": "samkriss",
                    "custom_domain": None,
                },
                "membership_state": "subscribed",
            },
            {
                "publication": {
                    "name": "Stratechery",
                    "subdomain": "stratechery",
                    "custom_domain": "stratechery.com",
                },
                "membership_state": "subscribed",
            },
            {
                "publication": {
                    "name": "Astral Codex Ten",
                    "subdomain": "astralcodexten",
                },
                "membership_state": "free_subscribed",
            },
        ]
    }


def _client_with_profile() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if "public_profile" in str(request.url):
            return httpx.Response(200, json=_profile_payload())
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_list_user_subscriptions_extracts_name_and_url() -> None:
    pubs = list_user_subscriptions(
        username="me",
        cookies={"substack.sid": "x"},
        client=_client_with_profile(),
    )

    by_name = {p.name: p for p in pubs}
    assert by_name["Numb at the Lodge"].url == "https://samkriss.substack.com"
    assert by_name["Stratechery"].url == "https://stratechery.com"
    # Free sub: paid=False
    assert by_name["Astral Codex Ten"].paid is False
    # Paid subs (subscribed state): paid=True
    assert by_name["Numb at the Lodge"].paid is True


def test_list_user_subscriptions_raises_on_http_failure() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="auth")))
    with pytest.raises(httpx.HTTPStatusError):
        list_user_subscriptions("me", {}, client)


@pytest.mark.parametrize(
    ("text", "total", "expected"),
    [
        ("all", 5, {1, 2, 3, 4, 5}),
        ("none", 5, set()),
        ("", 5, set()),
        ("1", 5, {1}),
        ("1,3", 5, {1, 3}),
        ("1-3", 5, {1, 2, 3}),
        ("1,3,5-7", 7, {1, 3, 5, 6, 7}),
        ("3-1", 5, {1, 2, 3}),  # reversed range
        ("99", 5, set()),  # out of range silently dropped
        ("ALL", 3, {1, 2, 3}),
    ],
)
def test_parse_selection(text: str, total: int, expected: set[int]) -> None:
    assert parse_selection(text, total) == expected


def test_parse_selection_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_selection("hello", 5)


def test_substack_publication_dataclass() -> None:
    p = SubstackPublication(name="x", url="https://x.com", paid=False)
    assert p.name == "x"


# ----------------------------- auto_reconcile -----------------------------


def _write_cookies(tmp_path: Path, *, name: str = "substack.sid") -> Path:
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps([{"name": name, "value": "x"}]), encoding="utf-8")
    return p


def _auth(tmp_path: Path) -> dict[str, dict[str, str | list[str]]]:
    return {
        "substack": {
            "username": "me",
            "cookies_path": str(_write_cookies(tmp_path)),
        }
    }


def test_auto_reconcile_skips_when_auth_not_configured() -> None:
    cfg = Config()
    new_cfg, outcome = auto_reconcile(cfg, client=_client_with_profile())
    assert outcome.skipped is True
    assert outcome.added == ()
    assert new_cfg is cfg  # no mutation


def test_auto_reconcile_returns_error_on_missing_cookies_file(tmp_path: Path) -> None:
    cfg = Config(
        auth={
            "substack": {
                "username": "me",
                "cookies_path": str(tmp_path / "does-not-exist.json"),
            }
        }
    )
    new_cfg, outcome = auto_reconcile(cfg, client=_client_with_profile())
    assert outcome.error is not None
    assert "substack auth" in outcome.error
    assert new_cfg is cfg


def test_auto_reconcile_returns_error_on_http_failure(tmp_path: Path) -> None:
    cfg = Config(auth=_auth(tmp_path))
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    new_cfg, outcome = auto_reconcile(cfg, client=client)
    assert outcome.error is not None
    assert "follows fetch failed" in outcome.error
    assert new_cfg is cfg


def test_auto_reconcile_flags_empty_response_as_expired_cookies(tmp_path: Path) -> None:
    """Substack returns 200 + empty array when cookies have silently expired."""
    cfg = Config(auth=_auth(tmp_path))
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"subscriptions": []}))
    )
    new_cfg, outcome = auto_reconcile(cfg, client=client)
    assert outcome.error is not None
    assert "expired" in outcome.error
    assert new_cfg is cfg


def test_auto_reconcile_adds_new_publications_and_persists(tmp_path: Path) -> None:
    cfg = Config(auth=_auth(tmp_path))
    new_cfg, outcome = auto_reconcile(cfg, client=_client_with_profile())

    # Three pubs in the profile fixture; all three are new.
    assert outcome.error is None
    assert outcome.skipped is False
    assert len(outcome.added) == 3
    assert outcome.already_present == 0

    # Returned config has the new subscriptions inline.
    sub_names = {s.name for s in new_cfg.subscriptions}
    assert {"numb-at-the-lodge", "stratechery", "astral-codex-ten"} <= sub_names

    # And the change was persisted (conftest points PULPLINE_CONFIG_PATH at tmp).
    persisted = load_config()
    assert {s.name for s in persisted.subscriptions} >= {"numb-at-the-lodge"}


def test_auto_reconcile_skips_already_present_by_hostname(tmp_path: Path) -> None:
    """Existing sub at the same hostname makes the pub count toward already_present."""
    cfg = Config(
        auth=_auth(tmp_path),
        subscriptions=(
            Subscription(
                name="some-other-name",
                source="substack",
                url="https://samkriss.substack.com",
            ),
        ),
    )
    _, outcome = auto_reconcile(cfg, client=_client_with_profile())
    # Of the 3 pubs: samkriss is already present; the other 2 are new.
    assert outcome.already_present == 1
    assert len(outcome.added) == 2


def test_auto_reconcile_handles_subscription_name_collision(tmp_path: Path) -> None:
    """When the slug of a new pub collides with an existing sub name (different host),
    it counts as a collision rather than added."""
    cfg = Config(
        auth=_auth(tmp_path),
        subscriptions=(
            # Same name pulpline would assign to the Stratechery pub but a different URL.
            Subscription(
                name="stratechery",
                source="rss",
                url="https://example.com/some-other-feed",
            ),
        ),
    )
    _, outcome = auto_reconcile(cfg, client=_client_with_profile())
    # The other 2 pubs add successfully; stratechery collides.
    assert outcome.name_collisions == 1
    assert len(outcome.added) == 2
