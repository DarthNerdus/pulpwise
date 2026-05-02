"""Smoke tests for the TUI - import and instantiation, not rendering.

Full Textual rendering tests need pytest-textual-snapshot; we leave those
out of the suite for now and rely on manual `pulp tui` dogfooding. These
tests ensure the views compose without errors and the registry is wired
up so refactors don't silently break the app.
"""

from __future__ import annotations

from pulpline.tui.app import PulplineApp
from pulpline.tui.views import VIEWS, LibraryView, StatsView, SubscriptionsView, SyncView


def test_views_registry_includes_all_views() -> None:
    assert LibraryView in VIEWS
    assert SubscriptionsView in VIEWS
    assert SyncView in VIEWS
    assert StatsView in VIEWS


def test_view_class_attrs() -> None:
    assert LibraryView.DISPLAY_NAME == "Library"
    assert LibraryView.ID == "library"
    assert SubscriptionsView.DISPLAY_NAME == "Subscriptions"
    assert SubscriptionsView.ID == "subscriptions"
    assert SyncView.DISPLAY_NAME == "Sync"
    assert SyncView.ID == "sync"
    assert StatsView.DISPLAY_NAME == "Stats"
    assert StatsView.ID == "stats"


def test_app_instantiates() -> None:
    """Catches import-level errors and basic class-construction issues."""
    app = PulplineApp()
    assert app.TITLE.startswith("pulpline")


def test_view_ids_unique() -> None:
    """Tab ids must be unique - duplicates would confuse TabbedContent."""
    ids = [cls.ID for cls in VIEWS]
    assert len(ids) == len(set(ids))
