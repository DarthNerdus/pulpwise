"""Library view - browse all ingested items, filterable."""

from __future__ import annotations

import os
import platform
import subprocess
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Input

from pulpline.state import LibraryItem, connect, list_items
from pulpline.tui.views.base import View


class LibraryView(View):
    DISPLAY_NAME: ClassVar[str] = "Library"
    ID: ClassVar[str] = "library"

    BINDINGS = [  # noqa: RUF012
        ("/", "focus_filter", "Filter"),
        ("enter", "open_file", "Open"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(placeholder="filter by title or url...", id="library-filter")
            yield DataTable(id="library-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Date", "Title", "From", "Format")
        self._items: list[LibraryItem] = []
        self.refresh_data()

    def refresh_data(self) -> None:
        filter_input = self.query_one("#library-filter", Input)
        filter_text = filter_input.value.strip() or None
        with connect() as conn:
            self._items = list_items(conn, limit=500, filter_text=filter_text)

        table = self.query_one(DataTable)
        table.clear()
        for item in self._items:
            table.add_row(
                _short_date(item.ingested_at),
                item.title or "(untitled)",
                item.subscription_name or "[one-shot]",
                _format_from_path(item.output_path),
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "library-filter":
            self.refresh_data()

    def action_focus_filter(self) -> None:
        self.query_one("#library-filter", Input).focus()

    def action_open_file(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._items):
            return
        item = self._items[table.cursor_row]
        if item.output_path:
            _open_file(item.output_path)


def _short_date(iso: str) -> str:
    return iso[:10] if iso else ""


def _format_from_path(path: str | None) -> str:
    if not path or "." not in path:
        return ""
    return path.rsplit(".", 1)[-1].upper()


def _open_file(path: str) -> None:
    """Best-effort open with the OS default app. Silent on failure."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["/usr/bin/open", path])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", path])
        elif system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
    except OSError:
        pass
