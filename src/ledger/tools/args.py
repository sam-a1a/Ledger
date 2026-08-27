"""Typed tool arguments.

The centrepiece of the project. Three properties do the work:

* **Every model is ``extra="forbid"``.** An unknown key is a clean, named error
  rather than a silently ignored one -- silently ignoring it is how a model ends
  up confidently reporting the answer to a question it did not ask.
* **Column names validate against a role-scoped catalogue**, passed in through
  Pydantic's validation context. A column the role cannot see does not resolve,
  and the resulting error is indistinguishable from a typo.
* **Filters are inline on every tool.** There is no ``filter_rows`` and no
  filter handle, so each call is a pure function of (arguments, principal) --
  independently auditable, independently retryable, and readable in a trace
  without cross-referencing anything.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ledger.catalog.models import ScopedCatalog, SemanticType
from ledger.tools.results import JsonScalar

#: Argument validation reads the caller's scoped catalogue from here. Building a
#: ToolContext without one is impossible, so "forgot to scope it" cannot happen.
SCOPE_CONTEXT_KEY = "scope"

MAX_IN_VALUES = 100
MAX_GROUP_BY = 3
MAX_METRICS = 8

Alias = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,30}$")]

FilterOp = Literal[
    "=",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "in",
    "not_in",
    "between",
    "is_null",
    "is_not_null",
    "contains",
]

MetricOp = Literal[
    "count",
    "count_distinct",
    "sum",
    "avg",
    "min",
    "max",
    "median",
    "p25",
    "p75",
    "p90",
    "p95",
    "stddev",
]

Grain = Literal["hour", "day", "week", "month", "quarter", "year"]

#: Metrics that are arithmetic and therefore need a numeric column.
ARITHMETIC_OPS: frozenset[str] = frozenset(
    {"sum", "avg", "median", "p25", "p75", "p90", "p95", "stddev"}
)
#: Metrics that work on any column type.
ANY_TYPE_OPS: frozenset[str] = frozenset({"count", "count_distinct", "min", "max"})

#: Operators that only make sense on an ordered column.
ORDERED_OPS: frozenset[str] = frozenset({"<", "<=", ">", ">=", "between"})
#: Operators that only make sense on text.
TEXT_OPS: frozenset[str] = frozenset({"contains"})
#: Operators that take no value at all.
NULLARY_OPS: frozenset[str] = frozenset({"is_null", "is_not_null"})

ORDERED_TYPES = frozenset({SemanticType.NUMERIC, SemanticType.TEMPORAL})
TEXT_TYPES = frozenset({SemanticType.TEXT, SemanticType.CATEGORICAL})


def _is_text_storage(duckdb_type: str) -> bool:
    """Whether a substring match is a meaningful question for this column."""
    return duckdb_type.upper().startswith(("VARCHAR", "TEXT", "STRING", "CHAR"))


class ArgumentError(ValueError):
    """Raised inside validation; converted to a ``ToolError`` by the executor."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_argument",
        field: str | None = None,
        suggestions: list[str] | None = None,
        column: str | None = None,
        available: list[str] | None = None,
        context_label: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.suggestions = suggestions or []
        #: The offending name and the names that *were* valid, carried
        #: structurally. The executor used to recover these by string-parsing
        #: the message, which silently produced a nonsense error whenever the
        #: valid set was not the dataset -- as it is for `plot`, whose columns
        #: are those of a previous result.
        self.column = column
        self.available = available
        #: What the ``available`` set belongs to, for the error message.
        self.context_label = context_label or "this dataset"


def scope_from(info: ValidationInfo) -> ScopedCatalog:
    context = info.context or {}
    scope = context.get(SCOPE_CONTEXT_KEY)
    if scope is None:
        raise ArgumentError(
            "internal: arguments were validated without a scoped catalogue",
            code="internal",
        )
    if not isinstance(scope, ScopedCatalog):
        raise ArgumentError(
            "internal: validation context carried the wrong scope type", code="internal"
        )
    return scope


def resolve_column(name: str, scope: ScopedCatalog, field: str) -> str:
    """Resolve a model-supplied column name, or fail the way a typo fails.

    ``column`` and ``available`` are carried on the exception rather than left
    to be recovered from its message, so the executor can build a suggestion
    list without parsing prose. ``available`` is the *scoped* set, which is why
    a better suggester can never widen what a caller discovers.
    """
    if scope.get(name) is None:
        raise ArgumentError(
            f"unknown column {name!r}",
            code="unknown_column",
            field=field,
            column=name,
            available=scope.names(),
        )
    return name


class _Scoped(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(_Scoped):
    """One predicate. Carried inline by every analytical tool."""

    column: str
    op: FilterOp
    value: JsonScalar | list[JsonScalar] = None

    @field_validator("column")
    @classmethod
    def _column_exists(cls, value: str, info: ValidationInfo) -> str:
        return resolve_column(value, scope_from(info), "filters[].column")

    @model_validator(mode="after")
    def _op_suits_the_column(self, info: ValidationInfo) -> Self:
        scope = scope_from(info)
        column = scope.columns[self.column]

        if self.op in NULLARY_OPS:
            if self.value is not None:
                raise ArgumentError(
                    f"{self.op!r} takes no value; drop the 'value' field.",
                    field="filters[].value",
                )
            return self

        if self.op in TEXT_OPS and not _is_text_storage(column.duckdb_type):
            # Semantic type is not enough: pickup_location_id is categorical but
            # stored as an integer, and a substring match on a number is a
            # meaningless question that DuckDB would happily answer.
            raise ArgumentError(
                f"operator 'contains' applies to text columns; {self.column!r} is stored "
                f"as {column.duckdb_type}. Use '=' or 'in' for an exact match.",
                code="type_mismatch",
                field="filters[].op",
                suggestions=["=", "in"],
            )

        if self.op in ORDERED_OPS and column.semantic_type not in ORDERED_TYPES:
            raise ArgumentError(
                f"operator {self.op!r} needs an ordered column; {self.column!r} is "
                f"{column.semantic_type.value}. Use '=' or 'in'.",
                code="type_mismatch",
                field="filters[].op",
                suggestions=["=", "in"],
            )

        if self.op in ("in", "not_in"):
            if not isinstance(self.value, list):
                raise ArgumentError(f"{self.op!r} needs a list of values.", field="filters[].value")
            if not self.value:
                raise ArgumentError(
                    f"{self.op!r} needs at least one value.", field="filters[].value"
                )
            if len(self.value) > MAX_IN_VALUES:
                raise ArgumentError(
                    f"{self.op!r} accepts at most {MAX_IN_VALUES} values; got "
                    f"{len(self.value)}. Filter on a range instead.",
                    field="filters[].value",
                )
            return self

        if self.op == "between":
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ArgumentError(
                    "'between' needs exactly two values, [start, end].",
                    field="filters[].value",
                )
            low, high = self.value
            if low is None or high is None:
                raise ArgumentError("'between' bounds cannot be null.", field="filters[].value")
            if _out_of_order(low, high):
                raise ArgumentError(
                    f"'between' on {self.column!r} needs start <= end; got "
                    f"[{low!r}, {high!r}]. Swap them.",
                    field="filters[].value",
                )
            return self

        if isinstance(self.value, list):
            raise ArgumentError(
                f"operator {self.op!r} takes a single value, not a list.",
                field="filters[].value",
            )
        if self.value is None:
            raise ArgumentError(
                f"operator {self.op!r} needs a value; use 'is_null' to test for missing.",
                field="filters[].value",
            )
        return self


def _out_of_order(low: Any, high: Any) -> bool:
    """Whether a range is inverted.

    Bounds are compared only against bounds of the same kind -- two timestamps,
    two numbers, or two strings. Mixed pairs are left alone rather than guessed
    at: DuckDB will reject them with a clearer message than we could invent.
    """
    low_dt, high_dt = _as_datetime(low), _as_datetime(high)
    if low_dt is not None and high_dt is not None:
        return low_dt > high_dt

    low_num, high_num = _as_number(low), _as_number(high)
    if low_num is not None and high_num is not None:
        return low_num > high_num

    if isinstance(low, str) and isinstance(high, str):
        return low > high
    return False


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


class Metric(_Scoped):
    """One aggregate to compute."""

    op: MetricOp
    column: str | None = None
    alias: Alias | None = None

    @model_validator(mode="after")
    def _column_suits_the_metric(self, info: ValidationInfo) -> Self:
        scope = scope_from(info)

        if self.op == "count":
            return self

        if self.column is None:
            raise ArgumentError(
                f"metric {self.op!r} needs a column; only 'count' may omit one.",
                field="metrics[].column",
            )

        resolve_column(self.column, scope, "metrics[].column")
        column = scope.columns[self.column]

        if self.op in ARITHMETIC_OPS and column.semantic_type is not SemanticType.NUMERIC:
            numeric = scope.numeric_names()
            raise ArgumentError(
                f"metric {self.op!r} needs a numeric column; {self.column!r} is "
                f"{column.semantic_type.value}.",
                code="type_mismatch",
                field="metrics[].column",
                suggestions=numeric[:8],
            )
        return self

    def default_alias(self) -> str:
        if self.alias:
            return self.alias
        if self.op == "count":
            return "row_count"
        return f"{self.op}_{self.column}"


class OrderBy(_Scoped):
    """Sort key. Resolved against the result's own columns, not the dataset."""

    key: str
    direction: Literal["asc", "desc"] = "desc"


class Having(_Scoped):
    """A post-aggregation filter, expressed against a metric alias."""

    metric: str
    op: Literal["=", "!=", "<", "<=", ">", ">="]
    value: float


class ChartSpec(_Scoped):
    """What the model chooses about a chart.

    Deliberately *not* an ECharts option object. The model picks the kind and
    the encoding; the frontend owns the rendering. That means the model cannot
    hallucinate a library option, and swapping charting libraries touches one
    file.
    """

    kind: Literal["bar", "line", "area", "scatter", "pie", "heatmap"]
    x: str
    y: list[str] = Field(min_length=1, max_length=6)
    series: str | None = None
    title: Annotated[str, StringConstraints(max_length=120)]
    x_label: str | None = None
    y_label: str | None = None
    stacked: bool = False
    sort: Literal["none", "x_asc", "y_desc"] = "none"
