"""Smoke tests for the TUI - imports, instantiation, and pure helpers.

Full Textual rendering tests need pytest-textual-snapshot; we leave those
out of the suite for now and rely on manual `pulpwise tui` dogfooding.
These tests ensure the views compose without errors, the registry is
wired up so refactors don't silently break the app, and the markup-facing
helpers survive hostile (bracket-laden) input from the wild.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from rich.text import Text

from pulpwise.models import FetchError
from pulpwise.state import LibraryItem
from pulpwise.tui.app import PulpwiseApp
from pulpwise.tui.views import (
    VIEWS,
    LibraryView,
    StatsView,
    SubscriptionsView,
    SyncView,
)
from pulpwise.tui.views import sync as sync_view
from pulpwise.tui.views.library import _group_label, _item_label


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
    app = PulpwiseApp()
    assert app.TITLE.startswith("pulpwise")
    assert app.SUB_TITLE == "content pipeline for Readwise Reader"


def test_view_ids_unique() -> None:
    """Tab ids must be unique - duplicates would confuse TabbedContent."""
    ids = [cls.ID for cls in VIEWS]
    assert len(ids) == len(set(ids))


# ---- Library tree labels ----
#
# Tree labels built from strings go through Text.from_markup, so any
# user-derived text (feed titles, subscription names) containing Rich
# markup - even *broken* markup like a stray closing tag - would raise
# MarkupError and crash the whole TUI. The label builders must escape.

HOSTILE = "[/span] stray closing [b]bold[/b] [link=http://x]tag[/link]"


def _library_item(
    title: str | None,
    *,
    deleted_at: str | None = None,
) -> LibraryItem:
    return LibraryItem(
        id=1,
        title=title,
        canonical_url="https://example.com/post",
        subscription_name="testsub",
        ingested_at="2026-07-01T12:00:00",
        pub_date=None,
        readwise_url=None,
        deleted_at=deleted_at,
    )


def test_item_label_survives_hostile_markup_title() -> None:
    label = _item_label(_library_item(HOSTILE), deleted=False)
    rendered = Text.from_markup(label)  # what Tree.process_label does
    assert HOSTILE in rendered.plain
    assert "2026-07-01" in rendered.plain


def test_item_label_deleted_mode_survives_hostile_title() -> None:
    label = _item_label(
        _library_item(HOSTILE, deleted_at="2026-07-02T08:00:00"),
        deleted=True,
    )
    rendered = Text.from_markup(label)
    assert HOSTILE in rendered.plain
    assert "2026-07-02" in rendered.plain


def test_item_label_untitled_fallback() -> None:
    label = _item_label(_library_item(None), deleted=False)
    assert "(untitled)" in Text.from_markup(label).plain


def test_group_label_survives_hostile_bucket_name() -> None:
    label = _group_label("bad [red]sub[/red] name", "(3)", deleted=False)
    rendered = Text.from_markup(label)
    assert rendered.plain == "bad [red]sub[/red] name  (3)"


def test_group_label_deleted_prefix_actually_renders() -> None:
    """'[deleted]' looks exactly like a Rich tag; unescaped, from_markup
    eats it and the tombstone heading loses its marker."""
    label = _group_label("testsub", "(2)", deleted=True)
    rendered = Text.from_markup(label)
    assert rendered.plain == "[deleted] testsub  (2)"


# ---- Sync view Readwise probe ----
#
# check_token has three outcomes: True (204, token accepted), False (401,
# token rejected), and FetchError (5xx/network - *inconclusive*). The probe
# must map each to a distinct status, and must never let a ConfigError
# escape into the worker (that takes down the whole TUI).


class _FakeSink:
    """Stands in for ReadwiseSink; scripted check_token outcome."""

    outcome: bool | Exception = True

    @classmethod
    def from_config(cls, cfg: object) -> _FakeSink:
        del cfg
        return cls()

    def __enter__(self) -> _FakeSink:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def check_token(self) -> bool:
        outcome = type(self).outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _probe_with(
    monkeypatch: pytest.MonkeyPatch, outcome: bool | Exception
) -> sync_view._ReadwiseStatus:
    monkeypatch.setattr(sync_view, "resolve_token", lambda cfg: "test-token")
    monkeypatch.setattr(_FakeSink, "outcome", outcome)
    monkeypatch.setattr(sync_view, "ReadwiseSink", _FakeSink)
    return sync_view._probe_readwise()


def test_probe_maps_check_token_true_to_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _probe_with(monkeypatch, True)
    assert status.configured is True
    assert status.token_valid is True
    assert status.check_error is None
    assert "token accepted" in sync_view._readwise_text(status).plain


def test_probe_maps_check_token_false_to_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _probe_with(monkeypatch, False)
    assert status.configured is True
    assert status.token_valid is False
    assert status.check_error is None
    assert "token rejected" in sync_view._readwise_text(status).plain


def test_probe_maps_fetch_error_to_could_not_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """An outage is inconclusive - it must NOT render as 'token rejected'
    (telling the user to regenerate a perfectly valid token)."""
    status = _probe_with(monkeypatch, FetchError("HTTP 503"))
    assert status.configured is True
    assert status.token_valid is None
    assert status.check_error == "HTTP 503"
    rendered = sync_view._readwise_text(status).plain
    assert "could not verify" in rendered
    assert "rejected" not in rendered


def test_probe_never_raises_on_config_error() -> None:
    """A malformed config.toml must map to a status, not crash the worker.

    The autouse state-isolation fixture points PULPWISE_CONFIG_PATH at a
    per-test file; writing invalid schema there makes load_config raise
    ConfigError inside the probe.
    """
    config_path = Path(os.environ["PULPWISE_CONFIG_PATH"])
    config_path.write_text('subscriptions = "not-an-array"\n', encoding="utf-8")
    status = sync_view._probe_readwise()  # must not raise
    assert status.config_error is not None
    assert "config error" in sync_view._readwise_text(status).plain
