"""Tests for the Substack bulk importer."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pulpline.importers.substack import (
    SubstackPublication,
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
