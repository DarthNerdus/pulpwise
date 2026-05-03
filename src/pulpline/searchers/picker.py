"""Interactive Textual picker for search results.

CLI search commands (`pulp search anna ...`) get a list of `SearchResult`s
from the searcher and need the user to pick one or more for download. This
module provides a small Textual App that renders the results as a
`DataTable` with a checkbox column, supports space-to-toggle and
enter-to-confirm semantics that match every other multi-select TUI users
already know (htop, ncdu, vim's quickfix, ...).

The app exits with a `list[int]` of selected row indices into the
original results list. Empty list = user cancelled (or confirmed with
nothing selected).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Static

if TYPE_CHECKING:
    from pulpline.searchers.base import SearchResult


_CHECKED = "[b green]●[/]"  # filled circle
_UNCHECKED = "○"  # empty circle


class SearchResultPicker(App[list[int]]):
    """Multi-select picker over a list of SearchResults.

    Bindings:
      space    toggle the current row
      enter    confirm picks (if none toggled, picks the cursor row)
      a        toggle all
      escape/q cancel - exits with []
    """

    CSS = """
    Screen {
        layout: vertical;
    }
    #header-help {
        height: 3;
        padding: 1;
        color: $text-muted;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        # DataTable's own enter handler fires `RowSelected` and we listen
        # for that below, so the enter Binding is mostly informational - it
        # surfaces "Confirm" in the footer help line. `toggle` would shadow
        # a DOMNode action so we name it `toggle_pick`.
        ("space", "toggle_pick", "Toggle"),
        ("enter", "confirm", "Confirm"),
        ("a", "toggle_all", "Toggle all"),
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]

    def __init__(self, results: list[SearchResult], query: str = "") -> None:
        super().__init__()
        self._results = results
        self._query = query
        self._picked: set[int] = set()

    def compose(self) -> ComposeResult:
        header = (
            f"[b]{len(self._results)} result(s)[/] for [i]{self._query}[/]\n"
            "space: toggle  -  enter: confirm  -  a: toggle all  -  q/esc: cancel"
            if self._query
            else (
                f"[b]{len(self._results)} result(s)[/]\n"
                "space: toggle  -  enter: confirm  -  a: toggle all  -  q/esc: cancel"
            )
        )
        yield Static(header, id="header-help")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("", "Title", "Author", "Year", "Lang", "Ext", "Size")
        # Anna's titles can be 200+ chars (subtitle + redundant repeats);
        # author column varies but is usually shorter. Hard cap both so
        # rows stay scannable without enabling text wrap (which would
        # break the row-cursor model DataTable uses).
        for i, r in enumerate(self._results):
            table.add_row(
                _UNCHECKED,
                _trunc(r.title, 80),
                _trunc(r.authors, 40),
                r.year or "",
                r.language or "",
                r.extension or "",
                r.size or "",
                key=str(i),
            )
        table.focus()

    def action_toggle_pick(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        row_idx = table.cursor_row
        self._toggle_row(row_idx)

    def action_toggle_all(self) -> None:
        if len(self._picked) == len(self._results):
            self._picked.clear()
        else:
            self._picked = set(range(len(self._results)))
        table = self.query_one(DataTable)
        for i in range(len(self._results)):
            mark = _CHECKED if i in self._picked else _UNCHECKED
            table.update_cell_at(Coordinate(i, 0), mark, update_width=False)

    def action_confirm(self) -> None:
        if self._picked:
            self.exit(sorted(self._picked))
            return
        # Nothing toggled -> treat as "pick the cursor row" so users who
        # just want one don't need to also press space.
        table = self.query_one(DataTable)
        if table.row_count:
            self.exit([table.cursor_row])
        else:
            self.exit([])

    def action_cancel(self) -> None:
        self.exit([])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """DataTable consumes Enter into its own `RowSelected` message; route
        it to the same confirm action so the user's muscle memory works."""
        del event
        self.action_confirm()

    def _toggle_row(self, idx: int) -> None:
        if idx in self._picked:
            self._picked.remove(idx)
            mark = _UNCHECKED
        else:
            self._picked.add(idx)
            mark = _CHECKED
        table = self.query_one(DataTable)
        table.update_cell_at(Coordinate(idx, 0), mark, update_width=False)


def pick_results(results: list[SearchResult], query: str = "") -> list[int]:
    """Launch the picker, return picked indices into `results`. [] = cancelled."""
    if not results:
        return []
    app = SearchResultPicker(results, query=query)
    picked = app.run()
    return picked or []


def _trunc(text: str | None, width: int) -> str:
    if not text:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"
