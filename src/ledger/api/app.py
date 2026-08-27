"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ledger import __version__
from ledger.api.routes import accounts, audit, auth, chat, conversations, health
from ledger.api.state import AppState
from ledger.catalog import store as catalog_store
from ledger.config import REPO_ROOT, Settings, get_settings
from ledger.db.session import create_engine, create_sessionmaker
from ledger.engine.duck import Engine
from ledger.governance.journal import EventJournal
from ledger.governance.publisher import KafkaAuditPublisher
from ledger.governance.topics import connect_producer, ensure_topics, topics_for
from ledger.logging import get_logger

log = get_logger(__name__)


async def build_state(settings: Settings) -> AppState:
    """Construct every process-wide resource, failing fast on any of them.

    Order matters: the broker is contacted before the engine is opened, so a
    misconfigured deployment fails on the thing it is actually missing rather
    than after several seconds of parquet work.
    """
    topics = topics_for(settings)
    producer = KafkaAuditPublisher.build_producer(settings)

    # Kafka is a hard dependency. Ledger audits every tool call *before*
    # serving it, so a broker it cannot reach is a startup failure, not a
    # degraded mode. The retry window exists because a broker that has just
    # passed a healthcheck can still refuse connections while electing a
    # controller -- fail-fast without it looks like a flake.
    await connect_producer(
        producer,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        timeout_s=settings.kafka_bootstrap_timeout_s,
    )
    await ensure_topics(settings.kafka_bootstrap_servers, topics)

    journal = EventJournal(settings.journal_path)
    publisher = KafkaAuditPublisher(producer, topics, journal)
    replayed = await publisher.drain_journal()
    if replayed:
        log.info("replayed %d event(s) journalled during an earlier outage", replayed)

    db_engine = create_engine(settings)
    if settings.database_auto_migrate:
        await _migrate(settings)

    engine = Engine.create(settings)
    catalog = catalog_store.load_for_startup(settings)

    return AppState(
        settings=settings,
        engine=engine,
        catalog=catalog,
        publisher=publisher,
        producer=producer,
        db_engine=db_engine,
        sessions=create_sessionmaker(db_engine),
    )


async def _migrate(settings: Settings) -> None:
    """Bring the schema up to date at startup.

    Convenient in development, where a clean clone should just work. Production
    sets `LEDGER_DATABASE_AUTO_MIGRATE=false` and runs `alembic upgrade head`
    as a deliberate step, because a schema change is not something that should
    happen as a side effect of a container restarting.
    """
    import anyio
    from alembic import command
    from alembic.config import Config

    def _run() -> None:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(config, "head")

    await anyio.to_thread.run_sync(_run)
    log.info("database schema is up to date")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_for_startup()

    log.info("starting ledger %s", __version__)
    if settings.demo_mode:
        log.warning(
            "DEMO MODE: answers come from the scripted fake model, not a real LLM. "
            "Set ANTHROPIC_API_KEY to use %s.",
            settings.anthropic_model,
        )

    state = await build_state(settings)
    app.state.ledger = state
    try:
        yield
    finally:
        state.engine.close()
        await state.producer.stop()
        await state.db_engine.dispose()
        log.info("ledger shut down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Ledger",
        version=__version__,
        summary="Streaming LLM chat over governed data.",
        lifespan=lifespan,
    )
    settings = get_settings()
    if settings.cors_origins:
        # Development only: the Vite server and the API are separate origins
        # because Vite's proxy cannot stream server-sent events. Behind nginx
        # in production they share an origin and this does nothing.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept"],
        )

    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(chat.router, prefix="/api")
    app.include_router(audit.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api/accounts")
    app.include_router(conversations.router, prefix="/api")
    return app


app = create_app()
