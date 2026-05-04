"""Textual app: tab bar + footer + view container."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.widgets import Footer, Header, TabbedContent, TabPane

from pulpline import __version__
from pulpline.tui.views import VIEWS, SyncView, View


class PulplineApp(App[None]):
    TITLE = f"pulpline {__version__}"
    SUB_TITLE = "local-first content pipeline"

    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "Quit"),
        ("r", "refresh_views", "Refresh"),
        ("s", "sync_now", "Sync"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(initial=VIEWS[0].ID):
            for view_cls in VIEWS:
                with TabPane(view_cls.DISPLAY_NAME, id=view_cls.ID):
                    yield view_cls()
        yield Footer()

    def action_refresh_views(self) -> None:
        for view in self.query(View):
            view.refresh_data()

    def action_sync_now(self) -> None:
        """Switch to the Sync tab and kick off a sync.

        Lifted to the app level (rather than a SyncView binding) because
        SyncView has no focusable widget, so view-level keybindings never
        dispatch. App-level also means 's' works from any tab.
        """
        tabbed = self.query_one(TabbedContent)
        tabbed.active = SyncView.ID
        self.query_one(SyncView).action_run_sync()


def run() -> None:
    PulplineApp().run()
