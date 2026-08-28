"""Context-free mirrors of the tool argument shapes, for the MCP wire.

``@mcp.tool()`` validates arguments into the annotated model *before* the
handler runs, and it has no way to supply Pydantic's validation context. The
real argument models read the caller's scoped catalogue out of that context, so
using them directly here fails every call with "arguments were validated
without a scoped catalogue" -- while the tool list still looks perfectly healthy.

These mirrors carry the same fields and the same enums, so the published schema
stays useful, but no validators. Authoritative validation still happens in the
executor, against the caller's scope, exactly as it does over HTTP.

``tests/mcp/test_mcp_parity.py`` asserts these fields match the real models, so
the mirrors cannot drift.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ledger.tools.args import FilterOp, Grain, MetricOp
from ledger.tools.results import JsonScalar


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterArg(_Wire):
    """One predicate. Combined with AND against the others."""

    column: str
    op: FilterOp
    value: JsonScalar | list[JsonScalar] = None


class MetricArg(_Wire):
    """One aggregate. `column` is required for every op except `count`."""

    op: MetricOp
    column: str | None = None
    alias: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,30}$")] | None = None


class OrderByArg(_Wire):
    """Sort key, naming a group column or a metric alias of this result."""

    key: str
    direction: Literal["asc", "desc"] = "desc"


class HavingArg(_Wire):
    """A filter applied after aggregation, against a metric alias."""

    metric: str
    op: Literal["=", "!=", "<", "<=", ">", ">="]
    value: float


class ChartSpecArg(_Wire):
    """Chart kind and encoding. Never a rendering-library option object."""

    kind: Literal["bar", "line", "area", "scatter", "pie", "heatmap"]
    x: str
    y: list[str] = Field(min_length=1, max_length=6)
    series: str | None = None
    title: Annotated[str, StringConstraints(max_length=120)]
    x_label: str | None = None
    y_label: str | None = None
    stacked: bool = False
    sort: Literal["none", "x_asc", "y_desc"] = "none"


GrainArg = Grain


class WindowArg(_Wire):
    """A half-open time window, `[start, end)`.

    Half-open so two adjacent windows neither overlap nor drop the row on the
    boundary between them.
    """

    start: datetime
    end: datetime
