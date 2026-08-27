"""An app wired against the fixture dataset and a real broker."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from ledger.api.app import build_state, create_app
from ledger.config import Settings

pytestmark = pytest.mark.kafka


@pytest.fixture
async def app(kafka_settings: Settings, settings: Settings) -> AsyncIterator[FastAPI]:
    """The real application, with real DuckDB, real audit, and a scripted model."""
    merged = kafka_settings.model_copy(
        update={
            "data_dir": settings.data_dir,
            "months": settings.months,
            "model_backend": "fake",
            "catalog_mode": "auto",
        }
    )
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


@pytest.fixture
async def analyst_token(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/auth/login", json={"role": "analyst"})
    response.raise_for_status()
    return str(response.json()["access_token"])


@pytest.fixture
async def viewer_token(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/auth/login", json={"role": "viewer"})
    response.raise_for_status()
    return str(response.json()["access_token"])
