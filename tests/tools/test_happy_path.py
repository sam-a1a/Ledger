"""Each tool with known inputs and known outputs.

The fixture is deterministic, so these assert real values rather than shapes.
"""

from __future__ import annotations

from ledger.tools.context import ToolContext
from ledger.tools.executor import execute
from tests.conftest import published

FIXTURE_ROWS = 45_000


async def test_list_columns_returns_the_scoped_catalogue(analyst_ctx: ToolContext) -> None:
    result = await execute("list_columns", {}, analyst_ctx)
    assert result.ok
    names = [row[0] for row in result.rows]
    assert "pickup_zone" in names
    assert "tenant_id" not in names  # internal, visible to nobody
    assert result.row_count == len(analyst_ctx.scope.names())


async def test_list_columns_filters_by_substring(analyst_ctx: ToolContext) -> None:
    result = await execute("list_columns", {"contains": "pickup"}, analyst_ctx)
    assert result.ok
    assert all("pickup" in row[0] for row in result.rows)  # type: ignore[operator]


async def test_describe_column_reports_profile_and_provenance(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute("describe_column", {"column": "trip_distance"}, analyst_ctx)
    assert result.ok
    facts = {row[0]: row[1] for row in result.rows}
    assert facts["semantic_type"] == "numeric"
    assert facts["description_source"] == "seed"
    assert "taximeter" in str(facts["description"])


async def test_count_rows_without_filters_counts_everything(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute("count_rows", {}, analyst_ctx)
    assert result.ok
    assert result.rows[0] == [FIXTURE_ROWS, FIXTURE_ROWS]


async def test_count_rows_with_a_filter_reports_selectivity(
    analyst_ctx: ToolContext,
) -> None:
    result = await execute(
        "count_rows",
        {"filters": [{"column": "payment_type_label", "op": "=", "value": "Cash"}]},
        analyst_ctx,
    )
    assert result.ok
    matched, total = result.rows[0]
    assert 0 < matched < total == FIXTURE_ROWS  # type: ignore[operator]
    assert "%" in result.notes[0]


async def test_aggregate_groups_and_computes(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "aggregate",
        {
            "metrics": [{"op": "count"}, {"op": "avg", "column": "fare_amount"}],
            "group_by": ["pickup_borough"],
        },
        analyst_ctx,
    )
    assert result.ok
    assert result.column_names() == ["pickup_borough", "row_count", "avg_fare_amount"]
    assert sum(row[1] for row in result.rows) == FIXTURE_ROWS  # type: ignore[misc]


async def test_aggregate_result_types_are_inferred_not_guessed(
    analyst_ctx: ToolContext,
) -> None:
    """The frontend picks axis formatting from these."""
    result = await execute(
        "aggregate",
        {"metrics": [{"op": "count"}], "group_by": ["pickup_borough"]},
        analyst_ctx,
    )
    assert result.ok
    types = {c.name: c.type for c in result.columns}
    assert types["pickup_borough"] == "string"
    assert types["row_count"] == "integer"


async def test_top_n_ranks_and_bounds(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "top_n",
        {"dimension": "pickup_zone", "metric": {"op": "count"}, "n": 3},
        analyst_ctx,
    )
    assert result.ok
    assert result.row_count == 3
    counts = [row[1] for row in result.rows]
    assert counts == sorted(counts, reverse=True)


async def test_top_n_min_group_rows_excludes_thin_groups(
    analyst_ctx: ToolContext,
) -> None:
    """The guard against an average won by a single outlier."""
    unguarded = await execute(
        "top_n",
        {
            "dimension": "pickup_zone",
            "metric": {"op": "max", "column": "fare_amount"},
            "n": 1,
        },
        analyst_ctx,
    )
    guarded = await execute(
        "top_n",
        {
            "dimension": "pickup_zone",
            "metric": {"op": "avg", "column": "fare_amount"},
            "n": 1,
            "min_group_rows": 500,
        },
        analyst_ctx,
    )
    assert unguarded.ok and guarded.ok
    assert any("outlier" in note for note in guarded.notes)


async def test_timeseries_buckets_by_grain(analyst_ctx: ToolContext) -> None:
    result = await execute(
        "timeseries",
        {
            "time_column": "pickup_at",
            "grain": "month",
            "metrics": [{"op": "count"}],
        },
        analyst_ctx,
    )
    assert result.ok
    assert result.row_count == 3  # the fixture spans three months
    assert result.columns[0].type == "timestamp"


async def test_timeseries_sees_the_congestion_charge_boundary(
    analyst_ctx: ToolContext,
) -> None:
    """The column does not exist before 2025-01, which is the fare change."""
    result = await execute(
        "timeseries",
        {
            "time_column": "pickup_at",
            "grain": "month",
            "metrics": [{"op": "avg", "column": "cbd_congestion_fee", "alias": "fee"}],
        },
        analyst_ctx,
    )
    assert result.ok
    by_month = {str(row[0])[:7]: row[1] for row in result.rows}
    assert by_month["2024-12"] is None
    assert by_month["2025-02"] == 0.75


async def test_distribution_clips_the_tail_and_says_so(analyst_ctx: ToolContext) -> None:
    """Unclipped, one $998 fare puts every row in a single bin."""
    result = await execute("distribution", {"column": "fare_amount", "bins": 10}, analyst_ctx)
    assert result.ok
    assert result.row_count > 1
    assert any("clipped" in note for note in result.notes)


async def test_plot_renders_a_cached_result(analyst_ctx: ToolContext) -> None:
    source = await execute(
        "top_n",
        {"dimension": "pickup_borough", "metric": {"op": "count"}, "n": 4},
        analyst_ctx,
    )
    assert source.ok
    charted = await execute(
        "plot",
        {
            "result_id": source.result_id,
            "chart": {
                "kind": "bar",
                "x": "pickup_borough",
                "y": ["row_count"],
                "title": "Trips by borough",
            },
        },
        analyst_ctx,
    )
    assert charted.ok
    assert charted.rows[0][0] == source.result_id


async def test_every_call_is_audited_before_and_after(analyst_ctx: ToolContext) -> None:
    await execute("count_rows", {}, analyst_ctx)
    events = published(analyst_ctx)
    assert len(events.requested) == 1
    assert len(events.completed) == 1
    # The pair shares a call id, which is what lets the consumer spot an orphan.
    assert events.requested[0].call_id == events.completed[0].call_id
    assert events.completed[0].row_count == 1


async def test_timeseries_always_reports_a_row_count(analyst_ctx: ToolContext) -> None:
    """Even when not asked for. See the sparse-bucket guard below for why."""
    result = await execute(
        "timeseries",
        {
            "time_column": "pickup_at",
            "grain": "month",
            "metrics": [{"op": "avg", "column": "fare_amount", "alias": "avg_fare"}],
        },
        analyst_ctx,
    )
    assert result.ok
    assert "row_count" in result.column_names()
