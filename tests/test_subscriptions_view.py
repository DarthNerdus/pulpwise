"""Tests for the Subscriptions view's pure display and message helpers."""

from __future__ import annotations

import pytest

from pulpwise.config import Subscription
from pulpwise.pipeline import BackfillReport
from pulpwise.tui.views.subscriptions import (
    _backfill_outcome_message,
    _canonical_destination,
    _destination_label,
    _sub_row_cells,
)


def _report(
    *,
    new: int = 0,
    skipped: int = 0,
    errors: int = 0,
    pages: int = 0,
    stopped: str = "exhausted",
) -> BackfillReport:
    return BackfillReport(
        name="testsub",
        new_items=new,
        skipped_already_ingested=skipped,
        errors=errors,
        pages_walked=pages,
        stopped_reason=stopped,
    )


def test_message_when_archive_fully_drained_and_nothing_new() -> None:
    """The 'I asked for posts but got 0' case must say 'nothing to fetch'
    explicitly - this is what users mistake for a bug."""
    msg, sev = _backfill_outcome_message("etymology", _report(skipped=78, stopped="exhausted"))
    assert "nothing more to fetch" in msg
    assert "78 posts" in msg  # the user's reality check
    assert sev == "information"


def test_message_when_max_new_hit_signals_more_available() -> None:
    """Hitting the limit means there's more to fetch; the message must say so,
    in TUI terms - the modal takes a bare count, so pointing the user at the
    CLI-only `--posts` flag would be a dead end."""
    msg, sev = _backfill_outcome_message(
        "etymology", _report(new=50, skipped=25, stopped="max_new")
    )
    assert "+50 from etymology" in msg
    assert "archive has more" in msg
    assert "higher count" in msg
    assert "--posts" not in msg
    assert sev == "information"


def test_message_when_exhausted_with_new_items_says_fully_ingested() -> None:
    """Partial-add-and-exhausted: we got the rest, archive done."""
    msg, sev = _backfill_outcome_message(
        "etymology", _report(new=3, skipped=75, stopped="exhausted")
    )
    assert "+3 from etymology" in msg
    assert "full archive now ingested" in msg
    assert "78 posts" in msg
    assert sev == "information"


def test_message_when_since_date_hit() -> None:
    """No CLI flag names in TUI notifications - `--since` doesn't exist here."""
    msg, _ = _backfill_outcome_message("etymology", _report(new=5, skipped=2, stopped="since"))
    assert "+5 from etymology" in msg
    assert "date cutoff" in msg
    assert "--since" not in msg


def test_message_severity_is_warning_when_errors_present() -> None:
    _msg, sev = _backfill_outcome_message(
        "etymology", _report(new=2, skipped=10, errors=1, stopped="max_new")
    )
    assert sev == "warning"


# ---- destination display --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical", "label"),
    [
        (None, "feed", "Feed"),
        ("feed", "feed", "Feed"),
        ("new", "new", "Inbox"),
        ("inbox", "new", "Inbox"),
        ("later", "later", "Later"),
        ("archive", None, "Archive (legacy)"),
        ("", None, "Invalid: ''"),
        (42, None, "Invalid: 42"),
    ],
)
def test_destination_mapping(raw: str | int | None, canonical: str | None, label: str) -> None:
    assert _canonical_destination(raw) == canonical
    assert _destination_label(raw) == label


def test_destination_label_escapes_hostile_markup() -> None:
    assert _destination_label("[red]evil") == r"Invalid: '\[red]evil'"


# ---- _sub_row_cells (table row rendering) ----------------------------------------


def test_row_cells_disabled_sub_shows_disabled_status_and_dims() -> None:
    sub = Subscription(name="paused", source="rss", url="https://p.example/feed", disabled=True)
    cells = _sub_row_cells(sub, None, {"paused": 7})
    assert cells[2] == "[dim]Feed[/dim]"  # Destination column
    assert cells[5] == "[dim]disabled[/dim]"  # Status column
    assert all(cell.startswith("[dim]") for cell in cells)


def test_row_cells_enabled_sub_is_not_dimmed() -> None:
    sub = Subscription(
        name="live",
        source="rss",
        url="https://l.example/feed",
        options={"location": "new"},
    )
    cells = _sub_row_cells(sub, None, {})
    assert cells[2] == "Inbox"
    assert cells[5] == "-"  # no state yet
    assert not any("[dim]" in cell for cell in cells)


def test_row_cells_escape_hostile_markup_in_name_and_url() -> None:
    """DataTable parses cells as markup; a bracket in a name must not crash the table."""
    sub = Subscription(
        name="[red]evil",
        source="rss",
        url="https://x.example/[b]feed",
        options={"location": "[blue]bad"},
    )
    cells = _sub_row_cells(sub, None, {})
    assert cells[0] == r"\[red]evil"
    assert cells[2] == r"Invalid: '\[blue]bad'"
    assert cells[6] == r"https://x.example/\[b]feed"
