"""The normalisation layer, and specifically the schema drift it absorbs.

The 2024-12 parquet has no ``cbd_congestion_fee`` column at all -- NYC congestion
pricing began 5 January 2025 -- and the 2025 files do. If ``union_by_name`` were
dropped from ``bootstrap.sql``, positional union would silently misalign columns
rather than fail, so this is asserted rather than assumed.
"""

from __future__ import annotations

import duckdb
import pytest

from ledger.engine.duck import Engine, run_query, run_scalar


async def test_trips_view_spans_every_parquet_file(cursor: duckdb.DuckDBPyConnection) -> None:
    assert await run_scalar(cursor, "SELECT count(*) FROM ledger.trips") == 45_000


async def test_cbd_fee_is_null_before_the_column_existed(
    cursor: duckdb.DuckDBPyConnection,
) -> None:
    _, rows = await run_query(
        cursor,
        """
        SELECT strftime(date_trunc('month', pickup_at), '%Y-%m') AS month,
               count(*)                     AS trips,
               count(cbd_congestion_fee)    AS rows_with_fee
        FROM ledger.trips GROUP BY 1 ORDER BY 1
        """,
    )
    by_month = {r[0]: (r[1], r[2]) for r in rows}

    # Every month contributes rows...
    assert by_month["2024-12"][0] == 15_000
    # ...but the month predating the column contributes no fee values.
    assert by_month["2024-12"][1] == 0
    assert by_month["2025-01"][1] == 15_000


async def test_congestion_charge_start_date_is_visible(
    cursor: duckdb.DuckDBPyConnection,
) -> None:
    """January straddles 5 Jan, so its mean fee sits strictly between 0 and 0.75."""
    jan = await run_scalar(
        cursor,
        """
        SELECT avg(cbd_congestion_fee) FROM ledger.trips
        WHERE pickup_at >= TIMESTAMP '2025-01-01' AND pickup_at < TIMESTAMP '2025-02-01'
        """,
    )
    feb = await run_scalar(
        cursor,
        """
        SELECT avg(cbd_congestion_fee) FROM ledger.trips
        WHERE pickup_at >= TIMESTAMP '2025-02-01' AND pickup_at < TIMESTAMP '2025-03-01'
        """,
    )
    assert 0.0 < jan < 0.75
    assert feb == pytest.approx(0.75)


async def test_airport_fee_casing_drift_is_normalised(
    cursor: duckdb.DuckDBPyConnection,
) -> None:
    """One `airport_fee` column regardless of how the source spelled it."""
    _, rows = await run_query(cursor, "DESCRIBE ledger.trips")
    names = {r[0] for r in rows}
    assert "airport_fee" in names
    assert "Airport_fee" not in names
    assert await run_scalar(cursor, "SELECT count(airport_fee) FROM ledger.trips") == 45_000


async def test_zone_join_resolves_ids_to_names(cursor: duckdb.DuckDBPyConnection) -> None:
    zone = await run_scalar(
        cursor,
        "SELECT DISTINCT pickup_zone FROM ledger.trips WHERE pickup_location_id = 132",
    )
    assert zone == "JFK Airport"


async def test_tenant_key_partitions_every_row(cursor: duckdb.DuckDBPyConnection) -> None:
    _, rows = await run_query(
        cursor, "SELECT tenant_id, count(*) FROM ledger.trips GROUP BY 1 ORDER BY 1"
    )
    assert [r[0] for r in rows] == [1, 2]
    assert sum(r[1] for r in rows) == 45_000


async def test_cursors_are_independent_and_see_the_views(engine: Engine) -> None:
    """Session variables do not propagate to cursors; `cursor()` re-binds them.

    Without that, `getvariable('trips_glob')` reads back as NULL rather than
    raising, and the failure surfaces as an unrelated parser error.
    """
    with engine.cursor() as a, engine.cursor() as b:
        assert await run_scalar(a, "SELECT getvariable('trips_glob')") is not None
        assert await run_scalar(b, "SELECT count(*) FROM ledger.trips") == 45_000
