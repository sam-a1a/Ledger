"""The column catalogue: what the model reads instead of the data.

Every model-facing surface -- the system prompt, the tool schemas, the
``list_columns`` and ``describe_column`` results, argument validation, and SQL
compilation -- is fed from a :class:`ScopedCatalog`, never from :class:`Catalog`
directly. :func:`ledger.catalog.scope.scope_catalog` is the only thing that
produces one.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ledger.security.policy import Sensitivity

#: Bumped when the shape of a profile changes, so an old cache invalidates.
CATALOG_SCHEMA_VERSION = 1


class SemanticType(StrEnum):
    """What a column *means*, as opposed to how DuckDB stores it.

    Drives which operators and metrics are legal, so it is derived once during
    profiling rather than re-inferred per call.
    """

    NUMERIC = "numeric"
    TEMPORAL = "temporal"
    CATEGORICAL = "categorical"
    TEXT = "text"
    BOOLEAN = "boolean"
    IDENTIFIER = "identifier"


#: Metrics that require arithmetic.
NUMERIC_METRIC_TYPES = frozenset({SemanticType.NUMERIC})
#: Types a time bucket can be built from.
TEMPORAL_TYPES = frozenset({SemanticType.TEMPORAL})


class DescriptionSource(StrEnum):
    """Where a column's one-line description came from.

    Recorded rather than hidden: whether a description was written by a human,
    generated, or fabricated from the profile is genuine governance metadata,
    and it costs nothing to keep.
    """

    SEED = "seed"
    LLM = "llm"
    DERIVED = "derived"


class Description(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    source: DescriptionSource
    unit: str | None = None
    caveat: str | None = None
    model: str | None = None
    generated_at: datetime | None = None
    #: The profile this description was written against. A description is reused
    #: only while this still matches, which is what keeps a data refresh from
    #: turning into a recurring API bill.
    profile_fingerprint: str | None = None


class ColumnProfile(BaseModel):
    """Everything known about one column, and nothing about any row."""

    model_config = ConfigDict(extra="forbid")

    name: str
    physical_name: str
    duckdb_type: str
    semantic_type: SemanticType
    sensitivity: Sensitivity = Sensitivity.PUBLIC

    null_fraction: float = 0.0
    approx_distinct: int = 0
    min_value: Any = None
    max_value: Any = None
    p01: float | None = None
    p50: float | None = None
    p99: float | None = None
    #: Only populated for low-cardinality columns. High-cardinality free text is
    #: never sampled: that is both a leak vector and a token sink.
    sample_values: list[Any] = Field(default_factory=list)

    description: Description | None = None

    @property
    def is_numeric(self) -> bool:
        return self.semantic_type is SemanticType.NUMERIC

    @property
    def is_temporal(self) -> bool:
        return self.semantic_type is SemanticType.TEMPORAL

    def fingerprint(self) -> str:
        """Coarse hash of this column's *shape*.

        Deliberately bucketed: a data refresh that leaves a column's character
        unchanged reuses its description at zero cost, while a genuine change
        (new type, cardinality shifting by an order of magnitude) invalidates it.
        """
        bucket_null = round(self.null_fraction, 1)
        bucket_distinct = len(str(self.approx_distinct))
        parts = [
            self.name,
            self.duckdb_type,
            self.semantic_type.value,
            str(bucket_null),
            str(bucket_distinct),
            str(CATALOG_SCHEMA_VERSION),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class DatasetStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_count: int
    profiled_rows: int
    source_files: list[str] = Field(default_factory=list)
    #: v1 profiles across all tenants. Recorded explicitly so the limitation is
    #: visible rather than assumed away.
    stats_scope: Literal["global", "per_tenant"] = "global"


class Catalog(BaseModel):
    """The full, unscoped catalogue. Never handed to the model."""

    model_config = ConfigDict(extra="forbid")

    version: str
    dataset_fingerprint: str
    built_at: datetime
    stats: DatasetStats
    columns: dict[str, ColumnProfile]

    def description_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {s.value: 0 for s in DescriptionSource}
        counts["missing"] = 0
        for column in self.columns.values():
            if column.description is None:
                counts["missing"] += 1
            else:
                counts[column.description.source.value] += 1
        return counts


class ScopedCatalog(BaseModel):
    """A catalogue filtered to one principal's role.

    Holding one of these is the *only* way to reach a column name in the tool
    layer, so a column the role may not see is not merely hidden -- it is
    unreachable.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    role: str
    columns: dict[str, ColumnProfile]
    stats: DatasetStats

    def get(self, name: str) -> ColumnProfile | None:
        return self.columns.get(name)

    def names(self) -> list[str]:
        return sorted(self.columns)

    def numeric_names(self) -> list[str]:
        return sorted(n for n, c in self.columns.items() if c.is_numeric)

    def temporal_names(self) -> list[str]:
        return sorted(n for n, c in self.columns.items() if c.is_temporal)
