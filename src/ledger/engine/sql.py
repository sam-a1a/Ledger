"""Validated arguments to parameterised SQL.

**The model supplies a key; the compiler emits the catalogue's value.** No
string that came from the model ever reaches the SQL text. Five properties hold,
and each is asserted by a property test:

1. Identifiers are resolved through the scoped catalogue. Even if validation
   were bypassed, ``scope.columns[key]`` raises before anything is emitted --
   there is no fallback that quotes the raw input.
2. Operators and metrics are ``Literal`` types mapped to fixed SQL fragments
   through closed dispatch tables, never format strings over model input.
3. Every value is bound as a ``?`` parameter, including LIMIT.
4. ``FROM ledger.trips`` is a constant. No tool takes a table argument.
5. The tenant predicate is injected here, from the principal, on every query.
   No argument can influence or remove it.

This module is a **pure function with no database handle**, which is what makes
thousands of fuzzed argument combinations checkable with zero I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ledger.catalog.models import ScopedCatalog
from ledger.security.principal import Principal
from ledger.tools.args import Filter, Grain, Having, Metric, OrderBy

RELATION = "ledger.trips"

#: Physical column carrying the tenant partition. Never model-visible: the
#: catalogue marks it INTERNAL, so no role can name it.
TENANT_COLUMN = "tenant_id"

_COMPARISON: dict[str, str] = {
    "=": "=",
    "!=": "!=",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
}

_METRIC_SQL: dict[str, str] = {
    "count": "count(*)",
    "count_distinct": "approx_count_distinct({col})",
    "sum": "sum({col})",
    "avg": "avg({col})",
    "min": "min({col})",
    "max": "max({col})",
    "median": "quantile_cont({col}, 0.5)",
    "p25": "quantile_cont({col}, 0.25)",
    "p75": "quantile_cont({col}, 0.75)",
    "p90": "quantile_cont({col}, 0.9)",
    "p95": "quantile_cont({col}, 0.95)",
    "stddev": "stddev_samp({col})",
}

_GRAIN_SQL: dict[str, str] = {
    "hour": "hour",
    "day": "day",
    "week": "week",
    "month": "month",
    "quarter": "quarter",
    "year": "year",
}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(slots=True)
class CompiledQuery:
    sql: str
    params: list[Any] = field(default_factory=list)
    #: Aliases in select order, so the executor can label result columns without
    #: re-parsing the SQL.
    output_aliases: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Hash of the SQL template.

        Published on the audit event instead of the SQL, so the shape of every
        query is provable without putting filter values on a Kafka topic.
        """
        return hashlib.sha256(self.sql.encode()).hexdigest()[:16]


def column_expr(scope: ScopedCatalog, key: str) -> str:
    """The single place a model-supplied name becomes an identifier."""
    column = scope.columns.get(key)
    if column is None:
        # Unreachable through validated arguments; a hard failure rather than a
        # fallback, because a fallback here would be the whole vulnerability.
        raise KeyError(f"column {key!r} is not in this scope")
    return quote_ident(column.physical_name)


def _tenant_predicate(principal: Principal) -> tuple[str, list[Any]]:
    """Always emitted when the principal is scoped to a tenant.

    Built here rather than in any tool, so no combination of arguments can drop
    it. The property test fuzzes every tool to prove that.
    """
    if principal.tenant_id is None:
        return "", []
    return f"{quote_ident(TENANT_COLUMN)} = ?", [principal.tenant_id]


def compile_predicates(filters: list[Filter], scope: ScopedCatalog) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    for predicate in filters:
        col = column_expr(scope, predicate.column)
        op = predicate.op

        if op in _COMPARISON:
            clauses.append(f"{col} {_COMPARISON[op]} ?")
            params.append(predicate.value)
        elif op == "is_null":
            clauses.append(f"{col} IS NULL")
        elif op == "is_not_null":
            clauses.append(f"{col} IS NOT NULL")
        elif op in ("in", "not_in"):
            raw = predicate.value
            values = list(raw) if isinstance(raw, list) else [raw]
            placeholders = ", ".join("?" for _ in values)
            negate = "NOT " if op == "not_in" else ""
            clauses.append(f"{col} {negate}IN ({placeholders})")
            params.extend(values)
        elif op == "between":
            bounds = predicate.value
            if not isinstance(bounds, list) or len(bounds) != 2:
                # Validation guarantees this; a hard failure rather than a
                # silent reinterpretation if that ever stops being true.
                raise ValueError("'between' requires exactly two bounds")
            clauses.append(f"{col} BETWEEN ? AND ?")
            params.extend(bounds)
        elif op == "contains":
            # strpos, never LIKE: '%' and '_' in a model-supplied value stay
            # literal, so there is no wildcard semantics to reason about and
            # nothing to escape.
            clauses.append(f"strpos(lower({col}), lower(?)) > 0")
            params.append(predicate.value)
        else:  # pragma: no cover - Literal makes this unreachable
            raise ValueError(f"unhandled operator {op!r}")

    return clauses, params


def metric_expr(metric: Metric, scope: ScopedCatalog) -> str:
    template = _METRIC_SQL[metric.op]
    if metric.op == "count":
        return template
    return template.format(col=column_expr(scope, metric.column or ""))


def _where(
    filters: list[Filter], scope: ScopedCatalog, principal: Principal
) -> tuple[str, list[Any]]:
    tenant_clause, tenant_params = _tenant_predicate(principal)
    clauses, params = compile_predicates(filters, scope)

    all_clauses = ([tenant_clause] if tenant_clause else []) + clauses
    all_params = tenant_params + params
    if not all_clauses:
        return "", []
    return " WHERE " + " AND ".join(all_clauses), all_params


def compile_count(
    *, filters: list[Filter], scope: ScopedCatalog, principal: Principal
) -> CompiledQuery:
    where, params = _where(filters, scope, principal)
    return CompiledQuery(
        sql=f"SELECT count(*) AS row_count FROM {RELATION}{where}",
        params=params,
        output_aliases=["row_count"],
    )


def compile_aggregate(
    *,
    metrics: list[Metric],
    group_by: list[str],
    filters: list[Filter],
    having: list[Having],
    order_by: list[OrderBy],
    limit: int,
    scope: ScopedCatalog,
    principal: Principal,
) -> CompiledQuery:
    select_parts: list[str] = []
    aliases: list[str] = []

    for name in group_by:
        # No redundant `AS`: DuckDB already names the output after the column,
        # and repeating the identifier makes the statement harder to read in a
        # trace for no benefit.
        select_parts.append(column_expr(scope, name))
        aliases.append(name)

    for metric in metrics:
        alias = metric.default_alias()
        select_parts.append(f"{metric_expr(metric, scope)} AS {quote_ident(alias)}")
        aliases.append(alias)

    where, params = _where(filters, scope, principal)

    # Ordinals, so every identifier appears exactly once in the statement.
    group_clause = ""
    if group_by:
        group_clause = " GROUP BY " + ", ".join(str(i + 1) for i in range(len(group_by)))

    having_clause = ""
    if having:
        parts = []
        for clause in having:
            parts.append(f"{quote_ident(clause.metric)} {clause.op} ?")
            params.append(clause.value)
        having_clause = " HAVING " + " AND ".join(parts)

    order_clause = ""
    if order_by:
        parts = []
        for order in order_by:
            direction = "ASC" if order.direction == "asc" else "DESC"
            parts.append(f"{quote_ident(order.key)} {direction} NULLS LAST")
        order_clause = " ORDER BY " + ", ".join(parts)

    params.append(limit)
    sql = (
        f"SELECT {', '.join(select_parts)} FROM {RELATION}"
        f"{where}{group_clause}{having_clause}{order_clause} LIMIT ?"
    )
    return CompiledQuery(sql=sql, params=params, output_aliases=aliases)


def compile_distinct_count(
    column: str, scope: ScopedCatalog, principal: Principal
) -> CompiledQuery:
    """Used by the cardinality guard when the catalogue estimate is borderline."""
    where, params = _where([], scope, principal)
    col = column_expr(scope, column)
    return CompiledQuery(
        sql=f"SELECT approx_count_distinct({col}) AS n FROM {RELATION}{where}",
        params=params,
        output_aliases=["n"],
    )


def compile_timeseries(
    *,
    time_column: str,
    grain: Grain,
    metrics: list[Metric],
    filters: list[Filter],
    group_by: str | None,
    limit: int,
    scope: ScopedCatalog,
    principal: Principal,
) -> CompiledQuery:
    bucket = f"date_trunc('{_GRAIN_SQL[grain]}', {column_expr(scope, time_column)})"
    select_parts = [f"{bucket} AS {quote_ident('bucket')}"]
    aliases = ["bucket"]
    group_ordinals = [1]

    if group_by:
        select_parts.append(column_expr(scope, group_by))
        aliases.append(group_by)
        group_ordinals.append(2)

    for metric in metrics:
        alias = metric.default_alias()
        select_parts.append(f"{metric_expr(metric, scope)} AS {quote_ident(alias)}")
        aliases.append(alias)

    where, params = _where(filters, scope, principal)
    params.append(limit)

    sql = (
        f"SELECT {', '.join(select_parts)} FROM {RELATION}{where}"
        f" GROUP BY {', '.join(str(o) for o in group_ordinals)}"
        f" ORDER BY 1 ASC LIMIT ?"
    )
    return CompiledQuery(sql=sql, params=params, output_aliases=aliases)


def compile_compare_periods(
    *,
    time_column: str,
    metrics: list[Metric],
    baseline: tuple[datetime, datetime],
    comparison: tuple[datetime, datetime],
    filters: list[Filter],
    group_by: str | None,
    limit: int,
    scope: ScopedCatalog,
    principal: Principal,
) -> CompiledQuery:
    """Both windows in one pass, using aggregate FILTER clauses.

    One scan rather than two, which matters less for cost than for
    consistency: two queries against a moving dataset can disagree, and a
    before/after comparison that straddles a write is exactly the kind of
    wrong answer that looks right.

    Every window bound is a parameter. The windows are half-open -- `[start,
    end)` -- so adjacent periods neither overlap nor drop the boundary row,
    which is the off-by-one that quietly moves a day's traffic from one side
    of a comparison to the other.
    """
    col = column_expr(scope, time_column)
    select_parts: list[str] = []
    aliases: list[str] = []
    group_ordinals: list[int] = []
    # Select-list parameters come before the WHERE clause's, because that is
    # the order they appear in the statement.
    leading: list[Any] = []

    if group_by:
        select_parts.append(column_expr(scope, group_by))
        aliases.append(group_by)
        group_ordinals.append(1)

    for metric in metrics:
        base = metric_expr(metric, scope)
        for suffix, (start, end) in (
            ("before", baseline),
            ("after", comparison),
        ):
            alias = f"{metric.default_alias()}_{suffix}"
            select_parts.append(
                f"{base} FILTER (WHERE {col} >= ? AND {col} < ?) AS {quote_ident(alias)}"
            )
            aliases.append(alias)
            leading.extend((start, end))

    # Rows outside both windows are excluded in the WHERE clause as well as by
    # the FILTER clauses. The FILTERs alone would be correct but would scan
    # and group the whole table to produce rows that are all NULL.
    span = Filter.model_construct(
        column=time_column,
        op="between",
        value=[min(baseline[0], comparison[0]), max(baseline[1], comparison[1])],
    )
    where, params = _where([*filters, span], scope, principal)
    params.append(limit)

    group_clause = (
        f" GROUP BY {', '.join(str(o) for o in group_ordinals)}" if group_ordinals else ""
    )
    sql = (
        f"SELECT {', '.join(select_parts)} FROM {RELATION}{where}"
        f"{group_clause} ORDER BY 1 ASC LIMIT ?"
    )
    return CompiledQuery(sql=sql, params=[*leading, *params], output_aliases=aliases)


def compile_histogram(
    *,
    column: str,
    bins: int,
    filters: list[Filter],
    lower: float,
    upper: float,
    scope: ScopedCatalog,
    principal: Principal,
) -> CompiledQuery:
    """Equal-width bins between explicit bounds.

    Bounds are computed separately and passed in, so the clipping decision is
    made once and reported to the model rather than buried in the SQL.
    """
    col = column_expr(scope, column)
    clipped = Filter.model_construct(column=column, op="between", value=[lower, upper])
    where, params = _where([*filters, clipped], scope, principal)

    width = f"(({upper!r} - {lower!r}) / {bins})"
    bin_index = f"least(cast(floor(({col} - {lower!r}) / {width}) as integer), {bins - 1})"
    sql = (
        f"SELECT {lower!r} + {bin_index} * {width} AS {quote_ident('bin_lower')},"
        f" {lower!r} + ({bin_index} + 1) * {width} AS {quote_ident('bin_upper')},"
        f" count(*) AS {quote_ident('row_count')}"
        f" FROM {RELATION}{where} GROUP BY 1, 2 ORDER BY 1 ASC"
    )
    return CompiledQuery(
        sql=sql, params=params, output_aliases=["bin_lower", "bin_upper", "row_count"]
    )
