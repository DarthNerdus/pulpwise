"""View ABC - the contract every TUI tab implements.

Each view is a Textual `Container` with two class-level fields and an
optional `refresh_data` hook. The app composes a `TabPane` per view from
`tui/views/__init__.VIEWS` and dispatches refresh-keyboard events to all
mounted views.
"""

from __future__ import annotations

from typing import ClassVar

from textual.containers import Container


class View(Container):
    """Base class for pulpwise TUI views."""

    DISPLAY_NAME: ClassVar[str] = "View"
    """Label shown in the tab bar."""

    ID: ClassVar[str] = "view"
    """Stable id used as the TabPane id; lowercase, no spaces."""

    def refresh_data(self) -> None:
        """Re-query state and update widgets. Default: no-op.

        Called when the user hits the refresh hotkey or after a state-changing
        action. Views that display dynamic data should override this and
        re-populate their widgets from the SQLite state.
        """
