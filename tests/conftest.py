"""Shared fixtures.

The engine and tool layers do not import ``ledger.events``, so nothing in
``tests/unit`` or ``tests/tools`` needs a broker. Kafka-backed fixtures live in
``tests/events/conftest.py`` behind the ``kafka`` marker, which keeps
``pytest -m "not kafka"`` runnable with no Docker at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from aiokafka import AIOKafkaProducer

from ledger.catalog import describe, store
from ledger.catalog.models import Catalog, ScopedCatalog
from ledger.catalog.profile import profile_dataset
from ledger.catalog.scope import scope_catalog
from ledger.config import Settings
from ledger.engine.duck import Engine
from ledger.governance.events import AuditTopics
from ledger.security.principal import Principal, Role
from ledger.tools.context import ToolContext
from tests.doubles import RecordingPublisher

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


@pytest.fixture
def analyst_ctx(
    analyst: Principal,
    analyst_scope: ScopedCatalog,
    cursor: duckdb.DuckDBPyConnection,
    settings: Settings,
) -> ToolContext:
    return ToolContext(
        principal=analyst,
        scope=analyst_scope,
        cursor=cursor,
        publisher=RecordingPublisher(),
        settings=settings,
        conversation_id="conv-test",
    )


@pytest.fixture
def viewer_ctx(
    viewer: Principal,
    viewer_scope: ScopedCatalog,
    cursor: duckdb.DuckDBPyConnection,
    settings: Settings,
) -> ToolContext:
    return ToolContext(
        principal=viewer,
        scope=viewer_scope,
        cursor=cursor,
        publisher=RecordingPublisher(),
        settings=settings,
        conversation_id="conv-test",
    )


def published(ctx: ToolContext) -> RecordingPublisher:
    """Narrow the publisher back to the double, for assertions."""
    assert isinstance(ctx.publisher, RecordingPublisher)
    return ctx.publisher


# --------------------------------------------------------------------------
# Kafka
#
# The broker starts lazily -- only when a test that needs one is collected --
# so `pytest -m "not kafka"` runs the entire pure layer with no Docker at all.
# The image is the same one Compose uses, rather than the Confluent image
# testcontainers defaults to: tests and production should exercise one broker.
# --------------------------------------------------------------------------

KAFKA_IMAGE = "apache/kafka:4.3.1"
STARTUP_TIMEOUT_S = 90.0


def _free_port() -> int:
    """Reserve a port before the container starts.

    Kafka has to advertise a listener the client can reach, and the advertised
    address is baked into the broker config at boot -- so the host port cannot be
    discovered afterwards the way it can for an ordinary service.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_broker(bootstrap: str, timeout_s: float) -> None:
    """Wait until a real client can connect, rather than scraping logs."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last: Exception | None = None
    while loop.time() < deadline:
        producer = AIOKafkaProducer(bootstrap_servers=bootstrap, request_timeout_ms=3000)
        try:
            await producer.start()
            await producer.stop()
            return
        except Exception as exc:
            last = exc
            with contextlib.suppress(Exception):
                await producer.stop()
            await asyncio.sleep(1.0)
    raise RuntimeError(f"kafka at {bootstrap} never became ready: {last}")


@pytest.fixture(scope="session")
def kafka_bootstrap() -> Iterator[str]:
    """A running broker, reused for the whole session."""
    if external := os.getenv("LEDGER_KAFKA_BOOTSTRAP"):
        yield external
        return

    docker = pytest.importorskip(
        "testcontainers.core.container", reason="testcontainers is not installed"
    )
    port = _free_port()
    container = docker.DockerContainer(KAFKA_IMAGE)
    container.with_bind_ports(9092, port)
    for key, value in {
        "KAFKA_NODE_ID": "1",
        "KAFKA_PROCESS_ROLES": "broker,controller",
        "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
        "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
        "KAFKA_ADVERTISED_LISTENERS": f"PLAINTEXT://localhost:{port}",
        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
        "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
        "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
        "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
        "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
        "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
        "CLUSTER_ID": "4L6g3nShT-eMCtK--X86sw",
    }.items():
        container.with_env(key, value)

    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"cannot start kafka container (is Docker running?): {exc}")

    bootstrap = f"localhost:{port}"
    try:
        asyncio.run(_wait_for_broker(bootstrap, STARTUP_TIMEOUT_S))
        yield bootstrap
    finally:
        container.stop()


@pytest.fixture
def audit_topics() -> AuditTopics:
    """Per-test topic names, so one test's events never reach another's consumer."""
    suffix = uuid.uuid4().hex[:8]
    return AuditTopics(
        tool_calls=f"test.tool-calls.{suffix}",
        access_denied=f"test.access-denied.{suffix}",
    )


@pytest.fixture
async def kafka_settings(kafka_bootstrap: str, audit_topics: AuditTopics) -> Settings:
    return Settings(
        kafka_bootstrap_servers=kafka_bootstrap,
        kafka_topic_tool_calls=audit_topics.tool_calls,
        kafka_topic_access_denied=audit_topics.access_denied,
    )
