"""Registered views, in tab order.

Adding a view = create the module under `views/`, import it here, append to
`VIEWS`. The app composes its tab bar from this list.
"""

from __future__ import annotations

from pulpwise.tui.views.base import View
from pulpwise.tui.views.library import LibraryView
from pulpwise.tui.views.stats import StatsView
from pulpwise.tui.views.subscriptions import SubscriptionsView
from pulpwise.tui.views.sync import SyncView

VIEWS: list[type[View]] = [LibraryView, SubscriptionsView, SyncView, StatsView]

__all__ = [
    "VIEWS",
    "LibraryView",
    "StatsView",
    "SubscriptionsView",
    "SyncView",
    "View",
]
