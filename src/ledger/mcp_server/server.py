"""An MCP server over the same tool registry the chat application uses.

Two things about this file are deliberate.

**Eight hand-written wrappers, not metaprogramming.** ``@mcp.tool()`` builds its
schema from type hints, so it needs real signatures. Generating them dynamically
is exactly the kind of clever code that is plausible and wrong: mypy cannot
check it, the resulting schema is invisible in the source, and a mismatch
surfaces only as a confused model. ``test_mcp_parity`` guards the drift instead.

**Nothing may write to stdout.** Under the stdio transport, stdout *is* the
JSON-RPC channel; one stray byte corrupts the framing and the client reports an
opaque disconnect. Every log handler is pinned to stderr, and a test asserts it.

MCP calls are audited identically to HTTP ones, with ``channel="mcp"``. Tool
access that bypasses the chat application needs *more* auditing, not less, so
this process fails fast without a broker exactly as the API does.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer

from ledger.api.state import AppState
from ledger.catalog.scope import scope_catalog
from ledger.config import Settings, get_settings
from ledger.logging import configure_logging, get_logger
from ledger.security.principal import Channel, Principal, Role
from ledger.tools.args import ChartSpec, Filter, Having, Metric, OrderBy
from ledger.tools.context import ResultCache, ToolContext
from ledger.tools.executor import execute

log = get_logger(__name__)

mcp = MCPServer("Ledger")

_state: AppState | None = None
#: One cache for the process. An MCP client has no conversation boundary, so
#: `plot` reads results from earlier calls in the same session.
_results = ResultCache()


def _principal(settings: Settings) -> Principal:
    """Who an MCP caller is.

    Defaults to ``viewer``. A stdio client has no auth layer, so the safe
    default is the restrictive one; opting up is an explicit env change that
    shows in the Claude Desktop config.
    """
    return Principal(
        subject=f"mcp-{settings.mcp_role}",
        role=Role(settings.mcp_role),
        channel=Channel.MCP,
        tenant_id=int(settings.mcp_tenant) if settings.mcp_tenant else None,
    )


@asynccontextmanager
async def _context() -> AsyncIterator[ToolContext]:
    if _state is None:  # pragma: no cover - guarded by main()
        raise RuntimeError("MCP server was not initialised")
    principal = _principal(_state.settings)
    with _state.engine.cursor() as cursor:
        yield ToolContext(
            principal=principal,
            scope=scope_catalog(_state.catalog, principal),
            cursor=cursor,
            publisher=_state.publisher,
            settings=_state.settings,
            results=_results,
            conversation_id="mcp",
        )


async def _run(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Every wrapper funnels through here, so all eight share one audit path."""
    async with _context() as ctx:
        outcome = await execute(tool, arguments, ctx)
        return dict(outcome.model_dump(mode="json", exclude_none=True))


# --------------------------------------------------------------------------
# The eight tools. Descriptions are duplicated from the registry on purpose:
# `@mcp.tool()` reads the docstring, and test_mcp_parity asserts the schemas
# stay structurally identical to the ones the chat application advertises.
# --------------------------------------------------------------------------


@mcp.tool()
async def list_columns(
    contains: str | None = None, semantic_type: str | None = None
) -> dict[str, Any]:
    """List the columns available in the dataset, with their types and descriptions.

    Call this first when unsure what the data contains, or to check a column
    name before using it.
    """
    return await _run(
        "list_columns",
        _drop_none({"contains": contains, "semantic_type": semantic_type}),
    )


@mcp.tool()
async def describe_column(column: str) -> dict[str, Any]:
    """Get the full profile of one column.

    Returns null fraction, cardinality, range, quantiles, sample values, and any
    caveat about how the column is recorded.
    """
    return await _run("describe_column", {"column": column})


@mcp.tool()
async def count_rows(filters: list[Filter] | None = None) -> dict[str, Any]:
    """Count rows matching a set of filters.

    If nothing matches, the result explains which filter eliminated everything
    and what values are actually present.
    """
    return await _run("count_rows", _drop_none({"filters": _dump(filters)}))


@mcp.tool()
async def aggregate(
    metrics: list[Metric],
    group_by: list[str] | None = None,
    filters: list[Filter] | None = None,
    having: list[Having] | None = None,
    order_by: list[OrderBy] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Compute one or more metrics, optionally grouped and filtered.

    The general-purpose analysis tool: use it for totals, averages, and
    breakdowns.
    """
    return await _run(
        "aggregate",
        _drop_none(
            {
                "metrics": _dump(metrics),
                "group_by": group_by,
                "filters": _dump(filters),
                "having": _dump(having),
                "order_by": _dump(order_by),
                "limit": limit,
            }
        ),
    )


@mcp.tool()
async def top_n(
    dimension: str,
    metric: Metric,
    n: int = 10,
    filters: list[Filter] | None = None,
    direction: str = "top",
    min_group_rows: int = 0,
) -> dict[str, Any]:
    """Rank the values of one column by a metric and return the best or worst n.

    Prefer this for "busiest", "highest", or "which X had the most Y". When
    ranking by an average, set min_group_rows so a single outlier cannot win.
    """
    return await _run(
        "top_n",
        _drop_none(
            {
                "dimension": dimension,
                "metric": metric.model_dump(exclude_none=True),
                "n": n,
                "filters": _dump(filters),
                "direction": direction,
                "min_group_rows": min_group_rows,
            }
        ),
    )


@mcp.tool()
async def timeseries(
    time_column: str,
    metrics: list[Metric],
    grain: str = "day",
    filters: list[Filter] | None = None,
    group_by: str | None = None,
) -> dict[str, Any]:
    """Compute metrics bucketed over time, optionally split into a few series.

    Use this for trends, seasonality, and any before-and-after comparison.
    """
    return await _run(
        "timeseries",
        _drop_none(
            {
                "time_column": time_column,
                "grain": grain,
                "metrics": _dump(metrics),
                "filters": _dump(filters),
                "group_by": group_by,
            }
        ),
    )


@mcp.tool()
async def distribution(
    column: str,
    filters: list[Filter] | None = None,
    bins: int = 20,
    clip_percentile: float = 99.0,
) -> dict[str, Any]:
    """Bin a numeric column into a histogram.

    The upper tail is clipped by default, because a single extreme value would
    otherwise put every row in one bin.
    """
    return await _run(
        "distribution",
        _drop_none(
            {
                "column": column,
                "filters": _dump(filters),
                "bins": bins,
                "clip_percentile": clip_percentile,
            }
        ),
    )


@mcp.tool()
async def plot(result_id: str, chart: ChartSpec) -> dict[str, Any]:
    """Render a chart from a result you already computed, by its result_id.

    Choose the chart kind and which of that result's columns map to the axes.
    """
    return await _run(
        "plot", {"result_id": result_id, "chart": chart.model_dump(exclude_none=True)}
    )


def _dump(models: list[Any] | None) -> list[dict[str, Any]] | None:
    if models is None:
        return None
    return [m.model_dump(exclude_none=True) if hasattr(m, "model_dump") else m for m in models]


def _drop_none(arguments: dict[str, Any]) -> dict[str, Any]:
    """Omit unset arguments so defaults come from the Pydantic models, once."""
    return {key: value for key, value in arguments.items() if value is not None}


async def _startup(settings: Settings) -> AppState:
    from ledger.api.app import build_state

    return await build_state(settings)


def main(argv: list[str] | None = None) -> int:
    # stderr, always: stdout belongs to the JSON-RPC framing.
    configure_logging()

    parser = argparse.ArgumentParser(prog="ledger-mcp", description=__doc__)
    parser.add_argument(
        "--http", action="store_true", help="Serve over streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container service
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.validate_for_startup()

    global _state
    _state = asyncio.run(_startup(settings))
    log.info(
        "ledger-mcp ready as %s over %s",
        _state.settings.mcp_role,
        "http" if args.http else "stdio",
    )

    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
