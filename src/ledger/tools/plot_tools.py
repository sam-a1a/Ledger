"""Charting.

``plot`` renders a *cached result*, never a fresh query. If it re-queried, a
non-deterministic tie-break or a shifted boundary could make the chart disagree
with the prose above it -- numbers that are individually correct and jointly
wrong, which is the worst failure this system can produce.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from ledger.tools.args import ArgumentError, ChartSpec, _Scoped
from ledger.tools.context import ToolContext
from ledger.tools.results import ResultColumn, ToolResult

#: Beyond this a pie chart is unreadable and a bar chart is the honest choice.
MAX_PIE_SLICES = 12


class PlotArgs(_Scoped):
    result_id: str = Field(min_length=1, max_length=64)
    chart: ChartSpec

    @model_validator(mode="after")
    def _shape_is_sane(self) -> Self:
        if self.chart.kind == "heatmap" and self.chart.series is None:
            raise ArgumentError(
                "a heatmap needs a 'series' column for its second axis.",
                field="chart.series",
            )
        if self.chart.kind == "scatter" and len(self.chart.y) != 1:
            raise ArgumentError("a scatter chart plots exactly one y against x.", field="chart.y")
        if self.chart.kind == "pie" and len(self.chart.y) != 1:
            raise ArgumentError("a pie chart shows exactly one measure.", field="chart.y")
        return self


async def plot(args: PlotArgs, ctx: ToolContext) -> ToolResult:
    cached = ctx.results.get(args.result_id)
    if cached is None:
        known = ctx.results.ids()
        hint = (
            f" Results available in this conversation: {', '.join(known)}."
            if known
            else " Run an analysis tool first, then plot its result_id."
        )
        raise ArgumentError(
            f"no result {args.result_id!r} to plot.{hint}",
            field="result_id",
            suggestions=known,
        )

    available = cached.column_names()
    # Encodings are validated against the *result's* columns, not the dataset:
    # a hallucinated axis names the columns that actually exist.
    for field_name, value in [("chart.x", args.chart.x), ("chart.series", args.chart.series)]:
        if value is not None and value not in available:
            raise ArgumentError(
                f"{value!r} is not a column of result {args.result_id!r}",
                code="unknown_column",
                field=field_name,
                column=value,
                available=available,
                context_label=f"result {args.result_id!r}",
            )
    for measure in args.chart.y:
        if measure not in available:
            raise ArgumentError(
                f"{measure!r} is not a column of result {args.result_id!r}",
                code="unknown_column",
                field="chart.y",
                column=measure,
                available=available,
                context_label=f"result {args.result_id!r}",
            )

    notes: list[str] = []
    if args.chart.kind == "pie" and cached.row_count > MAX_PIE_SLICES:
        raise ArgumentError(
            f"a pie chart of {cached.row_count:,} slices is unreadable; use kind='bar', "
            f"or reduce the result to at most {MAX_PIE_SLICES} rows first.",
            field="chart.kind",
            suggestions=["bar"],
        )

    # Deliberately tiny: the model has already seen these numbers, and echoing
    # them back would spend context re-reading its own output.
    return ToolResult(
        tool="plot",
        columns=[
            ResultColumn(name="chart_id", type="string"),
            ResultColumn(name="kind", type="string"),
            ResultColumn(name="rendered_points", type="integer"),
        ],
        rows=[[cached.result_id, args.chart.kind, cached.row_count]],
        row_count=1,
        notes=[*notes, f"chart rendered from {cached.row_count:,} row(s) of {args.result_id}."],
    )
