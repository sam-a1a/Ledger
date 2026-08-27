"""Shared fixtures.

The engine and tool layers do not import ``ledger.events``, so nothing in
``tests/unit`` or ``tests/tools`` needs a broker. Kafka-backed fixtures live in
``tests/events/conftest.py`` behind the ``kafka`` marker, which keeps
``pytest -m "not kafka"`` runnable with no Docker at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from ledger.config import Settings
from ledger.engine.duck import Engine

FIXTURE_DATA = Path(__file__).parent / "fixtures" / "data"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings pointed at the deterministic mini dataset."""
    return Settings(
        data_dir=FIXTURE_DATA,
        months=("2024-12", "2025-01", "2025-02"),
        model_backend="fake",
        catalog_mode="offline",
    )


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    if not FIXTURE_DATA.joinpath("raw", "taxi_zone_lookup.csv").exists():
        pytest.fail(
            "test fixture missing -- run `uv run python -m scripts.make_fixture`",
            pytrace=False,
        )
    eng = Engine.create(settings)
    yield eng
    eng.close()


@pytest.fixture
def cursor(engine: Engine) -> Iterator[duckdb.DuckDBPyConnection]:
    with engine.cursor() as cur:
        yield cur
