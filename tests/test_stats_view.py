"""Tests for the Stats view's pure helpers.

Visualization (text rendering) is exercised by eyeballing in `pulpwise tui`.
The percentile-peak picker is the one piece of real logic worth pinning -
get it wrong and a single outlier day flattens every bar in the chart.
"""

from __future__ import annotations

from pulpwise.tui.views.stats import _percentile_peak, _scaled_bar


def test_percentile_peak_returns_1_for_all_zeros() -> None:
    """Empty / all-zero data must yield a non-zero peak so callers can divide."""
    assert _percentile_peak([0, 0, 0, 0]) == 1
    assert _percentile_peak([]) == 1


def test_percentile_peak_falls_back_to_max_on_sparse_data() -> None:
    """With <5 non-zero values the percentile arithmetic is unreliable;
    falling back to max keeps a single-value column legible."""
    assert _percentile_peak([0, 0, 0, 7]) == 7
    assert _percentile_peak([3, 5]) == 5


def test_percentile_peak_ignores_a_single_outlier() -> None:
    """The whole point of this helper: 9 typical days + 1 spike should not
    pin the peak to the spike. The bottom 90% determines the scale."""
    values = [3, 4, 5, 5, 6, 8, 10, 15, 20, 98]
    # p90: index = int(10*0.9) - 1 = 8, sorted ascending -> values[8] = 20
    assert _percentile_peak(values) == 20


def test_scaled_bar_saturates_above_peak() -> None:
    """A value exceeding the peak must clamp at full width, not overflow.

    Without clamping, value=200 with peak=20 produces 10x width worth of
    █ characters and corrupts the chart's column alignment.
    """
    width = 14
    assert _scaled_bar(20, peak=20, width=width) == "█" * 14
    assert _scaled_bar(98, peak=20, width=width) == "█" * 14
    assert _scaled_bar(200, peak=20, width=width) == "█" * 14


def test_scaled_bar_zero_yields_empty_bar() -> None:
    assert _scaled_bar(0, peak=20, width=14) == "░" * 14


def test_scaled_bar_proportional_in_normal_range() -> None:
    """A value at half the peak should fill roughly half the bar width."""
    width = 14
    bar = _scaled_bar(10, peak=20, width=width)
    # Half of 14 is 7. round(10/20*14) = round(7) = 7.
    assert bar == "█" * 7 + "░" * 7
