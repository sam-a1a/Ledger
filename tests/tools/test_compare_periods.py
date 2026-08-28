"""`compare_periods`, against the wrong answer it exists to prevent.

Asking which zones dropped most after New York's congestion charge returns
*every zone up*, because the charge began on 5 January and the obvious
before/after split compares four days against twenty-seven. Every number is
correct and the answer is worthless. These assert that the tool makes the
correct comparison available and the incorrect one visible.
"""

from __future__ import annotations

import pytest

from ledger.tools.context import ToolContext
from ledger.tools.executor import execute

DECEMBER = {"start": "2024-12-01T00:00:00", "end": "2025-01-01T00:00:00"}
JANUARY = {"start": "2025-01-01T00:00:00", "end": "2025-02-01T00:00:00"}
#: Four days, against December's thirty-one. The trap, expressed as arguments.
POST_CHARGE = {"start": "2025-01-05T00:00:00", "end": "2025-01-09T00:00:00"}


def _at(result: object, name: str) -> int:
    """Position of a named output column."""
    return [c.name for c in result.columns].index(name)  # type: ignore[attr-defined]


def _names(result: object) -> list[str]:
    return [c.name for c in result.columns]  # type: ignore[attr-defined]


async def test_equal_windows_report_totals_and_rates(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": JANUARY,
            "metrics": [{"op": "count"}],
        },
        analyst_ctx,
    )
    assert result.ok, result.model_dump()

    assert "row_count_before" in _names(result)
    assert "row_count_after" in _names(result)
    assert "row_count_before_per_day" in _names(result)
    assert "row_count_after_per_day" in _names(result)
    assert "row_count_change_pct" in _names(result)

    row = result.rows[0]
    before = row[_at(result, "row_count_before")]
    per_day = row[_at(result, "row_count_before_per_day")]
    assert before and per_day
    assert per_day == pytest.approx(before / 31, rel=1e-6)


async def test_unequal_windows_are_called_out_rather_than_quietly_compared(
    analyst_ctx: ToolContext,
) -> None:
    """The note is the point. Without it the totals read as a real fall."""
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": POST_CHARGE,
            "metrics": [{"op": "count"}],
        },
        analyst_ctx,
    )
    assert result.ok
    notes = " ".join(result.notes)
    assert "differ in length" in notes
    assert "per-day" in notes
    assert "31 days" in notes and "4 days" in notes


async def test_the_per_day_rate_survives_an_unequal_comparison(
    analyst_ctx: ToolContext,
) -> None:
    """Totals fall because the window is shorter; the rate does not have to.

    This is the whole argument for the tool: the same question answered on
    totals and on rates gives opposite conclusions, and only one is a finding.
    """
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": POST_CHARGE,
            "metrics": [{"op": "count"}],
        },
        analyst_ctx,
    )
    row = result.rows[0]
    total_before = row[_at(result, "row_count_before")]
    total_after = row[_at(result, "row_count_after")]
    rate_before = row[_at(result, "row_count_before_per_day")]
    rate_after = row[_at(result, "row_count_after_per_day")]

    totals_change = (total_after - total_before) / total_before * 100  # type: ignore[operator]
    rates_change = (rate_after - rate_before) / rate_before * 100  # type: ignore[operator]

    # On totals it reads as a collapse, purely because four days is not
    # thirty-one. That is the answer this tool exists to stop being given.
    assert totals_change < -80
    # On rates it does not, and the gap between the two is the finding.
    assert rates_change > totals_change + 50

    # Reported to two decimal places, which is the precision a percentage
    # deserves; what matters is that it came from the rates.
    reported = row[_at(result, "row_count_change_pct")]
    assert reported == pytest.approx(rates_change, abs=0.01)


async def test_an_average_is_compared_as_a_level_not_as_a_rate(
    analyst_ctx: ToolContext,
) -> None:
    """An average fare per day is not a thing, so it is not offered."""
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": JANUARY,
            "metrics": [{"op": "avg", "column": "fare_amount"}],
        },
        analyst_ctx,
    )
    assert result.ok
    assert "avg_fare_amount_before" in _names(result)
    assert "avg_fare_amount_change_pct" in _names(result)
    assert "avg_fare_amount_before_per_day" not in _names(result)
    assert "compared as levels" in " ".join(result.notes)


async def test_grouping_orders_the_biggest_fall_first(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": JANUARY,
            "metrics": [{"op": "count"}],
            "group_by": "pickup_borough",
            "min_group_rows": 0,
        },
        analyst_ctx,
    )
    assert result.ok
    assert _names(result)[0] == "pickup_borough"

    changes = [
        row[_at(result, "row_count_change_pct")]
        for row in result.rows
        if row[_at(result, "row_count_change_pct")] is not None
    ]
    assert changes == sorted(changes)  # type: ignore[type-var]


async def test_thin_groups_are_excluded_and_the_exclusion_is_reported(
    analyst_ctx: ToolContext,
) -> None:
    """Two rows before and one after is a 50% drop, and it is not a finding."""
    generous = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": JANUARY,
            "metrics": [{"op": "count"}],
            "group_by": "pickup_zone",
            "min_group_rows": 0,
            "limit": 200,
        },
        analyst_ctx,
    )
    strict = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": JANUARY,
            "metrics": [{"op": "count"}],
            "group_by": "pickup_zone",
            # The fixture holds 45,000 rows across 30 zones, so this excludes
            # the long tail without excluding everything.
            "min_group_rows": 3_000,
            "limit": 200,
        },
        analyst_ctx,
    )
    assert strict.row_count < generous.row_count
    assert "were excluded" in " ".join(strict.notes)


async def test_overlapping_windows_are_refused(analyst_ctx: ToolContext) -> None:
    """Rows in the overlap would be counted on both sides of the comparison."""
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": {"start": "2024-12-01T00:00:00", "end": "2025-01-15T00:00:00"},
            "after": JANUARY,
            "metrics": [{"op": "count"}],
        },
        analyst_ctx,
    )
    assert not result.ok
    assert result.error == "invalid_argument"
    assert "overlap" in result.message


async def test_a_backwards_window_is_refused(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": {"start": "2025-01-01T00:00:00", "end": "2024-12-01T00:00:00"},
            "after": JANUARY,
            "metrics": [{"op": "count"}],
        },
        analyst_ctx,
    )
    assert not result.ok
    assert result.error == "invalid_argument"


async def test_a_non_temporal_time_column_is_refused_with_alternatives(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "compare_periods",
        {
            "time_column": "trip_distance",
            "before": DECEMBER,
            "after": JANUARY,
            "metrics": [{"op": "count"}],
        },
        analyst_ctx,
    )
    assert not result.ok
    assert result.error == "type_mismatch"
    assert any("pickup_at" in s for s in result.suggestions)


async def test_a_viewer_cannot_compare_a_restricted_column(viewer_ctx: ToolContext) -> None:
    """The same boundary as everywhere else, on a new tool.

    A tool added later is exactly where an access-control gap gets introduced,
    because the enforcement is somewhere the author did not have to think
    about.
    """
    result = await execute(
        "compare_periods",
        {
            "time_column": "pickup_at",
            "before": DECEMBER,
            "after": JANUARY,
            "metrics": [{"op": "avg", "column": "tip_amount"}],
        },
        viewer_ctx,
    )
    assert not result.ok
    assert result.error == "unknown_column"
    assert "tip_amount" not in " ".join(result.suggestions)
