"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ledger import __version__
from ledger.api.routes import health
from ledger.config import get_settings
from ledger.logging import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_for_startup()
    backend = settings.resolved_backend()
    log.info("starting ledger %s (model backend: %s)", __version__, backend)
    if settings.demo_mode:
        log.warning(
            "DEMO MODE: answers come from the scripted fake model, not a real LLM. "
            "Set ANTHROPIC_API_KEY to use %s.",
            settings.anthropic_model,
        )
    yield
    log.info("ledger shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Ledger",
        version=__version__,
        summary="Streaming LLM chat over governed data.",
        lifespan=lifespan,
    )
    app.include_router(health.router, prefix="/api")
    return app


app = create_app()
