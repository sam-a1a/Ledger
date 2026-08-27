"""What a tool hands back.

Two envelopes, and the error one carries as much design weight as the success
one. A tool failure is a *value* the model reads and retries against, not an
exception -- so every error names the correction rather than merely reporting
that something went wrong.
"""

from __future__ import annotations

import difflib
import uuid
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

JsonScalar = str | int | float | bool | None


def new_result_id() -> str:
    # Prefixed so a hallucinated or stale handle is obvious in a trace.
    return f"r_{uuid.uuid7().hex[:16]}"  # type: ignore[attr-defined,unused-ignore]


def suggest_columns(name: str, available: list[str], limit: int = 3) -> list[str]:
    """Find plausible corrections for a name the caller invented.

    Pure edit distance is too strict for how models actually get names wrong:
    `tip_pct` scores about 0.47 against `tip_amount`, well under any sensible
    cutoff, yet it is obviously what was meant. So token overlap and substring
    containment are considered too -- and since the suggestions come only from
    the caller's scoped names, a better suggester never widens what they can
    discover.
    """
    ranked: dict[str, float] = {}

    for candidate, score in zip(
        difflib.get_close_matches(name, available, n=limit, cutoff=0.6),
        (1.0, 0.9, 0.8),
        strict=False,
    ):
        ranked[candidate] = score

    tokens = {part for part in name.lower().split("_") if len(part) > 2}
    for candidate in available:
        candidate_tokens = set(candidate.lower().split("_"))
        shared = tokens & candidate_tokens
        if shared:
            ranked[candidate] = max(ranked.get(candidate, 0.0), 0.5 + 0.1 * len(shared))
        elif name.lower() in candidate.lower() or candidate.lower() in name.lower():
            ranked[candidate] = max(ranked.get(candidate, 0.0), 0.4)
        elif any(
            len(a) >= 3 and (a.startswith(b) or b.startswith(a))
            for a in tokens
            for b in candidate_tokens
            if len(b) >= 3
        ):
            # Catches a pluralised or truncated token: `tips` for `tip_amount`.
            ranked[candidate] = max(ranked.get(candidate, 0.0), 0.35)

    return [c for c, _ in sorted(ranked.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


class ErrorCode(StrEnum):
    UNKNOWN_COLUMN = "unknown_column"
    UNKNOWN_METRIC = "unknown_metric"
    INVALID_ARGUMENT = "invalid_argument"
    TYPE_MISMATCH = "type_mismatch"
    CARDINALITY_EXCEEDED = "cardinality_exceeded"
    RESULT_TOO_LARGE = "result_too_large"
    TIMEOUT = "timeout"
    #: The governance log could not record the call, so it did not happen.
    AUDIT_UNAVAILABLE = "audit_unavailable"
    INTERNAL = "internal"


#: The value types a bounded result can carry back to the model.
ResultType = Literal["integer", "number", "string", "boolean", "timestamp", "date"]


class ResultColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: ResultType


class ToolResult(BaseModel):
    """A bounded, structured answer. Never raw rows from the dataset."""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    result_id: str = Field(default_factory=new_result_id)
    tool: str
    columns: list[ResultColumn]
    #: Row-oriented with a separate header: roughly 40% fewer tokens than a list
    #: of dicts on a 100-row result, and it makes truncation explicit.
    rows: list[list[JsonScalar]]
    row_count: int
    truncated: bool = False
    #: The true group count when the result was truncated.
    total_groups: int | None = None
    duration_ms: int = 0
    #: Anything the model should know that is not an error: a clamped limit, a
    #: clipped tail, or why zero rows matched.
    notes: list[str] = Field(default_factory=list)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


class ToolError(BaseModel):
    """A failure the model can act on.

    ``message`` is one sentence, imperative, and names the fix. ``suggestions``
    are drawn only from what the caller's role can see, so an error can never be
    used to discover a column that was hidden from them.
    """

    model_config = ConfigDict(extra="forbid")

    ok: Literal[False] = False
    error: ErrorCode
    message: str
    #: A path into the arguments, e.g. ``metrics[0].column``.
    field: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    retryable: bool = True
    tool: str | None = None

    @classmethod
    def unknown_column(
        cls,
        name: str,
        *,
        available: list[str],
        field: str | None = None,
        tool: str | None = None,
        of: str = "this dataset",
    ) -> Self:
        """The error a hidden column produces, identical to the one a typo produces.

        ``available`` must already be role-scoped. Suggesting a restricted name
        here would turn every error into an oracle for what is hidden.
        """
        close = suggest_columns(name, available)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return cls(
            error=ErrorCode.UNKNOWN_COLUMN,
            message=(
                f"{name!r} is not a column of {of}.{hint} "
                + (
                    f"Call list_columns to see all {len(available)}."
                    if of == "this dataset"
                    else f"It has: {', '.join(available)}."
                )
            ),
            field=field,
            suggestions=close,
            tool=tool,
        )

    @classmethod
    def unknown_metric(cls, op: str, *, available: list[str], field: str | None = None) -> Self:
        return cls(
            error=ErrorCode.UNKNOWN_METRIC,
            message=(
                f"{op!r} is not an implemented metric. Available: {', '.join(sorted(available))}."
            ),
            field=field,
            suggestions=sorted(available),
        )

    @classmethod
    def type_mismatch(
        cls, message: str, *, field: str | None = None, suggestions: list[str] | None = None
    ) -> Self:
        return cls(
            error=ErrorCode.TYPE_MISMATCH,
            message=message,
            field=field,
            suggestions=suggestions or [],
        )

    @classmethod
    def invalid_argument(
        cls, message: str, *, field: str | None = None, suggestions: list[str] | None = None
    ) -> Self:
        return cls(
            error=ErrorCode.INVALID_ARGUMENT,
            message=message,
            field=field,
            suggestions=suggestions or [],
        )

    @classmethod
    def cardinality_exceeded(
        cls, column: str, *, estimated: int, limit: int, alternatives: list[tuple[str, int]]
    ) -> Self:
        """Refused before the query runs, from the catalogue's cached estimate.

        The naive implementation executes it and dies; this one costs a dict
        lookup and gives the model a better answer than the query would have.
        """
        options = ", ".join(f"{name} ({count:,})" for name, count in alternatives)
        return cls(
            error=ErrorCode.CARDINALITY_EXCEEDED,
            message=(
                f"grouping by {column!r} would produce about {estimated:,} groups "
                f"(limit {limit:,}). For a time column use the timeseries tool with a "
                f"coarser grain; otherwise group by a lower-cardinality column: {options}."
            ),
            field="group_by",
            suggestions=[name for name, _ in alternatives],
        )

    @classmethod
    def audit_unavailable(cls, detail: str) -> Self:
        """The governance log is down, so the query was not executed.

        Deliberately retryable: the request was well-formed, and nothing about
        it needs to change for it to succeed once the log is reachable.
        """
        return cls(
            error=ErrorCode.AUDIT_UNAVAILABLE,
            message=(
                "The audit log is unavailable, so this query was not executed. "
                f"Retry shortly. ({detail})"
            ),
            retryable=True,
        )


ToolOutcome = ToolResult | ToolError


def duckdb_type_to_result_type(duckdb_type: str) -> ResultType:
    upper = duckdb_type.upper()
    if upper.startswith("BOOLEAN"):
        return "boolean"
    if upper.startswith("DATE"):
        return "date"
    if upper.startswith(("TIMESTAMP", "TIME")):
        return "timestamp"
    if upper.startswith(("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT")):
        return "integer"
    if upper.startswith(("FLOAT", "DOUBLE", "DECIMAL", "REAL")):
        return "number"
    return "string"


def infer_result_type(values: list[JsonScalar]) -> ResultType:
    """Infer a column's type from the values actually returned.

    The frontend picks axis formatting from this, so getting it wrong renders a
    count as a date or a timestamp as a category. Inferring from data is more
    reliable here than threading DuckDB types through every aggregate, since a
    metric's type depends on the metric, not on its input column.
    """
    seen = [v for v in values if v is not None]
    if not seen:
        return "string"
    if all(isinstance(v, bool) for v in seen):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return "integer"
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in seen):
        return "number"
    if all(isinstance(v, str) and _looks_like_timestamp(v) for v in seen):
        return "timestamp"
    if all(isinstance(v, str) and _looks_like_date(v) for v in seen):
        return "date"
    return "string"


def _looks_like_date(value: str) -> bool:
    return len(value) == 10 and value[4] == "-" and value[7] == "-"


def _looks_like_timestamp(value: str) -> bool:
    return len(value) > 10 and value[4:5] == "-" and "T" in value


def json_safe(value: Any) -> JsonScalar:
    """Coerce a DuckDB value into something JSON can carry."""
    import datetime
    import decimal

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    return str(value)
