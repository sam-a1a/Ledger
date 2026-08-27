"""Fixtures for the Kafka-backed governance tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ledger.config import Settings
from ledger.governance.events import AuditTopics
from ledger.governance.journal import EventJournal
from ledger.governance.publisher import KafkaAuditPublisher
from ledger.governance.topics import ensure_topics


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
