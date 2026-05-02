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
    connect,
    count_by_extension,
    count_by_subscription,
    count_since,
    count_total,
    items_per_day,
)
from pulpline.tui.views.base import View

_BAR_WIDTH = 30


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
            activity = items_per_day(conn, days=30)

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
    text = Text()
    text.append(f"\n{heading}\n", style="bold")
    if not counts:
        text.append("  (nothing yet)\n", style="dim")
        return text

    total = sum(counts.values())
    name_w = max(len(name) for name in counts)
    for name, n in counts.items():
        pct = n / total if total else 0
        filled = round(pct * _BAR_WIDTH)
        bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        text.append(f"  {name:<{name_w}}  {bar}  {n}  ({pct * 100:.0f}%)\n")
    return text


def _activity_text(activity: list[tuple[str, int]]) -> Text:
    text = Text()
    text.append("\nACTIVITY (last 30 days)\n", style="bold")
    if not activity:
        text.append("  (nothing yet)\n", style="dim")
        return text

    peak = max(n for _, n in activity) or 1
    for day, n in activity:
        filled = round(n / peak * _BAR_WIDTH)
        bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        text.append(f"  {day}  {bar}  {n}\n")
    return text


def _uppercase_keys(d: dict[str, int]) -> dict[str, int]:
    return {k.upper(): v for k, v in d.items()}
