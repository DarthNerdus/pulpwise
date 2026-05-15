"""Stats view - totals, breakdowns, and a 30-day activity bar chart.

All charts are built from rich-text block characters; no extra plotting deps.
The view exposes one `Static` widget per section and rebuilds their content
on refresh - easier than DataTable shenanigans for this kind of static
summary data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from pulpline.state import (
    activity_per_day,
    connect,
    count_by_extension,
    count_by_subscription,
    count_since,
    count_total,
)
from pulpline.tui.views.base import View

_BAR_WIDTH = 30
_ACTIVITY_BAR_WIDTH = 14  # each of the two side-by-side bars in the activity chart


class StatsView(View):
    DISPLAY_NAME: ClassVar[str] = "Stats"
    ID: ClassVar[str] = "stats"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="stats-totals")
            yield Static(id="stats-by-source")
            yield Static(id="stats-by-format")
            yield Static(id="stats-activity")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        with connect() as conn:
            total = count_total(conn)
            now = datetime.now(tz=UTC)
            week = count_since(conn, (now - timedelta(days=7)).isoformat(timespec="seconds"))
            month = count_since(conn, (now - timedelta(days=30)).isoformat(timespec="seconds"))
            year = count_since(conn, (now - timedelta(days=365)).isoformat(timespec="seconds"))
            by_sub = count_by_subscription(conn)
            by_ext = count_by_extension(conn)
            activity = activity_per_day(conn, days=30)

        self.query_one("#stats-totals", Static).update(
            _totals_text(total=total, week=week, month=month, year=year)
        )
        self.query_one("#stats-by-source", Static).update(_section_bars("BY SOURCE", by_sub))
        self.query_one("#stats-by-format", Static).update(
            _section_bars("BY FORMAT", _uppercase_keys(by_ext))
        )
        self.query_one("#stats-activity", Static).update(_activity_text(activity))


def _totals_text(*, total: int, week: int, month: int, year: int) -> Text:
    text = Text()
    text.append("OVERVIEW\n", style="bold")
    text.append(f"  Total items:   {total}\n")
    text.append(f"  Last 7 days:   {week}\n")
    text.append(f"  Last 30 days:  {month}\n")
    text.append(f"  Last year:     {year}\n")
    return text


def _section_bars(heading: str, counts: dict[str, int]) -> Text:
    """Horizontal bars scaled to the largest bucket (not to total).

    Scaling to total wastes the visual range when many buckets share the
    space evenly: with 10 sources the leader is ~17%, and 17% of bar width
    looks small. Scaling to max means the leader's bar always fills the
    line; every other bar reads as 'this fraction of the leader.' The
    absolute share stays accessible via the trailing percentage label.
    """
    text = Text()
    text.append(f"\n{heading}\n", style="bold")
    if not counts:
        text.append("  (nothing yet)\n", style="dim")
        return text

    total = sum(counts.values())
    peak = max(counts.values())
    name_w = max(len(name) for name in counts)
    for name, n in counts.items():
        pct = n / total if total else 0
        filled = round(n / peak * _BAR_WIDTH) if peak else 0
        bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        text.append(f"  {name:<{name_w}}  {bar}  {n}  ({pct * 100:.0f}%)\n")
    return text


def _activity_text(activity: list[tuple[str, int, int]]) -> Text:
    """Joint added/deleted bars per day, shared scale with p90 outlier clipping.

    Shared scale because the whole purpose of putting adds + deletes on the
    same row is direct comparison ('I grew the library by N today'). The
    bars are useless for that if they're scaled independently - a single
    delete would look identical to a 20-delete day on different data.

    p90 of all non-zero activity (both axes combined) sets the scaling
    baseline. Days above that peak saturate at full bar width; the +N / -N
    label still tells you the real count. This is the trade: an
    onboarding-day outlier with 98 adds shouldn't squash every typical day
    to one cell, but it shouldn't disappear either - saturation conveys
    'unusually high' without distorting the day-to-day comparison.
    """
    text = Text()
    text.append("\nACTIVITY (last 30 days)  ", style="bold")
    text.append("[+ added]", style="green")
    text.append("  ")
    text.append("[- deleted]\n", style="red")
    if not activity:
        text.append("  (nothing yet)\n", style="dim")
        return text

    combined: list[int] = []
    for _, a, d in activity:
        combined.append(a)
        combined.append(d)
    peak = _percentile_peak(combined)

    for day, added, deleted in activity:
        added_bar = _scaled_bar(added, peak, _ACTIVITY_BAR_WIDTH)
        deleted_bar = _scaled_bar(deleted, peak, _ACTIVITY_BAR_WIDTH)
        text.append(f"  {day}  ")
        text.append(added_bar, style="green")
        text.append(f"  +{added}".ljust(5))
        text.append("  ")
        text.append(deleted_bar, style="red")
        text.append(f"  -{deleted}".ljust(5))
        text.append("\n")
    return text


def _percentile_peak(values: list[int], percentile: float = 0.90) -> int:
    """Pick a chart-scaling peak that ignores outliers.

    Returns the 90th percentile of non-zero values. With few non-zero data
    points (fewer than 5), falls back to max so the peak doesn't collapse
    to a tiny number on sparse data. Minimum return is 1 so callers can
    divide by it safely.
    """
    non_zero = sorted(v for v in values if v > 0)
    if not non_zero:
        return 1
    if len(non_zero) < 5:
        return max(non_zero)
    idx = min(len(non_zero) - 1, max(0, int(len(non_zero) * percentile) - 1))
    return max(1, non_zero[idx])


def _scaled_bar(value: int, peak: int, width: int) -> str:
    """Render a bar; values above `peak` saturate at full width."""
    if peak <= 0:
        return "░" * width
    filled = min(width, round(value / peak * width))
    return "█" * filled + "░" * (width - filled)


def _uppercase_keys(d: dict[str, int]) -> dict[str, int]:
    return {k.upper(): v for k, v in d.items()}
