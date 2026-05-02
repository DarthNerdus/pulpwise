"""Textual app: tab bar + footer + view container."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.widgets import Footer, Header, TabbedContent, TabPane

from pulpline import __version__
from pulpline.tui.views import VIEWS, View


class PulplineApp(App[None]):
    TITLE = f"pulpline {__version__}"
    SUB_TITLE = "local-first content pipeline"

    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "Quit"),
        ("r", "refresh_views", "Refresh"),
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


def run() -> None:
    PulplineApp().run()
