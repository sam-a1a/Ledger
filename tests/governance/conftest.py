"""Kafka-backed fixtures, behind the ``kafka`` marker.

The broker starts lazily -- only if a test that needs one is actually collected
-- so `pytest -m "not kafka"` runs the whole pure layer with no Docker at all.

The container runs ``apache/kafka:4.3.1``, the same image Compose uses, rather
than the Confluent image testcontainers defaults to. Tests and production should
exercise the same broker.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from aiokafka import AIOKafkaProducer

from ledger.config import Settings
from ledger.governance.events import AuditTopics
from ledger.governance.journal import EventJournal
from ledger.governance.publisher import KafkaAuditPublisher
from ledger.governance.topics import ensure_topics

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


@pytest.fixture
async def publisher(
    kafka_settings: Settings, audit_topics: AuditTopics, tmp_path
) -> AsyncIterator[KafkaAuditPublisher]:
    await ensure_topics(kafka_settings.kafka_bootstrap_servers, audit_topics)
    producer = KafkaAuditPublisher.build_producer(kafka_settings)
    await producer.start()
    journal = EventJournal(tmp_path / "journal.ndjson")
    try:
        yield KafkaAuditPublisher(producer, audit_topics, journal, timeout_s=10.0)
    finally:
        await producer.stop()
