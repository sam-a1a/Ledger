"""DuckDB engine lifecycle.

Three rules, each of which exists because breaking it produces a bug that
survives code review:

1. **One in-memory database per process.** A DuckDB *file* takes an exclusive
   writer lock, so the API, the MCP server, and pytest could not read the same
   dataset concurrently. Views over parquet cost nothing and lock nothing.
2. **A cursor per request, never the shared connection.** ``DuckDBPyConnection``
   is not safe for concurrent use; ``cursor()`` returns an independent handle
   onto the same database.
3. **Every query runs in a worker thread.** DuckDB is synchronous and blocking.
   Called from an ``async def`` it stalls the event loop for the whole scan --
   which stops token streaming and stops the disconnect watchdog, because that
   is on the same loop.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio.to_thread
import duckdb

from ledger.config import Settings
from ledger.errors import DataUnavailableError
from ledger.logging import get_logger

log = get_logger(__name__)

BOOTSTRAP_SQL = Path(__file__).with_name("bootstrap.sql")

#: Statements DuckDB applies per connection to keep memory and ordering sane.
# SET GLOBAL, not SET: a plain SET is session-scoped and every cursor() opens a
# fresh session, so per-connection settings would silently not apply.
_PRAGMAS = (
    "SET GLOBAL threads = {threads}",
    "SET GLOBAL memory_limit = '{memory_limit}'",
    # We always ORDER BY explicitly when order matters; preserving insertion
    # order across a 10M-row scan costs memory for a guarantee we never use.
    "SET GLOBAL preserve_insertion_order = false",
)


class Engine:
    """Owns the process-wide DuckDB connection and hands out per-request cursors."""

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        session_variables: dict[str, str],
    ) -> None:
        self._connection = connection
        self._session_variables = session_variables
        self._lock = threading.Lock()

    @classmethod
    def create(cls, settings: Settings) -> Engine:
        """Open an in-memory database and apply the normalisation views."""
        raw_dir = settings.raw_dir
        zones = raw_dir / "taxi_zone_lookup.csv"
        trips = sorted(raw_dir.glob("yellow_tripdata_*.parquet"))

        if not zones.exists() or not trips:
            raise DataUnavailableError(
                f"no dataset in {raw_dir}. Run `make fetch` "
                "(or `python -m scripts.fetch_data`) first."
            )

        connection = duckdb.connect(":memory:")
        for pragma in _PRAGMAS:
            connection.execute(
                pragma.format(
                    threads=settings.duckdb_threads,
                    memory_limit=settings.duckdb_memory_limit,
                )
            )

        # Bound as parameters rather than interpolated. DuckDB rejects prepared
        # parameters inside CREATE VIEW, so the paths go into session variables
        # that the views read through getvariable().
        #
        # Session variables do NOT propagate to cursors, and an unset one reads
        # back as NULL rather than raising -- which surfaces much later as a
        # baffling "read_parquet cannot take NULL list". So every cursor re-binds
        # them; see `cursor()`.
        session_variables = {
            "trips_glob": str(raw_dir / "yellow_tripdata_*.parquet"),
            "zones_path": str(zones),
        }
        for name, value in session_variables.items():
            connection.execute(f"SET VARIABLE {name} = ?", [value])
        connection.execute(BOOTSTRAP_SQL.read_text())

        if settings.materialize:
            log.info("materialising ledger.trips (LEDGER_MATERIALIZE=1)")
            connection.execute(
                "CREATE OR REPLACE TABLE ledger.trips_mat AS SELECT * FROM ledger.trips"
            )
            connection.execute(
                "CREATE OR REPLACE VIEW ledger.trips AS SELECT * FROM ledger.trips_mat"
            )

        log.info("engine ready over %d parquet file(s) in %s", len(trips), raw_dir)
        return cls(connection, session_variables)

    @contextmanager
    def cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield an independent cursor onto the shared database."""
        with self._lock:
            cursor = self._connection.cursor()
        try:
            for name, value in self._session_variables.items():
                cursor.execute(f"SET VARIABLE {name} = ?", [value])
            yield cursor
        finally:
            cursor.close()

    def close(self) -> None:
        self._connection.close()


async def run_query(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """Execute ``sql`` off the event loop and return ``(column_names, rows)``.

    ``abandon_on_cancel=False`` is deliberate. On cancellation we want to *wait*
    for the interrupted thread rather than orphan it -- an abandoned thread still
    holds the cursor we are about to close.
    """

    def _execute() -> tuple[list[str], list[list[Any]]]:
        result = cursor.execute(sql, list(params) if params else None)
        columns = [d[0] for d in (result.description or [])]
        rows = [list(row) for row in result.fetchall()]
        return columns, rows

    return await anyio.to_thread.run_sync(_execute, abandon_on_cancel=False)


async def run_scalar(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> Any:
    """Execute ``sql`` and return the first column of the first row, or None."""
    _, rows = await run_query(cursor, sql, params)
    return rows[0][0] if rows else None
