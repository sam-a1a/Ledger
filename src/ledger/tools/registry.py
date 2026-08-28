"""The single tool registry.

Everything -- the agent loop, the MCP server, the golden suite, the tests --
goes through here. That is the point: the tool layer is not welded to the chat
application, and the MCP surface cannot drift from the Anthropic surface because
both schemas are generated from one source.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ledger.catalog.models import ScopedCatalog
from ledger.tools import analytics_tools as analytics
from ledger.tools import catalog_tools as catalogue
from ledger.tools import plot_tools as plotting
from ledger.tools.context import ToolContext
from ledger.tools.results import ToolResult

ToolHandler = Callable[[Any, ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler


#: Descriptions are written for the model, not for a developer: each says when
#: to reach for the tool, because that is the decision the model is making.
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_columns",
        description=(
            "List the columns available in the dataset, with their types and "
            "descriptions. Call this first when you are unsure what the data "
            "contains, or to check a column name before using it."
        ),
        args_model=catalogue.ListColumnsArgs,
        handler=catalogue.list_columns,
    ),
    ToolSpec(
        name="describe_column",
        description=(
            "Get the full profile of one column: null fraction, cardinality, "
            "range, quantiles, sample values, and any caveat about how it is "
            "recorded. Call this before relying on a column you have not used."
        ),
        args_model=catalogue.DescribeColumnArgs,
        handler=catalogue.describe_column,
    ),
    ToolSpec(
        name="count_rows",
        description=(
            "Count rows matching a set of filters. Use it to size a question "
            "before aggregating. If nothing matches, the result explains which "
            "filter eliminated everything and what values are actually present."
        ),
        args_model=analytics.CountRowsArgs,
        handler=analytics.count_rows,
    ),
    ToolSpec(
        name="aggregate",
        description=(
            "Compute one or more metrics, optionally grouped by up to three "
            "columns and filtered. The general-purpose analysis tool: use it for "
            "totals, averages, and breakdowns."
        ),
        args_model=analytics.AggregateArgs,
        handler=analytics.aggregate,
    ),
    ToolSpec(
        name="top_n",
        description=(
            "Rank the values of one column by a metric and return the best or "
            "worst n. Prefer this over aggregate for 'busiest', 'highest', or "
            "'which X had the most Y' questions. When ranking by an average, set "
            "min_group_rows so a single outlier cannot win."
        ),
        args_model=analytics.TopNArgs,
        handler=analytics.top_n,
    ),
    ToolSpec(
        name="timeseries",
        description=(
            "Compute metrics bucketed over time at a chosen grain, optionally "
            "split into a few series. Use this for trends and seasonality. For "
            "an explicit before-and-after comparison use compare_periods "
            "instead. A row count per bucket is always included, so check it "
            "before trusting a bucket: real data contains bad timestamps that "
            "produce buckets built from a handful of rows."
        ),
        args_model=analytics.TimeseriesArgs,
        handler=analytics.timeseries,
    ),
    ToolSpec(
        name="compare_periods",
        description=(
            "Compare two explicit time windows on the same metrics, with "
            "per-day rates alongside the totals. Use this for every "
            "before-and-after question -- 'did X change after Y', 'which zones "
            "dropped most' -- rather than running two queries and subtracting. "
            "Windows of different lengths cannot be compared on totals, and "
            "this returns both lengths and the per-day rates so the comparison "
            "is right even when they differ. Averages and percentiles are "
            "compared as levels, since they do not scale with window length."
        ),
        args_model=analytics.ComparePeriodsArgs,
        handler=analytics.compare_periods,
    ),
    ToolSpec(
        name="distribution",
        description=(
            "Bin a numeric column into a histogram. The upper tail is clipped by "
            "default, because a single extreme value would otherwise put every "
            "row in one bin."
        ),
        args_model=analytics.DistributionArgs,
        handler=analytics.distribution,
    ),
    ToolSpec(
        name="plot",
        description=(
            "Render a chart from a result you already computed, by its result_id. "
            "Choose the chart kind and which of that result's columns map to the "
            "axes. Call this after the analysis tool whose numbers you just "
            "described, so the chart and your text agree."
        ),
        args_model=plotting.PlotArgs,
        handler=plotting.plot,
    ),
)

BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def get(name: str) -> ToolSpec | None:
    return BY_NAME.get(name)


def names() -> list[str]:
    return [spec.name for spec in TOOLS]


def _inject_column_enums(schema: dict[str, Any], scope: ScopedCatalog) -> dict[str, Any]:
    """Constrain column-shaped fields to the names this role can see.

    **Advisory only.** A JSON Schema is a constraint the *model* is asked to
    honour; it does not exist at all on the MCP surface, where a client
    hand-writes the call. Runtime validation against the scoped catalogue is the
    authoritative gate -- this just makes the model far more likely to ask a
    well-formed question in the first place.
    """
    visible = scope.names()

    def walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            if (
                key in ("column", "dimension", "time_column", "group_by")
                and node.get("type") == "string"
                and "enum" not in node
            ):
                node = {**node, "enum": visible}
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(item, key) for item in node]
        return node

    walked = walk(schema)
    return dict(walked) if isinstance(walked, dict) else schema


def schema_for(spec: ToolSpec, scope: ScopedCatalog) -> dict[str, Any]:
    """The Anthropic tool definition for one tool, scoped to a role."""
    raw = spec.args_model.model_json_schema()
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": _inject_column_enums(raw, scope),
    }


def schemas_for(scope: ScopedCatalog) -> list[dict[str, Any]]:
    """Every tool definition, scoped. Stable order, for prompt caching."""
    return [schema_for(spec, scope) for spec in TOOLS]
