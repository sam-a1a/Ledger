"""The materialiser: offsets, durability, and the rebuild path."""

from __future__ import annotations

import pytest

from ledger.config import Settings
from ledger.governance.consumer import AuditConsumer
from ledger.governance.events import Outcome, PrincipalRef, completed, requested
from ledger.governance.publisher import KafkaAuditPublisher
from ledger.governance.store import events_dir, read_events
from ledger.security.principal import Channel, Principal, Role

pytestmark = pytest.mark.kafka

ANALYST_REF = PrincipalRef(subject="u-audit", role=Role.ANALYST, tenant_id=None)
ANALYST = Principal(subject="u-audit", role=Role.ANALYST, tenant_id=None)


async def _publish_pair(publisher: KafkaAuditPublisher, conversation: str) -> None:
    request = requested(
        principal=ANALYST_REF,
        channel=Channel.HTTP,
        tool="aggregate",
        args={"metrics": [{"op": "count"}]},
        call_id="call-audit-1",
        conversation_id=conversation,
    )
    await publisher.publish(request)
    await publisher.publish(completed(request, outcome=Outcome.ALLOW, duration_ms=17, row_count=4))


async def test_events_are_materialised_and_readable(
    publisher: KafkaAuditPublisher,
    kafka_settings: Settings,
    tmp_path,
    cursor,
) -> None:
    settings = kafka_settings.model_copy(update={"data_dir": tmp_path})
    await _publish_pair(publisher, "conv-materialise")

    consumer = AuditConsumer(settings)
    await consumer.start()
    try:
        written = 0
        for _ in range(10):
            written += await consumer.run_once(timeout_ms=1500)
            if written >= 2:
                break
    finally:
        await consumer.stop()

    assert written >= 2
    assert list(events_dir(tmp_path).rglob("*.parquet"))

    events = read_events(cursor, tmp_path, ANALYST, conversation_id="conv-materialise")
    kinds = {e["event_type"] for e in events}
    assert kinds == {"tool_call_requested", "tool_call_completed"}
    assert events[-1]["row_count"] == 4


async def test_the_read_path_deduplicates_by_event_id(
    publisher: KafkaAuditPublisher,
    kafka_settings: Settings,
    tmp_path,
    cursor,
) -> None:
    """At-least-once delivery plus idempotent read is effectively exactly-once.

    A crash between flush and offset commit re-delivers, so the store can
    genuinely hold a duplicate; the view must not.
    """
    settings = kafka_settings.model_copy(update={"data_dir": tmp_path})
    await _publish_pair(publisher, "conv-dedupe")

    # Two independent consumer groups both materialise every event.
    for suffix in ("-a", "-b"):
        consumer = AuditConsumer(settings, group_suffix=suffix)
        await consumer.start()
        try:
            for _ in range(10):
                if await consumer.run_once(timeout_ms=1500):
                    break
        finally:
            await consumer.stop()

    files = list(events_dir(tmp_path).rglob("*.parquet"))
    assert len(files) >= 2  # the duplication really happened

    events = read_events(cursor, tmp_path, ANALYST, conversation_id="conv-dedupe")
    ids = [e["event_id"] for e in events]
    assert len(ids) == len(set(ids))


async def test_a_viewer_cannot_read_another_subjects_events(
    publisher: KafkaAuditPublisher,
    kafka_settings: Settings,
    tmp_path,
    cursor,
) -> None:
    """Authorisation on the audit endpoint, enforced in the query."""
    settings = kafka_settings.model_copy(update={"data_dir": tmp_path})
    await _publish_pair(publisher, "conv-authz")

    consumer = AuditConsumer(settings)
    await consumer.start()
    try:
        for _ in range(10):
            if await consumer.run_once(timeout_ms=1500):
                break
    finally:
        await consumer.stop()

    stranger = Principal(subject="someone-else", role=Role.VIEWER, tenant_id=1)
    assert read_events(cursor, tmp_path, stranger) == []
    assert read_events(cursor, tmp_path, ANALYST)


async def test_reading_an_empty_store_is_not_an_error(tmp_path, cursor) -> None:
    """A fresh deployment has no events yet, and that is not a failure."""
    assert read_events(cursor, tmp_path, ANALYST) == []
