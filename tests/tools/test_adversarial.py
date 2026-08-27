"""The hostile half of the tool suite.

Every case asserts three things: the error *code*, the *field* it points at, and
that the message *names the correction*. A tool that fails cleanly but unhelpfully
leaves the model no better off than one that crashes -- the recovery path is the
feature, so it is what gets tested.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ledger.engine.duck import run_query, run_scalar
from ledger.tools.context import ToolContext
from ledger.tools.executor import execute
from ledger.tools.results import ErrorCode
from tests.conftest import published

# --------------------------------------------------------------- names


async def test_hallucinated_column_names_the_real_one(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "aggregate", {"metrics": [{"op": "avg", "column": "tip_pct"}]}, analyst_ctx
    )
    assert not result.ok
    assert result.error is ErrorCode.UNKNOWN_COLUMN
    assert result.field == "metrics[].column"
    assert "tip_amount" in result.suggestions
    assert result.retryable


async def test_unimplemented_metric_enumerates_the_real_ones(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "aggregate", {"metrics": [{"op": "kurtosis", "column": "fare_amount"}]}, analyst_ctx
    )
    assert not result.ok
    assert "median" in result.message and "stddev" in result.message


async def test_unknown_argument_is_named_rather_than_ignored(
    analyst_ctx: ToolContext,
) -> None:
    """Silently ignoring it is how a model answers a question it did not ask."""
    result = await execute("count_rows", {"filterz": []}, analyst_ctx)
    assert not result.ok
    assert result.error is ErrorCode.INVALID_ARGUMENT
    assert "filterz" in result.message


async def test_unknown_tool_lists_the_real_ones(analyst_ctx: ToolContext) -> None:
    result = await execute("summarise_everything", {}, analyst_ctx)
    assert not result.ok
    assert "aggregate" in result.message


# --------------------------------------------------------------- types


async def test_arithmetic_on_a_label_is_refused(analyst_ctx: ToolContext) -> None:
    """`avg(payment_type)` is a confidently meaningless answer."""
    result = await execute(
        "aggregate", {"metrics": [{"op": "avg", "column": "payment_type"}]}, analyst_ctx
    )
    assert not result.ok
    assert result.error is ErrorCode.TYPE_MISMATCH
    assert "numeric" in result.message


async def test_substring_match_on_a_number_is_refused(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "count_rows",
        {"filters": [{"column": "trip_distance", "op": "contains", "value": "5"}]},
        analyst_ctx,
    )
    assert not result.ok
    assert result.error is ErrorCode.TYPE_MISMATCH
    assert "=" in result.suggestions


async def test_ordering_operator_on_a_category_is_refused(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "count_rows",
        {"filters": [{"column": "pickup_zone", "op": ">", "value": "M"}]},
        analyst_ctx,
    )
    assert not result.ok
    assert result.error is ErrorCode.TYPE_MISMATCH


async def test_timeseries_on_a_non_temporal_column_is_refused(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "timeseries",
        {"time_column": "fare_amount", "grain": "day", "metrics": [{"op": "count"}]},
        analyst_ctx,
    )
    assert not result.ok
    assert "pickup_at" in result.suggestions


# --------------------------------------------------------------- ranges


async def test_backwards_date_range_says_to_swap_them(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "count_rows",
        {
            "filters": [
                {
                    "column": "pickup_at",
                    "op": "between",
                    "value": ["2025-03-01", "2025-01-01"],
                }
            ]
        },
        analyst_ctx,
    )
    assert not result.ok
    assert result.error is ErrorCode.INVALID_ARGUMENT
    assert "Swap them" in result.message


async def test_in_list_beyond_the_cap_is_refused(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "count_rows",
        {"filters": [{"column": "pickup_zone", "op": "in", "value": [str(i) for i in range(500)]}]},
        analyst_ctx,
    )
    assert not result.ok
    assert "100" in result.message


async def test_nullary_operator_with_a_value_is_refused(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "count_rows",
        {"filters": [{"column": "pickup_zone", "op": "is_null", "value": "x"}]},
        analyst_ctx,
    )
    assert not result.ok
    assert "takes no value" in result.message


async def test_invalid_grain_lists_the_real_ones(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "timeseries",
        {"time_column": "pickup_at", "grain": "century", "metrics": [{"op": "count"}]},
        analyst_ctx,
    )
    assert not result.ok
    assert "day" in result.message


# --------------------------------------------------------------- scale


async def test_million_cardinality_group_by_never_reaches_duckdb(
    analyst_ctx: ToolContext,
) -> None:
    """Refused from the cached estimate, not by executing and dying.

    The spy is the point: a version that runs the query and catches the fallout
    would pass a test that only checked the error code.
    """
    with patch("ledger.tools.analytics_tools.run_query") as spy:
        result = await execute(
            "aggregate",
            {"metrics": [{"op": "count"}], "group_by": ["pickup_at"]},
            analyst_ctx,
        )
    assert not result.ok
    assert result.error is ErrorCode.CARDINALITY_EXCEEDED
    assert spy.call_count == 0
    assert "timeseries" in result.message
    assert result.suggestions


async def test_too_many_series_in_a_timeseries_is_refused(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "timeseries",
        {
            "time_column": "pickup_at",
            "grain": "month",
            "metrics": [{"op": "count"}],
            "group_by": "pickup_zone",
        },
        analyst_ctx,
    )
    assert not result.ok
    assert result.error is ErrorCode.CARDINALITY_EXCEEDED


async def test_absurd_limit_is_clamped_rather_than_refused(
    analyst_ctx: ToolContext,
) -> None:
    """The model wanted rows, not a lecture."""
    result = await execute(
        "aggregate",
        {"metrics": [{"op": "count"}], "group_by": ["pickup_zone"], "limit": 10_000_000},
        analyst_ctx,
    )
    assert result.ok
    assert any("clamped" in note for note in result.notes)


# --------------------------------------------------------------- empty


async def test_zero_rows_names_the_filter_that_killed_it(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "count_rows",
        {
            "filters": [
                {"column": "payment_type", "op": "=", "value": 99},
                {"column": "pickup_borough", "op": "=", "value": "Manhattan"},
            ]
        },
        analyst_ctx,
    )
    assert result.ok
    assert result.rows[0][0] == 0
    joined = " ".join(result.notes)
    assert "filters[0]" in joined
    assert "eliminates everything" in joined
    # ...and says what *is* there, so one retry fixes it.
    assert "values present in payment_type" in joined


async def test_a_combination_that_matches_nothing_says_so(
    analyst_ctx: ToolContext,
) -> None:
    """Each filter matches alone; only together do they match nothing."""
    result = await execute(
        "count_rows",
        {
            "filters": [
                {"column": "pickup_borough", "op": "=", "value": "Brooklyn"},
                {"column": "pickup_zone", "op": "=", "value": "JFK Airport"},
            ]
        },
        analyst_ctx,
    )
    assert result.ok
    assert result.rows[0][0] == 0
    assert "their combination" in " ".join(result.notes)


# --------------------------------------------------------------- injection


@pytest.mark.parametrize(
    "hostile",
    [
        "'; DROP TABLE ledger.trips; --",
        "' OR '1'='1",
        "%_%",
        '" OR 1=1 --',
    ],
)
async def test_injection_in_a_value_is_parameterised_not_executed(
    analyst_ctx: ToolContext, hostile: str
) -> None:
    result = await execute(
        "count_rows",
        {"filters": [{"column": "pickup_zone", "op": "=", "value": hostile}]},
        analyst_ctx,
    )
    assert result.ok
    assert result.rows[0][0] == 0
    # ...and the table is still there.
    assert await run_scalar(analyst_ctx.cursor, "SELECT count(*) FROM ledger.trips") == 45_000


async def test_injection_in_a_column_name_is_an_unknown_column(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "count_rows",
        {
            "filters": [
                {"column": 'fare_amount"; DROP TABLE ledger.trips; --', "op": ">", "value": 1}
            ]
        },
        analyst_ctx,
    )
    assert not result.ok
    assert result.error is ErrorCode.UNKNOWN_COLUMN
    _, rows = await run_query(analyst_ctx.cursor, "SELECT count(*) FROM ledger.trips")
    assert rows[0][0] == 45_000


async def test_wildcards_in_a_contains_value_stay_literal(
    analyst_ctx: ToolContext,
) -> None:
    """`contains` compiles to strpos, so '%' matches a literal percent sign."""
    result = await execute(
        "count_rows",
        {"filters": [{"column": "pickup_zone", "op": "contains", "value": "%"}]},
        analyst_ctx,
    )
    assert result.ok
    assert result.rows[0][0] == 0


# --------------------------------------------------------------- plot


async def test_plot_with_a_stale_result_id_is_actionable(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "plot",
        {
            "result_id": "r_does_not_exist",
            "chart": {"kind": "bar", "x": "a", "y": ["b"], "title": "t"},
        },
        analyst_ctx,
    )
    assert not result.ok
    assert "run an analysis tool first" in result.message.lower()


async def test_plot_naming_a_column_the_result_lacks(analyst_ctx: ToolContext) -> None:
    source = await execute(
        "top_n", {"dimension": "pickup_borough", "metric": {"op": "count"}, "n": 3}, analyst_ctx
    )
    result = await execute(
        "plot",
        {
            "result_id": source.result_id,
            "chart": {"kind": "bar", "x": "pickup_borough", "y": ["revenue"], "title": "t"},
        },
        analyst_ctx,
    )
    assert not result.ok
    assert result.error is ErrorCode.UNKNOWN_COLUMN
    # Names the columns that result actually has, not the dataset's.
    assert "row_count" in result.message
    assert "trip_distance" not in result.message


async def test_unreadable_pie_chart_is_refused_with_an_alternative(
    analyst_ctx: ToolContext,
) -> None:
    source = await execute(
        "top_n", {"dimension": "pickup_zone", "metric": {"op": "count"}, "n": 30}, analyst_ctx
    )
    result = await execute(
        "plot",
        {
            "result_id": source.result_id,
            "chart": {"kind": "pie", "x": "pickup_zone", "y": ["row_count"], "title": "t"},
        },
        analyst_ctx,
    )
    assert not result.ok
    assert "bar" in result.suggestions


# --------------------------------------------------------------- rbac


async def test_viewer_reaching_for_a_restricted_column_sees_a_typo_error(
    viewer_ctx: ToolContext,
) -> None:
    """Indistinguishable from a misspelling, and never suggests the hidden name."""
    result = await execute(
        "aggregate", {"metrics": [{"op": "avg", "column": "tip_amount"}]}, viewer_ctx
    )
    assert not result.ok
    assert result.error is ErrorCode.UNKNOWN_COLUMN
    assert "tip_amount" not in result.suggestions
    assert "restricted" not in result.message.lower()
    assert "permission" not in result.message.lower()


async def test_the_audit_log_records_what_the_viewer_reached_for(
    viewer_ctx: ToolContext,
) -> None:
    """The caller is told it does not exist; the log knows better."""
    await execute("aggregate", {"metrics": [{"op": "avg", "column": "tip_amount"}]}, viewer_ctx)
    events = published(viewer_ctx)
    assert events.denied_columns() == {"tip_amount"}
    assert events.completed[-1].outcome.value == "deny"  # type: ignore[union-attr]


async def test_a_viewers_suggestions_never_include_a_hidden_column(
    viewer_ctx: ToolContext,
) -> None:
    """A better suggester must not become an oracle for what is hidden."""
    result = await execute(
        "aggregate", {"metrics": [{"op": "avg", "column": "tip_pct"}]}, viewer_ctx
    )
    assert not result.ok
    assert not {"tip_amount", "tolls_amount", "extra"} & set(result.suggestions)


async def test_nobody_can_name_the_internal_tenant_column(
    analyst_ctx: ToolContext,
) -> None:
    """Even an analyst. The compiler applies it; no caller may filter on it."""
    result = await execute(
        "aggregate", {"metrics": [{"op": "count"}], "group_by": ["tenant_id"]}, analyst_ctx
    )
    assert not result.ok
    assert result.error is ErrorCode.UNKNOWN_COLUMN


async def test_a_bucket_built_from_a_handful_of_rows_is_called_out(
    analyst_ctx: ToolContext,
) -> None:
    """Real TLC data carries timestamps years outside the file's month.

    Unflagged, those become chart points indistinguishable from real ones while
    being drawn from a single row -- a confidently wrong picture that no amount
    of query correctness prevents. The rows are kept, not dropped; the model is
    told which buckets are negligible.
    """
    from ledger.tools import analytics_tools

    names = ["bucket", "row_count"]
    rows: list[list[object]] = [
        ["2008-12-01T00:00:00", 2],
        ["2024-12-01T00:00:00", 3_668_358],
        ["2025-01-01T00:00:00", 3_475_236],
    ]
    notes = analytics_tools._sparse_bucket_notes(names, rows)  # type: ignore[arg-type]

    assert notes
    assert "2008-12-01" in notes[0]
    assert "2024-12-01" not in notes[0]


async def test_a_healthy_series_is_not_flagged(analyst_ctx: ToolContext) -> None:
    """The guard must stay quiet on ordinary data, or it becomes noise."""
    from ledger.tools import analytics_tools

    rows: list[list[object]] = [
        ["2025-01-01T00:00:00", 1000],
        ["2025-02-01T00:00:00", 1100],
        ["2025-03-01T00:00:00", 900],
    ]
    assert analytics_tools._sparse_bucket_notes(["bucket", "row_count"], rows) == []  # type: ignore[arg-type]
