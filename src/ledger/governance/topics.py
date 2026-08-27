"""Broker connection and topic provisioning.

Fail-fast is not the same as zero retry. A broker that has just passed a
healthcheck can still refuse a connection while it elects a controller, so the
API retries for a bounded window and *then* exits non-zero. Either half alone
produces intermittent startup failures that look like flakes.
"""

from __future__ import annotations

import asyncio

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaError, TopicAlreadyExistsError

from ledger.config import Settings
from ledger.errors import ConfigurationError
from ledger.governance.events import AuditTopics
from ledger.logging import get_logger

log = get_logger(__name__)

#: Conversation ordering only needs per-key ordering, which any partition count
#: preserves. Three gives the consumer something to parallelise over later.
TOOL_CALL_PARTITIONS = 3
#: Denials are rare and worth reading in strict global order.
ACCESS_DENIED_PARTITIONS = 1


def topics_for(settings: Settings) -> AuditTopics:
    return AuditTopics(
        tool_calls=settings.kafka_topic_tool_calls,
        access_denied=settings.kafka_topic_access_denied,
    )


async def connect_producer(
    producer: AIOKafkaProducer,
    *,
    bootstrap_servers: str,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
) -> None:
    """Start ``producer``, retrying for a bounded window, then give up loudly."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    attempt = 0
    last: Exception | None = None

    while asyncio.get_running_loop().time() < deadline:
        attempt += 1
        try:
            await producer.start()
            log.info("connected to kafka at %s", bootstrap_servers)
            return
        except (KafkaError, OSError) as exc:
            last = exc
            log.info("kafka not ready (attempt %d): %s", attempt, exc)
            await asyncio.sleep(interval_s)

    raise ConfigurationError(
        f"no Kafka broker at {bootstrap_servers} after {timeout_s:.0f}s "
        f"({type(last).__name__}: {last}). Ledger audits every tool call before "
        "serving it, so it does not start without one. Try `docker compose up kafka`."
    )


async def ensure_topics(bootstrap_servers: str, topics: AuditTopics) -> None:
    """Create the audit topics if they do not exist."""
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap_servers)
    await admin.start()
    try:
        wanted = [
            NewTopic(topics.tool_calls, TOOL_CALL_PARTITIONS, 1),
            NewTopic(topics.access_denied, ACCESS_DENIED_PARTITIONS, 1),
        ]
        try:
            await admin.create_topics(wanted)
            log.info("created topics: %s", ", ".join(topics.all()))
        except TopicAlreadyExistsError:
            log.debug("topics already present")
    finally:
        await admin.close()
