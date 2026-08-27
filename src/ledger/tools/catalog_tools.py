"""Tools that read the catalogue. These never touch DuckDB.

The model orients itself here rather than by sampling rows, which is the whole
reason the enrichment pass exists.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints, ValidationInfo, field_validator

from ledger.catalog.models import SemanticType
from ledger.tools.args import _Scoped, resolve_column, scope_from
from ledger.tools.context import ToolContext
from ledger.tools.results import JsonScalar, ResultColumn, ToolResult, json_safe


class ListColumnsArgs(_Scoped):
    """Optional filters, so a wide catalogue can be explored in pieces."""

    contains: Annotated[str, StringConstraints(max_length=64)] | None = None
    semantic_type: SemanticType | None = None


class DescribeColumnArgs(_Scoped):
    column: str

    @field_validator("column")
    @classmethod
    def _exists(cls, value: str, info: ValidationInfo) -> str:
        return resolve_column(value, scope_from(info), "column")


async def list_columns(args: ListColumnsArgs, ctx: ToolContext) -> ToolResult:
    scope = ctx.scope
    names = scope.names()

    if args.contains:
        needle = args.contains.lower()
        names = [n for n in names if needle in n.lower()]
    if args.semantic_type:
        names = [n for n in names if scope.columns[n].semantic_type is args.semantic_type]

    rows: list[list[JsonScalar]] = []
    for name in names:
        column = scope.columns[name]
        description = column.description
        rows.append(
            [
                name,
                column.semantic_type.value,
                round(column.null_fraction, 4),
                column.approx_distinct,
                description.text if description else "",
                description.source.value if description else "",
            ]
        )

    return ToolResult(
        tool="list_columns",
        columns=[
            ResultColumn(name="column", type="string"),
            ResultColumn(name="semantic_type", type="string"),
            ResultColumn(name="null_fraction", type="number"),
            ResultColumn(name="approx_distinct", type="integer"),
            ResultColumn(name="description", type="string"),
            ResultColumn(name="description_source", type="string"),
        ],
        rows=rows,
        row_count=len(rows),
        notes=[
            f"{len(rows)} of {len(scope.names())} columns shown."
            if len(rows) != len(scope.names())
            else f"{len(rows)} columns available to you."
        ],
    )


async def describe_column(args: DescribeColumnArgs, ctx: ToolContext) -> ToolResult:
    column = ctx.scope.columns[args.column]
    description = column.description

    facts: list[tuple[str, object]] = [
        ("column", column.name),
        ("semantic_type", column.semantic_type.value),
        ("storage_type", column.duckdb_type),
        ("null_fraction", round(column.null_fraction, 4)),
        ("approx_distinct", column.approx_distinct),
        ("min", json_safe(column.min_value)),
        ("max", json_safe(column.max_value)),
        ("p01", column.p01),
        ("p50", column.p50),
        ("p99", column.p99),
        ("sample_values", ", ".join(str(json_safe(v)) for v in column.sample_values)),
        ("description", description.text if description else ""),
        ("description_source", description.source.value if description else ""),
        ("unit", description.unit if description else None),
        ("caveat", description.caveat if description else None),
    ]

    return ToolResult(
        tool="describe_column",
        columns=[
            ResultColumn(name="property", type="string"),
            ResultColumn(name="value", type="string"),
        ],
        rows=[[name, json_safe(value)] for name, value in facts if value not in (None, "")],
        row_count=len(facts),
    )
