"""Tests for the SearchResultPicker Textual app.

Uses Textual's `Pilot` for keyboard interaction. We check the contract
the CLI relies on: which row indices come back for various keypress
sequences. Rendering correctness is left to manual dogfooding.

Tests are written sync (asyncio.run wrappers) to avoid a pytest-asyncio
dev dep - Textual's Pilot is async-only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from pulpline.searchers.base import SearchResult
from pulpline.searchers.picker import SearchResultPicker, pick_results


def _make_results(n: int = 3) -> list[SearchResult]:
    return [
        SearchResult(
            target_url=f"https://annas-archive.gl/md5/{i:032x}",
            title=f"Result {i}",
            authors=f"Author {i}",
            year=str(2000 + i),
            language="en",
            extension="epub",
            size=f"{i}.0 MB",
        )
        for i in range(n)
    ]


async def _drive(
    app: SearchResultPicker,
    actions: Callable[[Any], Coroutine[Any, Any, None]],
) -> list[int] | None:
    async with app.run_test() as pilot:
        await actions(pilot)
    return app.return_value


def _run(app: SearchResultPicker, actions: Callable[[Any], Coroutine[Any, Any, None]]) -> list[int]:
    return asyncio.run(_drive(app, actions)) or []


def test_pick_results_with_empty_list_returns_empty() -> None:
    assert pick_results([]) == []


def test_enter_with_no_toggle_picks_cursor_row() -> None:
    async def go(pilot: Any) -> None:
        await pilot.press("enter")

    assert _run(SearchResultPicker(_make_results(3)), go) == [0]


def test_space_toggles_then_enter_returns_picked() -> None:
    async def go(pilot: Any) -> None:
        await pilot.press("space")  # row 0
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("space")  # row 2
        await pilot.press("enter")

    assert _run(SearchResultPicker(_make_results(3)), go) == [0, 2]


def test_space_twice_untoggles() -> None:
    async def go(pilot: Any) -> None:
        await pilot.press("space")
        await pilot.press("space")  # back to nothing toggled
        await pilot.press("enter")  # falls back to cursor row

    assert _run(SearchResultPicker(_make_results(3)), go) == [0]


def test_toggle_all_then_enter_returns_all() -> None:
    async def go(pilot: Any) -> None:
        await pilot.press("a")
        await pilot.press("enter")

    assert _run(SearchResultPicker(_make_results(3)), go) == [0, 1, 2]


def test_toggle_all_when_all_already_picked_clears() -> None:
    async def go(pilot: Any) -> None:
        await pilot.press("a")  # all on
        await pilot.press("a")  # all off
        await pilot.press("enter")  # cursor row fallback

    assert _run(SearchResultPicker(_make_results(3)), go) == [0]


def test_escape_cancels() -> None:
    async def go(pilot: Any) -> None:
        await pilot.press("space")
        await pilot.press("escape")

    assert _run(SearchResultPicker(_make_results(3)), go) == []
