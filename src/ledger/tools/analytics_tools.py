"""The tools that actually query the dataset.

Two behaviours here carry most of the design weight, and neither is about
producing the right SQL:

* **A zero-row result is diagnosed, not merely reported.** The naive version
  returns an empty list, and the model then either says "no data" or invents a
  number. This one says which filter eliminated everything and what values are
  actually present, so a dead end becomes a one-turn recovery.
* **A hopeless group-by is refused before the query runs**, from the
  catalogue's cached estimate. The naive version executes it and hangs.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, StringConstraints, ValidationInfo, field_validator, model_validator

from ledger.catalog.models import SemanticType
from ledger.engine import sql as sqlc
from ledger.engine.duck import run_query
from ledger.tools.args import (
    MAX_GROUP_BY,
    MAX_METRICS,
    ArgumentError,
    Filter,
    Grain,
    Having,
    Metric,
    OrderBy,
    _Scoped,
    resolve_column,
    scope_from,
)
from ledger.tools.context import ToolContext
from ledger.tools.results import (
    JsonScalar,
    ResultColumn,
    ToolResult,
    infer_result_type,
    json_safe,
)

#: Buckets beyond this and a chart is unreadable anyway.
MAX_TIME_BUCKETS = 2_000
#: More series than this and a legend is useless.
MAX_SERIES = 8


# ---------------------------------------------------------------- arguments


class CountRowsArgs(_Scoped):
    filters: list[Filter] = Field(default_factory=list, max_length=10)


class AggregateArgs(_Scoped):
    metrics: list[Metric] = Field(min_length=1, max_length=MAX_METRICS)
    group_by: list[str] = Field(default_factory=list, max_length=MAX_GROUP_BY)
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    having: list[Having] = Field(default_factory=list, max_length=4)
    order_by: list[OrderBy] = Field(default_factory=list, max_length=3)
    #: Not capped by validation: an over-large limit is clamped and explained
    #: instead. The model asked for rows, not a lecture, and a hard rejection
    #: costs a whole turn to recover from something we can simply do.
    limit: int = Field(default=100, ge=1)

    @field_validator("group_by")
    @classmethod
    def _group_columns_exist(cls, value: list[str], info: ValidationInfo) -> list[str]:
        scope = scope_from(info)
        for name in value:
            resolve_column(name, scope, "group_by")
        if len(set(value)) != len(value):
            raise ArgumentError("group_by contains a duplicate column.", field="group_by")
        return value

    @model_validator(mode="after")
    def _keys_are_resolvable(self) -> Self:
        available = {*self.group_by, *(m.default_alias() for m in self.metrics)}
        aliases = [m.default_alias() for m in self.metrics]
        if len(set(aliases)) != len(aliases):
            raise ArgumentError(
                "two metrics resolve to the same name; set a distinct 'alias'.",
                field="metrics[].alias",
            )
        for order in self.order_by:
            if order.key not in available:
                raise ArgumentError(
                    f"cannot order by {order.key!r}; this result has "
                    f"{', '.join(sorted(available))}.",
                    field="order_by[].key",
                    suggestions=sorted(available),
                )
        for clause in self.having:
            if clause.metric not in aliases:
                raise ArgumentError(
                    f"cannot filter on {clause.metric!r}; this result computes "
                    f"{', '.join(sorted(aliases))}.",
                    field="having[].metric",
                    suggestions=sorted(aliases),
                )
        return self


class TopNArgs(_Scoped):
    """The most common question shape, given a purpose-built signature.

    The model gets this materially more right than the equivalent
    ``aggregate`` + ``order_by`` + ``limit``, which is why it is a tool.
    """

    dimension: str
    metric: Metric
    n: int = Field(default=10, ge=1, le=100)
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    direction: Annotated[str, StringConstraints(pattern="^(top|bottom)$")] = "top"
    #: Guard against an average won by a single outlier. Without it, "highest
    #: average fare by zone" is answered by whichever zone had one $400 trip --
    #: correct arithmetic, useless answer, and exactly the failure an analyst
    #: would catch and a naive tool would not surface.
    min_group_rows: int = Field(default=0, ge=0)

    @field_validator("dimension")
    @classmethod
    def _dimension_exists(cls, value: str, info: ValidationInfo) -> str:
        return resolve_column(value, scope_from(info), "dimension")


class TimeseriesArgs(_Scoped):
    time_column: str
    grain: Grain = "day"
    metrics: list[Metric] = Field(min_length=1, max_length=4)
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    group_by: str | None = None

    @field_validator("time_column")
    @classmethod
    def _is_temporal(cls, value: str, info: ValidationInfo) -> str:
        scope = scope_from(info)
        resolve_column(value, scope, "time_column")
        column = scope.columns[value]
        if column.semantic_type is not SemanticType.TEMPORAL:
            raise ArgumentError(
                f"{value!r} is {column.semantic_type.value}, not a time column.",
                code="type_mismatch",
                field="time_column",
                suggestions=scope.temporal_names(),
            )
        return value

    @field_validator("group_by")
    @classmethod
    def _series_column_exists(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return resolve_column(value, scope_from(info), "group_by")


class DistributionArgs(_Scoped):
    column: str
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    bins: int = Field(default=20, ge=2, le=100)
    #: Taxi fares and distances have absurd tails. Unclipped, the histogram is
    #: one bin holding every row: a wrong answer dressed as a chart.
    clip_percentile: float = Field(default=99.0, ge=50.0, le=100.0)

    @field_validator("column")
    @classmethod
    def _is_numeric(cls, value: str, info: ValidationInfo) -> str:
        scope = scope_from(info)
        resolve_column(value, scope, "column")
        column = scope.columns[value]
        if column.semantic_type is not SemanticType.NUMERIC:
            raise ArgumentError(
                f"distribution needs a numeric column; {value!r} is {column.semantic_type.value}.",
                code="type_mismatch",
                field="column",
                suggestions=scope.numeric_names()[:8],
            )
        return value


# ---------------------------------------------------------------- helpers


def _as_int(value: JsonScalar) -> int:
    """Coerce a scalar the engine returned. A count is never null in practice."""
    return int(value) if isinstance(value, int | float | str) else 0


def _as_float(value: JsonScalar) -> float:
    return float(value) if isinstance(value, int | float | str) else 0.0


async def _run(
    ctx: ToolContext, compiled: sqlc.CompiledQuery
) -> tuple[list[str], list[list[JsonScalar]]]:
    names, raw = await run_query(ctx.cursor, compiled.sql, compiled.params)
    return names, [[json_safe(v) for v in row] for row in raw]


async def _diagnose_empty(ctx: ToolContext, filters: list[Filter]) -> list[str]:
    """Explain which filter eliminated everything.

    Costs one cheap count per predicate, and only on the zero-row path.
    """
    notes = [f"0 rows matched all {len(filters)} filter(s)."]
    culprits: list[int] = []

    for index, predicate in enumerate(filters):
        compiled = sqlc.compile_count(filters=[predicate], scope=ctx.scope, principal=ctx.principal)
        _, rows = await _run(ctx, compiled)
        alone = _as_int(rows[0][0]) if rows else 0
        rendered = f"filters[{index}] {predicate.column} {predicate.op} {predicate.value!r}"
        notes.append(f"{rendered} -> {alone:,} rows in isolation.")
        if alone == 0:
            culprits.append(index)
            column = ctx.scope.columns[predicate.column]
            if column.sample_values:
                present = ", ".join(str(json_safe(v)) for v in column.sample_values[:8])
                notes.append(f"  values present in {predicate.column}: {present}")

    if culprits:
        which = ", ".join(str(i) for i in culprits)
        notes.append(f"Filter {which} eliminates everything on its own; start there.")
    elif filters:
        notes.append("Each filter matches rows alone; it is their combination that does not.")
    return notes


async def _guard_cardinality(ctx: ToolContext, columns: list[str], limit: int) -> None:
    """Refuse a hopeless group-by using the cached estimate, before querying."""
    for name in columns:
        column = ctx.scope.columns[name]
        if column.approx_distinct > limit:
            alternatives = sorted(
                (
                    (other.name, other.approx_distinct)
                    for other in ctx.scope.columns.values()
                    if 0 < other.approx_distinct <= limit
                ),
                key=lambda pair: -pair[1],
            )[:4]
            raise ArgumentError(
                f"grouping by {name!r} would produce about {column.approx_distinct:,} "
                f"groups (limit {limit:,}). For a time column use the timeseries tool "
                f"with a coarser grain; otherwise group by a lower-cardinality column: "
                + ", ".join(f"{n} ({c:,})" for n, c in alternatives)
                + ".",
                code="cardinality_exceeded",
                field="group_by",
                suggestions=[n for n, _ in alternatives],
            )


# ---------------------------------------------------------------- tools


async def count_rows(args: CountRowsArgs, ctx: ToolContext) -> ToolResult:
    compiled = sqlc.compile_count(filters=args.filters, scope=ctx.scope, principal=ctx.principal)
    _, rows = await _run(ctx, compiled)
    matched = _as_int(rows[0][0]) if rows else 0

    total_compiled = sqlc.compile_count(filters=[], scope=ctx.scope, principal=ctx.principal)
    _, total_rows = await _run(ctx, total_compiled)
    total = _as_int(total_rows[0][0]) if total_rows else 0

    notes: list[str] = []
    if matched == 0 and args.filters:
        notes = await _diagnose_empty(ctx, args.filters)
    elif total:
        notes = [f"{matched:,} of {total:,} rows ({matched / total:.1%})."]

    # Routed through the same bounding and caching path as every other tool.
    # A count is a result like any other: it gets a result_id, it is citable,
    # and nothing has to special-case it.
    return _bounded_result(
        "count_rows",
        compiled,
        ["matching_rows", "total_rows"],
        [[matched, total]],
        ctx,
        notes,
    )


async def aggregate(args: AggregateArgs, ctx: ToolContext) -> ToolResult:
    await _guard_cardinality(ctx, args.group_by, ctx.settings.max_group_cardinality)

    compiled = sqlc.compile_aggregate(
        metrics=args.metrics,
        group_by=args.group_by,
        filters=args.filters,
        having=args.having,
        order_by=args.order_by,
        limit=min(args.limit, ctx.settings.cache_max_rows),
        scope=ctx.scope,
        principal=ctx.principal,
    )
    names, rows = await _run(ctx, compiled)

    notes: list[str] = []
    if args.limit > ctx.settings.cache_max_rows:
        notes.append(f"limit clamped from {args.limit:,} to {ctx.settings.cache_max_rows:,}.")
    if not rows and args.filters:
        notes.extend(await _diagnose_empty(ctx, args.filters))

    return _bounded_result("aggregate", compiled, names, rows, ctx, notes)


async def top_n(args: TopNArgs, ctx: ToolContext) -> ToolResult:
    await _guard_cardinality(ctx, [args.dimension], ctx.settings.max_group_cardinality)

    alias = args.metric.default_alias()
    having = (
        [Having.model_construct(metric="row_count", op=">=", value=float(args.min_group_rows))]
        if args.min_group_rows
        else []
    )
    metrics = [args.metric]
    if args.min_group_rows and alias != "row_count":
        metrics = [*metrics, Metric.model_construct(op="count", column=None, alias="row_count")]

    compiled = sqlc.compile_aggregate(
        metrics=metrics,
        group_by=[args.dimension],
        filters=args.filters,
        having=having,
        order_by=[
            OrderBy.model_construct(
                key=alias, direction="desc" if args.direction == "top" else "asc"
            )
        ],
        limit=args.n,
        scope=ctx.scope,
        principal=ctx.principal,
    )
    names, rows = await _run(ctx, compiled)

    notes: list[str] = []
    if args.min_group_rows:
        notes.append(
            f"only groups with at least {args.min_group_rows:,} rows were considered, "
            "so a single outlier cannot win."
        )
    if not rows and args.filters:
        notes.extend(await _diagnose_empty(ctx, args.filters))

    return _bounded_result("top_n", compiled, names, rows, ctx, notes)


async def timeseries(args: TimeseriesArgs, ctx: ToolContext) -> ToolResult:
    if args.group_by:
        series_column = ctx.scope.columns[args.group_by]
        if series_column.approx_distinct > MAX_SERIES:
            raise ArgumentError(
                f"{args.group_by!r} has about {series_column.approx_distinct:,} distinct "
                f"values; a time series supports at most {MAX_SERIES} series. Filter it "
                "down first, or group by a coarser column.",
                code="cardinality_exceeded",
                field="group_by",
            )

    compiled = sqlc.compile_timeseries(
        time_column=args.time_column,
        grain=args.grain,
        metrics=args.metrics,
        filters=args.filters,
        group_by=args.group_by,
        limit=MAX_TIME_BUCKETS,
        scope=ctx.scope,
        principal=ctx.principal,
    )
    names, rows = await _run(ctx, compiled)

    notes: list[str] = []
    if len(rows) >= MAX_TIME_BUCKETS:
        notes.append(
            f"hit the {MAX_TIME_BUCKETS:,} bucket limit at grain={args.grain!r}; "
            "use a coarser grain or narrow the range."
        )
    if not rows and args.filters:
        notes.extend(await _diagnose_empty(ctx, args.filters))

    return _bounded_result("timeseries", compiled, names, rows, ctx, notes)


async def distribution(args: DistributionArgs, ctx: ToolContext) -> ToolResult:
    bounds = sqlc.compile_aggregate(
        metrics=[
            Metric.model_construct(op="min", column=args.column, alias="lo"),
            Metric.model_construct(
                op=_percentile_op(args.clip_percentile), column=args.column, alias="hi"
            ),
        ],
        group_by=[],
        filters=args.filters,
        having=[],
        order_by=[],
        limit=1,
        scope=ctx.scope,
        principal=ctx.principal,
    )
    _, bound_rows = await _run(ctx, bounds)
    if not bound_rows or bound_rows[0][0] is None:
        return ToolResult(
            tool="distribution",
            columns=[ResultColumn(name="bin_lower", type="number")],
            rows=[],
            row_count=0,
            notes=await _diagnose_empty(ctx, args.filters)
            if args.filters
            else ["no non-null values to bin."],
        )

    lower = _as_float(bound_rows[0][0])
    upper = _as_float(bound_rows[0][1])
    notes: list[str] = []
    if upper <= lower:
        upper = lower + 1.0
        notes.append("all values are identical; showing a single unit-wide bin.")
    elif args.clip_percentile < 100.0:
        notes.append(
            f"upper bound clipped at the {args.clip_percentile:g}th percentile "
            f"({upper:,.2f}); the tail beyond it is excluded from the bins."
        )

    compiled = sqlc.compile_histogram(
        column=args.column,
        bins=args.bins,
        filters=args.filters,
        lower=lower,
        upper=upper,
        scope=ctx.scope,
        principal=ctx.principal,
    )
    names, rows = await _run(ctx, compiled)
    return _bounded_result("distribution", compiled, names, rows, ctx, notes)


def _percentile_op(percentile: float) -> str:
    if percentile >= 100.0:
        return "max"
    if percentile >= 95.0:
        return "p95"
    if percentile >= 90.0:
        return "p90"
    if percentile >= 75.0:
        return "p75"
    return "median"


def _bounded_result(
    tool: str,
    compiled: sqlc.CompiledQuery,
    names: list[str],
    rows: list[list[JsonScalar]],
    ctx: ToolContext,
    notes: list[str],
) -> ToolResult:
    """Apply the two different bounds and record the difference.

    The model sees at most ``model_max_rows``; the cache holds up to
    ``cache_max_rows`` so a chart can render points the model never had to read.
    """
    total = len(rows)
    visible = rows[: ctx.settings.model_max_rows]
    truncated = total > len(visible)
    if truncated:
        notes = [
            *notes,
            f"showing {len(visible):,} of {total:,} rows to you; a chart of this "
            "result will render all of them.",
        ]

    result = ToolResult(
        tool=tool,
        columns=[
            ResultColumn(name=name, type=infer_result_type([row[i] for row in rows]))
            for i, name in enumerate(names)
        ],
        rows=visible,
        row_count=len(visible),
        truncated=truncated,
        total_groups=total if truncated else None,
        notes=notes,
    )
    # The cache keeps the full set so `plot` never has to re-query.
    full = result.model_copy(update={"rows": rows, "row_count": total})
    ctx.results.put(full)
    result.result_id = full.result_id
    return result
