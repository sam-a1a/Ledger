"""A real Postgres for the account and conversation tests.

Postgres rather than SQLite-in-memory, because the schema uses JSONB, real
foreign keys with cascade, and partial-index semantics -- testing against a
different engine would test a different thing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledger.config import Settings
from ledger.db.base import Base
from ledger.db.session import create_engine, create_sessionmaker

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.getenv(
        "LEDGER_TEST_DATABASE_URL",
        "postgresql+asyncpg://ledger:ledger@localhost:5455/ledger_test",
    )


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A fresh schema per test.

    Dropping and recreating rather than truncating: the schema itself changes
    often enough during development that a stale one produces failures which
    look like logic errors.
    """
    engine = create_engine(Settings(database_url=database_url))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment, not logic
        pytest.skip(f"no Postgres at {database_url}: {exc}")

    yield create_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def session(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessions() as active:
        yield active
        await active.commit()
