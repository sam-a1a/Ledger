"""The delivery contract, exercised against a real broker.

The claim being tested is specific: a tool call that cannot be audited does not
happen. So the failure path matters as much as the happy one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiokafka import AIOKafkaConsumer

from ledger.errors import EventPublishError
from ledger.governance.events import (
    AuditTopics,
    EventType,
    GovernanceEvent,
    Outcome,
    PrincipalRef,
    access_denied,
    completed,
    requested,
)
from ledger.governance.journal import EventJournal
from ledger.governance.publisher import KafkaAuditPublisher
from ledger.security.principal import Channel, Role

pytestmark = pytest.mark.kafka

ANALYST = PrincipalRef(subject="u-1", role=Role.ANALYST, tenant_id=None)


async def _drain(
    bootstrap: str, topic: str, expected: int, budget_s: float = 20.0
) -> list[GovernanceEvent]:
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        group_id=None,
    )
    await consumer.start()
    try:
        collected: list[GovernanceEvent] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        while len(collected) < expected and loop.time() < deadline:
            batch = await consumer.getmany(timeout_ms=1000)
            for records in batch.values():
                collected.extend(GovernanceEvent.model_validate_json(r.value) for r in records)
        return collected
    finally:
        await consumer.stop()


async def test_a_published_event_is_readable_from_the_topic(
    publisher: KafkaAuditPublisher, kafka_bootstrap: str, audit_topics: AuditTopics
) -> None:
    event = requested(
        principal=ANALYST,
        channel=Channel.HTTP,
        tool="aggregate",
        args={"metrics": [{"op": "count"}]},
        call_id="call-1",
        conversation_id="conv-1",
    )
    await publisher.publish(event)

    [received] = await _drain(kafka_bootstrap, audit_topics.tool_calls, 1)
    assert received.event_id == event.event_id
    assert received.tool == "aggregate"
    assert received.principal.role is Role.ANALYST
    assert received.event_type is EventType.TOOL_CALL_REQUESTED


async def test_request_and_completion_share_a_call_id(
    publisher: KafkaAuditPublisher, kafka_bootstrap: str, audit_topics: AuditTopics
) -> None:
    """The pair is what lets the consumer spot an orphaned request."""
    request = requested(
        principal=ANALYST,
        channel=Channel.HTTP,
        tool="top_n",
        args={"dimension": "pickup_zone"},
        call_id="call-2",
        conversation_id="conv-2",
    )
    await publisher.publish(request)
    await publisher.publish(completed(request, outcome=Outcome.ALLOW, duration_ms=42, row_count=10))

    events = await _drain(kafka_bootstrap, audit_topics.tool_calls, 2)
    by_type = {e.event_type: e for e in events}
    assert by_type[EventType.TOOL_CALL_REQUESTED].call_id == "call-2"
    assert by_type[EventType.TOOL_CALL_COMPLETED].call_id == "call-2"
    assert by_type[EventType.TOOL_CALL_COMPLETED].row_count == 10


async def test_denials_go_to_their_own_topic_with_the_attempted_column(
    publisher: KafkaAuditPublisher, kafka_bootstrap: str, audit_topics: AuditTopics
) -> None:
    """The caller is told the column does not exist; the audit log knows better."""
    await publisher.publish(
        access_denied(
            principal=PrincipalRef(subject="u-2", role=Role.VIEWER, tenant_id=1),
            channel=Channel.MCP,
            tool="aggregate",
            attempted_columns=["tip_amount"],
            call_id="call-3",
        )
    )
    [received] = await _drain(kafka_bootstrap, audit_topics.access_denied, 1)
    assert received.attempted_columns == ["tip_amount"]
    assert received.outcome is Outcome.DENY
    assert received.channel is Channel.MCP


async def test_events_for_one_conversation_share_a_partition_key(
    publisher: KafkaAuditPublisher,
) -> None:
    """Per-conversation ordering is what the trace panel depends on."""
    a = requested(
        principal=ANALYST,
        channel=Channel.HTTP,
        tool="a",
        args={},
        call_id="c1",
        conversation_id="conv-x",
    )
    b = completed(a, outcome=Outcome.ALLOW, duration_ms=1)
    assert a.topic_key() == b.topic_key() == "conv-x"


async def test_publish_raises_when_the_broker_is_unreachable(tmp_path: Path) -> None:
    """This is the assertion the whole hard-dependency decision rests on.

    The caller treats this exception as fatal to the tool call, so the query
    never runs. No un-audited data access, not merely no un-audited result.
    """
    settings_bootstrap = "127.0.0.1:1"  # nothing listens here
    from ledger.config import Settings

    settings = Settings(kafka_bootstrap_servers=settings_bootstrap, kafka_request_timeout_ms=1000)
    producer = KafkaAuditPublisher.build_producer(settings)
    journal = EventJournal(tmp_path / "journal.ndjson")
    topics = AuditTopics(tool_calls="t", access_denied="d")
    publisher = KafkaAuditPublisher(producer, topics, journal, timeout_s=2.0)

    event = requested(
        principal=ANALYST,
        channel=Channel.HTTP,
        tool="aggregate",
        args={},
        call_id="c",
    )
    with pytest.raises(EventPublishError):
        await publisher.publish(event)

    # ...and the attempt is still on disk, which is when it matters most.
    assert journal.count() == 1
    assert next(iter(journal.pending())).event_id == event.event_id


async def test_best_effort_publish_journals_instead_of_raising(tmp_path: Path) -> None:
    """The completion event must not fail a call whose query already succeeded."""
    from ledger.config import Settings

    settings = Settings(kafka_bootstrap_servers="127.0.0.1:1", kafka_request_timeout_ms=1000)
    journal = EventJournal(tmp_path / "journal.ndjson")
    publisher = KafkaAuditPublisher(
        KafkaAuditPublisher.build_producer(settings),
        AuditTopics(tool_calls="t", access_denied="d"),
        journal,
        timeout_s=2.0,
    )
    request = requested(
        principal=ANALYST,
        channel=Channel.HTTP,
        tool="aggregate",
        args={},
        call_id="c",
    )
    await publisher.publish_best_effort(completed(request, outcome=Outcome.ALLOW, duration_ms=5))
    assert journal.count() == 1


async def test_journal_drains_once_the_broker_returns(
    publisher: KafkaAuditPublisher, kafka_bootstrap: str, audit_topics: AuditTopics
) -> None:
    """An event is in Kafka, in the journal, or visibly orphaned -- never dropped."""
    event = requested(
        principal=ANALYST,
        channel=Channel.HTTP,
        tool="count_rows",
        args={},
        call_id="call-drain",
        conversation_id="conv-drain",
    )
    publisher._journal.append(event)  # simulate an earlier outage
    assert publisher._journal.count() == 1

    assert await publisher.drain_journal() == 1
    assert publisher._journal.count() == 0

    [received] = await _drain(kafka_bootstrap, audit_topics.tool_calls, 1)
    assert received.event_id == event.event_id
