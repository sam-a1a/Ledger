"""An app wired against the fixture dataset and a real broker."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from ledger.api.app import build_state, create_app
from ledger.config import Settings
from ledger.db.base import Base
from ledger.db.session import create_engine

# Postgres too, now that the API has accounts behind it.
pytestmark = [pytest.mark.kafka, pytest.mark.postgres]


@pytest.fixture
async def app(kafka_settings: Settings, settings: Settings) -> AsyncIterator[FastAPI]:
    """The real application, with real DuckDB, real audit, and a scripted model."""
    merged = kafka_settings.model_copy(
        update={
            "data_dir": settings.data_dir,
            "months": settings.months,
            "model_backend": "fake",
            "catalog_mode": "auto",
            # Its own database, reset per test, so account ordering is
            # deterministic: the first account created becomes an analyst.
            "database_url": os.getenv(
                "LEDGER_TEST_DATABASE_URL",
                "postgresql+asyncpg://ledger:ledger@localhost:5455/ledger_test",
            ),
            "database_auto_migrate": False,
        }
    )
    engine = create_engine(merged)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    application = create_app()
    state = await build_state(merged)
    application.state.ledger = state
    try:
        yield application
    finally:
        state.engine.close()
        await state.producer.stop()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ledger") as c:
        yield c


async def _signup(client: httpx.AsyncClient, label: str) -> str:
    """Create an account and return its token.

    Roles are not requested: the first account created becomes an analyst and
    every later one a viewer, which is the property the RBAC tests rely on and
    the reason the database is reset per test module.
    """
    response = await client.post(
        "/api/accounts/signup",
        json={
            "email": f"{label}-{uuid.uuid4().hex[:8]}@example.com",
            "password": "correct-horse-battery",
        },
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


@pytest.fixture
async def analyst_token(client: httpx.AsyncClient) -> str:
    return await _signup(client, "analyst")


@pytest.fixture
async def viewer_token(client: httpx.AsyncClient) -> str:
    # Only a viewer if an analyst already exists, which `analyst_token`
    # guarantees when both are requested; requesting this alone on an empty
    # database would produce an analyst.
    await _signup(client, "seed-analyst")
    return await _signup(client, "viewer")
