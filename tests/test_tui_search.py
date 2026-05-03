"""Pilot-driven test of the TUI Search view.

Drives a real Textual app, types a query, asserts results land in the
DataTable, toggles a row, and verifies the picked count surfaces in the
status line. Worker threads are stubbed via monkeypatch so the test
doesn't hit the network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from textual.widgets import DataTable, Input, Static

from pulpline.searchers.base import SearchResult
from pulpline.tui.app import PulplineApp
from pulpline.tui.views.search import SearchView


def _make_results(n: int = 3) -> list[SearchResult]:
    return [
        SearchResult(
            target_url=f"https://annas-archive.gl/md5/{i:032x}",
            title=f"Result {i}",
            authors=f"Author {i}",
            year="2020",
            language="en",
            extension="epub",
            size="1.0 MB",
        )
        for i in range(n)
    ]


def _patch_search(monkeypatch: pytest.MonkeyPatch, results: list[SearchResult]) -> None:
    """Replace AnnaSearcher.from_config with a stub yielding `results`."""

    class _StubSearcher:
        def __enter__(self) -> _StubSearcher:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def search(self, query: str, **_kw: Any) -> list[SearchResult]:
            del query
            return results

    monkeypatch.setattr(
        "pulpline.searchers.annas.AnnaSearcher.from_config",
        lambda cfg, **kw: _StubSearcher(),
    )


def test_typing_query_and_pressing_enter_populates_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search(monkeypatch, _make_results(3))

    async def go() -> tuple[int, str]:
        app = PulplineApp()
        async with app.run_test() as pilot:
            # Switch to Search tab.
            search_view = app.query_one(SearchView)
            search_view.query_one("#search-query", Input).focus()
            await pilot.press(*"berserk")
            await pilot.press("enter")
            # Worker is threaded; let it finish + post back to UI thread.
            for _ in range(20):
                table = search_view.query_one(DataTable)
                if table.row_count:
                    break
                await pilot.pause(0.05)
            return (
                search_view.query_one(DataTable).row_count,
                str(search_view.query_one("#search-status", Static).render()),
            )

    rows, status = asyncio.run(go())
    assert rows == 3
    assert "3 result" in status


def test_space_toggles_row_and_updates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search(monkeypatch, _make_results(3))

    async def go() -> str:
        app = PulplineApp()
        async with app.run_test() as pilot:
            search_view = app.query_one(SearchView)
            search_view.query_one("#search-query", Input).focus()
            await pilot.press(*"q")
            await pilot.press("enter")
            for _ in range(20):
                if search_view.query_one(DataTable).row_count:
                    break
                await pilot.pause(0.05)
            # Focus the table, then toggle.
            search_view.query_one(DataTable).focus()
            await pilot.pause(0.05)
            await pilot.press("space")
            return str(search_view.query_one("#search-status", Static).render())

    status = asyncio.run(go())
    assert "1 picked" in status
