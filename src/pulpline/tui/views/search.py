"""Search view - Anna's Archive search inside the TUI.

Mirrors `pulp search anna` but lives inline in the TUI so users don't
have to drop to the shell to find a book. Same backend (`AnnaSearcher`),
same download path (`pipeline.add_once`), same checkbox-style multi-pick
the standalone picker uses - just composed into a tab instead of run as
a one-shot Textual app.

Search and download both run on worker threads so the UI doesn't freeze
during the slow network calls. Status messages flow back through the
worker callback into a Static below the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input, Static

from pulpline.config import load_config
from pulpline.models import ExtractionError, FetchError
from pulpline.tui.views.base import View

if TYPE_CHECKING:
    from pulpline.searchers.base import SearchResult

_CHECKED = "[b green]●[/]"
_UNCHECKED = "○"


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    results: list[SearchResult]
    error: str | None = None


class SearchView(View):
    DISPLAY_NAME: ClassVar[str] = "Search"
    ID: ClassVar[str] = "search"

    BINDINGS = [  # noqa: RUF012
        ("/", "focus_query", "Focus query"),
        ("space", "toggle_pick", "Toggle"),
        ("d", "download", "Download picked"),
        ("a", "toggle_all", "Toggle all"),
    ]

    DEFAULT_CSS = """
    SearchView {
        layout: vertical;
    }
    SearchView Input {
        margin: 0 0 1 0;
    }
    SearchView DataTable {
        height: 1fr;
    }
    SearchView #search-status {
        height: 2;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._results: list[SearchResult] = []
        self._picked: set[int] = set()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(
                placeholder="search Anna's Archive (book title, author, ISBN)...",
                id="search-query",
            )
            yield DataTable(cursor_type="row", zebra_stripes=True, id="search-results")
            yield Static("type a query and press enter", id="search-status")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("", "Title", "Author", "Year", "Lang", "Ext", "Size")

    def action_focus_query(self) -> None:
        self.query_one("#search-query", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-query":
            return
        query = event.value.strip()
        if not query:
            return
        self._set_status(f"searching for {query!r}...")
        self._run_search(query)

    @work(thread=True, exclusive=True, group="search")
    def _run_search(self, query: str) -> None:
        from pulpline.searchers.annas import AnnaSearcher

        try:
            cfg = load_config()
            with AnnaSearcher.from_config(cfg) as searcher:
                results = list(searcher.search(query, limit=30))
            outcome = _SearchOutcome(results=results)
        except FetchError as exc:
            outcome = _SearchOutcome(results=[], error=str(exc))
        except Exception as exc:
            outcome = _SearchOutcome(results=[], error=f"{type(exc).__name__}: {exc}")

        self.app.call_from_thread(self._on_search_done, outcome)

    def _on_search_done(self, outcome: _SearchOutcome) -> None:
        if outcome.error:
            self._set_status(f"[red]search failed:[/] {outcome.error}")
            return

        self._results = outcome.results
        self._picked.clear()

        table = self.query_one(DataTable)
        table.clear()
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

        if not self._results:
            self._set_status("no results")
            return

        self._set_status(f"{len(self._results)} result(s)  -  space toggles, d downloads picked")
        table.focus()

    def action_toggle_pick(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        idx = table.cursor_row
        if idx in self._picked:
            self._picked.remove(idx)
            mark = _UNCHECKED
        else:
            self._picked.add(idx)
            mark = _CHECKED
        table.update_cell_at(Coordinate(idx, 0), mark, update_width=False)
        self._refresh_status_counts()

    def action_toggle_all(self) -> None:
        if not self._results:
            return
        if len(self._picked) == len(self._results):
            self._picked.clear()
        else:
            self._picked = set(range(len(self._results)))
        table = self.query_one(DataTable)
        for i in range(len(self._results)):
            mark = _CHECKED if i in self._picked else _UNCHECKED
            table.update_cell_at(Coordinate(i, 0), mark, update_width=False)
        self._refresh_status_counts()

    def action_download(self) -> None:
        if not self._results:
            return
        targets = sorted(self._picked) if self._picked else self._cursor_indices()
        if not targets:
            return
        self._set_status(f"downloading {len(targets)} item(s)...")
        self._run_downloads(targets)

    @work(thread=True, exclusive=True, group="download")
    def _run_downloads(self, indices: list[int]) -> None:
        from pulpline import pipeline
        from pulpline.sources.annas import AnnaSource

        cfg = load_config()
        for n, idx in enumerate(indices, start=1):
            r = self._results[idx]
            self.app.call_from_thread(
                self._set_status, f"downloading {n}/{len(indices)}: {r.title}"
            )
            try:
                path = pipeline.add_once(r.target_url, config=cfg)
            except (FetchError, ExtractionError) as exc:
                self.app.call_from_thread(self._set_status, f"[red]failed:[/] {exc}")
                continue
            quota = AnnaSource.LAST_QUOTA_INFO
            quota_str = (
                f"  -  {quota.downloads_left}/{quota.downloads_per_day} quota left" if quota else ""
            )
            self.app.call_from_thread(
                self._set_status,
                f"wrote {path.name}{quota_str}",
            )

        self.app.call_from_thread(self._on_downloads_done)

    def _on_downloads_done(self) -> None:
        # Clear the checkboxes so the row state reflects "downloaded".
        table = self.query_one(DataTable)
        for i in self._picked:
            table.update_cell_at(Coordinate(i, 0), _UNCHECKED, update_width=False)
        self._picked.clear()

    def _cursor_indices(self) -> list[int]:
        table = self.query_one(DataTable)
        if table.row_count:
            return [table.cursor_row]
        return []

    def _refresh_status_counts(self) -> None:
        n = len(self._picked)
        total = len(self._results)
        if n:
            self._set_status(f"{total} result(s)  -  {n} picked  -  d to download")
        else:
            self._set_status(f"{total} result(s)  -  space toggles, d downloads")

    def _set_status(self, text: str) -> None:
        self.query_one("#search-status", Static).update(text)


def _trunc(text: str | None, width: int) -> str:
    if not text:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"
