"""Phase one of enrichment: deterministic profiling. No LLM involved.

Profiles the *shape* of every column -- type, nulls, cardinality, range,
and a bounded sample -- so the model can reason about what to ask for without
anyone pasting rows into a prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from ledger.catalog.fingerprint import dataset_fingerprint
from ledger.catalog.models import (
    Catalog,
    ColumnProfile,
    DatasetStats,
    SemanticType,
)
from ledger.logging import get_logger
from ledger.security.policy import sensitivity_of

log = get_logger(__name__)

#: Above this cardinality a column's values are not sampled. Free text is never
#: sampled at all: it is a leak vector and a token sink, and the model does not
#: need it to decide whether to filter on the column.
SAMPLE_CARDINALITY_LIMIT = 200

#: At or below this, a low-cardinality string is a category rather than text.
CATEGORICAL_LIMIT = 200

_NUMERIC_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
)
_TEMPORAL_TYPES = ("DATE", "TIME", "TIMESTAMP")


def _semantic_type(
    name: str,
    duckdb_type: str,
    approx_distinct: int,
    row_count: int,
    all_names: frozenset[str],
) -> SemanticType:
    upper = duckdb_type.upper()
    if upper.startswith("BOOLEAN"):
        return SemanticType.BOOLEAN
    if any(upper.startswith(t) for t in _TEMPORAL_TYPES):
        return SemanticType.TEMPORAL
    if any(upper.startswith(t) for t in _NUMERIC_TYPES):
        # A numeric column that has a `_label` sibling is a *code*, not a
        # quantity -- bootstrap.sql only creates those labels for coded columns.
        # Left as numeric, `avg(payment_type)` would be arithmetic on a label:
        # a confidently meaningless answer, which is the failure mode this whole
        # project exists to prevent.
        if f"{name}_label" in all_names:
            return SemanticType.CATEGORICAL
        # An id column with near-unique values is a key rather than a category.
        if name.endswith("_id") and row_count and approx_distinct / row_count > 0.5:
            return SemanticType.IDENTIFIER
        if name.endswith("_id"):
            return SemanticType.CATEGORICAL
        return SemanticType.NUMERIC
    if approx_distinct <= CATEGORICAL_LIMIT:
        return SemanticType.CATEGORICAL
    return SemanticType.TEXT


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _one(cursor: duckdb.DuckDBPyConnection, sql: str) -> tuple[Any, ...]:
    """Fetch exactly one row, or fail loudly.

    Every aggregate here returns a row by construction; a None means the query
    was not what we thought it was, and unpacking it silently would hide that.
    """
    row = cursor.execute(sql).fetchone()
    if row is None:
        raise RuntimeError(f"expected one row from: {sql}")
    return row


def profile_dataset(
    cursor: duckdb.DuckDBPyConnection,
    *,
    raw_dir: Path,
    relation: str = "ledger.trips",
) -> Catalog:
    """Build a full catalogue by profiling every column of ``relation``."""
    described = cursor.execute(f"DESCRIBE {relation}").fetchall()
    row_count = int(_one(cursor, f"SELECT count(*) FROM {relation}")[0])  # noqa: S608

    all_names = frozenset(str(row[0]) for row in described)

    columns: dict[str, ColumnProfile] = {}
    for name, duckdb_type, *_ in described:
        col = _quote(name)
        nulls, distinct = _one(
            cursor,
            f"SELECT count(*) FILTER ({col} IS NULL), approx_count_distinct({col}) "  # noqa: S608
            f"FROM {relation}",
        )

        semantic = _semantic_type(name, duckdb_type, int(distinct), row_count, all_names)

        min_value: Any = None
        max_value: Any = None
        p01 = p50 = p99 = None
        if semantic in (SemanticType.NUMERIC, SemanticType.TEMPORAL):
            min_value, max_value = _one(
                cursor,
                f"SELECT min({col}), max({col}) FROM {relation}",  # noqa: S608
            )
        if semantic is SemanticType.NUMERIC:
            p01, p50, p99 = _one(
                cursor,
                f"SELECT quantile_cont({col}, 0.01), quantile_cont({col}, 0.5), "  # noqa: S608
                f"quantile_cont({col}, 0.99) FROM {relation}",
            )

        samples: list[Any] = []
        if 0 < int(distinct) <= SAMPLE_CARDINALITY_LIMIT and semantic is not SemanticType.TEXT:
            samples = [
                row[0]
                for row in cursor.execute(
                    f"SELECT {col} FROM {relation} WHERE {col} IS NOT NULL "  # noqa: S608
                    f"GROUP BY 1 ORDER BY count(*) DESC LIMIT 10"
                ).fetchall()
            ]

        columns[name] = ColumnProfile(
            name=name,
            physical_name=name,
            duckdb_type=str(duckdb_type),
            semantic_type=semantic,
            sensitivity=sensitivity_of(name),
            null_fraction=(int(nulls) / row_count) if row_count else 0.0,
            approx_distinct=int(distinct),
            min_value=min_value,
            max_value=max_value,
            p01=p01,
            p50=p50,
            p99=p99,
            sample_values=samples,
        )

    fingerprint = dataset_fingerprint(raw_dir, described)
    log.info("profiled %d columns over %s rows", len(columns), f"{row_count:,}")

    return Catalog(
        version=fingerprint[:12],
        dataset_fingerprint=fingerprint,
        built_at=datetime.now(UTC),
        stats=DatasetStats(
            row_count=row_count,
            profiled_rows=row_count,
            source_files=sorted(p.name for p in raw_dir.glob("*.parquet")),
        ),
        columns=columns,
    )
