"""Registered views, in tab order.

Adding a view = create the module under `views/`, import it here, append to
`VIEWS`. The app composes its tab bar from this list.
"""

from __future__ import annotations

from pulpline.tui.views.base import View
from pulpline.tui.views.library import LibraryView
from pulpline.tui.views.stats import StatsView

VIEWS: list[type[View]] = [LibraryView, StatsView]

__all__ = ["VIEWS", "LibraryView", "StatsView", "View"]
