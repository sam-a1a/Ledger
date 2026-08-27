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

from ledger.catalog import describe, store
from ledger.catalog.models import Catalog, ScopedCatalog
from ledger.catalog.profile import profile_dataset
from ledger.catalog.scope import scope_catalog
from ledger.config import Settings
from ledger.engine.duck import Engine
from ledger.security.principal import Principal, Role

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


@pytest.fixture(scope="session")
def catalog(engine: Engine, settings: Settings) -> Catalog:
    """A fully profiled catalogue over the fixture, descriptions resolved."""
    with engine.cursor() as cur:
        built = profile_dataset(cur, raw_dir=settings.raw_dir)
    descriptions = describe.resolve(
        built,
        seed_path=store.seed_path(settings),
        generated_path=store.generated_path(settings),
    )
    return describe.apply(built, descriptions)


@pytest.fixture
def analyst() -> Principal:
    return Principal(subject="test-analyst", role=Role.ANALYST, tenant_id=None)


@pytest.fixture
def viewer() -> Principal:
    return Principal(subject="test-viewer", role=Role.VIEWER, tenant_id=1)


@pytest.fixture
def analyst_scope(catalog: Catalog, analyst: Principal) -> ScopedCatalog:
    return scope_catalog(catalog, analyst)


@pytest.fixture
def viewer_scope(catalog: Catalog, viewer: Principal) -> ScopedCatalog:
    return scope_catalog(catalog, viewer)
