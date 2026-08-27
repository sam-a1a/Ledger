"""Cache keys, and specifically the two different lifetimes they encode."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

from ledger.catalog.fingerprint import dataset_fingerprint
from ledger.catalog.models import Catalog, ColumnProfile, SemanticType
from ledger.config import Settings
from ledger.engine.duck import Engine


def _described(cursor: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in cursor.execute("DESCRIBE ledger.trips").fetchall()]


def test_dataset_fingerprint_is_stable(
    cursor: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    described = _described(cursor)
    first = dataset_fingerprint(settings.raw_dir, described)
    second = dataset_fingerprint(settings.raw_dir, described)
    assert first == second


def test_dataset_fingerprint_changes_when_a_file_is_added(
    cursor: duckdb.DuckDBPyConnection, settings: Settings, tmp_path: Path
) -> None:
    described = _described(cursor)
    before = dataset_fingerprint(settings.raw_dir, described)

    raw = tmp_path / "raw"
    shutil.copytree(settings.raw_dir, raw)
    shutil.copy(raw / "yellow_tripdata_2025-02.parquet", raw / "yellow_tripdata_2025-03.parquet")

    assert dataset_fingerprint(raw, described) != before


def test_dataset_fingerprint_changes_when_the_schema_changes(
    cursor: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    described = _described(cursor)
    before = dataset_fingerprint(settings.raw_dir, described)
    mutated = [*described, ("a_new_column", "DOUBLE")]
    assert dataset_fingerprint(settings.raw_dir, mutated) != before


def _profile(**overrides: object) -> ColumnProfile:
    base = {
        "name": "fare_amount",
        "physical_name": "fare_amount",
        "duckdb_type": "DOUBLE",
        "semantic_type": SemanticType.NUMERIC,
        "null_fraction": 0.01,
        "approx_distinct": 4026,
    }
    return ColumnProfile.model_validate(base | overrides)


def test_column_fingerprint_ignores_small_drift() -> None:
    """A refresh that leaves a column's character unchanged reuses its description.

    This is what stops enrichment becoming a recurring bill: descriptions almost
    never invalidate, profiles do.
    """
    assert (
        _profile().fingerprint()
        == _profile(approx_distinct=4111, null_fraction=0.012).fingerprint()
    )


def test_column_fingerprint_reacts_to_real_change() -> None:
    original = _profile().fingerprint()
    # An order of magnitude more distinct values is a different column.
    assert _profile(approx_distinct=402_600).fingerprint() != original
    assert _profile(duckdb_type="VARCHAR").fingerprint() != original
    assert _profile(semantic_type=SemanticType.CATEGORICAL).fingerprint() != original


def test_catalog_round_trips_through_json(catalog: Catalog) -> None:
    restored = Catalog.model_validate_json(catalog.model_dump_json())
    assert restored.dataset_fingerprint == catalog.dataset_fingerprint
    assert set(restored.columns) == set(catalog.columns)
    assert restored.columns["fare_amount"].description is not None


def test_engine_fingerprint_matches_a_fresh_build(settings: Settings) -> None:
    """A second engine over the same files produces the same fingerprint."""
    engine = Engine.create(settings)
    try:
        with engine.cursor() as cur:
            assert dataset_fingerprint(settings.raw_dir, _described(cur))
    finally:
        engine.close()
