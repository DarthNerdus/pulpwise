"""Registered views, in tab order.

Adding a view = create the module under `views/`, import it here, append to
`VIEWS`. The app composes its tab bar from this list.
"""

from __future__ import annotations

from pulpline.tui.views.base import View
from pulpline.tui.views.library import LibraryView
from pulpline.tui.views.search import SearchView
from pulpline.tui.views.stats import StatsView
from pulpline.tui.views.subscriptions import SubscriptionsView
from pulpline.tui.views.sync import SyncView

VIEWS: list[type[View]] = [LibraryView, SubscriptionsView, SearchView, SyncView, StatsView]

__all__ = [
    "VIEWS",
    "LibraryView",
    "SearchView",
    "StatsView",
    "SubscriptionsView",
    "SyncView",
    "View",
]
