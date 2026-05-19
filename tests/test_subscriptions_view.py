"""Tests for the Subscriptions view's pure helpers.

The widget itself is exercised in `pulp tui`; only the
notification-message logic gets a unit test because the wording matters
- 'nothing to fetch' vs '+0 from sub, 78 dedup'd' is the difference
between 'you understand what happened' and 'you think it's broken'.
"""

from __future__ import annotations

from pulpline.pipeline import BackfillReport
from pulpline.tui.views.subscriptions import _backfill_outcome_message


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
    """Hitting the limit means there's more to fetch; the message must say so."""
    msg, sev = _backfill_outcome_message(
        "etymology", _report(new=50, skipped=25, stopped="max_new")
    )
    assert "+50 from etymology" in msg
    assert "archive has more" in msg
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
    msg, _ = _backfill_outcome_message(
        "etymology", _report(new=5, skipped=2, stopped="since")
    )
    assert "+5 from etymology" in msg
    assert "--since" in msg


def test_message_severity_is_warning_when_errors_present() -> None:
    _msg, sev = _backfill_outcome_message(
        "etymology", _report(new=2, skipped=10, errors=1, stopped="max_new")
    )
    assert sev == "warning"
